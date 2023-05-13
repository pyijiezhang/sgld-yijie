#!/bin/bash

Ts=(2.0 1.0 0.667 0.5 0.4 0.333 0.286)

for T in ${Ts[*]}; do 
    echo $T
    python experiments/train_lik.py --temperature=$T \
        --augment=False \
        --replacement=False \
        --perm=False \
        --seed=15 \
        --dataset="mnist" \
        --data_dir="mnist" \
        --dirty_lik="lenet" \
        --likelihood="softmax" \
        --label_noise=0.0 \
        --logits_temp=5.0 \
        --noise=0.0 \
        --prior-scale=0.01 \
        --sgld-epochs=20 \
        --sgld-lr=1e-6 \
        --momentum=0.99 \
        --n-samples=4 \
        --n_cycles=4 &   

    python experiments/train_lik.py --temperature=$T \
        --augment=False \
        --replacement=False \
        --perm=False \
        --seed=15 \
        --dataset="mnist" \
        --data_dir="mnist" \
        --dirty_lik="lenet" \
        --likelihood="softmax" \
        --label_noise=0.0 \
        --logits_temp=5.0 \
        --noise=0.0 \
        --prior-scale=1.0 \
        --sgld-epochs=20 \
        --sgld-lr=1e-6 \
        --momentum=0.99 \
        --n-samples=4 \
        --n_cycles=4 & 

    python experiments/train_lik.py --temperature=$T \
        --augment=False \
        --replacement=False \
        --perm=False \
        --seed=15 \
        --dataset="mnist" \
        --data_dir="mnist" \
        --dirty_lik="lenet" \
        --likelihood="softmax" \
        --label_noise=0.0 \
        --logits_temp=1.0 \
        --noise=0.0 \
        --prior-scale=0.01 \
        --sgld-epochs=20 \
        --sgld-lr=1e-6 \
        --momentum=0.99 \
        --n-samples=4 \
        --n_cycles=4 &     
    wait
done