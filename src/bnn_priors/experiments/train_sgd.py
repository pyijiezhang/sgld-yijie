"""
Train CIFAR10 using SGD. Adapted from https://github.com/kuangliu/pytorch-cifar
Available under the MIT License. Copyright 2017 liukuang
"""
import torch
import torch.nn as nn
import torch.optim as optim
import torch.backends.cudnn as cudnn
import json
import math
import atexit

import argparse
import os

from bnn_priors.exp_utils import (HDF5Metrics, HDF5ModelSaver, get_data,
                                  get_model, he_uniform_initialize,
                                  evaluate_model, load_samples)
from bnn_priors.models.base import ConsistencyLikelihood, DirichletGaussian

def optional_int(s):
    if s == "None":
        return None
    return int(s)

parser = argparse.ArgumentParser(description='PyTorch CIFAR10 Training')
parser.add_argument('--lr', default=0.05, type=float, help='learning rate')
parser.add_argument('--momentum', default=0.9, type=float, help='momentum')
parser.add_argument('--weight_decay', default=0.0, type=float, help='weight decay')
parser.add_argument('--model', default="thin_resnet18", type=str, help='name of model')
parser.add_argument('--optimizer', default="SGD", type=str, help='name of optimizer')
parser.add_argument('--data', default="cifar10_augmented", type=str, help='name of data set')
parser.add_argument('--width', default=64, type=int, help='width of nn architecture')
parser.add_argument('--batch_size', default=128, type=optional_int, help='train batch size')
parser.add_argument('--sampling_decay', default="stairs", type=str, help='schedule of learning rate')
parser.add_argument('--n_epochs', default=150*3, type=int, help='number of epochs to train for')
parser.add_argument('--epochs_per_sample', default=50, type=int, help='number of epochs to skip between samples')
parser.add_argument('--skip_first', default=3, type=int, help='number of epochs to skip between samples')
parser.add_argument('--loss', default="xent", type=str, help='loss fn to use', 
        choices=["xent", "mse", "consistency", "dirichlet"],
)
parser.add_argument("--alpha", default=0.01, type=float, help="dirichlet tuning constant",)
parser.add_argument("--output", default="./", type=str, help="output directory")

args = parser.parse_args()

os.mkdir(args.output)
print("finished making ", args.output)

with open(args.output + "/config.json", "w") as f:
    json.dump(dict(lr=args.lr, momentum=args.momentum,
                   weight_decay=args.weight_decay, model=args.model,
                   optimizer=args.optimizer, data=args.data, width=args.width,
                   batch_size=args.batch_size, temperature=0.0,
                   sampling_decay=args.sampling_decay, n_epochs=args.n_epochs,
                   epochs_per_sample=args.epochs_per_sample,
                   skip_first=args.skip_first), f)
with open(args.output + "/run.json", "w") as f:
    json.dump({"status": "RUNNING"}, f)

@atexit.register
def _error_exit():
    with open(args.output + "/run.json", "w") as f:
        f.write('{"status": "FAILED"}\n')


device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
best_acc = -math.inf  # best test accuracy
start_epoch = 0  # start from epoch 0 or last checkpoint epoch

print('==> Preparing data..')
data = get_data(args.data, device=device)
trainloader = torch.utils.data.DataLoader(data.norm.train, batch_size=args.batch_size, shuffle=True)
testloader = torch.utils.data.DataLoader(data.norm.test, batch_size=100, shuffle=False)

if args.loss == "mse":
    criterion = nn.MSELoss()
elif args.loss == "xent":
    criterion = nn.CrossEntropyLoss()
elif args.loss == "consistency":
    criterion = ConsistencyLikelihood
elif args.loss == "dirichlet":
    criterion = lambda inputs, targets: -DirichletGaussian(
        inputs, alpha_epsilon=args.alpha
    ).log_prob(targets).mean()
sigma = args.alpha

# Model
print('==> Building model..')
model = get_model(data.norm.train_X, data.norm.train_y, model=args.model,
                  width=args.width, depth=3,
                  weight_prior="improper", weight_loc=0., weight_scale=1.,
                  bias_prior="improper", bias_loc=0., bias_scale=1.,
                  batchnorm=True, weight_prior_params={}, bias_prior_params={},
                  softmax_temp=1.0, likelihood="std", consistency_std=sigma,
                  label_smoothing=0.0,)
