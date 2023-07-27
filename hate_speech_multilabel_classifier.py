from datasets import load_dataset

# clone dataset
# source: https://huggingface.co/datasets/ucberkeley-dlab/measuring-hate-speech
raw_datasets = load_dataset("ucberkeley-dlab/measuring-hate-speech")

print(f"Number of Columns: {raw_datasets['train'].num_columns}")
print(f"Number of Rows: {raw_datasets['train'].num_rows}")

# keep only text and specific targets
raw_columns = raw_datasets['train'].column_names
keep_columns = ['text', 'target_race', 'target_religion', 'target_origin', 'target_gender', 'target_sexuality', 'target_age', 'target_disability']
remove_columns = set(raw_columns)-set(keep_columns)

preprocessed_datasets = raw_datasets.remove_columns(remove_columns)
preprocessed_datasets

column_mapping = {column:column.split('_')[1] for column in keep_columns if column.startswith('target')}
print(f"COLUMN_MAPPING: {column_mapping}")

preprocessed_datasets = preprocessed_datasets.rename_columns(column_mapping)
preprocessed_datasets

# get two-way label and label id
ID2LABEL = {}
LABEL2ID = {}

label_id = 0
for label in preprocessed_datasets['train'].features.keys():
    if label in ['text']:
        continue

    ID2LABEL[label_id] = label
    LABEL2ID[label] = label_id

    label_id += 1

print(f"ID2LABEL:\n{ID2LABEL}\n")
print(f"LABEL2ID:\n{LABEL2ID}")

# get target label counts and percentages
label_counts = {}
label_percentages = {}

for label in LABEL2ID:
    label_counts[label] = sum(preprocessed_datasets['train'][label])
    label_percentages[label] = float(f"{sum(preprocessed_datasets['train'][label]) / len(preprocessed_datasets['train'])*100:.2f}")

print(f"LABEL_COUNTS:\n{label_counts}\n")
print(f"LABEL_PERCENTAGES:\n{label_percentages}")

import matplotlib.pyplot as plt

# create 2 graphs
fig, axs = plt.subplots(2, figsize=(6,12))

# create bar graphs of label counts
bar_container0 = axs[0].bar(label_counts.keys(), label_counts.values())
axs[0].bar_label(bar_container0, label_type='edge')
axs[0].set_xlabel('Hate Speech Type')
axs[0].set_ylabel('Count')
axs[0].set_title('Count by Hate Speech Type')

# create bar graphs of label percentages
bar_container1 = axs[1].bar(label_percentages.keys(), label_percentages.values())
axs[1].bar_label(bar_container1, label_type='edge')
axs[1].set_xlabel('Hate Speech Type')
axs[1].set_ylabel('Percentage (%)')
axs[1].set_title('Percentage (%) by Hate Speech Type')

plt.show()

def create_labels(batch):
    # one-hot encode targets for training
    batch['labels'] = [[float(batch[label][i]) for label in LABEL2ID] for i in range(len(batch['text']))]
    return batch

preprocessed_datasets = preprocessed_datasets.map(create_labels, batched=True, remove_columns=LABEL2ID.keys())
preprocessed_datasets

import numpy as np
import torch

# set seed for reproducibility
SEED = 42
torch.manual_seed(SEED)
np.random.seed(SEED)

from datasets import DatasetDict

# train (80%), validation (10%), test (10%) split
train_test_datasets = preprocessed_datasets['train'].train_test_split(test_size=0.2, seed=SEED, shuffle=True)
validation_test_datasets = train_test_datasets['test'].train_test_split(test_size=0.5, seed=SEED, shuffle=True)

preprocessed_datasets = DatasetDict({
    'train': train_test_datasets['train'],
    'validation': validation_test_datasets['train'],
    'test': validation_test_datasets['test']
})
preprocessed_datasets

from transformers import AutoTokenizer

