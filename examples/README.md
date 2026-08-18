# gpumesh examples

Every script here runs. With a coordinator up they use the mesh; with nothing
running they fall back to local execution and print the same answers, because
that is the guarantee gpumesh makes and an example that only works in the
happy path is not worth much.

These files ship with the git repository, not with the pip package. Clone
first:

```bash
git clone https://github.com/K4-LABS/gpumesh.git
cd gpumesh
pip install -e .
```

## Start here

One machine is enough. `gpumesh serve` joins your own CPU/GPU to the pool by
default, so a mesh of one exercises exactly the same code path as a mesh of
ten.

```bash
# terminal 1 — generate a real token, not "mysecret"
gpumesh serve --token "$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"

# terminal 2
python examples/hello_mesh.py
```

The coordinator binds to `127.0.0.1` by default, so nothing outside this
machine can connect. [`second_machine.py`](second_machine.py) covers opening
that up, and what you are agreeing to when you do.

## The files

| File | What it shows | Needs |
|------|---------------|-------|
| [`hello_mesh.py`](hello_mesh.py) | The smallest complete program: `@mesh`, a single call, a `.map()`, and the pool size | nothing |
| [`second_machine.py`](second_machine.py) | Reads your live mesh and prints the exact `join` command for another machine, plus what `--host 0.0.0.0` really means | nothing |
| [`param_sweep.py`](param_sweep.py) | A hyperparameter grid spread across the pool with `.map()` — the workload gpumesh exists for | nothing |
| [`pytorch_sweep.py`](pytorch_sweep.py) | The same sweep with a real (tiny) PyTorch model, results back as a pandas DataFrame | torch (on every worker), pandas optional |
| [`numpy_result.py`](numpy_result.py) | Returning values JSON cannot express: ndarrays, `Decimal`, `set`, `bytes` — and one that genuinely cannot cross machines | numpy (optional; skips cleanly without it) |
| [`worker_leaves.py`](worker_leaves.py) | Lease expiry and re-queue, why your own bug is not retried three times, and the local fallback when the mesh is gone | nothing |
| [`dev_mode.py`](dev_mode.py) | The "connect once, code normally" flow for VS Code / Jupyter / PyCharm | nothing |
| [`grid_search.py`](grid_search.py) + [`payloads.json`](payloads.json) | The *script* path rather than the decorator path: one payload per task, JSON on stdin, JSON on stdout | nothing |

## Two ways to run work

The decorator path is for code you are writing right now:

```python
from gpumesh import mesh

@mesh
def train(lr, epochs):
    return {"accuracy": 0.95}

train.map([{"lr": 0.01, "epochs": 100}, {"lr": 0.05, "epochs": 200}])
```

The script path is for a job you want to submit and walk away from. Your
script reads one payload as JSON on stdin and prints its result as JSON on
stdout:

```console
$ gpumesh submit examples/grid_search.py --payloads examples/payloads.json --wait
```

The script path is also what `--safe-mode` leaves enabled: a coordinator
started with `--safe-mode` refuses function distribution entirely and accepts
submitted scripts only.

## Things worth knowing before you write your own

- **gpumesh ships your code, not your environment.** Every import your
  function makes has to already be installed on the worker. That is why the
  examples here stick to the standard library wherever they can.
- **Payload keys become keyword arguments.** `{"lr": 0.01}` calls
  `your_function(lr=0.01)`. A key your function does not accept is a
  `TypeError`, not a silent ignore.
- **`cost` is a scheduler key, not an argument.** In `gpumesh submit
  --payloads` payloads, `mesh.submit()` payloads, and `.map()` lists alike,
  `cost` is a relative task weight (default `1.0`) that routes heavier tasks to
  stronger workers. It is stripped before your function is called, on the mesh
  path and on the local fallback both, so your signature does not need a `cost`
  parameter. `param_sweep.py` uses it. (Placement hints — `gpu`,
  `gpu_memory_mb`, `cpu_cores` — behave the *opposite* way in a `.map()` list:
  they are not stripped, because there they are ordinary payload keys and
  reach your function. They only act as hints when `@mesh`/`@accelerate` writes
  them from its own keywords.)
- **Return plain data.** Arrays, tensors and DataFrames are fine. Anything
  bound to a live process — sockets, locks, database handles, CUDA handles —
  is not, and fails with a message naming the type.
- **Same Python minor version on every machine, ideally.** Functions ship as
  cloudpickled bytecode when versions match and as source text when they do
  not, and the source path cannot carry closures, module-level constants,
  bound methods or callable objects. See
  [`docs/protocol.md`](../docs/protocol.md).
