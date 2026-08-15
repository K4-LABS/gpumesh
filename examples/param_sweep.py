"""A hyperparameter sweep with `.map()` — the workload gpumesh exists for.

`.map()` turns one payload list into one task per payload and spreads them
across every worker in the pool. Heavier payloads (a larger `cost`) are
routed to stronger workers, so a 4090 and a laptop CPU in the same mesh both
stay busy instead of the laptop holding everyone up.

Run it:

    python examples/param_sweep.py

With no coordinator running, every payload is evaluated locally, in order.
Same results, less parallelism.
"""

import itertools
import math
import random
import time

from gpumesh import mesh


@mesh
def evaluate(lr, l2, epochs):
    """Train a two-feature logistic regression and report validation accuracy.

    Pure Python on purpose: no numpy, no torch, nothing that has to be
    installed on the other machines. gpumesh ships your code, not your
    environment — every import here has to exist on the worker too.
    """
    rng = random.Random(0)
    train = [((rng.gauss(0, 1), rng.gauss(0, 1)),) for _ in range(1500)]
    train = [(x, 1 if (1.5 * x[0] - 2.0 * x[1] + rng.gauss(0, 0.3)) > 0 else 0)
             for (x,) in train]

    w1 = w2 = b = 0.0
    n = len(train)
    for _ in range(epochs):
        g1 = g2 = gb = 0.0
        for (x1, x2), y in train:
            z = max(-30.0, min(30.0, w1 * x1 + w2 * x2 + b))
            d = 1.0 / (1.0 + math.exp(-z)) - y
            g1 += d * x1
            g2 += d * x2
            gb += d
        w1 -= lr * (g1 / n + l2 * w1)
        w2 -= lr * (g2 / n + l2 * w2)
        b -= lr * gb / n

    hits = sum(1 for (x1, x2), y in train
               if (w1 * x1 + w2 * x2 + b > 0) == (y == 1))
    return {"lr": lr, "l2": l2, "epochs": epochs,
            "accuracy": round(hits / n, 4)}


if __name__ == "__main__":
    # Every key here is passed to evaluate() as a keyword argument, so the
    # payload keys and the function signature have to match exactly — with one
    # exception, and it is the point of this example.
    #
    # `cost` is a scheduler key, not an argument. It is a relative task weight
    # (default 1.0) that the coordinator reads to decide *which* worker gets
    # *which* payload: the queue is sorted by cost and strong workers are
    # served from the heavy end. It is stripped before evaluate() is called,
    # so the signature does not need a `cost` parameter and must not grow one.
    #
    # Putting it in a `.map()` list is supported on every path — the mesh path
    # and both local fallbacks strip it identically, so this file produces the
    # same results with a coordinator up, with GPUMESH_LOCAL=1, and with
    # nothing running at all. (In gpumesh 1.3.0 and earlier the local fallback
    # did not strip it and raised `unexpected keyword argument 'cost'` the
    # moment no worker was alive; if you are on an older release, leave `cost`
    # out of `.map()` lists.)
    #
    # Here cost tracks the epoch count, because epochs is what actually drives
    # the runtime of evaluate(). A cost that does not correlate with real work
    # is worse than no cost at all — it routes confidently in the wrong
    # direction.
    grid = [
        {"lr": lr, "l2": l2, "epochs": epochs, "cost": epochs / 100.0}
        for lr, l2, epochs in itertools.product(
            (0.05, 0.2), (0.0, 0.01), (100, 300)
        )
    ]

    print(f"pool: {mesh.device_count()} device(s), sweeping {len(grid)} configs")
    started = time.time()
    results = evaluate.map(grid)
    elapsed = time.time() - started

    results.sort(key=lambda r: r["accuracy"], reverse=True)
    print(f"finished in {elapsed:.1f}s\n")
    print(f"{'lr':>6} {'l2':>6} {'epochs':>7} {'accuracy':>9}")
    for r in results:
        print(f"{r['lr']:>6} {r['l2']:>6} {r['epochs']:>7} {r['accuracy']:>9}")
    print(f"\nbest: {results[0]}")
