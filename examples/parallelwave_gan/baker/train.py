# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import sys
import logging
import argparse
import dataclasses
from pathlib import Path

import yaml
import json
import paddle
import numpy as np
from paddle import nn
from paddle.nn import functional as F
from paddle import distributed as dist
from paddle.io import DataLoader, DistributedBatchSampler
from paddle.optimizer import Adam  # No RAdaom
from paddle.optimizer.lr import StepDecay
from paddle import DataParallel
from visualdl import LogWriter

from parakeet.datasets.data_table import DataTable
from parakeet.training.updater import UpdaterBase
from parakeet.training.trainer import Trainer
from parakeet.training.reporter import report
from parakeet.training.checkpoint import KBest, KLatest
from parakeet.models.parallel_wavegan import PWGGenerator, PWGDiscriminator
from parakeet.modules.stft_loss import MultiResolutionSTFTLoss

from batch_fn import Clip
from config import get_cfg_default
from pwg_updater import PWGUpdater


def train_sp(args, config):
    # decides device type and whether to run in parallel
    # setup running environment correctly
    if not paddle.is_compiled_with_cuda:
        paddle.set_device("cpu")
    else:
        paddle.set_device("gpu")
        world_size = paddle.distributed.get_world_size()
        if world_size > 1:
            paddle.distributed.init_parallel_env()

    print(
        f"rank: {dist.get_rank()}, pid: {os.getpid()}, parent_pid: {os.getppid()}",
    )

    # construct dataset for training and validation
    with open(args.train_metadata) as f:
        train_metadata = json.load(f)
    train_dataset = DataTable(
        data=train_metadata,
        fields=["wave_path", "feats_path"],
        converters={
            "wave_path": np.load,
            "feats_path": np.load,
        }, )
    with open(args.dev_metadata) as f:
        dev_metadata = json.load(f)
    dev_dataset = DataTable(
        data=dev_metadata,
        fields=["wave_path", "feats_path"],
        converters={
            "wave_path": np.load,
            "feats_path": np.load,
        }, )

    # collate function and dataloader
    train_sampler = DistributedBatchSampler(
        train_dataset,
        batch_size=config.batch_size,
        shuffle=True,
        drop_last=True)
    dev_sampler = DistributedBatchSampler(
        dev_dataset,
        batch_size=config.batch_size,
        shuffle=False,
        drop_last=False)
    print("samplers done!")

    train_batch_fn = Clip(
        batch_max_steps=config.batch_max_steps,
        hop_size=config.hop_length,
        aux_context_window=config.generator_params.aux_context_window)
    train_dataloader = DataLoader(
        train_dataset,
        batch_sampler=train_sampler,
        collate_fn=train_batch_fn,  # TODO(defaine collate fn)
        num_workers=config.num_workers)
    dev_dataloader = DataLoader(
        dev_dataset,
        batch_sampler=dev_sampler,
        collate_fn=train_batch_fn,  # TODO(defaine collate fn)
        num_workers=config.num_workers)
    print("dataloaders done!")

    generator = PWGGenerator(**config["generator_params"])
    discriminator = PWGDiscriminator(**config["discriminator_params"])
    if world_size > 1:
        generator = DataParallel(generator)
        discriminator = DataParallel(discriminator)
    print("models done!")

    criterion_stft = MultiResolutionSTFTLoss(**config["stft_loss_params"])
    criterion_mse = nn.MSELoss()
    print("criterions done!")

    lr_schedule_g = StepDecay(**config["generator_scheduler_params"])
    gradient_clip_g = nn.ClipGradByGlobalNorm(config["generator_grad_norm"])
    optimizer_g = Adam(
        learning_rate=lr_schedule_g,
        grad_clip=gradient_clip_g,
        parameters=generator.parameters(),
        **config["generator_optimizer_params"])
    lr_schedule_d = StepDecay(**config["discriminator_scheduler_params"])
    gradient_clip_d = nn.ClipGradByGlobalNorm(config[
        "discriminator_grad_norm"])
    optimizer_d = Adam(
        learning_rate=lr_schedule_d,
        grad_clip=gradient_clip_d,
        parameters=discriminator.parameters(),
        **config["discriminator_optimizer_params"])
    print("optimizers done!")

    output_dir = Path(args.output_dir)
    log_writer = None
    if dist.get_rank() == 0:
        output_dir.mkdir(parents=True, exist_ok=True)
        log_writer = LogWriter(str(output_dir))

    updater = PWGUpdater(
        models={
            "generator": generator,
            "discriminator": discriminator,
        },
        optimizers={
            "generator": optimizer_g,
            "discriminator": optimizer_d,
        },
        criterions={
            "stft": criterion_stft,
            "mse": criterion_mse,
        },
        schedulers={
            "generator": lr_schedule_g,
            "discriminator": lr_schedule_d,
        },
        dataloaders={
            "train": train_dataloader,
            "dev": dev_dataloader,
        },
        discriminator_train_start_steps=config.discriminator_train_start_steps,
        lambda_adv=config.lambda_adv, )

    trainer = Trainer(
        updater,
        stop_trigger=(10, "iteration"),  # PROFILING
        out=output_dir, )
    with paddle.fluid.profiler.profiler('All', 'total',
                                        str(output_dir / "profiler.log"),
                                        'Default') as prof:
        trainer.run()


def main():
    # parse args and config and redirect to train_sp
    parser = argparse.ArgumentParser(description="Train a ParallelWaveGAN "
                                     "model with Baker Mandrin TTS dataset.")
    parser.add_argument(
        "--config", type=str, help="config file to overwrite default config")
    parser.add_argument("--train-metadata", type=str, help="training data")
    parser.add_argument("--dev-metadata", type=str, help="dev data")
    parser.add_argument("--output-dir", type=str, help="output dir")
    parser.add_argument(
        "--nprocs", type=int, default=1, help="number of processes")
    parser.add_argument("--verbose", type=int, default=1, help="verbose")

    args = parser.parse_args()
    config = get_cfg_default()
    if args.config:
        config.merge_from_file(args.config)

    print("========Args========")
    print(yaml.safe_dump(vars(args)))
    print("========Config========")
    print(config)
    print(
        f"master see the word size: {dist.get_world_size()}, from pid: {os.getpid()}"
    )

    # dispatch
    if args.nprocs > 1:
        dist.spawn(train_sp, (args, config), nprocs=args.nprocs)
    else:
        train_sp(args, config)


if __name__ == "__main__":
    main()