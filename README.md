<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/K4-LABS/gpumesh/master/docs/img/gpumesh-logo.png">
  <img alt="gpumesh — GPU Mesh Network" src="https://raw.githubusercontent.com/K4-LABS/gpumesh/master/docs/img/gpumesh-logo-light.png" width="240">
</picture>

# gpumesh

**Borrow your friends' GPUs.** A distributed compute mesh that lets you share GPU power
across machines on your network — with one decorator, one CLI command, or a Python API.

[![Tests](https://img.shields.io/github/actions/workflow/status/K4-LABS/gpumesh/tests.yml?branch=master&label=tests&style=for-the-badge)](https://github.com/K4-LABS/gpumesh/actions/workflows/tests.yml)
[![PyPI](https://img.shields.io/pypi/v/gpumesh?style=for-the-badge)](https://pypi.org/project/gpumesh/)
[![Python](https://img.shields.io/pypi/pyversions/gpumesh?style=for-the-badge)](https://pypi.org/project/gpumesh/)
[![License](https://img.shields.io/badge/License-AGPL--3.0-blue.svg?style=for-the-badge)](https://github.com/K4-LABS/gpumesh/blob/master/LICENSE)
[![Contributors](https://img.shields.io/github/contributors/K4-LABS/gpumesh?style=for-the-badge)](https://github.com/K4-LABS/gpumesh/graphs/contributors)

[Quickstart](#quickstart) ·
[Docs](https://github.com/K4-LABS/gpumesh/tree/master/docs) ·
[Contributing](https://github.com/K4-LABS/gpumesh/blob/master/CONTRIBUTING.md) ·
[Issues](https://github.com/K4-LABS/gpumesh/issues)

</div>



## What is gpumesh?

gpumesh turns multiple machines into a **single, unified compute pool**. Start a **coordinator** on one machine, join **workers** from other machines (laptops, desktops, servers — anything with Python), and run code across all of them as if they were one device.

**Use cases:**

- Hyperparameter search across multiple GPUs
- Data preprocessing sharded across machines
- Model training on a pool of consumer GPUs
- Any embarrassingly parallel workload

### Key features

- **`@mesh` / `@accelerate` decorators** — mark a function and it runs on the pool; no job system, no ceremony
- **`.map()`** — spread one call across *every* connected machine at once
- **Smart routing** — calls go to a mesh worker when one is alive, to your own machine when none is
- **Graceful fallback** — mesh unreachable? your code runs locally and returns the same value
- **Any return value** — numpy arrays, torch tensors, DataFrames — exactly what your function returned
- **Fault tolerance** — workers survive sleep, WiFi drops, and coordinator restarts; dead workers' tasks are re-queued
- **Benchmark scoring** — every worker gets a relative compute score; the scheduler routes work to the strongest hardware
- **Memory-aware scheduling** — VRAM is tracked; a payload's `gpu_memory_mb` hint keeps a task off a busy worker
- **Live radar** — `gpumesh radar` discovers nearby devices on your network; no config needed
- **Isolated execution** — every task runs in its own subprocess; a crashing task can't take down a worker
- **Token security** — all API calls require a token; rate-limited, timing-safe verification
- **Jupyter support** — `%%mesh` cell magic wraps every function in a cell automatically

---

> [!CAUTION]
> **gpumesh runs code you send it — there is no sandbox.** A worker executes
> arbitrary Python as the OS user that started it, and results are deserialized
> by the submitter — trust runs **both ways**. Use it only with machines and
> people you trust. See [SECURITY.md](https://github.com/K4-LABS/gpumesh/blob/master/SECURITY.md)
> and [THREAT_MODEL.md](https://github.com/K4-LABS/gpumesh/blob/master/THREAT_MODEL.md).

---

## Quick demo

```python
from gpumesh import GPUMesh, accelerate

mesh = GPUMesh("http://coordinator:8000", token=TOKEN)

@accelerate(mesh)
def train(lr, epochs):
    return {"accuracy": 0.95}

result = train(lr=0.01, epochs=100)         # one mesh worker (or local if none)

results = train.map([                        # spread across all mesh devices
    {"lr": 0.01, "epochs": 100},
    {"lr": 0.05, "epochs": 200},
])
```

---

## Installation

```bash
pip install gpumesh
pip install gpumesh[gpu]       # GPU detection + CUDA benchmarks
pip install gpumesh[tunnel]    # ngrok for public URLs
pip install gpumesh[sysinfo]   # System info (psutil)
pip install gpumesh[notebook]  # DataFrame support (pandas)
pip install gpumesh[ui]        # Setup wizard (rich + questionary)
pip install gpumesh[all]       # Everything above
```

Or with Docker:

```bash
docker pull samurai007ak/gpumesh:3.0.0
```

**Requires:** Python 3.9+, cloudpickle (auto-installed). PyTorch is optional (needed for GPU detection).

Verify the install:

```console
$ gpumesh --version
gpumesh 3.0.0 (3.11.9, Windows)
```

---

## Quickstart

**One machine is enough.** `gpumesh serve` joins your own CPU/GPU to the pool
by default, so a mesh of one runs exactly the same code path as a mesh of ten
— same scheduler, same wire format, same results. Get that working first;
adding a second machine is one flag and one command.

All terminal output below is real, copied from actual runs, with addresses
and hostnames replaced by placeholders.

### 1. Generate a token

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Do this once and reuse the value. It is not decoration: the token is the only
thing standing between anyone who can reach the port and code execution as
you. Short, memorable tokens are guessable, and the coordinator's hash is a
single SHA-256 round with no KDF — the entropy of the token is the whole
defence.

### 2. Start a coordinator

```bash
pip install gpumesh
gpumesh serve --port 8000 --token $TOKEN
```

```console
[gpumesh] config saved to ~/.gpumesh/config.json
[OK] Coordinator listening on 127.0.0.1:8000
   Token: <your token>

   Join from THIS machine:   gpumesh join http://127.0.0.1:8000 --token <your token>
   Other machines CANNOT reach this coordinator (bound to 127.0.0.1 only).
   That is the default now: a worker executes code as the user who started it.
   To let other machines join, re-run with: gpumesh serve --host 0.0.0.0 --port 8000 --token <your token>
   (or set GPUMESH_HOST=0.0.0.0). Read the warning it prints before you do.
   Ctrl+C to stop
[OK] Self-check: server is up (tested on 127.0.0.1 only)
[OK] Self-worker started — this machine's CPU/GPU is part of the pool
   (disable with --no-self-worker)
[worker] device=CPU Intel64 Family 6 Model 154 Stepping 3, GenuineIntel score=0.373 GFLOP/s
[mesh] worker joined: laptop-a (Intel64 Family 6 Model 154 Stepping 3, GenuineIntel, score=0.373)
[worker] joined mesh as 0afecb2dfd44
```

You now have a working mesh. Confirm it in another terminal:

```console
$ gpumesh workers

  WORKERS
  ------------------------------------------------------
  CPU  0afecb2d    laptop-a          score=0.373     [alive]
```

Prefer a guided wizard? Run `gpumesh setup`.

### 3. Run a distributed task

Save as `demo.py`:

```python
from gpumesh import mesh          # auto-connects using the saved config

@mesh
def train(lr, epochs):
    return {"lr": lr, "accuracy": round(0.90 + lr, 3)}

print(train(lr=0.01, epochs=100))                              # one worker
print(train.map([{"lr": 0.01, "epochs": 100},                  # every worker
                 {"lr": 0.05, "epochs": 200}]))
```

```console
$ python demo.py
[gpumesh] connected to coordinator at http://127.0.0.1:8000
{'lr': 0.01, 'accuracy': 0.91}
[{'lr': 0.01, 'accuracy': 0.91}, {'lr': 0.05, 'accuracy': 0.95}]
```

That is the whole workflow. Kill the coordinator and run it again — you get
the identical output, computed locally. Your script does not break because
the mesh went away.

More runnable examples, all verified:
[`examples/`](https://github.com/K4-LABS/gpumesh/tree/master/examples).

### 4. Add a second machine

The coordinator binds to loopback, so nothing outside your machine can reach
it. That is the default, and opening it up is a decision rather than an
oversight — a worker runs whatever code the coordinator hands it, as the OS
user who started that worker. Restart with `--host 0.0.0.0` when you mean it:

```bash
gpumesh serve --host 0.0.0.0 --port 8000 --token $TOKEN
```

```console
   Join from THIS machine:   gpumesh join http://127.0.0.1:8000 --token <your token>
   Join from ANOTHER machine: gpumesh join http://192.0.2.10:8000 --token <your token>
   (127.0.0.1 always works locally; the LAN IP is for other machines)

  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
  !! NETWORK-EXPOSED COORDINATOR
  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
   Bound to:      0.0.0.0:8000 (not loopback — other machines can connect)
   Tasks run as:  alex
   On device:     CPU Intel64 Family 6 Model 154 Stepping 3, GenuineIntel

   Anyone who can reach this port AND has the token can run
   arbitrary code on this machine as alex.
   Only do this on a network you trust, and keep the token secret.
   Loopback-only (the default) is: gpumesh serve --port 8000
  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!

   Reachability of http://192.0.2.10:8000 from other machines is not tested here — a worker joining confirms it.
```

**Copy the `Join from ANOTHER machine` line** — that is the exact command the
second machine needs.

```bash
# on machine B
pip install gpumesh
gpumesh join http://192.0.2.10:8000 --token $TOKEN
```

```console
[worker] device=CPU Intel64 Family 6 Model 154 Stepping 3 score=0.310 GFLOP/s
[worker] joined mesh as 7c1e04ab93f2
```

```console
$ gpumesh workers

  WORKERS
  ------------------------------------------------------
  CPU  0afecb2d    laptop-a          score=0.373     [alive]
  CPU  7c1e04ab    laptop-b          score=0.310     [alive]
```

> **Workers never die.** They survive laptop sleep, WiFi drops and coordinator
> restarts, and reconnect on their own.
>
> Windows: run `gpumesh serve` as Administrator so the firewall rule is added
> for you. Without it, workers on other machines may be blocked.
>
> If the worker reports a timeout instead, it could not reach that address —
> see [Troubleshooting](#troubleshooting).

`examples/second_machine.py` reads your live mesh and prints the exact
command, filled in with your address, port and token.

### Prefer the CLI?

Submit a script instead of decorating a function. (The `examples/` files ship
with the repository, not the pip package — `git clone` first, or point the
command at any script of your own that reads a JSON payload on stdin.)

```console
$ gpumesh submit examples/grid_search.py --payloads examples/payloads.json --wait
[OK] Submitted job c3f81cc498f0
Job: examples/grid_search.py (c3f81cc498f0)

  #################### 100%  6/6 done  (10s)

  Status: finished
  Counts: {'done': 6}

  ✓ task 463a58e92490 [done]  cost=1.0  worker=0afecb2dfd44
    result: {"lr": 0.01, "epochs": 100, "l2": 0.0, "val_accuracy": 0.948, "weights": [0.2271, -0.2757, 0.0019]}
  ✓ task 7fa79a95725f [done]  cost=10.0  worker=0afecb2dfd44
    result: {"lr": 0.3, "epochs": 1000, "l2": 0.01, "val_accuracy": 0.952, "weights": [2.1907, -2.9448, 0.0191]}
```

### Code normally

This is the entire point. Once a worker is connected, every machine sees the
same pool, and the `demo.py` above is already the whole API — write normal
Python, mark the heavy functions, call `.map()` when you have a batch. It works
in VS Code, Jupyter, PyCharm, or a plain terminal. No job submission, no CLI
commands, no ceremony.

Your function returns whatever it normally returns — a dict, an int, a list, a numpy array, a torch tensor — and you get that same object back:

```python
@mesh
def evaluate(seed):
    import numpy as np
    return {"loss": np.float32(0.12), "preds": np.arange(10)}

out = evaluate(seed=1)      # {'loss': np.float32(0.12), 'preds': array([0, ..., 9])}
```

---

## Jupyter notebooks

Load the extension once, in its own cell:

```python
%load_ext gpumesh
```

Then `%%mesh` as the **first line** of any cell wraps every function defined in that cell with `@mesh`:

```python
%%mesh
def preprocess(chunk_id, rows):
    return {"chunk": chunk_id, "rows": rows * rows}

results = preprocess.map([{"chunk_id": i, "rows": 100 + i} for i in range(6)])
```

Like `%%time`, the cell's own output displays normally. Loading the extension also injects a bare `@mesh` decorator into the notebook namespace, so you can decorate individual functions instead of a whole cell.

| Magic | What it does |
|-------|--------------|
| `%%mesh` | Wrap every function in this cell with `@mesh` |
| `%mesh_devices` | List the devices in the pool |
| `%mesh_status` | Show the saved connection and device count |
| `%mesh_connect URL TOKEN` | Connect to a coordinator from inside the notebook |

---

## CLI reference

Every command — server, jobs, monitoring — plus the environment variables
behind them, with `--host` vs `--host-ip` and colour handling explained:
[`docs/cli.md`](https://github.com/K4-LABS/gpumesh/blob/master/docs/cli.md).

---

## Python API

```python
from gpumesh import GPUMesh

mesh = GPUMesh("http://coordinator:8000", token=TOKEN)
```

### Distribute a function

```python
results = mesh.distribute(
    function=train_model,
    params=[{"lr": 0.01, "epochs": 100}, {"lr": 0.05, "epochs": 200}],
    timeout=600,
)
```

### Inspect the pool

```python
workers = mesh.workers()        # [{'id', 'device', 'device_name', 'hostname', 'score', 'alive'}]
devices = mesh.devices()        # unified pool view
count   = mesh.device_count()   # alive machines contributing compute (GPU or CPU)
gpus    = mesh.gpu_count()      # alive GPUs only
total   = mesh.total_score()    # combined compute score
best    = mesh.auto_device()    # most powerful alive device
```

### Job management

```python
job_id = mesh.submit(name="preprocess", script="process.py",
                     payloads=[{"file": "data.csv"}])
status = mesh.status(job_id)
df     = mesh.results_to_dataframe(results)   # requires pandas
```

### From Python, non-blocking

```python
GPUMesh.start_coordinator(port=8000, token=TOKEN)   # binds 127.0.0.1; pass host="0.0.0.0" to open it up
GPUMesh.add_worker("http://coordinator:8000", token=TOKEN)
```

---

## `@accelerate` patterns

```python
from gpumesh import GPUMesh, accelerate

mesh = GPUMesh("http://coordinator:8000", token=TOKEN)

# Basic
@accelerate(mesh)
def preprocess(chunk_id, data_path):
    import pandas as pd
    df = pd.read_parquet(data_path)
    return {"chunk": chunk_id, "rows": len(df)}

# Hardware selection — only run on an A100
@accelerate(mesh, gpu="A100")
def train(model):
    return model.cuda().forward(x)

# Resource specs
@accelerate(mesh, cores=8, memory="16GB", timeout=300)
def heavy_computation(data):
    return processed

# Batch: spread across every device
results = train.map([{"lr": 0.01}, {"lr": 0.05}])

# Bind to a specific device
gpu_predict = predict.to("cuda")
result = gpu_predict(x)

# Global install — @accelerate with no arguments
accelerate.install(mesh)

@accelerate
def train(lr, epochs):
    return {"accuracy": 0.95}
```

### Smart routing

| Scenario | What happens |
|----------|--------------|
| `func(x)`, workers alive | Runs as a single task on one mesh worker |
| `func(x)`, no workers | Runs on the best LOCAL device (CPU/GPU) |
| `func.map([...])` | Spreads across ALL mesh devices |
| Mesh unreachable | Falls back to LOCAL execution silently |
| `GPUMESH_LOCAL=1` | Forces local-only (no mesh) |
| `GPUMESH_VERBOSE=1` | Prints which device handled each task |

Either path returns the identical value, so switching between them never changes your results. Note that `gpumesh serve` joins your own machine to the pool by default, so a single call is dispatched through the mesh even when you are the only participant — pass `--no-self-worker` if you want it to stay purely local.

---

## Docker

A prebuilt image is available on Docker Hub ([samurai007ak/gpumesh](https://hub.docker.com/r/samurai007ak/gpumesh)).

First, a token:

```bash
export GPUMESH_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
```

There are **two** host decisions in a container, and they are not the same
one:

1. **What the coordinator binds to inside the container.** It must be
   `0.0.0.0`, because anything reaching it arrives from outside the container
   — a loopback bind inside a container answers nothing, including the port
   Docker published.
2. **What Docker publishes on the machine running Docker.** This is the one
   that decides who can reach you. `-p 8732:8732` binds `0.0.0.0` on the host,
   which puts a coordinator on every network the machine is attached to.
   Prefix the host side with `127.0.0.1` unless you mean otherwise.

```bash
# Coordinator — reachable from this machine only
docker run -d --name gpumesh-coordinator \
  -p 127.0.0.1:8732:8732 -p 127.0.0.1:48900:48900/udp \
  -e GPUMESH_TOKEN \
  samurai007ak/gpumesh:3.0.0 \
  serve --host 0.0.0.0 --port 8732

# Worker
docker run -d --name gpumesh-worker \
  -e GPUMESH_TOKEN \
  samurai007ak/gpumesh:3.0.0 \
  join http://coordinator-ip:8732
```

Drop the `127.0.0.1:` prefix — or better, name a specific interface, e.g.
`-p 192.0.2.10:8732:8732` — only when you deliberately want other machines to
join. That is the same decision `gpumesh serve --host 0.0.0.0` asks you to
make on the host, and it deserves the same pause.

Both `serve` and `join` read `GPUMESH_TOKEN` from the environment, so
`-e GPUMESH_TOKEN` is enough and the token never appears in `docker ps`
output. `join` takes the coordinator URL as a positional argument, so there is
no `GPUMESH_URL` to set here — that variable is only read by the job and
monitoring commands.

Or use the included
[`docker-compose.yaml`](https://github.com/K4-LABS/gpumesh/blob/master/docker-compose.yaml) for a
coordinator + N workers, with a healthcheck the workers wait on before
starting. It pulls the published image, so the file works on its own:

```bash
GPUMESH_TOKEN=$TOKEN docker compose up -d                      # localhost only
WORKER_REPLICAS=4 GPUMESH_TOKEN=$TOKEN docker compose up -d    # scale workers
GPUMESH_BIND=0.0.0.0 GPUMESH_TOKEN=$TOKEN docker compose up -d # reachable from the LAN
```

`GPUMESH_TOKEN` is required — compose stops with a clear message if it is
unset, rather than starting a coordinator with a random token that no worker
could authenticate against. `GPUMESH_BIND` defaults to `127.0.0.1` and is the
host-side publish address from decision (2) above.

**Ports:** `8732` (TCP API) and `48900/udp` (LAN discovery). Persistent state
(the SQLite database and `~/.gpumesh/config.json`) lives under `/data`; the
compose file mounts a named volume there.

> The container listens on **8732**, while `gpumesh serve` on the host defaults to **8000**. That is deliberate — the image pins an explicit port so published `docker run` and compose recipes stay stable. Both are just defaults: pass `--port` to use whatever you like, and make sure workers point at the same number the coordinator is listening on.

---

## Network options

| Method | Setup | Best for | Encrypted |
|--------|-------|----------|-----------|
| **LAN** | None | Same Wi-Fi, fastest | No |
| **Tailscale** | Install Tailscale | Remote teams | Yes |
| **ngrok** | `pip install gpumesh[tunnel]` | Public access, demos | Yes |

- **LAN:** `gpumesh serve --host 0.0.0.0` + `gpumesh join http://192.0.2.10:8000 --token $TOKEN`. UDP broadcast helps workers *find* a coordinator, but joining still needs the token and a coordinator that is actually bound beyond loopback.
- **Tailscale:** `gpumesh serve --host 0.0.0.0 --port 8000 --tailscale`, then join via the Tailscale IP. This is the option to reach for if the mesh crosses any network you do not control — the tunnel, not the token, becomes the boundary.
- **ngrok:** `gpumesh serve --host 0.0.0.0 --port 8000 --public` prints a public `https://...` URL that workers anywhere can join. "Anywhere" includes people you did not invite; the token is the only thing keeping them out.

The default — no flag at all — is **loopback only**. Every row above is an
opt-in to a wider audience.

---

## Architecture

Jobs are stored in SQLite; workers pull tasks over HTTP with a lease (a crashed
worker's task is automatically re-queued), run each task in an isolated
subprocess, and post results back. Workers are scored by a benchmark and the
scheduler assigns heavier tasks to stronger workers. Diagram and job flow:
[`docs/architecture.md`](https://github.com/K4-LABS/gpumesh/blob/master/docs/architecture.md).

---

## Security

| Feature | Status |
|---------|--------|
| Loopback bind by default | `gpumesh serve` listens on `127.0.0.1`; exposure needs `--host 0.0.0.0` and prints a banner |
| Token authentication | All API requests, including reads |
| Timing-safe comparison | HMAC `compare_digest` |
| Rate limiting | 5 failures -> 15 min IP lockout (loopback exempt — see below) |
| Process isolation | Tasks in subprocesses |
| File permissions | 0o600 on the saved config (`~/.gpumesh/config.json`) |
| Token hashing | SHA-256, in memory only — the token is never written to the database |

Read that table as "what raises the cost of an attack", not as "what makes
this safe". None of it is a sandbox.

> **The token is a licence to execute code, not a password guarding data.**
> Anyone holding your URL and token can run arbitrary Python on every machine
> in your mesh, as the user who started each worker. And it runs both ways: a
> worker's result is deserialized on the submitting machine, so a hostile
> worker executes code on you. gpumesh is built for trusted networks — home
> labs, lab benches, your own machines, a team you know.
>
> Use a real token: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
> The coordinator hashes it with a single SHA-256 round and a deterministic
> salt, which is fine for 250 bits of entropy and useless for a token you
> chose by hand. Entropy is the whole defence.
>
> Run the coordinator with `--safe-mode` to refuse function distribution and
> accept submitted scripts only. That closes the pickle path; it does not stop
> a submitted script from doing anything a script can do.
>
> Rate limiting exempts loopback deliberately. Anyone who can open a socket
> from `127.0.0.1` can read the token out of the process and the config file
> rather than guess it, so a lockout there costs an attacker nothing and costs
> you your own mesh.
>
> Traffic is **not encrypted** on a plain LAN. Use `--tailscale` or `--public`
> (ngrok) when the mesh crosses a network you do not control.

Full detail: **[SECURITY.md](https://github.com/K4-LABS/gpumesh/blob/master/SECURITY.md)**
(what a token grants, how to report a vulnerability) and
**[THREAT_MODEL.md](https://github.com/K4-LABS/gpumesh/blob/master/THREAT_MODEL.md)**
(the same flow traced through the code, file and line).

---

## Benchmark scoring

Each worker runs a benchmark on join and gets a score of
`gflops * 0.7 + bandwidth_gbps * 0.3`. It is a **relative, unbounded** number —
there is no normalisation and no ceiling, so it only means anything next to the
other workers in your pool. The scheduler uses it to rank workers, nothing else.

Rough magnitudes, to read the numbers `gpumesh workers` prints:

| Typical hardware | Score, roughly |
|------------------|----------------|
| RTX 4090, A100 | ~100+ |
| RTX 3080, 3090 | ~50–100 |
| RTX 3060, T4 | ~10–50 |
| CPU only | under 1 |

Those are illustrative, not a scale — a laptop CPU commonly lands around `0.24`,
and a faster GPU than anything listed here would simply score higher.

---

## Troubleshooting

The usual suspects — bind vs firewall, mismatched tokens, cross-version tasks,
missing torch on a worker — in one table, plus how to read `gpumesh doctor`:
[`docs/troubleshooting.md`](https://github.com/K4-LABS/gpumesh/blob/master/docs/troubleshooting.md).

Windows-specific setup — why Administrator matters for firewall rules, the
manual `netsh` command, `python -m gpumesh` when the script is not on PATH,
and how to tell a firewall block (10060) from a coordinator that is not
running (10061): [`docs/windows.md`](docs/windows.md).

---

## Development

```bash
git clone https://github.com/K4-LABS/gpumesh.git
cd gpumesh
pip install -e ".[dev]"
pytest                 # on Windows a few skip and one xpasses
python -m build        # build wheel + sdist
```

CI runs the suite on every push and pull request, plus a weekly scheduled run —
that last one exists because the regressions this project actually hits come
from dependencies changing under it, not from commits here. The matrix is
Linux on every Python gpumesh claims to support, with Windows and macOS spot
checks; the authoritative list is
[`.github/workflows/tests.yml`](https://github.com/K4-LABS/gpumesh/blob/master/.github/workflows/tests.yml),
and the [tests badge](https://github.com/K4-LABS/gpumesh/actions/workflows/tests.yml)
at the top of this page is that matrix's live result, so neither can drift the
way a hand-copied list does.

---

## Contributing

Issues and pull requests are both welcome, and small ones are welcome too — a
typo fix counts.

- [**CONTRIBUTING.md**](https://github.com/K4-LABS/gpumesh/blob/master/CONTRIBUTING.md)
  — dev setup, how the pieces fit together, and what makes a change easy to review
- [**Good first issues**](https://github.com/K4-LABS/gpumesh/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22)
  — scoped starting points that name the file to change
- [**Issue tracker**](https://github.com/K4-LABS/gpumesh/issues) — bugs, and
  questions about whether something is worth doing before you write the code

---

## Documentation

| Page | What it covers |
|------|----------------|
| [`docs/protocol.md`](https://github.com/K4-LABS/gpumesh/blob/master/docs/protocol.md) | The HTTP API, every endpoint, the task payload, the function and result envelopes, cross-Python-version behaviour |
| [`docs/stability.md`](https://github.com/K4-LABS/gpumesh/blob/master/docs/stability.md) | What counts as gpumesh's public API, what a version bump promises, the wire-protocol compatibility window, and the deprecation policy |
| [`docs/why-not-ray-or-dask.md`](https://github.com/K4-LABS/gpumesh/blob/master/docs/why-not-ray-or-dask.md) | An honest comparison, including when Ray or Dask is the right answer |
| [`examples/`](https://github.com/K4-LABS/gpumesh/tree/master/examples) | Runnable scripts for the first hour: hello-mesh, a second machine, a `.map()` sweep, non-JSON return values, a worker that disappears |
| [`docs/windows.md`](https://github.com/K4-LABS/gpumesh/blob/master/docs/windows.md) | Windows setup: firewall rules, `python -m gpumesh`, and reading 10060 vs 10061 connection errors |
| [SECURITY.md](https://github.com/K4-LABS/gpumesh/blob/master/SECURITY.md) · [THREAT_MODEL.md](https://github.com/K4-LABS/gpumesh/blob/master/THREAT_MODEL.md) | What a token grants, and the same flow traced through the code |

---

## Limitations

- Python only — tasks must be Python functions or scripts
- Arguments and return values must be picklable. Anything tied to a live process — open files, sockets, locks, database handles, CUDA handles — cannot cross machines. Return plain data (numbers, arrays, tensors, DataFrames) instead
- Every worker needs the imports your function uses already installed; gpumesh ships your code, not your environment
- Workers should run the same Python minor version as the submitter — cloudpickle falls back to source when they differ, which does not cover every function
- No GPU memory sharing — each task gets its own process
- No model sharding — each task runs on one machine at a time
- No task graphs — every task is independent; if task B needs task A's output, that plumbing is yours
- No distributed data structures — no shared arrays, dataframes or object store. If your data does not fit on one machine, gpumesh will not help
- Built and tested for meshes of roughly 2–20 machines. Nobody has run it at hundreds of workers
- Single coordinator — single point of failure (use Tailscale for reliability)
- No built-in encryption — use Tailscale for encrypted tunnels
- Trusted networks only — workers run whatever code the coordinator sends, and submitters deserialize whatever workers return

If several of those are dealbreakers, read
[**Why not Ray or Dask?**](https://github.com/K4-LABS/gpumesh/blob/master/docs/why-not-ray-or-dask.md) — it says plainly
which tool to use instead.

---

## Prior art and credits

gpumesh borrows shamelessly, at the level of **API shape and scheduling
strategy**. No code was copied from any of these projects; what was taken is
the idea of what a good interface looks like, and each one is credited in the
source where its influence lands.

[**exo**](https://github.com/exo-explore/exo) (Apache-2.0) is the closest
relative in spirit — running AI workloads on the consumer hardware you
already own. Its runner-supervisor pattern is why every gpumesh task runs in
its own subprocess, and its crash diagnostics and topology-change events are
reflected in `worker.py` and `db.py`.
[**hivemind**](https://github.com/learning-at-home/hivemind) (MIT) supplied
the TTL-based worker expiry that prunes a machine that has stopped
heartbeating. [**Petals**](https://github.com/bigscience-workshop/petals)
(MIT) supplied straggler deprioritisation — a worker slower than twice its
peers' median gets lighter tasks rather than holding up the batch.
[**cudf.pandas**](https://github.com/rapidsai/cudf) (Apache-2.0) is where
`accelerate.install(mesh)` comes from: an import hook that makes existing
code use the accelerator without editing it.
[**Hugging Face Accelerate**](https://github.com/huggingface/accelerate)
(Apache-2.0) is the source of the `.to(device)` placement idea.
[**clustrix**](https://github.com/ContextLab/clustrix) (MIT) is where
`@accelerate(cores=8, memory="16GB")` gets its shape — declaring resource
requirements on the decorator rather than in a separate config.
[**burla**](https://github.com/Burla-Cloud/burla) contributed the
`remote_parallel_map` batch pattern behind `.map()` and the `func_gpu="A100"`
hardware-selection idea; note that burla is licensed **FSL-1.1-Apache-2.0**,
which is *source-available, not OSI open source*, and reads back to Apache-2.0
after two years. Two smaller influences round it out: **distry**, for the
plain single-function decorator, and **ezpz**, for `setup_torch()`-style
automatic backend detection.

If you maintain one of these and think the credit is wrong, or the influence
is closer to copying than we believe,
[open an issue](https://github.com/K4-LABS/gpumesh/issues) and it will be
corrected.

---

## Maintainers

<table>
  <tr>
    <td align="center">
      <a href="https://github.com/Samurai007AK">
        <img src="https://avatars.githubusercontent.com/Samurai007AK?s=150" width="120" alt="Samurai007AK"/><br/>
        <strong>Samurai007AK</strong>
      </a>
      <p>
        <a href="https://github.com/Samurai007AK"><img src="https://img.shields.io/badge/GitHub-100000?style=flat&logo=github&logoColor=white" alt="GitHub"/></a>
        <a href="mailto:arijitkonar16@gmail.com"><img src="https://img.shields.io/badge/Email-D14836?style=flat&logo=gmail&logoColor=white" alt="Email"/></a>
      </p>
    </td>
    <td align="center">
      <a href="https://github.com/jinia-konar">
        <img src="https://avatars.githubusercontent.com/jinia-konar?s=150" width="120" alt="jinia-konar"/><br/>
        <strong>jinia-konar</strong>
      </a>
      <p>
        <a href="https://github.com/jinia-konar"><img src="https://img.shields.io/badge/GitHub-100000?style=flat&logo=github&logoColor=white" alt="GitHub"/></a>
      </p>
    </td>
  </tr>
</table>

---

## License

GNU AGPL-3.0-or-later. See
[LICENSE](https://github.com/K4-LABS/gpumesh/blob/master/LICENSE) for details.

```
gpumesh — a distributed compute mesh
Copyright (C) 2026 K4-LABS

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published
by the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
```

That notice lives here rather than at the top of `LICENSE` on purpose. GitHub
detects a project's license by matching the whole `LICENSE` file against the
known texts, and a prepended notice pushes the match below its threshold — the
repository then reports its license as "Other", which is exactly the wrong
signal for a copyleft project. `LICENSE` is now the unmodified AGPL-3.0 text,
and the "or (at your option) any later version" grant above is what the
`AGPL-3.0-or-later` in `pyproject.toml` reports.

gpumesh was MIT-licensed through 2.0.0 and moves to the AGPL in the next
release. That change is not retroactive: every release published up to and
including 2.0.0 remains available under the MIT terms it was published
under. If you run a modified gpumesh as a network service, AGPL section 13
requires you to offer your modified source to its users.

---

[GitHub](https://github.com/K4-LABS/gpumesh) · [Issues](https://github.com/K4-LABS/gpumesh/issues) · [PyPI](https://pypi.org/project/gpumesh/)