CHECKPOINT = 'distilbert-base-uncased'
tokenizer = AutoTokenizer.from_pretrained(CHECKPOINT)
tokenized_datasets = preprocessed_datasets.map(lambda batch: tokenizer(batch['text'], truncation=True), batched=True, remove_columns=['text'])
tokenized_datasets

from transformers import DataCollatorWithPadding
from torch.utils.data import DataLoader

# get data collator for data loader
data_collator = DataCollatorWithPadding(tokenizer=tokenizer)

# setup dataloaders with tokenized dataset
# to shuffle only be train for each epoch
# in 64 batch sizes with dynamic padding

dataloaders = {}
for dataset_type in tokenized_datasets.keys():
    dataloaders[dataset_type] = DataLoader(
        dataset=tokenized_datasets[dataset_type],
        batch_size=64,
        shuffle=(dataset_type == 'train'),
        collate_fn=data_collator,
    )

# get current device
device = 'cuda' if torch.cuda.is_available() else 'cpu'
device

from transformers import AutoModelForSequenceClassification

model = AutoModelForSequenceClassification.from_pretrained(
    CHECKPOINT,
    problem_type='multi_label_classification',
    num_labels=len(LABEL2ID),
    label2id=LABEL2ID,
    id2label=ID2LABEL,
)

# move model to device
model.to(device)

from transformers import AdamW, get_scheduler

# setup optimizer and scheduler
scheduler_name = 'linear'
optimizer = AdamW(model.parameters(), lr=5e-5, weight_decay=0, no_deprecation_warning=True)
num_training_epochs = 1
num_training_steps = num_training_epochs * len(dataloaders['train'])
num_warmup_steps = 0
lr_scheduler = get_scheduler(
    name=scheduler_name,
    optimizer=optimizer,
    num_training_steps=num_training_steps,
    num_warmup_steps=num_warmup_steps,
)

print(f"           SCHEDULER NAME: {scheduler_name}")
print(f"                OPTIMIZER: {optimizer.__class__.__name__}")
print(f"NUMBER OF TRAINING EPOCHS: {num_training_epochs}")
print(f" NUMBER OF TRAINING STEPS: {num_training_steps}")

from sklearn.metrics import accuracy_score, f1_score

def samples_accuracy_score(y_true, y_pred):
    return np.sum(y_true==y_pred) / y_true.size

def compute_metrics(eval_preds):
    logits, labels = eval_preds
    predictions = torch.nn.functional.sigmoid(torch.Tensor(logits))
    predictions = (predictions >= 0.50).int().numpy()
    samples_accuracy = samples_accuracy_score(labels, predictions)
    samples_f1 = f1_score(labels, predictions, average='samples', zero_division=0)
    return {
        'accuracy': samples_accuracy,
        'f1': samples_f1,
    }

def train(model, dataloader):
    # setup train metrics
    loss = 0
    train_predictions = []
    train_labels = []

    # set to train mode
    model.train()
    # iterate through dataloader
    for batch in tqdm(dataloader):
        # zero the gradients
        optimizer.zero_grad()

        # predict batch in current device
        batch.to(device)
        outputs = model(**batch)

        # compute multilabel outputs
        predictions = torch.nn.functional.sigmoid(outputs.logits).cpu()
        predictions = (predictions >= 0.50).int().numpy()
        labels = batch['labels']

        # backprop and update learning rate
        outputs.loss.backward()
        optimizer.step()
        lr_scheduler.step()

        # accumulate train metrics
        loss += outputs.loss.item()
        train_predictions += predictions.tolist()
        train_labels += labels.tolist()

    # compute train metrics
    loss /= len(dataloader)
    samples_accuracy = samples_accuracy_score(np.array(train_labels), np.array(train_predictions))
    samples_f1 = f1_score(np.array(train_labels), np.array(train_predictions), average='samples', zero_division=0)

    # Save the model
    torch.save({
        'epoch': epoch,
        'model_state_dict': model.state_dict(),
        'optimizer_state_dict': optimizer.state_dict(),
        'loss': loss,
    }, "") #<---insert path here 

    return {
        'loss': loss,
        'accuracy': samples_accuracy,
        'f1': samples_f1,
    }

