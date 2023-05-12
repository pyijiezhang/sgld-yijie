import logging
from pathlib import Path
from tqdm.auto import tqdm
import wandb
import torch
from torch.utils.data import DataLoader
from torch.optim import SGD

from data_aug.optim import SGLD
from data_aug.optim.lr_scheduler import CosineLR
from data_aug.utils import set_seeds
from data_aug.models import ResNet18, ResNet18FRN
from data_aug.datasets import get_cifar10, get_tiny_imagenet
from data_aug.nn import CPriorAugmentedCELoss


@torch.no_grad()
def test(data_loader, net, criterion, device=None):
  net.eval()

  total_loss = 0.
  N = 0
  Nc = 0

  for X, Y in tqdm(data_loader, leave=False):
    X, Y = X.to(device), Y.to(device)
    
    f_hat = net(X)
    Y_pred = f_hat.argmax(dim=-1)
    loss = criterion(f_hat, Y, N=X.size(0))

    N += Y.size(0)
    Nc += (Y_pred == Y).sum().item()
    total_loss += loss

  acc = Nc / N

  return {
    'total_loss': total_loss.item(),
    'acc': acc,
  }


@torch.no_grad()
def test_bma(net, data_loader, samples_dir, nll_criterion=None, device=None):
  net.eval()

  ens_logits = []
  ens_nll = []

  for sample_path in tqdm(Path(samples_dir).rglob('*.pt'), leave=False):
    net.load_state_dict(torch.load(sample_path))

    all_logits = []
    all_Y = []
    all_nll = torch.tensor(0.0).to(device)
    for X, Y in tqdm(data_loader, leave=False):
      X, Y = X.to(device), Y.to(device)
      _logits = net(X)
      all_logits.append(_logits)
      all_Y.append(Y)
      if nll_criterion is not None:
        all_nll += nll_criterion(_logits, Y)
    all_logits = torch.cat(all_logits)
    all_Y = torch.cat(all_Y)

    ens_logits.append(all_logits)
    ens_nll.append(all_nll)

  ens_logits = torch.stack(ens_logits)
  ens_nll = torch.stack(ens_nll)

  ce_nll = - torch.distributions.Categorical(logits=ens_logits)\
              .log_prob(all_Y).sum(dim=-1).mean(dim=-1)

  nll = ens_nll.mean(dim=-1)

  Y_pred = ens_logits.softmax(dim=-1).mean(dim=0).argmax(dim=-1)
  acc = (Y_pred == all_Y).sum().item() / Y_pred.size(0)

  return { 'acc': acc, 'nll': nll, 'ce_nll': ce_nll }


def run_sgd(train_loader, test_loader, net, criterion, device=None,
            lr=1e-2, momentum=.9, epochs=1):
  train_data = train_loader.dataset
  N = len(train_data)

  sgd = SGD(net.parameters(), lr=lr, momentum=momentum)
  # sgd_scheduler = CosineAnnealingLR(sgd, T_max=200)

  best_acc = 0.

  for e in tqdm(range(epochs)):
    net.train()
    for i, (X, Y) in tqdm(enumerate(train_loader), leave=False):
      X, Y = X.to(device), Y.to(device).to(device)

      sgd.zero_grad()

      f_hat = net(X)
      loss = criterion(f_hat, Y, N=N, diri=True)

      loss.backward()

      sgd.step()

      if i % 50 == 0:
        metrics = {
          'epoch': e,
          'mini_idx': i,
          'mini_loss': loss.detach().item(),
        }
        wandb.log({f'sgd/train/{k}': v for k, v in metrics.items() }, step=e)

    # sgd_scheduler.step()

    test_metrics = test(test_loader, net, criterion, device=device)

    wandb.log({f'sgd/test/{k}': v for k, v in test_metrics.items() }, step=e)

    if test_metrics['acc'] > best_acc:
      best_acc = test_metrics['acc']

      torch.save(net.state_dict(), Path(wandb.run.dir) / 'sgd_model.pt')
      wandb.save('*.pt')
      wandb.run.summary['sgd/test/best_epoch'] = e
      wandb.run.summary['sgd/test/best_acc'] = test_metrics['acc']

      # train_metrics = test(train_loader, net, criterion, device=device)
      logging.info(f"SGD (Epoch {e}): {wandb.run.summary['sgd/test/best_acc']:.4f}")


