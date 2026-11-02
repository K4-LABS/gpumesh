"""GUI payoff demo: a tiny sweep where every task reports which machine ran it.

The dashboard's best moment is watching work land on other people's laptops.
This script produces that moment with nothing but the standard library: six
small tasks, each printing the hostname of the worker that ran it, and a final
line counting how many machines worked as one.

Run it (Python API path, the same `@mesh` + `.map()` as hello_mesh.py):

    python examples/gui_demo_sweep.py

Or submit it (script path, the same stdin/stdout contract as grid_search.py):

    gpumesh submit examples/gui_demo_sweep.py --payloads examples/payloads.json --wait

With no coordinator running at all, this still works: `@mesh` falls back to
local execution and every task reports this machine. That fallback is the
existing behaviour of `gpumesh.mesh` / `gpumesh.accelerate` — nothing here
reimplements it. Same values, less parallelism.
"""

import json
import sys

from gpumesh import mesh


@mesh
def sweep_task(task_id, size):
    """One embarrassingly parallel unit: checksum `size` numbers, name the worker.

    Pure stdlib on purpose: no numpy, no torch, nothing that has to be
    installed on the other machines. gpumesh ships your code, not your
    environment. `socket` is imported inside the function so it travels with
    the function to the worker rather than relying on the worker's globals.
    """
    import socket

    checksum = sum((i * i) % 10007 for i in range(size))
    return {
        "task_id": task_id,
        "size": size,
        "checksum": checksum,
        "worker": socket.gethostname(),
    }


# Six tasks: enough to see work spread, small enough to finish in a second.
PAYLOADS = [{"task_id": i, "size": 20000 + i * 5000} for i in range(6)]


def _stdin_payload():
    """A piped JSON object means `gpumesh submit` is driving: run one task.

    Returns the payload dict, or None when this is a normal terminal run.
    An empty pipe or non-JSON stdin is not a payload — it is just how some
    launchers invoke scripts — so those fall through to the sweep below.
    """
    if sys.stdin is None or sys.stdin.isatty():
        return None
    try:
        raw = sys.stdin.read()
    except Exception:
        return None
    if not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) and "size" in payload else None


def main():
    single = _stdin_payload()
    if single is not None:
        # Script path: one payload on stdin, result JSON as the last stdout
        # line (the grid_search.py contract). Call the undecorated function:
        # this process IS the worker, so going back through `@mesh` would
        # re-submit the task to the pool instead of running it.
        result = sweep_task.__wrapped__(
            task_id=single.get("task_id", 0), size=single["size"]
        )
        print(json.dumps(result))
        return 0

    print(f"[gui-demo] pool: {mesh.device_count()} device(s), "
          f"sweeping {len(PAYLOADS)} configs")
    results = sweep_task.map(PAYLOADS)

    for r in sorted(results, key=lambda d: d["task_id"]):
        print(f"[gui-demo] task {r['task_id']}: size={r['size']} "
              f"checksum={r['checksum']} worker={r['worker']}")

    machines = sorted({r["worker"] for r in results})
    print(f"[gui-demo] {len(results)} tasks, {len(machines)} machine(s) "
          f"worked as one: {', '.join(machines)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
