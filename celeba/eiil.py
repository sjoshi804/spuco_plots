# TODO: Check hparams
import argparse
import os
from glob import glob

import numpy as np
import pandas as pd
import torch
import torchvision.transforms as transforms
from sklearn.metrics import f1_score
from torch.optim import SGD
from wilds import get_dataset

from spuco.datasets import GroupLabeledDatasetWrapper, SpuCoAnimals, WILDSDatasetWrapper
from spuco.evaluate import Evaluator
from spuco.group_inference import EIIL
from spuco.robust_train import GroupDRO
from spuco.models import model_factory
from spuco.utils import Trainer, set_seed

parser = argparse.ArgumentParser()
parser.add_argument("--gpu", type=int, default=0)
parser.add_argument("--seed", type=int, default=0)
parser.add_argument("--root_dir", type=str, default="/data")
parser.add_argument("--label_noise", type=float, default=0.0)
parser.add_argument("--results_csv", type=str, default="/home/sjoshi/spuco_experiments/celeba/eiil.csv")

parser.add_argument("--arch", type=str, default="resnet50")
parser.add_argument("--batch_size", type=int, default=128)
parser.add_argument("--num_epochs", type=int, default=50)
parser.add_argument("--lr", type=float, default=1e-5)
parser.add_argument("--weight_decay", type=float, default=0.1)
parser.add_argument("--momentum", type=float, default=0.9)

parser.add_argument("--infer_lr", type=float, default=1e-4)
parser.add_argument("--infer_weight_decay", type=float, default=1e-4)
parser.add_argument("--infer_momentum", type=float, default=0.9)
parser.add_argument("--infer_num_epochs", type=int, default=1)

parser.add_argument("--eiil_num_steps", type=int, default=10000)
parser.add_argument("--eiil_lr", type=float, default=0.01)

args = parser.parse_args()

device = torch.device(f"cuda:{args.gpu}" if torch.cuda.is_available() else "cpu")
set_seed(args.seed)

# Load the full dataset, and download it if necessary
transform = transforms.Compose([
            transforms.Resize(256),
            transforms.CenterCrop(224),
            transforms.ToTensor(),
            transforms.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225))
        ])

dataset = get_dataset(dataset="celebA", download=False, root_dir="/home/data")
train_data = dataset.get_subset(
        "train",
        transform=transform
    )
val_data = dataset.get_subset(
    "val",
    transform=transform
)
test_data = dataset.get_subset(
    "test",
    transform=transform
)
trainset = WILDSDatasetWrapper(dataset=train_data, metadata_spurious_label="male", verbose=True)
valset = WILDSDatasetWrapper(dataset=val_data, metadata_spurious_label="male", verbose=True)
testset = WILDSDatasetWrapper(dataset=test_data, metadata_spurious_label="male", verbose=True)


model = model_factory(args.arch, trainset[0][0].shape, trainset.num_classes, pretrained=True).to(device)

trainer = Trainer(
    trainset=trainset,
    model=model,
    batch_size=args.batch_size,
    optimizer=SGD(model.parameters(), lr=args.infer_lr, weight_decay=args.infer_weight_decay, momentum=args.infer_momentum),
    device=device,
    verbose=True
)
trainer.train(num_epochs=args.infer_num_epochs)

logits = trainer.get_trainset_outputs()
eiil = EIIL(
    logits=logits,
    class_labels=trainset.labels,
    num_steps=args.eiil_num_steps,
    lr=args.eiil_lr,
    device=device,
    verbose=True
)

group_partition = eiil.infer_groups()

import pickle 
with open("celeba_eiil_groups.pkl", "wb") as f:
    pickle.dump(group_partition, f)
    
for key in sorted(group_partition.keys()):
    print(key, len(group_partition[key]))
evaluator = Evaluator(
    testset=trainset,
    group_partition=group_partition,
    group_weights=trainset.group_weights,
    batch_size=args.batch_size,
    model=model,
    device=device,
    verbose=True
)
evaluator.evaluate()

robust_trainset = GroupLabeledDatasetWrapper(trainset, group_partition)

model = model_factory(args.arch, trainset[0][0].shape, trainset.num_classes, pretrained=True).to(device)
valid_evaluator = Evaluator(
    testset=valset,
    group_partition=valset.group_partition,
    group_weights=trainset.group_weights,
    batch_size=args.batch_size,
    model=model,
    device=device,
    verbose=True
)
group_dro = GroupDRO(
    model=model,
    val_evaluator=valid_evaluator,
    num_epochs=args.num_epochs,
    trainset=robust_trainset,
    batch_size=args.batch_size,
    optimizer=SGD(model.parameters(), lr=args.lr, weight_decay=args.weight_decay, momentum=args.momentum),
    device=device,
    verbose=True
)
group_dro.train()

evaluator = Evaluator(
    testset=testset,
    group_partition=testset.group_partition,
    group_weights=trainset.group_weights,
    batch_size=args.batch_size,
    model=model,
    device=device,
    verbose=True
)
evaluator.evaluate()
results = pd.DataFrame(index=[0])
results["timestamp"] = pd.Timestamp.now()
results["seed"] = args.seed
results["pretrained"] = True
results["lr"] = args.lr
results["weight_decay"] = args.weight_decay
results["momentum"] = args.momentum
results["num_epochs"] = args.num_epochs
results["batch_size"] = args.batch_size
results["alg"] = "eiil"
results["worst_group_accuracy"] = evaluator.worst_group_accuracy[1]
results["average_accuracy"] = evaluator.average_accuracy

evaluator = Evaluator(
    testset=testset,
    group_partition=testset.group_partition,
    group_weights=trainset.group_weights,
    batch_size=args.batch_size,
    model=group_dro.best_model,
    device=device,
    verbose=True
)
evaluator.evaluate()

results["early_stopping_worst_group_accuracy"] = evaluator.worst_group_accuracy[1]
results["early_stopping_average_accuracy"] = evaluator.average_accuracy

if os.path.exists(args.results_csv):
    results_df = pd.read_csv(args.results_csv)
else:
    results_df = pd.DataFrame()

results_df = pd.concat([results_df, results], ignore_index=True)
results_df.to_csv(args.results_csv, index=False)
print(results)
print('Done!')
print('Results saved to', args.results_csv)


