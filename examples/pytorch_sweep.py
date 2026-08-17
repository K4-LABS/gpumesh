"""A PyTorch hyperparameter sweep spread across the pool — the workload
gpumesh exists for.

A tiny MLP is trained on a synthetic dataset for every point in a
hyperparameter grid, and the grid is spread across every worker in the mesh
with `.map()`. The results come back as a pandas DataFrame via
`mesh.results_to_dataframe()`.

Run it:

    python examples/pytorch_sweep.py

With no coordinator running, every payload is evaluated locally, in order.
Same results, less parallelism.

**torch must be installed on every worker.** gpumesh ships your code, not
your environment — the `import torch` inside `train()` has to succeed on
whatever machine runs it. That import is *inside* the function for exactly
this reason: the module-level `try/except` here only decides whether this
machine can run the sweep at all, and the worker-side import is what actually
matters. Install torch on each worker with:

    pip install torch

(CPU-only torch is fine — this example is deliberately small enough to run
quickly on a laptop CPU, and the `@mesh` decorator is how you would add
`gpu="cuda"` to pin the whole sweep to GPU workers instead.)
"""

import itertools
import time

from gpumesh import mesh

try:
    import torch  # noqa: F401 — only used to decide whether we can run locally
except ImportError:                                    # pragma: no cover
    torch = None


@mesh
def train(lr, hidden, epochs):
    """Train a tiny two-layer MLP on a synthetic XOR-ish problem.

    Import torch *inside* the function: gpumesh ships your code, not your
    environment, so the import must work on the worker that executes this —
    and that import is the first thing that fails if you forgot to install
    torch there.
    """
    import torch
    import torch.nn as nn

    torch.manual_seed(0)

    # Synthetic binary classification: points near the origin are class 1,
    # the rest class 0. Small on purpose — this must run fast on a CPU.
    n = 400
    x = torch.rand(n, 2) * 4 - 2
    y = ((x.norm(dim=1) < 1.0).float()).unsqueeze(1)

    model = nn.Sequential(
        nn.Linear(2, hidden),
        nn.ReLU(),
        nn.Linear(hidden, 1),
    )
    loss_fn = nn.BCEWithLogitsLoss()
    opt = torch.optim.SGD(model.parameters(), lr=lr)

    for _ in range(epochs):
        opt.zero_grad()
        loss = loss_fn(model(x), y)
        loss.backward()
        opt.step()

    with torch.no_grad():
        pred = (model(x) > 0).float()
        accuracy = (pred == y).float().mean().item()

    return {"lr": lr, "hidden": hidden, "epochs": epochs,
            "accuracy": round(accuracy, 4)}


if __name__ == "__main__":
    if torch is None:
        print("torch is not installed on this machine — skipping.")
        print("  pip install torch      (and install it on every worker too)")
        raise SystemExit(0)

    # A 3x2x2 grid = 12 tasks, one per payload, spread across the pool.
    # `cost` is a scheduler key, not an argument: it weights the tasks so the
    # heavier (more-epoch) ones go to stronger workers, and it is stripped
    # before train() is called. See param_sweep.py for the full explanation.
    grid = [
        {"lr": lr, "hidden": hidden, "epochs": epochs, "cost": epochs / 100.0}
        for lr, hidden, epochs in itertools.product(
            (0.05, 0.2, 0.5), (8, 32), (50, 200)
        )
    ]

    print(f"pool: {mesh.device_count()} device(s), sweeping {len(grid)} configs")
    started = time.time()
    results = train.map(grid)
    elapsed = time.time() - started

    results.sort(key=lambda r: r["accuracy"], reverse=True)
    print(f"finished in {elapsed:.1f}s\n")

    try:
        import pandas as pd
        df = pd.DataFrame(results)
        print(df.to_string(index=False))
    except ImportError:
        # pandas is only needed for the pretty print — the sweep itself is
        # complete. Results are always plain dicts, so fall back to a table.
        print(f"{'lr':>6} {'hidden':>7} {'epochs':>7} {'accuracy':>9}")
        for r in results:
            print(f"{r['lr']:>6} {r['hidden']:>7} {r['epochs']:>7} "
                  f"{r['accuracy']:>9}")
        print("\n(results_to_dataframe() needs pandas:  pip install pandas)")

    print(f"\nbest: {results[0]}")
