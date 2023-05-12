"""
Training script for the BNN experiments with different data sets and priors.
"""

import os
import math
import uuid
import json
import contextlib

import numpy as np
import torch as t
from pathlib import Path
from pyro.infer.mcmc import NUTS, HMC
from pyro.infer.mcmc.api import MCMC
from sacred import Experiment
from sacred.utils import apply_backspaces_and_linefeeds
from sacred.observers import FileStorageObserver

from bnn_priors.data import UCI, CIFAR10, Synthetic
from bnn_priors.models import RaoBDenseNet, DenseNet, PreActResNet18, PreActResNet34
from bnn_priors.prior import LogNormal
from bnn_priors import prior
import bnn_priors.inference
import bnn_priors.inference_reject
from bnn_priors import exp_utils
from bnn_priors.exp_utils import get_prior

# Makes CUDA faster
if t.cuda.is_available():
    t.backends.cudnn.benchmark = True

TMPDIR = "/tmp/"

ex = Experiment("bnn_temp_training")
ex.captured_out_filter = apply_backspaces_and_linefeeds

@ex.config
def config():
    # random seed TODO: does this do anything
    seed = 0
    # the dataset to be trained on, e.g., "mnist", "cifar10", "UCI_boston"
    data = "mnist"
    # the amount of label smoothing
    label_smoothing = 0.
    # the inference method to be used, defaults to GGMC from https://arxiv.org/abs/2102.01691
    inference = "VerletSGLDReject"
    # model to be used, e.g., "classificationdensenet", "classificationconvnet", "googleresnet"
    model = "classificationconvnet"
    # width of the model (might not have an effect in some models)
    width = 50
    # depth of the model (might not have an effect in some models)
    depth = 3
    # weight prior, e.g., "gaussian", "laplace", "student-t"
    weight_prior = "gaussian"
    # bias prior, same as above
    bias_prior = "gaussian"
    # location parameter for the weight prior
    weight_loc = 0.
    # scale parameter for the weight prior
    weight_scale = 2.**0.5
    # location parameter for the bias prior
    bias_loc = 0.
    # scale parameter for the bias prior
    bias_scale = 1.
    # temperatrue for classification layer
    softmax_temp = 1.0
    # additional keyword arguments for the weight prior
    weight_prior_params = {}
    # additional keyword arguments for the bias prior
    bias_prior_params = {}
    if not isinstance(weight_prior_params, dict):
        weight_prior_params = json.loads(weight_prior_params)
    if not isinstance(bias_prior_params, dict):
        bias_prior_params = json.loads(bias_prior_params)
    # total number of samples to be drawn from the posterior
    n_samples = 300
    # number of learning rate cycles for the Markov chain (see https://arxiv.org/abs/1902.03932)
    cycles =  60
    # number of epochs per cycle without added Langevin noise
    burnin = 0
    # number of epochs per cycle with added noise, but without sampling
    warmup = 45
    # number of epochs to skip between taking samples (1 means no skipping)
    skip = 1
    # number of epochs to skip between computing metrics
    metrics_skip = 10
    # number of first samples to discard when evaluating them
    skip_first = 50  # for evaluating accuracy et al at the end
    # temperature of the sampler
    temperature = 1.0
    # learning rate schedule during sampling
    sampling_decay = "cosine"
    # momentum for the sampler
    momentum = 0.994
    # update factor for the preconditioner
    precond_update = 1
    # learning rate
    lr = 5e-4
    # initialization method for the network weights
    init_method = "he"
    # previous samples to be loaded to initialize the chain
    load_samples = None
    # batch size for the training
    batch_size = 128
    # whether to use Metropolis-Hastings rejection steps (works only with some integrators)
    reject_samples = False
    # whether to use batch normalization
    batchnorm = True
    # device to use, "cpu", "cuda:0", "try_cuda"
    device = "try_cuda"
    # whether the samples should be saved
    save_samples = True
    # whether a progressbar should be plotted to stdout during the training
    progressbar = True
    # cold likelihood
    likelihood = "std" # ["std", "cold", "consistency"]
    consistency_std = 100. # standard devication for the consistency loss, ignored otherwise
    # a random unique ID for the run
    run_id = uuid.uuid4().hex
    print(run_id)
    # directory where the results will be stored
    log_dir = str(Path(__file__).resolve().parent.parent/"logs")
    if log_dir is not None:
        os.makedirs(log_dir, exist_ok=True)
        ex.observers.append(FileStorageObserver(log_dir))
    

device = ex.capture(exp_utils.device)
get_model = ex.capture(exp_utils.get_model)

@ex.capture
def get_data(data, batch_size, _run):
    if data == "empty":
        dataset = exp_utils.get_data("UCI_boston", device())
        dataset.norm.train = [(None, None)]
        dataset.norm.test = [(None, None)]
        dataset.unnorm.train = [(None, None)]
        dataset.unnorm.test = [(None, None)]
        return dataset

    if data[:9] == "synthetic":
        _, data, prior = data.split(".")
        dataset = get_data(data)
        x_train = dataset.norm.train_X
        y_train = dataset.norm.train_y
        model = get_model(x_train=x_train, y_train=y_train, weight_prior=prior, weight_prior_params={})
        model.sample_all_priors()
        data = Synthetic(dataset=dataset, model=model, batch_size=batch_size, device=device())
        t.save(data, exp_utils.sneaky_artifact(_run, "synthetic_data.pt"))
        t.save(model, exp_utils.sneaky_artifact(_run, "true_model.pt"))
        return data
    else:
        return exp_utils.get_data(data, device())


