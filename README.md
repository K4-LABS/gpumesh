# gpumesh

> Borrow your friends' GPUs. A distributed compute mesh that lets you share GPU power across machines on your network — with one decorator, one CLI command, or a Python API.

[![PyPI version](https://img.shields.io/pypi/v/gpumesh.svg)](https://pypi.org/project/gpumesh/)
[![Python](https://img.shields.io/pypi/pyversions/gpumesh.svg)](https://pypi.org/project/gpumesh/)
[![License](https://img.shields.io/pypi/l/gpumesh.svg)](https://github.com/Samurai007AK/gpumesh/blob/main/LICENSE)
[![Tests](https://github.com/Samurai007AK/gpumesh/actions/workflows/tests.yml/badge.svg)](https://github.com/Samurai007AK/gpumesh/actions/workflows/tests.yml)
[![Status](https://img.shields.io/badge/status-beta-blue)](https://github.com/Samurai007AK/gpumesh)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/r/samurai007ak/gpumesh)

---

```
  ╔═══════════════════════════════════════════════════════════╗
  ║              gpumesh - GPU Mesh Network                   ║
  ║        "like Bluetooth, but for your GPUs"                ║
  ╚═══════════════════════════════════════════════════════════╝

         ┌─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─┐
         │           NETWORK TRAFFIC FLOW            │
         │                                           │
         │   ┌──────────┐     ┌──────────┐           │
         │   │ RTX 4090 │◄───►│ RTX 3080 │           │
         │   │  Server  │     │  Laptop  │           │
         │   │120.5 G/s │     │ 85.2 G/s │           │
         │   └────┬─────┘     └────┬─────┘           │
         │        │                │                 │
         │   ┌────▼────────────────▼────┐            │
         │   │       T4 (12.0)        │              │
         │   │     running tasks      │              │
         │   └──────────────────────────┘            │
         │                                           │
         │   >>> results collected automatically     │
         └─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─┘
```

---

## What is gpumesh?

gpumesh turns multiple machines into a **single, unified compute pool**. Start a **coordinator** on one machine, join **workers** from other machines (laptops, desktops, servers — anything with Python), and run code across all of them as if they were one device.

**Use cases:**

- Hyperparameter search across multiple GPUs
- Data preprocessing sharded across machines
- Model training on a pool of consumer GPUs
- Any embarrassingly parallel workload

### Key features

| Feature | What it does |
|---------|--------------|
| **`@mesh` / `@accelerate` decorators** | Mark a function and it runs on the pool — no job system, no ceremony |
| **`.map()`** | Spread one call across *every* connected machine at once |
| **Smart routing** | Calls go to a mesh worker when one is alive, and to your own machine when none is |
| **Graceful fallback** | Mesh unreachable? Your code runs locally and returns the same value — it never breaks |
| **Any return value** | numpy arrays, torch tensors, DataFrames — you get back exactly what your function returned |
| **Fault tolerance** | Workers survive sleep, WiFi drops, and coordinator restarts; dead workers' tasks are re-queued |
| **Benchmark scoring** | Every worker gets a 0–100 compute score; the scheduler routes work to the strongest hardware |
| **Memory-aware scheduling** | VRAM is tracked; tasks with memory hints go to workers with enough free memory |
| **Live radar** | `gpumesh radar` discovers nearby devices on your network — no config needed |
| **Isolated execution** | Every task runs in its own subprocess; a crashing task can't take down a worker |
| **Token security** | All API calls require a token; rate-limited, timing-safe verification |
| **Jupyter support** | `%%mesh` cell magic wraps every function in a cell automatically |

---

## Quick demo

```python
from gpumesh import GPUMesh, accelerate

mesh = GPUMesh("http://coordinator:8000", token="mysecret")

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
docker pull samurai007ak/gpumesh:latest
```

**Requires:** Python 3.9+, cloudpickle (auto-installed). PyTorch is optional (needed for GPU detection).

Verify the install:

```console
$ gpumesh --version
gpumesh 1.3.0 (3.11.9, Windows)
```

---

## Quickstart

From nothing to a task running across two machines. The terminal output below
is real, copied from an actual run.

### 1. Start a coordinator (machine A)

```bash
pip install gpumesh
gpumesh serve --port 8000 --token mysecret
```

```console
[OK] Coordinator listening on 0.0.0.0:8000
   Token: mysecret

   Join from THIS machine:   gpumesh join http://127.0.0.1:8000 --token mysecret
   Join from ANOTHER machine: gpumesh join http://10.126.13.54:8000 --token mysecret
   (127.0.0.1 always works locally; the LAN IP is for other machines)
[OK] Self-check: server is up (tested on 127.0.0.1 only)
[OK] Self-worker started - this machine's CPU/GPU is part of the pool
[worker] joined mesh as bdffd272eda3
```

**Copy the `Join from ANOTHER machine` line** — that is the exact command machine B needs.

> Your own machine joins the pool automatically, so the mesh works even before
> anyone else connects.
>
> Windows: run as Administrator so the firewall rule is added for you. Without
> it, workers on other machines may be blocked.

Prefer a guided wizard? Run `gpumesh setup`.

### 2. Join a worker (machine B)

```bash
pip install gpumesh
gpumesh join http://10.126.13.54:8000 --token mysecret
```

```console
[worker] device=CPU Intel64 Family 6 Model 154 Stepping 3 score=0.242 GFLOP/s
[worker] joined mesh as 7c1e04ab93f2
```

Confirm from either machine:

```console
$ gpumesh workers

  WORKERS
  ------------------------------------------------------
  CPU  bdffd272    machine-a         score=0.242     [alive]
  CPU  7c1e04ab    machine-b         score=0.310     [alive]
```

> **Workers never die.** They survive laptop sleep, WiFi drops and coordinator
> restarts, and reconnect on their own.
>
> If the worker reports a timeout instead, it could not reach that address —
> see [Troubleshooting](#troubleshooting).

### 3. Run a distributed task

Save as `demo.py` on either machine:

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
[gpumesh] connected to coordinator at http://10.126.13.54:8000
{'lr': 0.01, 'accuracy': 0.91}
[{'lr': 0.01, 'accuracy': 0.91}, {'lr': 0.05, 'accuracy': 0.95}]
```

That is the whole workflow. `train()` ran on a mesh worker; `train.map()` was
split across every machine in the pool.

Prefer the CLI? Submit a script instead. (The `examples/` files ship with the
repository, not the pip package — `git clone` first, or point the command at
any script of your own that reads a JSON payload on stdin.)

```console
$ gpumesh submit examples/grid_search.py --payloads examples/payloads.json --wait

Job: examples/grid_search.py (6b9415d1275e)

  #################### 100%  6/6 done  (12s)

  Status: finished
  Counts: {'done': 6}

  [OK] task 144c05b7a271 [done]  cost=1.0  worker=bdffd272eda3
    result: {"lr": 0.01, "epochs": 100, "val_accuracy": 0.948, ...}
```

### Code normally

This is the entire point. Once a worker is connected, every machine sees the same pool. Write normal Python and mark the heavy functions:

```python
from gpumesh import mesh   # auto-connects from saved config

@mesh
def train(lr, epochs):
    return {"accuracy": 0.95}

# Single call — runs on a mesh worker (your own machine if nothing else joined)
result = train(lr=0.01, epochs=100)

# .map() — spreads across EVERY connected laptop + your machine
results = train.map([{"lr": 0.01}, {"lr": 0.05}, {"lr": 0.1}])
```

Works in VS Code, Jupyter, PyCharm, or a plain terminal. No job submission, no CLI commands, no ceremony.

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

### Server & connection

| Command | Description |
|---------|-------------|
| `gpumesh setup` | Interactive setup wizard (coordinator or worker) |
| `gpumesh serve` | Start the coordinator (`--port`, `--token`, `--db`, `--host-ip`, `--public`, `--tailscale`, `--no-discovery`, `--safe-mode`, `--no-self-worker`) |
| `gpumesh join URL` | Join a mesh as a worker (`--token`, `--timeout`, `--safe-mode`) |
| `gpumesh quickjoin [URL]` | One-click: install, detect GPU, join (`--token`, `--tailscale`, `--port`, `--timeout`, `--safe-mode`) |
| `gpumesh worker` | Broadcast presence and wait to be claimed (`--token`, `--claim-port`, `--timeout`, `--safe-mode`) |
| `gpumesh radar` | Scan for nearby devices (live radar; `--mode coordinator|worker`) |
| `gpumesh show-connection` | Show the saved URL + token |
| `gpumesh disconnect` | Clear the saved connection |

### Jobs

| Command | Description |
|---------|-------------|
| `gpumesh submit SCRIPT --payloads FILE` | Submit a script job (`--name`, `--wait` blocks until done, `--wait-timeout`) |
| `gpumesh status JOB_ID` | Show job progress and results |
| `gpumesh cancel JOB_ID` | Cancel a running job |
| `gpumesh retry JOB_ID` | Re-queue failed/timed-out tasks |
| `gpumesh kill [--force]` | Kill all tasks (graceful or immediate) |

### Monitoring

| Command | Description |
|---------|-------------|
| `gpumesh workers` | List connected workers and their status |
| `gpumesh devices` | Show all GPUs/CPUs as one unified pool |

Job and monitoring commands (`submit`, `status`, `cancel`, `retry`, `workers`, `devices`, `kill`) accept `--url URL --token TOKEN`. If you omit them, gpumesh falls back to the `GPUMESH_URL` / `GPUMESH_TOKEN` environment variables, then to the connection saved by `join`/`serve`.

### Environment variables

| Variable | Where it applies | Effect |
|----------|------------------|--------|
| `GPUMESH_URL` | Job/monitoring commands | Coordinator URL when `--url` is omitted |
| `GPUMESH_TOKEN` | All commands | Auth token; also read by `serve` and `join` when `--token` is omitted |
| `GPUMESH_HOST_IP` | `serve` | Pin the address advertised to workers (same as `--host-ip`) |
| `GPUMESH_LOCAL=1` | `@mesh` / `@accelerate` | Force local execution, never touch the mesh |
| `GPUMESH_VERBOSE=1` | `@mesh` / `@accelerate` | Print which device handled each task |
| `GPUMESH_COLOR` | All output | `1` forces colour, `0` disables it, `auto` (default) checks whether stdout is a TTY |

---

## Python API

```python
from gpumesh import GPUMesh

mesh = GPUMesh("http://coordinator:8000", token="mysecret")
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
GPUMesh.start_coordinator(port=8000, token="mysecret")
GPUMesh.add_worker("http://coordinator:8000", token="mysecret")
```

---

## `@accelerate` patterns

```python
from gpumesh import GPUMesh, accelerate

mesh = GPUMesh("http://coordinator:8000", token="mysecret")

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

A prebuilt image is available on Docker Hub ([samurai007ak/gpumesh](https://hub.docker.com/r/samurai007ak/gpumesh)):

```bash
# Coordinator
docker run -d --name gpumesh-coordinator \
  -p 8732:8732 -p 48900:48900/udp \
  -e GPUMESH_TOKEN=mysecret \
  samurai007ak/gpumesh:latest \
  serve --port 8732 --token mysecret

# Worker
docker run -d --name gpumesh-worker \
  -e GPUMESH_URL=http://coordinator-ip:8732 \
  -e GPUMESH_TOKEN=mysecret \
  samurai007ak/gpumesh:latest \
  join http://coordinator-ip:8732 --token mysecret
```

Or use the included [`docker-compose.yaml`](docker-compose.yaml) for a coordinator + N workers with healthchecks. It pulls the published image, so the file works on its own:

```bash
GPUMESH_TOKEN=mysecret docker compose up -d
WORKER_REPLICAS=4 GPUMESH_TOKEN=mysecret docker compose up -d   # scale workers
```

`GPUMESH_TOKEN` is required — compose stops with a clear message if it is unset, rather than starting a coordinator with a random token that no worker could authenticate against.

**Ports:** `8732` (TCP API) and `48900/udp` (LAN discovery).

> The container listens on **8732**, while `gpumesh serve` on the host defaults to **8000**. That is deliberate — the image pins an explicit port so published `docker run` and compose recipes stay stable. Both are just defaults: pass `--port` to use whatever you like, and make sure workers point at the same number the coordinator is listening on.

---

## Network options

| Method | Setup | Best for | Encrypted |
|--------|-------|----------|-----------|
| **LAN** | None | Same Wi-Fi, fastest | No |
| **Tailscale** | Install Tailscale | Remote teams | Yes |
| **ngrok** | `pip install gpumesh[tunnel]` | Public access, demos | Yes |

- **LAN (default):** workers discover the coordinator automatically via UDP broadcast. `gpumesh serve` + `gpumesh join http://192.168.1.10:8000 --token mysecret`.
- **Tailscale:** `gpumesh serve --port 8000 --tailscale`, then join via the Tailscale IP.
- **ngrok:** `gpumesh serve --port 8000 --public` prints a public `https://...` URL that workers anywhere can join.

---

## Architecture

```
                         COORDINATOR
        ┌─────────────────────────────────────────────────┐
        │                                                 │
        │  ┌──────────┐  ┌──────────┐  ┌──────────────┐  │
        │  │ Job Queue │  │ Task DB  │  │ Worker       │  │
        │  │ (memory)  │  │ (SQLite) │  │ Registry     │  │
        │  └────┬─────┘  └──────────┘  └──────┬───────┘  │
        │       │                              │          │
        │       └──────────┬───────────────────┘          │
        │                  │                              │
        │         HTTP API :8000                          │
        └──────────────────┼──────────────────────────────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
        ┌─────▼────┐ ┌────▼────┐ ┌────▼────┐
        │ Worker 1 │ │Worker 2 │ │Worker 3 │
        │ RTX 4090 │ │RTX 3080 │ │   T4    │
        │Score: 120│ │Score: 85│ │Score: 12│
        └──────────┘ └─────────┘ └─────────┘
              │            │            │
              └────────────┼────────────┘
                           │
                    ┌──────▼──────┐
                    │   Results   │
                    │  Collected  │
                    └─────────────┘

  JOB FLOW:  Submit ─► Queue ─► Claim ─► Execute ─► Report ─► Collect
```

**How it works:** jobs are stored in SQLite, workers pull tasks over HTTP with a lease (so a crashed worker's task is automatically re-queued), run each task in an isolated subprocess, and post results back. Workers are scored by a benchmark and the scheduler assigns heavier tasks to stronger workers.

---

## Security

| Feature | Status |
|---------|--------|
| Token authentication | All API requests |
| Timing-safe comparison | HMAC `compare_digest` |
| Rate limiting | 5 failures -> 15 min IP lockout |
| Process isolation | Tasks in subprocesses |
| File permissions | 0o600 on the saved config (`~/.gpumesh/config.json`) |
| Token hashing | SHA-256, in memory only — the token is never written to the database |

> **Workers execute code sent by the coordinator.** Anyone holding your URL and token can run arbitrary code on every machine in your mesh. Only share them with people you trust, and treat the token like a password. gpumesh is built for trusted networks — home labs, lab benches, your own machines, a team you know. It is not a sandbox and is not designed to run untrusted code.
>
> Run the coordinator with `--safe-mode` to refuse function distribution and accept submitted scripts only.
>
> Traffic is **not encrypted** on a plain LAN. Use `--tailscale` or `--public` (ngrok) when the mesh crosses a network you do not control.

---

## Benchmark scoring

Each worker runs a benchmark on join and gets a 0–100 score:

| Score | Typical hardware | Use case |
|-------|------------------|----------|
| 80–100 | RTX 4090, A100 | Heavy training, large models |
| 50–80 | RTX 3080, 3090 | Medium training, inference |
| 20–50 | RTX 3060, T4 | Light tasks, preprocessing |
| 0–20 | CPU only | Very light tasks |

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `command not found: gpumesh` | Use `python -m gpumesh` or check your PATH |
| `401 bad token` | Use the same token on coordinator and worker |
| Coordinator unreachable | Check firewall; is the coordinator running? |
| Task timed out | Increase `--timeout` or split tasks |
| Windows connection error | Run `gpumesh serve` as Administrator for firewall rules |
| Worker not showing up | Both on the same network? Try `gpumesh radar` |
| `ModuleNotFoundError: torch` | `pip install gpumesh[gpu]` |
| UDP broadcast not working | Use `gpumesh join URL` directly |
| `ModuleNotFoundError` inside a task | Install that package on the **worker** too — gpumesh ships your code, not your environment |
| `cannot send result of type ...` | Return plain data. Open files, sockets, locks and live GPU handles can't cross machines |
| Results differ from a local run | They shouldn't — file an issue. Confirm with `GPUMESH_LOCAL=1 python your_script.py` |

**Verbose logging:** `GPUMESH_VERBOSE=1 gpumesh serve` — **force local-only:** `GPUMESH_LOCAL=1 python my_script.py`

---

## Development

```bash
git clone https://github.com/Samurai007AK/gpumesh.git
cd gpumesh
pip install -e ".[dev]"
pytest                 # 649 tests on Linux (Windows: 645 + 3 skips + 1 xpass)
python -m build        # build wheel + sdist
```

Contributions are welcome — issues and pull requests both. See
[CONTRIBUTING.md](CONTRIBUTING.md) for setup, how the pieces fit together,
and what makes a change easy to review.

---

## Limitations

- Python only — tasks must be Python functions or scripts
- Arguments and return values must be picklable. Anything tied to a live process — open files, sockets, locks, database handles, CUDA handles — cannot cross machines. Return plain data (numbers, arrays, tensors, DataFrames) instead
- Every worker needs the imports your function uses already installed; gpumesh ships your code, not your environment
- Workers should run the same Python minor version as the submitter — cloudpickle falls back to source when they differ, which does not cover every function
- No GPU memory sharing — each task gets its own process
- No model sharding — each task runs on one machine at a time
- Single coordinator — single point of failure (use Tailscale for reliability)
- No built-in encryption — use Tailscale for encrypted tunnels
- Trusted networks only — workers run whatever code the coordinator sends

---

## License

MIT License. See [LICENSE](LICENSE) for details.

---

[GitHub](https://github.com/Samurai007AK/gpumesh) · [Issues](https://github.com/Samurai007AK/gpumesh/issues) · [PyPI](https://pypi.org/project/gpumesh/)
