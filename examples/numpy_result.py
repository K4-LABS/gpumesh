"""Returning something JSON cannot express — a numpy array.

A task's return value crosses three hops: worker subprocess -> worker ->
coordinator (SQLite, as JSON) -> you. Only the middle hop is constrained, so
gpumesh wraps every function result in an envelope: JSON-encodable values
pass through verbatim, anything else is cloudpickled. You get back the object
your function returned, with its type intact.

That covers numpy arrays and scalars, torch tensors, pandas DataFrames, sets,
dataclasses, Decimals — anything cloudpickle can handle.

What it does NOT cover is anything tied to a live process: sockets, locks,
database connections, CUDA handles. Those cannot be moved to another machine
at all, and the task fails with a message naming the type. Return plain data
instead.

Watch out for the near miss: an open file object *does* survive the trip,
because cloudpickle reads it and hands you back a StringIO. The bytes arrive;
the file on the worker's disk does not. Returning a path and reading it on
the wrong machine is the bug that pattern hides.

Run it:

    python examples/numpy_result.py
"""

from decimal import Decimal
from fractions import Fraction

from gpumesh import mesh

try:
    import numpy as np
except ImportError:                                   # pragma: no cover
    np = None


@mesh
def stats(seed, size):
    """Return numpy objects, not just numbers."""
    import numpy as np                    # imported inside: must exist on the worker
    rng = np.random.default_rng(seed)
    sample = rng.normal(size=size)
    return {
        "mean": np.float64(sample.mean()),   # numpy scalar, not a Python float
        "hist": np.histogram(sample, bins=4)[0],   # ndarray of int64
        "dtype": str(sample.dtype),
    }


@mesh
def exotic(n):
    """Types the standard library has but JSON does not."""
    return {
        "decimal": Decimal("1.10") * n,
        "fraction": Fraction(1, 3) * n,
        "as_set": {n, n + 1, n + 2},
        "as_bytes": b"\x00\xff" * n,
    }


@mesh
def unsendable():
    """Deliberately broken: a lock belongs to one process on one machine."""
    import threading
    return {"lock": threading.Lock()}


if __name__ == "__main__":
    if np is None:
        print("numpy is not installed here — skipping the numpy section.")
        print("  pip install numpy      (and install it on every worker too)")
    else:
        out = stats(seed=1, size=1000)
        print("stats() returned:")
        for key, value in out.items():
            print(f"  {key:8} {type(value).__name__:12} {value!r}")
        assert isinstance(out["hist"], np.ndarray), "envelope lost the ndarray type"
        assert out["hist"].sum() == 1000
        print("  -> still a real ndarray, sums to 1000\n")

    out = exotic(n=3)
    print("exotic() returned:")
    for key, value in out.items():
        print(f"  {key:9} {type(value).__name__:9} {value!r}")
    assert out["decimal"] == Decimal("3.30")
    assert out["as_set"] == {3, 4, 5}
    print()

    print("unsendable() — expected to fail:")
    try:
        value = unsendable()
    except Exception as exc:
        print(f"  {type(exc).__name__}: {exc}")
    else:
        # No coordinator: the call never left this process, so nothing had to
        # be serialized and the lock came back intact.
        print(f"  ran locally, so nothing was serialized: {value}")
        print("  start a coordinator to see the real failure")