def run_sgld(train_loader, test_loader, net, criterion, samples_dir, device=None,
             lr=1e-7, momentum=.9, temperature=1, burn_in=0, n_samples=20,
             epochs=1, nll_criterion=None):
  train_data = train_loader.dataset
  N = len(train_data)

  sgld = SGLD(net.parameters(), lr=lr, momentum=momentum, temperature=temperature)
  sample_int = (epochs - burn_in) // n_samples

  for e in tqdm(range(epochs)):
    net.train()
    for i, (X, Y) in tqdm(enumerate(train_loader), leave=False):
      X, Y = X.to(device).to(device), Y.to(device)

      sgld.zero_grad()

      f_hat = net(X)
      loss = criterion(f_hat, Y, N=N, diri=True)

      loss.backward()

      sgld.step()

      if i % 100 == 0:
        metrics = {
          'epoch': e,
          'mini_idx': i,
          'mini_loss': loss.detach().item(),
        }
        wandb.log({f'sgld/train/{k}': v for k, v in metrics.items() }, step=e)

    test_metrics = test(test_loader, net, criterion, device=device)
    wandb.log({f'sgld/test/{k}': v for k, v in test_metrics.items() }, step=e)

    logging.info(f"SGLD (Epoch {e}) : {test_metrics['acc']:.4f}")

    if e + 1 > burn_in and (e + 1 - burn_in) % sample_int == 0:
        torch.save(net.state_dict(), samples_dir / f's_e{e}.pt')
        wandb.save('samples/*.pt')

        bma_test_metrics = test_bma(net, test_loader, samples_dir, nll_criterion=nll_criterion, device=device)
        wandb.log({f'sgld/test/bma_{k}': v for k, v in bma_test_metrics.items() })

        logging.info(f"SGLD BMA (Epoch {e}): {bma_test_metrics['acc']:.4f}")

  bma_test_metrics = test_bma(net, test_loader, samples_dir, nll_criterion=nll_criterion, device=device)
  wandb.log({f'sgld/test/bma_{k}': v for k, v in bma_test_metrics.items() })
  wandb.run.summary['sgld/test/bma_acc'] = bma_test_metrics['acc']

  logging.info(f"SGLD BMA: {wandb.run.summary['sgld/test/bma_acc']:.4f}")    


def run_csgld(train_loader, test_loader, net, criterion, samples_dir, device=None,
              lr=1e-2, momentum=.9, temperature=1, n_samples=20, n_cycles=1,
              epochs=1, nll_criterion=None):
  train_data = train_loader.dataset
  N = len(train_data)

  sgld = SGLD(net.parameters(), lr=lr, momentum=momentum, temperature=temperature)
  sgld_scheduler = CosineLR(sgld, n_cycles=n_cycles, n_samples=n_samples,
                            T_max=len(train_loader) * epochs)

  for e in tqdm(range(epochs)):
    net.train()
    for i, (X, Y) in tqdm(enumerate(train_loader), leave=False):
      X, Y = X.to(device), Y.to(device)

      sgld.zero_grad()

      f_hat = net(X)
      loss = criterion(f_hat, Y, N=N, diri=True)

      loss.backward()
      # torch.nn.utils.clip_grad_norm_(net.parameters(), 200.)

      if sgld_scheduler.get_last_beta() < sgld_scheduler.beta:
        sgld.step(noise=False)
      else:
        sgld.step()

        if sgld_scheduler.should_sample():
          torch.save(net.state_dict(), samples_dir / f's_e{e}_m{i}.pt')
          wandb.save('samples/*.pt')

          bma_test_metrics = test_bma(net, test_loader, samples_dir, nll_criterion=nll_criterion, device=device)

          wandb.log({f'csgld/test/bma_{k}': v for k, v in bma_test_metrics.items() })

          logging.info(f"cSGLD BMA (Epoch {e}): {bma_test_metrics['acc']:.4f}")

      sgld_scheduler.step()

      if i % 100 == 0:
        metrics = {
          'epoch': e,
          'mini_idx': i,
          'mini_loss': loss.detach().item(),
        }
        wandb.log({f'csgld/train/{k}': v for k, v in metrics.items() }, step=e)

    test_metrics = test(test_loader, net, criterion, device=device)

    wandb.log({f'csgld/test/{k}': v for k, v in test_metrics.items() }, step=e)

    logging.info(f"cSGLD (Epoch {e}) : {test_metrics['acc']:.4f}")

  bma_test_metrics = test_bma(net, test_loader, samples_dir, nll_criterion=nll_criterion, device=device)

  wandb.log({f'csgld/test/bma_{k}': v for k, v in bma_test_metrics.items() })
  wandb.run.summary['csgld/test/bma_acc'] = bma_test_metrics['acc']

  logging.info(f"cSGLD BMA: {wandb.run.summary['csgld/test/bma_acc']:.4f}")