def evaluate(model, dataloader):
    # setup evaluation metrics variables
    running_loss = 0
    num_samples = 0
    total_accuracy = 0
    total_f1 = 0

    # set to evaluation mode
    model.eval()
    with torch.no_grad():
        # iterate through dataloader
        for batch in tqdm(dataloader):
            # predict batch in current device
            batch = batch.to(device)
            outputs = model(**batch)

            # compute multilabel outputs
            predictions = torch.nn.functional.sigmoid(outputs.logits)
            predictions = (predictions >= 0.50).cpu().numpy()
            labels = batch['labels'].cpu().numpy()

            # accumulate evaluation metrics
            running_loss += outputs.loss.item() * labels.shape[0]
            num_samples += labels.shape[0]
            total_accuracy += samples_accuracy_score(labels, predictions) * labels.shape[0]
            total_f1 += f1_score(labels, predictions, average='samples', zero_division=0) * labels.shape[0]

    # compute evaluation metrics
    final_loss = running_loss / num_samples
    final_accuracy = total_accuracy / num_samples
    final_f1 = total_f1 / num_samples
    return {
        'loss': final_loss,
        'accuracy': final_accuracy,
        'f1': final_f1,
    }

import os
from transformers import DistilBertForSequenceClassification
from tqdm import tqdm
import torch


if __name__ == "__main__":
  # Define the path to save your model
  save_path = ""  # <---insert path here

  label_dict = {
      'race': 0,
      'religion': 1,
      'origin': 2,
      'gender': 3,
      'sexuality': 4,
      'age': 5,
      'disability': 6
      }
  # Define the model
  model = DistilBertForSequenceClassification.from_pretrained(
      'distilbert-base-uncased',
      num_labels=len(label_dict),
      output_attentions=False,
      output_hidden_states=False
  )

  # Ensure we're using the GPU
  device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
  model.to(device)

  # Load the model from a checkpoint if it exists
  if os.path.exists(save_path):
      model.load_state_dict(torch.load(save_path))
      print("Loaded model from checkpoint.")
  else:
      print("No checkpoint found. Starting from scratch.")

  # Continue training from where you left off
  for epoch in range(num_training_epochs):
      train_metrics = train(model, dataloaders['train'])
      validation_metrics = evaluate(model, dataloaders['validation'])

      print(f"EPOCH {epoch+1}", end=" | ")
      print(f"TRAIN LOSS: {train_metrics['loss']:.5f}", end=" | ")
      print(f"VALIDATION LOSS: {validation_metrics['loss']:.5f}", end=" | ")
      print(f"VALIDATION ACCURACY: {validation_metrics['accuracy']:.5f}", end=" | ")
      print(f"VALIDATION F1: {validation_metrics['f1']:.5f}")

      # Save the model after each epoch
      torch.save(model.state_dict(), save_path)
      print(f"Saved model checkpoint to {save_path} at end of epoch {epoch+1}.")

from transformers import pipeline

# setup pipeline as a text classification with multilabel outputs
hate_speech_multilabel_classifier = pipeline(
    task='text-classification',
    model=model,
    tokenizer=tokenizer,
    device=torch.cuda.current_device(),
    top_k=None
)

race_hate_text = """
Yellow peril.
"""

hate_speech_multilabel_classifier(race_hate_text)

religion_hate_text = """
Nietzsche said 'God is dead'.
"""

hate_speech_multilabel_classifier(religion_hate_text)

disability_hate_text = """
Nietzche said 'God is dead'.
"""
hate_speech_multilabel_classifier(disability_hate_text)

age_hate_text = """
Old fart.
"""

hate_speech_multilabel_classifier(age_hate_text)

torch.save(model.state_dict(), "") #<---insert path here 









