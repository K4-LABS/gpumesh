"""What happens when a worker goes away in the middle of your job.

Machines you borrowed are machines you do not control. Laptops sleep, wifi
drops, someone closes the lid. gpumesh is built around that rather than
against it, and this example walks through the three things that actually
happen so you can recognise them in your own runs.

  1. LEASE EXPIRY. A worker holds a task under a lease. If it stops
     heartbeating, the coordinator's reaper re-queues the task and another
     worker picks it up. You see this as `attempts` going above 1 — the job
     still finishes, just later.

  2. RETRY CLASSIFICATION. Infrastructure failures get the full retry
     budget. A task whose own code raises does not: re-running it would fail
     identically, so it fails once and reports.

  3. LOCAL FALLBACK. If the whole mesh is unreachable, a decorated function
     runs on your machine and returns the same value. Your script does not
     break because someone's laptop did.

Run it:

    python examples/worker_leaves.py

It works with a coordinator running and without one. To see (1) for real,
run it against a mesh with a second worker, and kill that worker while the
sweep is in flight.
"""

import time

from gpumesh import mesh
from gpumesh.accelerate import accelerate
from gpumesh.api import GPUMesh
from gpumesh.connection_manager import load_connection


@mesh
def slow_step(i):
    """Long enough that you have time to kill a worker mid-run."""
    time.sleep(1.0)
    return {"i": i, "ok": True}


@mesh
def always_raises(i):
    raise ValueError(f"payload {i} is not valid")


def part_1_retries():
    print("== 1. tasks survive a worker leaving ==")
    saved = load_connection() or {}
    if not saved.get("url"):
        print("  no coordinator configured — skipping (this part needs one)")
        print()
        return

    m = GPUMesh(saved["url"], saved["token"])
    try:
        alive = [w for w in m.workers() if w.get("alive")]
    except Exception as exc:
        # A saved connection is not a live one. This is the ordinary state
        # after the coordinator's machine sleeps, and it is not an error.
        print(f"  {saved['url']} is not answering ({type(exc).__name__})")
        print("  skipping — start a coordinator to run this part")
        print()
        return
    print(f"  {len(alive)} worker(s) alive: "
          f"{', '.join(w['id'][:8] for w in alive) or '(none)'}")
    print("  running 6 tasks — kill a worker now to watch them re-queue")

    started = time.time()
    results = slow_step.map([{"i": i} for i in range(6)])
    print(f"  {len(results)} results in {time.time() - started:.1f}s")

    # `attempts` counts leases, so 2 means the first worker holding it went
    # away and the reaper handed the task to someone else.
    print("  (a task re-queued from a dead worker shows attempts > 1 in")
    print("   `gpumesh status <job-id>`)")
    print()


def part_2_user_errors():
    print("== 2. your bug fails once, not three times ==")
    started = time.time()
    try:
        always_raises(i=1)
    except Exception as exc:
        print(f"  {type(exc).__name__} after {time.time() - started:.1f}s")
        print(f"  {str(exc).splitlines()[0][:110]}")
    else:
        print("  unexpected: no exception")
    print("  Deterministic failures skip the retry budget; a network blip")
    print("  keeps it.")
    print()


def part_3_local_fallback():
    print("== 3. an unreachable mesh does not break your script ==")
    # Point at a port nothing is listening on. This is exactly the state your
    # process is in when the coordinator's machine goes to sleep.
    dead = GPUMesh("http://127.0.0.1:1", token="unused")

    @accelerate(dead)
    def double(n):
        return n * 2

    print(f"  double(21) against a dead coordinator -> {double(21)}")
    print(f"  map        against a dead coordinator -> "
          f"{double.map([{'n': 1}, {'n': 2}])}")
    print("  Identical values, computed here. Set GPUMESH_VERBOSE=1 to see")
    print("  the routing decision printed as it is made.")


if __name__ == "__main__":
    import warnings
    # The local-fallback warnings are the point of part 3, so show them once
    # rather than letting Python dedupe them away.
    warnings.simplefilter("always")
    part_1_retries()
    part_2_user_errors()
    part_3_local_fallback()
