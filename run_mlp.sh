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
        --dirty_lik="mlp" \
        --likelihood="softmax" \
        --label_noise=0.0 \
        --logits_temp=1.0 \
        --noise=0.0 \
        --prior-scale=1.0 \
        --sgld-epochs=100 \
        --sgld-lr=1e-6 \
        --momentum=0.99 \
        --n-samples=10 \
        --n_cycles=10 &

    python experiments/train_lik.py --temperature=$T \
        --augment=False \
        --replacement=False \
        --perm=False \
        --seed=15 \
        --dataset="mnist" \
        --data_dir="mnist" \
        --dirty_lik="mlp" \
        --likelihood="softmax" \
        --label_noise=0.0 \
        --logits_temp=0.5 \
        --noise=0.0 \
        --prior-scale=1.0 \
        --sgld-epochs=100 \
        --sgld-lr=1e-6 \
        --momentum=0.99 \
        --n-samples=10 \
        --n_cycles=10 &

    python experiments/train_lik.py --temperature=$T \
        --augment=False \
        --replacement=False \
        --perm=False \
        --seed=15 \
        --dataset="mnist" \
        --data_dir="mnist" \
        --dirty_lik="mlp" \
        --likelihood="softmax" \
        --label_noise=0.0 \
        --logits_temp=0.2 \
        --noise=0.0 \
        --prior-scale=1.0 \
        --sgld-epochs=100 \
        --sgld-lr=1e-6 \
        --momentum=0.99 \
        --n-samples=10 \
        --n_cycles=10 &

    python experiments/train_lik.py --temperature=$T \
        --augment=False \
        --replacement=False \
        --perm=False \
        --seed=15 \
        --dataset="mnist" \
        --data_dir="mnist" \
        --dirty_lik="mlp" \
        --likelihood="softmax" \
        --label_noise=0.0 \
        --logits_temp=0.1 \
        --noise=0.0 \
        --prior-scale=1.0 \
        --sgld-epochs=100 \
        --sgld-lr=1e-6 \
        --momentum=0.99 \
        --n-samples=10 \
        --n_cycles=10 &
wait
done