# gpumesh

> Borrow your friends' GPUs. A distributed compute mesh that turns several machines into one pool — driven by a decorator, a CLI command, or a Python API.

[![PyPI version](https://img.shields.io/pypi/v/gpumesh.svg)](https://pypi.org/project/gpumesh/)
[![Python](https://img.shields.io/pypi/pyversions/gpumesh.svg)](https://pypi.org/project/gpumesh/)
[![License](https://img.shields.io/pypi/l/gpumesh.svg)](https://github.com/Samurai007AK/gpumesh/blob/main/LICENSE)
[![Tests](https://img.shields.io/badge/tests-624%20passed-brightgreen)](https://github.com/Samurai007AK/gpumesh)
[![Status](https://img.shields.io/badge/status-beta-blue)](https://github.com/Samurai007AK/gpumesh)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/r/samurai007ak/gpumesh)

```
   ╔════════════════════════════════════════════════════════╗
   ║   gpumesh — one compute pool out of many machines      ║
   ║   "like Bluetooth, but for your GPUs"                  ║
   ╚════════════════════════════════════════════════════════╝

     your laptop            desktop              old server
     ┌────────────┐      ┌────────────┐      ┌────────────┐
     │  RTX 3080  │      │  RTX 4090  │      │  CPU only  │
     └─────┬──────┘      └─────┬──────┘      └─────┬──────┘
           │                   │                   │
           └───────────────────┼───────────────────┘
                               │
                      ┌────────┴────────┐
                      │   coordinator   │
                      │  gpumesh serve  │
                      └────────┬────────┘
                               │
                    @mesh def train(...)
                    results come back to you
```

---

## Contents

- [What is gpumesh?](#what-is-gpumesh)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Jupyter notebooks](#jupyter-notebooks)
- [CLI reference](#cli-reference)
- [Python API](#python-api)
- [`@accelerate` patterns](#accelerate-patterns)
- [Docker](#docker)
- [Network options](#network-options)
- [Architecture](#architecture)
- [Security](#security)
- [Benchmark scoring](#benchmark-scoring)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations)

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
| **Fault tolerance** | Workers survive sleep, Wi-Fi drops, and coordinator restarts; a dead worker's tasks are re-queued |
| **Benchmark scoring** | Every worker is benchmarked on join; the scheduler routes heavy tasks to the strongest hardware |
| **Memory-aware scheduling** | VRAM is tracked; tasks carrying a `gpu_memory_mb` hint go to workers with enough free memory |
| **Live radar** | `gpumesh radar` discovers nearby devices on your network — no config needed |
| **Isolated execution** | Every task runs in its own subprocess; a crashing task can't take down a worker |
| **Token security** | All API calls require a token; rate-limited, timing-safe verification |
| **Jupyter support** | `%%mesh` cell magic wraps every function in a cell automatically |

### Quick demo

```python
from gpumesh import GPUMesh, accelerate

mesh = GPUMesh("http://coordinator:8000", token="mysecret")

@accelerate(mesh)
def train(lr, epochs):
    return {"accuracy": 0.95}

result = train(lr=0.01, epochs=100)          # one mesh worker (or local if none)

results = train.map([                        # spread across all mesh devices
    {"lr": 0.01, "epochs": 100},
    {"lr": 0.05, "epochs": 200},
])
```

---

## Installation

```bash
pip install gpumesh
pip install "gpumesh[gpu]"       # GPU detection + CUDA benchmarks (torch)
pip install "gpumesh[tunnel]"    # ngrok for public URLs (pyngrok)
pip install "gpumesh[sysinfo]"   # richer system info (psutil)
pip install "gpumesh[notebook]"  # DataFrame support (pandas)
pip install "gpumesh[ui]"        # setup wizard (rich + questionary)
pip install "gpumesh[all]"       # everything above
```

Quote the extras — `zsh` treats bare `[...]` as a glob.

**Requires:** Python 3.9+ and `cloudpickle` (installed automatically). Everything else is optional. Without `torch`, a machine still joins the mesh, but it is detected as CPU-only and benchmarks with a slow pure-Python fallback, so it will be given only the lightest tasks.

---

## Quick start

### 1. Start a coordinator (one machine)

```bash
gpumesh serve --port 8000 --token mysecret
```

The command prints the URL and token that workers need.

> **Your own machine automatically joins the pool** — your CPU/GPU is used alongside any laptops that connect. Pass `--no-self-worker` to keep the coordinator purely a scheduler.
>
> **Windows:** run `gpumesh serve` from an Administrator terminal so the firewall rules are added automatically.
>
> **Multiple network adapters?** If workers cannot reach the address that is printed (common with VPNs, WSL, Docker, or Hyper-V adapters), pin the right one with `gpumesh serve --host-ip 192.168.1.10` or `GPUMESH_HOST_IP=192.168.1.10`.

Prefer a guided wizard? Run `gpumesh setup` (needs `pip install "gpumesh[ui]"`).

### 2. Join a worker (another machine)

```bash
gpumesh join http://192.168.1.10:8000 --token mysecret

# or, one command that installs deps, detects the GPU, then joins:
gpumesh quickjoin http://192.168.1.10:8000 --token mysecret
```

> **Workers survive outages.** Laptop sleep, Wi-Fi drops, and coordinator restarts are all recoverable — the worker reconnects on its own and re-registers when the coordinator comes back.

### 3. Code normally

This is the entire point. Once a worker is connected, every machine sees the same pool. Write normal Python and mark the heavy functions:

```python
from gpumesh import mesh   # auto-connects using the saved config

@mesh
def train(lr, epochs):
    return {"accuracy": 0.95}

# Single call — runs on a mesh worker (your own machine if nothing else joined)
result = train(lr=0.01, epochs=100)

# .map() — spreads across EVERY connected machine
results = train.map([{"lr": 0.01}, {"lr": 0.05}, {"lr": 0.1}])
```

Works in VS Code, Jupyter, PyCharm, or a plain terminal. No job submission, no CLI ceremony.

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

Run `gpumesh --help` or `gpumesh <command> --help` for the authoritative list. Global flags: `--version`, `-v/--verbose`, `--json-logs`.

### Server and connection

| Command | Description |
|---------|-------------|
| `gpumesh serve` | Start the coordinator |
| `gpumesh join URL` | Join a mesh as a worker |
| `gpumesh quickjoin [URL] --token T` | One command: install deps, detect GPU, join |
| `gpumesh worker --token T` | Broadcast presence and wait to be claimed by a coordinator |
| `gpumesh radar` | Scan for nearby devices (live display) |
| `gpumesh setup` | Interactive setup wizard (coordinator or worker) |
| `gpumesh show-connection` | Show the saved URL and token |
| `gpumesh disconnect` | Clear the saved connection |

| Command | Flags |
|---------|-------|
| `serve` | `--port` (8000), `--token` (random if omitted), `--db` (`gpumesh.db`), `--host-ip`, `--public`, `--tailscale`, `--no-discovery`, `--safe-mode`, `--no-self-worker`, `--color` |
| `join` | `--token`, `--timeout` (240s per task), `--safe-mode`, `--color` |
| `quickjoin` | `--token` (**required**), `--tailscale`, `--port` (8000), `--timeout` (240s), `--safe-mode` |
| `worker` | `--token` (**required**, min 8 chars), `--claim-port` (auto), `--timeout` (240s), `--safe-mode` |
| `radar` | `--mode coordinator\|worker` (default `coordinator`) |

### Jobs

| Command | Description |
|---------|-------------|
| `gpumesh submit SCRIPT --payloads FILE` | Submit a script job (`--name`, `--wait`, `--wait-timeout` 3600s; `0` waits forever) |
| `gpumesh status JOB_ID` | Show job progress and results |
| `gpumesh cancel JOB_ID` | Cancel a running job |
| `gpumesh retry JOB_ID` | Re-queue failed or timed-out tasks |
| `gpumesh kill [--force]` | Kill all tasks (graceful, or immediate with `--force`) |

### Monitoring

| Command | Description |
|---------|-------------|
| `gpumesh workers` | List connected workers and their status |
| `gpumesh devices` | Show all GPUs/CPUs as one unified pool |

Every job and monitoring command accepts `--url URL --token TOKEN`. If you omit them, gpumesh falls back to the connection saved by `join`/`serve` in `~/.gpumesh/config.json`, then to the `GPUMESH_URL` / `GPUMESH_TOKEN` environment variables.

### Environment variables

| Variable | Read by | Effect |
|----------|---------|--------|
| `GPUMESH_URL` | client commands | Coordinator URL when `--url` is omitted |
| `GPUMESH_TOKEN` | client commands, `join` | Auth token when `--token` is omitted. **Not read by `serve`** — pass `serve --token` explicitly |
| `GPUMESH_HOST_IP` | `serve` | Address the coordinator advertises to workers (same as `--host-ip`) |
| `GPUMESH_LOCAL=1` | `@mesh` / `@accelerate` | Force local execution, never touch the mesh |
| `GPUMESH_VERBOSE=1` | `@mesh` / `@accelerate` | Print which device handled each task |
| `GPUMESH_COLOR` | all output | `1` forces color, `0` disables it, `auto` (default) checks whether stdout is a TTY |

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
    name="sweep",        # optional label
    timeout=600.0,       # default 300.0 seconds for the whole batch
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
status = mesh.status(job_id)                  # alias: mesh.job_status(job_id)
df     = mesh.results_to_dataframe(results)   # requires pandas
```

### Start a coordinator or worker from Python (non-blocking)

```python
url = GPUMesh.start_coordinator(port=8000, token="mysecret")   # returns the shareable URL
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

# Bind to a specific local device
gpu_predict = predict.to("cuda")
result = gpu_predict(x)
```

Install a default mesh once and use bare `@accelerate` everywhere after that:

```python
from gpumesh import accelerate, accelerate_install

accelerate_install(mesh)

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

A prebuilt image is on Docker Hub: [samurai007ak/gpumesh](https://hub.docker.com/r/samurai007ak/gpumesh). Full Docker documentation lives in [DOCKER_HUB_README.md](https://github.com/Samurai007AK/gpumesh/blob/main/DOCKER_HUB_README.md).

> **The published image is CPU-only.** It is built on `python:3.11-slim` and installs gpumesh without the `gpu` extra, so there is no CUDA runtime and no `torch` inside. Containers join the mesh happily and run CPU tasks, but they are detected as CPU-only and score near zero. For GPU workers, run `gpumesh join` directly on the host, or build your own image `FROM nvidia/cuda:...` with `pip install "gpumesh[gpu]"`.

```bash
# Coordinator
docker run -d --name gpumesh-coordinator \
  -p 8732:8732 -p 48900:48900/udp \
  -e GPUMESH_HOST_IP=192.168.1.10 \
  samurai007ak/gpumesh:latest \
  serve --port 8732 --token mysecret

# Worker
docker run -d --name gpumesh-worker \
  -e GPUMESH_TOKEN=mysecret \
  samurai007ak/gpumesh:latest \
  join http://192.168.1.10:8732 --token mysecret
```

`GPUMESH_HOST_IP` matters here: without it the coordinator advertises its container-internal IP, which workers outside the container network cannot reach.

Or use the included [`docker-compose.yaml`](https://github.com/Samurai007AK/gpumesh/blob/main/docker-compose.yaml) for a coordinator plus N workers with healthchecks:

```bash
GPUMESH_TOKEN=mysecret docker compose up -d
GPUMESH_TOKEN=mysecret docker compose up -d --scale worker=4
```

**Ports:** `8732/tcp` (API) and `48900/udp` (LAN discovery).

> The container listens on **8732**, while `gpumesh serve` on the host defaults to **8000**. That is deliberate — the image pins an explicit port so published `docker run` and compose recipes stay stable. Both are just defaults: pass `--port` to use whatever you like, and make sure workers point at the same number the coordinator is listening on.

---

## Network options

| Method | Setup | Best for | Encrypted |
|--------|-------|----------|-----------|
| **LAN** | None | Same Wi-Fi, fastest | No |
| **Tailscale** | Install Tailscale | Remote teams | Yes |
| **ngrok** | `pip install "gpumesh[tunnel]"` | Public access, demos | Yes |

- **LAN (default):** workers discover the coordinator automatically via UDP broadcast on port `48900`. `gpumesh serve`, then `gpumesh join http://192.168.1.10:8000 --token mysecret`.
- **Tailscale:** `gpumesh serve --port 8000 --tailscale`, then join via the Tailscale IP.
- **ngrok:** `gpumesh serve --port 8000 --public` prints a public `https://...` URL that workers anywhere can join.

---

## Architecture

gpumesh is a **pull-based work queue**. The coordinator never pushes to workers, so workers need no inbound ports, no static address, and no uptime guarantee — they poll, and a worker that disappears mid-task simply loses its lease.

```
    YOUR CODE                     @mesh / @accelerate / GPUMesh(...)
    ─────────                     cloudpickle(function) + params
        │
        │  POST /api/jobs                            gpumesh/serializer.py
        ▼
 ╔══════════════════════════════════════════════════════════════════╗
 ║  COORDINATOR                              gpumesh serve :8000    ║
 ╟──────────────────────────────────────────────────────────────────╢
 ║                                                                  ║
 ║   HTTP API  (ThreadingHTTPServer, X-Auth-Token on every call)    ║
 ║       │                                          server.py       ║
 ║       ├──▶ auth + rate limit ....................security.py     ║
 ║       ├──▶ scheduler (score percentile, memory) .db.lease_task   ║
 ║       ├──▶ jobs / tasks / workers (SQLite, WAL) .db.py           ║
 ║       └──▶ reaper: expire leases + dead workers .server.py       ║
 ║                                                                  ║
 ║   UDP :48900  beacon listener ...................discovery.py    ║
 ╚══════════════════════════════════════════════════════════════════╝
        ▲                    ▲                    ▲
        │  register          │  heartbeat  10s    │  lease / result
        │                    │                    │
 ┌──────┴───────┐     ┌──────┴───────┐     ┌──────┴───────┐
 │  WORKER A    │     │  WORKER B    │     │  WORKER C    │
 │  RTX 4090    │     │  RTX 3080    │     │  CPU only    │
 │              │     │              │     │              │
 │  poll loop   │     │  poll loop   │     │  poll loop   │
 │  benchmark   │     │  benchmark   │     │  benchmark   │
 │  subprocess  │     │  subprocess  │     │  subprocess  │
 │  per task    │     │  per task    │     │  per task    │
 └──────────────┘     └──────────────┘     └──────────────┘
     worker.py           worker.py            worker.py
   ↳ _function_subprocess.py (functions) · sandbox.py (scripts)
```

### Components

| Module | Role |
|--------|------|
| `server.py` | Coordinator HTTP API, threaded; background reaper for stale leases and dead workers |
| `db.py` | SQLite persistence (WAL mode, single locked connection): jobs, tasks, workers, worker stats. Also holds `lease_task`, the scheduler |
| `worker.py` | Worker agent: register → heartbeat → poll for a lease → execute → post result |
| `_function_subprocess.py` | Runs one pickled **function** in a fresh process, results piped back over stdin/stdout |
| `sandbox.py` | Runs one **script** job in a fresh process: JSON payload on stdin, JSON result on the last stdout line, own process group, optional CPU rlimit on POSIX |
| `serializer.py` | cloudpickle encode/decode of functions and arguments, with a source-based fallback |
| `capability.py` | Hardware probe plus the matmul + memory-bandwidth benchmark that produces a worker's score |
| `security.py` | Token hashing (SHA-256 + salt), timing-safe comparison, per-IP rate limiting and lockout |
| `discovery.py` | UDP beacon broadcast/listen on port 48900 for zero-config LAN discovery |
| `claimer.py` | Worker-side claim endpoint so `gpumesh worker` can be recruited by a coordinator |
| `connection_manager.py` | Saves and loads `~/.gpumesh/config.json` so commands work without flags |
| `accelerate.py` / `mesh.py` | The `@accelerate` and `@mesh` decorators, routing, and local fallback |
| `api.py` | The `GPUMesh` client class |
| `tunnel.py` | Optional ngrok public URL and Tailscale IP detection |

### Task lifecycle

```
submit ─▶ pending ─▶ leased/running ─▶ done
                          │
                          ├─ task raised ───────▶ failed  ──(gpumesh retry)──▶ pending
                          ├─ lease expired (300s) ─────────────────────────────▶ pending
                          └─ worker died / TTL ────────────────────────────────▶ pending
```

1. **Submit.** A function is cloudpickled client-side; a script job uploads the script text. One task row is created per payload, each with a `cost` (default `1.0`).
2. **Lease.** A worker polls `POST /api/lease`. The coordinator does not simply hand out the oldest task — see scheduling below.
3. **Execute.** The worker runs the task in a **fresh subprocess**, so a segfault, an `os._exit`, or a CUDA crash kills only that process.
4. **Report.** `POST /api/result` writes the outcome, updates that worker's rolling average time, and frees the lease.
5. **Recover.** Anything that never reports — a crashed worker, a closed laptop lid — has its lease expire and the task returns to `pending` for someone else.

### Scheduling

`db.lease_task` picks a task by matching worker strength to task cost:

- **Score percentile matching.** A worker's benchmark score is ranked against all live workers; that percentile selects the same percentile in the pending tasks sorted by `cost`. The strongest machine gets the heaviest queued work, the weakest gets the lightest.
- **Straggler tolerance.** A worker whose rolling average task time exceeds **2× the median** across active workers is deprioritized to lighter tasks.
- **Memory filtering.** If a payload carries a `gpu_memory_mb` hint, workers reporting less free VRAM than that are filtered out before scoring.

### HTTP API

Every request carries an `X-Auth-Token` header.

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `POST` | `/api/register` | Worker announces hostname, device, score → `{worker_id}` |
| `POST` | `/api/heartbeat` | Liveness plus updated score and free VRAM |
| `POST` | `/api/lease` | Request a task → task JSON, or `204` when the queue is empty |
| `POST` | `/api/result` | Report success or failure for a task |
| `POST` | `/api/jobs` | Create a job from `{name, script, payloads}` → `{job_id}` |
| `GET` | `/api/jobs/<id>` | Job status plus per-task results |
| `POST` | `/api/cancel` | Cancel a job's pending and running tasks |
| `POST` | `/api/retry` | Re-queue a job's failed tasks |
| `POST` | `/api/kill` | Cancel everything across all workers |
| `GET` | `/api/workers` | Live worker list |
| `GET` | `/api/devices` | Unified device pool view |
| `GET` | `/api/events` | Last 100 join/leave events |
| `GET` | `/api/health` | Liveness probe |

### Timing and durability

| Behavior | Value | Where |
|----------|-------|-------|
| Worker heartbeat interval | 10s | `worker.HEARTBEAT_INTERVAL` |
| Idle poll interval | 1s (0.1s while busy) | `worker.IDLE_POLL_INTERVAL` |
| Re-benchmark interval | 600s | `worker.REBENCHMARK_INTERVAL` |
| Task lease before re-queue | 300s | `db.LEASE_SECONDS` |
| Stale worker row deleted after | 300s | `db.WORKER_DELETE_AFTER` |
| Reaper sweep interval | 5s | `server.REAP_INTERVAL` |
| Default per-task wall clock | 240s | `join --timeout` |
| Graceful shutdown grace period | 30s | `worker.GRACEFUL_SHUTDOWN_TIMEOUT` |

Jobs, tasks, and results live in the coordinator's SQLite file (`gpumesh.db` by default, `--db` to change it), so a coordinator restart resumes an in-flight job rather than losing it. Workers hold no durable state — they re-register and re-benchmark on reconnect, which is why they get a new worker ID after an outage.

### Deliberate non-goals

Single coordinator, no leader election, no replication. No cross-worker communication — every task is independent, so there is no collective, no all-reduce, and no model sharding. This is a work queue for embarrassingly parallel jobs, not a training fabric.

---

## Security

| Feature | Detail |
|---------|--------|
| Token authentication | Required on every API request |
| Timing-safe comparison | `hmac.compare_digest` |
| Rate limiting | 5 failed attempts within 300s → 900s (15 min) IP lockout |
| Process isolation | Every task runs in its own subprocess |
| File permissions | `0o600` on `~/.gpumesh/config.json` (Unix; a no-op on Windows) |
| Token hashing | SHA-256 with a salt, in memory only — the token is never written to the database |

> **Workers execute code sent by the coordinator.** Anyone holding your URL and token can run arbitrary code on every machine in your mesh. Only share them with people you trust, and treat the token like a password. gpumesh is built for trusted networks — home labs, lab benches, your own machines, a team you know. It is not a sandbox and is not designed to run untrusted code.
>
> Run the coordinator with `--safe-mode` to refuse function distribution and accept submitted scripts only. Workers accept `--safe-mode` too, so an individual machine can opt out of running pickled functions even when the coordinator allows them.
>
> Traffic is **not encrypted** on a plain LAN. Use `--tailscale` or `--public` (ngrok) when the mesh crosses a network you do not control.

---

## Benchmark scoring

On join — and every 10 minutes after — each worker runs a 1024×1024 matmul and a 256 MB memory copy, then combines them:

```
score = 0.7 × GFLOP/s  +  0.3 × memory bandwidth (GB/s)
```

The score is **unbounded and relative**, not a 0–100 rating. Only the ratio between workers matters: the scheduler ranks each worker's score against the others and hands out task cost by percentile.

| Machine | Rough score | Typical use |
|---------|-------------|-------------|
| Datacenter GPU (A100, H100) | Highest in a mixed pool | Heavy training, large models |
| High-end consumer GPU (RTX 4090) | Well above a CPU peer | Heavy training |
| Mid consumer GPU (RTX 3060, T4) | Mid-pool | Inference, preprocessing |
| CPU with `torch` installed | Low | Light tasks |
| CPU **without** `torch` | Near zero (pure-Python fallback) | Lightest tasks only |

Absolute numbers depend on your hardware, drivers, and thermal state — inspect the real ones with `gpumesh workers` or `mesh.workers()`. Install `"gpumesh[gpu]"` on every machine you want scored fairly: a torch-less worker runs a deliberately slow pure-Python fallback and will be starved of real work.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `command not found: gpumesh` | Use `python -m gpumesh`, or add your Python scripts directory to `PATH` |
| `401 bad token` | Use the same token on coordinator and worker. Remember `gpumesh serve` ignores `GPUMESH_TOKEN` — pass `--token` |
| Coordinator unreachable | Check the firewall; confirm the coordinator is running and the port matches |
| Workers connect to the wrong address | Pin the advertised IP: `gpumesh serve --host-ip 192.168.1.10` |
| Task timed out | Raise `join --timeout`, or split the work into smaller tasks |
| Windows connection error | Run `gpumesh serve` as Administrator so firewall rules are added |
| Worker not showing up | Same network? Try `gpumesh radar`, or join by explicit URL |
| UDP broadcast not working | Skip discovery and use `gpumesh join URL --token T` directly |
| `ModuleNotFoundError: torch` | `pip install "gpumesh[gpu]"` |
| `ModuleNotFoundError` inside a task | Install that package on the **worker** too — gpumesh ships your code, not your environment |
| `cannot send result of type ...` | Return plain data. Open files, sockets, locks, and live GPU handles cannot cross machines |
| Results differ from a local run | They shouldn't — please file an issue. Confirm with `GPUMESH_LOCAL=1 python your_script.py` |

**Verbose logging:** `gpumesh -v serve` or `GPUMESH_VERBOSE=1`. **Force local-only:** `GPUMESH_LOCAL=1 python my_script.py`.

---

## Development

```bash
git clone https://github.com/Samurai007AK/gpumesh.git
cd gpumesh
pip install -e ".[dev]"
pytest                 # 624 tests
python -m build        # build wheel + sdist
```

---

## Limitations

- Python only — tasks must be Python functions or scripts
- Arguments and return values must be picklable. Anything tied to a live process — open files, sockets, locks, database handles, CUDA handles — cannot cross machines. Return plain data (numbers, arrays, tensors, DataFrames) instead
- Every worker needs the imports your function uses already installed; gpumesh ships your code, not your environment
- Workers should run the same Python minor version as the submitter — cloudpickle falls back to source when they differ, which does not cover every function
- No GPU memory sharing — each task gets its own process
- No model sharding and no cross-worker communication — each task runs on one machine at a time
- Single coordinator — a single point of failure
- No built-in encryption — use Tailscale or ngrok for encrypted transport
- Trusted networks only — workers run whatever code the coordinator sends

---

## License

MIT License. See [LICENSE](https://github.com/Samurai007AK/gpumesh/blob/main/LICENSE) for details.

---

[GitHub](https://github.com/Samurai007AK/gpumesh) · [Issues](https://github.com/Samurai007AK/gpumesh/issues) · [PyPI](https://pypi.org/project/gpumesh/) · [Docker Hub](https://hub.docker.com/r/samurai007ak/gpumesh) · [Changelog](https://github.com/Samurai007AK/gpumesh/blob/main/CHANGELOG.md)