def main(seed=None, device=0, data_dir=None, ckpt_path=None, label_noise=0, dataset='cifar10',
         batch_size=128, dirty_lik=True, prior_scale=1,
         epochs=0, lr=1e-6, noise=1e-4,
         sgld_epochs=0, sgld_lr=1e-6, momentum=.9, temperature=1, burn_in=0, n_samples=20, n_cycles=0):
  if data_dir is None and os.environ.get('DATADIR') is not None:
    data_dir = os.environ.get('DATADIR')
  if ckpt_path:
    ckpt_path = Path(ckpt_path).resolve()

  torch.backends.cudnn.benchmark = True

  set_seeds(seed)
  device = f"cuda:{device}" if (device >= 0 and torch.cuda.is_available()) else "cpu"

  wandb.init(config={
    'seed': seed,
    'dataset': dataset,
    'batch_size': batch_size,
    'lr': lr,
    'prior_scale': prior_scale,
    'dirty_lik': dirty_lik,
    'temperature': temperature,
    'burn_in': burn_in,
    'sgld_lr': sgld_lr,
    'noise': noise,
  })

  samples_dir = Path(wandb.run.dir) / 'samples'
  samples_dir.mkdir()

  if dataset == 'tiny-imagenet':
    train_data, test_data = get_tiny_imagenet(root=data_dir, label_noise=label_noise)
  elif dataset == 'cifar10':
    train_data, test_data = get_cifar10(root=data_dir, label_noise=label_noise)
  else:
    raise NotImplementedError
  
  train_loader = DataLoader(train_data, batch_size=batch_size, num_workers=2,
                            shuffle=True)
  test_loader = DataLoader(test_data, batch_size=batch_size, num_workers=2)

  if dirty_lik:
    net = ResNet18(num_classes=train_data.total_classes)
  else:
    net = ResNet18FRN(num_classes=train_data.total_classes)

  net = net.to(device)
  if ckpt_path is not None and ckpt_path.is_file():
    net.load_state_dict(torch.load(ckpt_path))
    logging.info(f'Loaded {ckpt_path}')
  
  nll_criterion = None
  criterion = CPriorAugmentedCELoss(net.parameters(), prior_scale=prior_scale, dir_noise=noise,
                                    logits_temp=temperature)

  if epochs:
    run_sgd(train_loader, test_loader, net, criterion, device=device,
            lr=lr, epochs=epochs)

  if sgld_epochs:
    if n_cycles:
      run_csgld(train_loader, test_loader, net, criterion, samples_dir, device=device, nll_criterion=nll_criterion,
                lr=sgld_lr, momentum=momentum, temperature=1., n_samples=n_samples, n_cycles=n_cycles, epochs=sgld_epochs)
    else:
      run_sgld(train_loader, test_loader, net, criterion, samples_dir, device=device, nll_criterion=nll_criterion,
                lr=sgld_lr, momentum=momentum, temperature=1., burn_in=burn_in, n_samples=n_samples, epochs=sgld_epochs)


if __name__ == '__main__':
  import fire
  import os

  logging.getLogger().setLevel(logging.INFO)

  os.environ['WANDB_MODE'] = os.environ.get('WANDB_MODE', default='dryrun')
  fire.Fire(main)