@ex.capture
def evaluate_model(model, dataloader_test, samples):
    return exp_utils.evaluate_model(
        model=model, dataloader_test=dataloader_test, samples=samples,
        likelihood_eval=True, accuracy_eval=True,
        calibration_eval=False)


@ex.automain
def main(inference, model, width, n_samples, warmup, init_method, burnin, skip,
         metrics_skip, cycles, temperature, momentum, precond_update, lr,
         batch_size, load_samples, save_samples, reject_samples, run_id,
         log_dir, sampling_decay, progressbar, skip_first, _run, _log, seed):
    assert inference in ["SGLD", "HMC", "VerletSGLD", "OurHMC", "HMCReject", "VerletSGLDReject", "SGLDReject"]
    assert width > 0
    assert n_samples > 0
    assert cycles > 0
    assert temperature >= 0

    t.random.manual_seed(seed)

    data = get_data()

    x_train = data.norm.train_X
    y_train = data.norm.train_y

    x_test = data.norm.test_X
    y_test = data.norm.test_y

    model = get_model(x_train=x_train, y_train=y_train)

    if load_samples is None:
        print(init_method, "init_method is!!!")
        if init_method == "he":
            exp_utils.he_initialize(model)
        elif init_method == "he_uniform":
            exp_utils.he_uniform_initialize(model)
        elif init_method == "he_zerobias":
            exp_utils.he_zerobias_initialize(model)
        elif init_method == "prior":
            pass
        else:
            raise ValueError(f"unknown init_method={init_method}")
    else:
        state_dict = exp_utils.load_samples(load_samples, idx=-1, keep_steps=False)
        model_sd = model.state_dict()
        for k in state_dict.keys():
            if k not in model_sd:
                _log.warning(f"key {k} not in model, ignoring")
                del state_dict[k]
            elif model_sd[k].size() != state_dict[k].size():
                _log.warning(f"key {k} size mismatch, model={model_sd[k].size()}, loaded={state_dict[k].size()}")
                state_dict[k] = model_sd[k]

        missing_keys = set(model_sd.keys()) - set(state_dict.keys())
        _log.warning(f"The following keys were not found in loaded state dict: {missing_keys}")
        model_sd.update(state_dict)
        model.load_state_dict(model_sd)
        del state_dict
        del model_sd

    if save_samples:
        model_saver_fn = (lambda: exp_utils.HDF5ModelSaver(
            exp_utils.sneaky_artifact(_run, "samples.pt"), "w"))
    else:
        @contextlib.contextmanager
        def model_saver_fn():
            yield None

    with exp_utils.HDF5Metrics(
            exp_utils.sneaky_artifact(_run, "metrics.h5"), "w") as metrics_saver,\
         model_saver_fn() as model_saver:
        if inference == "HMC":
            _potential_fn = model.get_potential(x_train, y_train, eff_num_data=len(x_train))
            kernel = HMC(potential_fn=_potential_fn,
                         adapt_step_size=False, adapt_mass_matrix=False,
                         step_size=1e-3, num_steps=32)
            mcmc = MCMC(kernel, num_samples=n_samples, warmup_steps=warmup, initial_params=model.params_dict())
        else:
            if inference == "SGLD":
                runner_class = bnn_priors.inference.SGLDRunner
            elif inference == "VerletSGLD":
                runner_class = bnn_priors.inference.VerletSGLDRunner
            elif inference == "OurHMC":
                runner_class = bnn_priors.inference.HMCRunner
            elif inference == "VerletSGLDReject":
                runner_class = bnn_priors.inference_reject.VerletSGLDRunnerReject
            elif inference == "HMCReject":
                runner_class = bnn_priors.inference_reject.HMCRunnerReject
            elif inference == "SGLDReject":
                runner_class = bnn_priors.inference_reject.SGLDRunnerReject

            assert (n_samples * skip) % cycles == 0
            sample_epochs = n_samples * skip // cycles
            epochs_per_cycle = warmup + burnin + sample_epochs
            if batch_size is None:
                batch_size = len(data.norm.train)
            # Disable parallel loading for `TensorDataset`s.
            num_workers = (0 if isinstance(data.norm.train, t.utils.data.TensorDataset) else 1)
            dataloader = t.utils.data.DataLoader(data.norm.train, batch_size=batch_size, shuffle=True, drop_last=False, num_workers=num_workers)
            dataloader_test = t.utils.data.DataLoader(data.norm.test, batch_size=batch_size, shuffle=False, drop_last=False, num_workers=num_workers)
            mcmc = runner_class(model=model, dataloader=dataloader, dataloader_test=dataloader_test, epochs_per_cycle=epochs_per_cycle,
                                warmup_epochs=warmup, sample_epochs=sample_epochs, learning_rate=lr,
                                skip=skip, metrics_skip=metrics_skip, sampling_decay=sampling_decay, cycles=cycles, temperature=temperature,
                                momentum=momentum, precond_update=precond_update,
                                metrics_saver=metrics_saver, model_saver=model_saver, reject_samples=reject_samples)

        mcmc.run(progressbar=progressbar)
    samples = mcmc.get_samples()
    samples = {k: v[skip_first:] for k, v in samples.items()}
    model.eval()

    batch_size = min(batch_size, len(data.norm.test))
    dataloader_test = t.utils.data.DataLoader(data.norm.test, batch_size=batch_size)

    return evaluate_model(model, dataloader_test, samples)