model = model.to(device)

if device == torch.device('cuda'):
    cudnn.benchmark = True

he_uniform_initialize(model)  # We destroyed He init by using priors, bring it back

optim_class = getattr(optim, args.optimizer)
optimizer = optim_class(model.parameters(), lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)

if args.sampling_decay == "stairs":
    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, 150, 0.1)  # Decrease to 1/10 every 150 epochs
elif args.sampling_decay == "stairs2":
    def _schedule(t):
        for epoch, mult in reversed([(80, 0.1), (120, 0.01), (160, 0.001), (180, 0.0005)]):
            if t >= epoch:
                return mult
        return 1.
    scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, _schedule)
elif args.sampling_decay == "flat":
    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, 2**30, 1.0)
else:
    raise ValueError(f"args.sampling_decay={args.sampling_decay}")


# Training
def train(epoch, metrics_saver):
    model.train()
    for batch_idx, (inputs, targets) in enumerate(trainloader):
        optimizer.zero_grad()
        if args.loss != "consistency":
            inputs, targets = inputs.to(device), targets.to(device)
            outputs = model.net(inputs)
            loss = criterion(outputs, targets)
        else:
            inputs, targets = inputs.to(device), [x.to(device) for x in targets]
            outputs = model.net(inputs)
            dist = criterion(net = model.net, logits=outputs, sigma=sigma)  
            loss = -dist.log_prob(targets).mean()

        loss.backward()
        optimizer.step()

    with torch.no_grad():
        train_loss = loss.item() * (batch_idx + 1)
        total = inputs.size(0)
        if args.loss != "mse":
            _, predicted = outputs.max(1)
            if args.loss == "consistency":
                targets = targets[0]
            correct = predicted.eq(targets).sum().item()
        else:
            correct = -loss * targets.size(0)  # sum of square errors
    metrics_saver.add_scalar("loss", train_loss, epoch)
    metrics_saver.add_scalar("acc", correct/total, epoch)
    print(f"Epoch {epoch}: loss={train_loss/(batch_idx+1)}, acc={correct/total} ({correct}/{total})")
    scheduler.step()


def test(epoch, metrics_saver, model_saver):
    global best_acc
    model.eval()
    test_loss = 0
    correct = 0
    total = 0
    with torch.no_grad():
        for batch_idx, (inputs, targets) in enumerate(testloader):
            inputs, targets = inputs.to(device), targets.to(device)
            outputs = model.net(inputs)
            if args.loss == "consistency":
                dist = criterion(net = model.net, logits=outputs, sigma=sigma)
                loss = -dist.log_prob(targets).mean().item()
            else:
                loss = criterion(outputs, targets).item()

            test_loss += loss
            total += targets.size(0)
            if args.loss != "mse":
                _, predicted = outputs.max(1)
                correct += predicted.eq(targets).sum().item()
            else:
                correct -= loss * targets.size(0)  # sum of square errors

    metrics_saver.add_scalar("test/loss", test_loss/(batch_idx+1), epoch)
    metrics_saver.add_scalar("test/acc", correct/total, epoch)
    print(f"Epoch {epoch}: test_loss={test_loss/(batch_idx+1)}, test_acc={correct/total} ({correct}/{total})")

    # Save checkpoint.
    if (epoch+1) % args.epochs_per_sample == 0:
        model_saver.add_state_dict(model.state_dict(), epoch)
        model_saver.flush()


with HDF5Metrics(args.output + "/metrics.h5", "w") as metrics_saver,\
     HDF5ModelSaver(args.output + "/samples.pt", "w") as model_saver:
    for epoch in range(args.n_epochs):
        train(epoch, metrics_saver)
        test(epoch, metrics_saver, model_saver)

calibration_eval = (args.data[:7] == "cifar10" or args.data[-5:] == "mnist")
samples = load_samples(args.output + "/samples.pt", idx=slice(args.skip_first, None, None))
del samples["steps"]
del samples["timestamps"]

model.eval()
result = evaluate_model(model, testloader, samples, likelihood_eval=True,
                        accuracy_eval=True, calibration_eval=calibration_eval)

atexit.unregister(_error_exit)
with open(args.output + "/run.json", "w") as f:
    json.dump({"status": "COMPLETED",
               "result": result}, f)
