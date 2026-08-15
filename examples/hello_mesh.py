"""The smallest useful gpumesh program: one machine, one decorator.

You do not need a second machine. `gpumesh serve` joins your own CPU/GPU to
the pool by default, so a mesh of one is a real mesh — the same code path,
the same wire format, the same results.

Run it:

    # terminal 1
    gpumesh serve --token "$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"

    # terminal 2
    python examples/hello_mesh.py

With no coordinator running at all, this still works: @mesh falls back to
local execution and returns the identical value. That is the point — your
script never breaks because the mesh is down.
"""

from gpumesh import mesh


@mesh
def square(n):
    """Pretend this is expensive."""
    return {"n": n, "square": n * n}


if __name__ == "__main__":
    # How much compute is in the pool right now? 1 means "just this machine".
    print(f"devices in pool: {mesh.device_count()}")
    print(f"total score:     {mesh.total_score():.3f}")
    print()

    # One call -> one task on one worker (which may well be this machine).
    print("single call:", square(n=7))

    # .map() -> one task per payload, spread across every worker in the pool.
    print("map:        ", square.map([{"n": i} for i in range(1, 6)]))

    print()
    print("Same values whether that ran on the mesh or locally. Confirm with:")
    print("    GPUMESH_LOCAL=1 python examples/hello_mesh.py")
    print("    GPUMESH_VERBOSE=1 python examples/hello_mesh.py   # prints where each task ran")
