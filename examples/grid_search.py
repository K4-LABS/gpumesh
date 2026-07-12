"""Example gpumesh task: hyperparameter search shard.

Trains a tiny logistic-regression classifier (pure Python, no deps) on
synthetic data with the hyperparameters given in the payload, and reports
validation accuracy. Each mesh task runs one payload = one hyperparameter
combination.

Contract with gpumesh:
  - payload JSON arrives on stdin
  - result JSON is printed as the last stdout line
"""

import json
import math
import random
import sys


def make_data(n, seed):
    rng = random.Random(seed)
    xs, ys = [], []
    for _ in range(n):
        x1, x2 = rng.gauss(0, 1), rng.gauss(0, 1)
        label = 1 if (1.5 * x1 - 2.0 * x2 + rng.gauss(0, 0.3)) > 0 else 0
        xs.append((x1, x2))
        ys.append(label)
    return xs, ys


def train(xs, ys, lr, epochs, l2):
    w1 = w2 = b = 0.0
    n = len(xs)
    for _ in range(epochs):
        g1 = g2 = gb = 0.0
        for (x1, x2), y in zip(xs, ys):
            z = w1 * x1 + w2 * x2 + b
            p = 1.0 / (1.0 + math.exp(-max(-30, min(30, z))))
            d = p - y
            g1 += d * x1
            g2 += d * x2
            gb += d
        w1 -= lr * (g1 / n + l2 * w1)
        w2 -= lr * (g2 / n + l2 * w2)
        b -= lr * gb / n
    return w1, w2, b


def accuracy(xs, ys, w1, w2, b):
    correct = sum(
        1 for (x1, x2), y in zip(xs, ys)
        if (w1 * x1 + w2 * x2 + b > 0) == (y == 1)
    )
    return correct / len(xs)


def main():
    payload = json.load(sys.stdin)
    lr = payload.get("lr", 0.1)
    epochs = payload.get("epochs", 200)
    l2 = payload.get("l2", 0.0)
    n_samples = payload.get("n_samples", 2000)

    train_x, train_y = make_data(n_samples, seed=42)
    val_x, val_y = make_data(500, seed=7)

    w1, w2, b = train(train_x, train_y, lr, epochs, l2)
    acc = accuracy(val_x, val_y, w1, w2, b)

    print(json.dumps({
        "lr": lr, "epochs": epochs, "l2": l2,
        "val_accuracy": round(acc, 4),
        "weights": [round(w1, 4), round(w2, 4), round(b, 4)],
    }))


if __name__ == "__main__":
    main()
