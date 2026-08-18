# 🖥️ gpumesh — Borrow Your Friends' GPUs

> *Like Bluetooth, but for your GPUs*

[![PyPI version](https://img.shields.io/pypi/v/gpumesh.svg)](https://pypi.org/project/gpumesh/)
[![Python](https://img.shields.io/pypi/pyversions/gpumesh.svg)](https://pypi.org/project/gpumesh/)
[![License](https://img.shields.io/pypi/l/gpumesh.svg)](https://github.com/K4-LABS/gpumesh/blob/master/LICENSE)
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
         │   │        T4 (12.0)         │            │
         │   │      running tasks       │            │
         │   └──────────────────────────┘            │
         │                                           │
         │   >>> results collected automatically     │
         └─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─┘
```

---

> [!CAUTION]
> **gpumesh runs code you send it, on machines that trust you. There is no sandbox.**
>
> A worker executes arbitrary Python as the OS user that started it — same
> files, same GPUs, same network, same credentials. A valid token is not a
> password guarding data; it is a licence to execute code on every machine in
> the mesh.
>
> It runs **both ways**: a worker's results are deserialized by whoever
> submitted the task, so a hostile worker executes code on the submitter.
>
> gpumesh provides **no sandbox** and does not try to. Two consequences for
> anyone running this image:
>
> - Publish the port to `127.0.0.1` unless you deliberately want other
>   machines to join — `-p 127.0.0.1:8732:8732`, not `-p 8732:8732`.
> - Generate a real token:
>   `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
>
> Read [SECURITY.md](https://github.com/K4-LABS/gpumesh/blob/master/SECURITY.md)
> and [THREAT_MODEL.md](https://github.com/K4-LABS/gpumesh/blob/master/THREAT_MODEL.md).

---

## 🚀 What is gpumesh?

**gpumesh** turns multiple machines into a single, unified GPU compute pool. Start a **coordinator** on one machine, join **workers** from other machines (laptops, desktops, servers — anything with Python), and run code across all of them as if they were one device.

### 💡 Use Cases

| Use Case | Description |
|----------|-------------|
| 🔬 **Hyperparameter Search** | Sweep across multiple GPUs in parallel |
| 📊 **Data Preprocessing** | Shard heavy workloads across machines |
| 🧠 **Model Training** | Pool consumer GPUs for serious compute |
| ⚡ **Parallel Workloads** | Any embarrassingly parallel task |

---

## 🆕 What's New in 3.0.0

- **Job queue persistence** — submitted jobs survive coordinator restarts (SQLite-backed)
- **AGPL-3.0-or-later license** — proper open-source copyleft
- **Loopback by default** — coordinator binds `127.0.0.1` unless you opt in
- **Tailored startup errors** — clear messages for port conflicts, bad `--host-ip`, unwritable DB
- **Radar token via getpass** — interactive claim tokens never echoed to terminal
- **CI hardening** — compat tests verify version skew, pip-audit retries, scorecard stops re-scoring on push
- **Threat model + SECURITY-INSIGHTS** — machine-readable security docs

See [CHANGELOG](https://github.com/K4-LABS/gpumesh/blob/master/CHANGELOG.md) for full details.

---

## ⚡ Quick Start (30 seconds)

### 0️⃣ Generate a Token

```bash
export GPUMESH_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
```

The token is the only thing between anyone who can reach the port and code
execution as the container's user. Do not use a memorable one.

### 1️⃣ Start a Coordinator

```bash
docker run -d \
  --name gpumesh-coordinator \
  -p 127.0.0.1:8732:8732 \
  -e GPUMESH_TOKEN \
  samurai007ak/gpumesh:3.0.0 \
  serve --host 0.0.0.0 --port 8732
```

Two different "hosts" are in play here, and they are separate decisions:

| | What it controls | Value above |
|---|---|---|
| `serve --host` | What the coordinator binds to **inside** the container. Must be `0.0.0.0` — a loopback bind inside a container answers nothing, including the published port | `0.0.0.0` |
| `-p <host-side>:8732` | What Docker publishes on the **machine running Docker**. This is the one that decides who can reach you | `127.0.0.1` — this machine only |

Drop the `127.0.0.1:` prefix, or better name a specific interface
(`-p 192.0.2.10:8732:8732`), only when you deliberately want other machines to
join. Plain `-p 8732:8732` binds every interface the host has — office wifi,
coffee-shop wifi, and on many home routers the public internet via UPnP.

### 2️⃣ Join a Worker

```bash
docker run -d \
  --name gpumesh-worker \
  -e GPUMESH_TOKEN \
  samurai007ak/gpumesh:3.0.0 \
  join http://coordinator-ip:8732
```

> `join` takes the coordinator URL as a positional argument — there is no
> `GPUMESH_URL` to set here. Both `serve` and `join` read `GPUMESH_TOKEN` from
> the environment, so `-e GPUMESH_TOKEN` is enough and the token stays out of
> `docker ps` output.

### 3️⃣ Use Your Mesh

```python
import os
from gpumesh import GPUMesh, accelerate

mesh = GPUMesh("http://coordinator:8732", token=os.environ["GPUMESH_TOKEN"])

@accelerate(mesh)
def train(lr, epochs):
    return {"accuracy": 0.95}

result = train(lr=0.01, epochs=100)
results = train.map([{"lr": 0.01}, {"lr": 0.05}, {"lr": 0.1}])
```

---

## 🐳 Docker Images

| Tag | Description |
|-----|-------------|
| `latest` | Latest stable release (currently 3.0.0) |
| `3.0.0` | AGPL-licensed release with queue persistence, loopback default, and all security hardening |

### Pull

```bash
docker pull samurai007ak/gpumesh:3.0.0
# or
docker pull samurai007ak/gpumesh:latest
```

---

## 🎯 Docker Compose (Recommended)

Create a `docker-compose.yml`. This mirrors the
[file in the repository](https://github.com/K4-LABS/gpumesh/blob/master/docker-compose.yaml),
which carries the full reasoning in comments:

```yaml
services:
  coordinator:
    # Pinned, not `latest`. The wire format is pickled Python, so a version
    # skew between coordinator and worker is a real failure, not a cosmetic one.
    image: samurai007ak/gpumesh:3.0.0
    ports:
      # host-side bind : container port.
      # GPUMESH_BIND defaults to 127.0.0.1 — this machine only. Set it to
      # 0.0.0.0 (or better, one interface address) to accept other machines.
      - "${GPUMESH_BIND:-127.0.0.1}:${GPUMESH_PORT:-8732}:8732"
      - "${GPUMESH_BIND:-127.0.0.1}:48900:48900/udp"
    environment:
      - GPUMESH_TOKEN=${GPUMESH_TOKEN:?set GPUMESH_TOKEN, e.g. GPUMESH_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))") docker compose up -d}
      - GPUMESH_COLOR=1
      # Bind inside the container. Must be 0.0.0.0 — the worker service
      # reaches it across the compose network.
      - GPUMESH_HOST=0.0.0.0
    command: serve --host 0.0.0.0 --port 8732
    volumes:
      # The image sets WORKDIR=/data and HOME=/data, so the SQLite database
      # and ~/.gpumesh/config.json both live here. One mount covers both.
      - gpumesh_data:/data
    healthcheck:
      test: ["CMD", "nc", "-z", "localhost", "8732"]
      interval: 10s
      timeout: 3s
      retries: 3
      start_period: 10s
    restart: unless-stopped
    # Defence in depth for a service that runs code it was sent. The image
    # already drops to UID 10001; these stop a compromised task from
    # re-acquiring privilege inside the container.
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL

  worker:
    image: samurai007ak/gpumesh:3.0.0
    depends_on:
      coordinator:
        condition: service_healthy
    environment:
      - GPUMESH_TOKEN=${GPUMESH_TOKEN:?set GPUMESH_TOKEN}
      - GPUMESH_COLOR=1
    # The coordinator URL is a required positional argument — `join` on its
    # own exits with "the following arguments are required: url".
    command: join http://coordinator:8732
    deploy:
      replicas: ${WORKER_REPLICAS:-2}
    restart: unless-stopped
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL

volumes:
  gpumesh_data:
```

`GPUMESH_TOKEN` is required — `:?` stops the run with a readable message when
it is unset, rather than starting a coordinator with a random token that no
worker could authenticate against.

Nothing in this file is published beyond `127.0.0.1` by default. The workers
reach the coordinator over the compose network, which needs no host
publishing at all.

Then run:

```bash
# Generate a token once
export GPUMESH_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")

# Start coordinator + 2 workers, reachable from this machine only
docker compose up -d

# Scale to 4 workers
WORKER_REPLICAS=4 docker compose up -d

# Deliberately reachable from other machines on your LAN
GPUMESH_BIND=0.0.0.0 docker compose up -d

# View logs
docker compose logs -f

# Stop everything
docker compose down
```

---

## 🌟 Features

### 🎨 Transparent Acceleration

```python
@accelerate(mesh)
def preprocess(chunk_id, data_path):
    return {"chunk": chunk_id, "rows": len(df)}

# Single call → best local device
result = preprocess(chunk_id=0, data_path="data.parquet")

# Map call → spread across ALL mesh devices
results = preprocess.map([
    {"chunk_id": 0, "data_path": "part0.parquet"},
    {"chunk_id": 1, "data_path": "part1.parquet"},
])
```

### 🧠 Smart Routing

| Scenario | What Happens |
|----------|--------------|
| `func(x)`, workers alive | Runs as a single task on one mesh worker |
| `func(x)`, no workers | Runs on the best LOCAL device (CPU/GPU) |
| `func.map()` | Spreads across ALL mesh devices |
| Mesh unreachable | Falls back to LOCAL execution |
| `GPUMESH_LOCAL=1` | Forces local-only |
| `GPUMESH_VERBOSE=1` | Prints device info |

### 🔧 Hardware Selection

```python
@accelerate(mesh, gpu="A100")
def train(model):
    return model.cuda().forward(x)
```

### 📊 Resource Specs

```python
@accelerate(mesh, cores=8, memory="16GB", timeout=300)
def heavy_computation(data):
    return processed
```

### 🛡️ Fault Tolerance

- ✅ Dead workers detected & tasks re-queued
- ✅ Straggler workers deprioritized
- ✅ Graceful fallback to local execution
- ✅ Crash diagnostics on worker failures
- ✅ TTL-based worker expiry (auto-prune)
- ✅ Memory-aware scheduling (VRAM tracking)

---

## 📈 Benchmark Scoring

Each worker runs a benchmark on join and gets a score of
`gflops * 0.7 + bandwidth_gbps * 0.3` — a **relative, unbounded** number with no
normalisation and no ceiling. It only means something next to the other workers
in your pool, and the scheduler uses it purely to rank them.

Rough magnitudes, so the numbers `gpumesh workers` prints make sense:

```
  Score       Typical GPU        Use Case
  ─────       ───────────        ────────
  ~100+       RTX 4090, A100     Heavy training, large models
  ~50-100     RTX 3080, 3090     Medium training, inference
  ~10-50      RTX 3060, T4       Light tasks, preprocessing
  under 1     CPU only           Very light tasks
```

Illustrative, not a scale — a laptop CPU commonly lands around `0.24`, and
faster hardware than anything listed simply scores higher.

---

## 🛠️ Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GPUMESH_TOKEN` | (required) | Authentication token. Read by both `serve` and `join` |
| `GPUMESH_HOST` | `127.0.0.1` | **Bind address inside the container.** Must be `0.0.0.0` for anything outside the container to connect — including the port Docker published. Same as `serve --host` |
| `GPUMESH_HOST_IP` | (auto) | Advertised address only — which address is *printed* for workers to dial. Does **not** change the bind. Must be an IP literal; a hostname is discarded with a warning and auto-detection used instead |
| `GPUMESH_BIND` | `127.0.0.1` | Compose only: the **host-side** publish address. This is what decides who can reach the coordinator |
| `GPUMESH_URL` | (unset) | Coordinator URL for job/monitoring commands (`submit`, `status`, `workers`, ...). `join` takes the URL as a positional argument and ignores this |
| `GPUMESH_PORT` | `8732` | Host port published by `docker-compose.yaml`; the container always listens on 8732 |
| `GPUMESH_COLOR` | `auto` | `1` forces colored output, `0` disables it, `auto` checks whether stdout is a TTY. `serve` and `join` also accept `--color` / `--no-color`, which set this variable for the container process and for the function-task subprocesses it spawns |
| `GPUMESH_CLAIM_HOST` | `0.0.0.0` | Bind address for `gpumesh worker`'s claim port. Unlike `GPUMESH_HOST` it defaults to all interfaces on purpose — a claim server exists to be reached from another machine. Inside a container the boundary is the Docker port publish, so leave it alone unless you are on host networking |
| `GPUMESH_VERBOSE` | `0` | `1` makes `@mesh` / `@accelerate` print which device handled each task |
| `GPUMESH_LOCAL` | `0` | Force local-only mode |
| `WORKER_REPLICAS` | `2` | Number of workers (compose) |

---

## 🔐 Security

| Feature | Status |
|---------|--------|
| Loopback by default | ✅ `serve` binds `127.0.0.1`; the compose file publishes to `127.0.0.1` |
| Non-root container | ✅ Runs as UID 10001, `cap_drop: ALL`, `no-new-privileges` |
| Token authentication | ✅ All API requests, including reads |
| Timing-safe comparison | ✅ HMAC compare_digest |
| Rate limiting | ✅ 5 failures → blocked (loopback exempt) |
| Process isolation | ✅ Tasks in subprocesses |
| File permissions | ✅ 0o600 on token files |
| Token hashing | ✅ SHA-256, in memory only — never written to the database |

Read that as "what raises the cost of an attack", not "what makes this safe".
None of it is a sandbox.

> ⚠️ **A token is a licence to execute code, not a password guarding data.**
> Anyone holding your URL and token runs arbitrary Python on every machine in
> the mesh, as each worker's user. And it runs both ways: results are
> deserialized by whoever submitted the task, so a hostile worker executes
> code on the submitter.
>
> Generate a real token —
> `python -c "import secrets; print(secrets.token_urlsafe(32))"`. The hash is
> a single SHA-256 round with no KDF, so the token's entropy is the whole
> defence.
>
> `serve --safe-mode` refuses function distribution and accepts submitted
> scripts only. That closes the pickle path; it does not stop a script from
> doing anything a script can do.
>
> Traffic is **not encrypted**. Use `--tailscale` or `--public` when the mesh
> crosses a network you do not control.
>
> Full detail:
> [SECURITY.md](https://github.com/K4-LABS/gpumesh/blob/master/SECURITY.md)
> · [THREAT_MODEL.md](https://github.com/K4-LABS/gpumesh/blob/master/THREAT_MODEL.md)

---

## 🏗️ Architecture

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
   │         HTTP API :8732                          │
   └──────────────────┼──────────────────────────────┘
                      │
         ┌────────────┼────────────┐
         │            │            │
   ┌─────▼────┐ ┌────▼────┐ ┌────▼────┐
   │ Worker 1 │ │Worker 2 │ │Worker 3 │
   │ RTX 4090 │ │RTX 3080 │ │   T4    │
   │Score: 120│ │Score: 85│ │Score: 12│
   └──────────┘ └─────────┘ └─────────┘
```

---

## 📚 Full Documentation

- **GitHub:** [github.com/K4-LABS/gpumesh](https://github.com/K4-LABS/gpumesh)
- **PyPI:** [pypi.org/project/gpumesh](https://pypi.org/project/gpumesh/)
- **Issues:** [github.com/K4-LABS/gpumesh/issues](https://github.com/K4-LABS/gpumesh/issues)
- **HTTP API and wire format:** [docs/protocol.md](https://github.com/K4-LABS/gpumesh/blob/master/docs/protocol.md)
- **Public API, versioning and the protocol compatibility window:** [docs/stability.md](https://github.com/K4-LABS/gpumesh/blob/master/docs/stability.md)
- **Why not Ray or Dask?** [docs/why-not-ray-or-dask.md](https://github.com/K4-LABS/gpumesh/blob/master/docs/why-not-ray-or-dask.md)
- **Runnable examples:** [examples/](https://github.com/K4-LABS/gpumesh/tree/master/examples)

---

## 🤝 Contributing

Contributions are welcome! See the [GitHub repository](https://github.com/K4-LABS/gpumesh) for guidelines.

---

## 📄 License

GNU AGPL-3.0 — see [LICENSE](https://github.com/K4-LABS/gpumesh/blob/master/LICENSE) for details.

---

**Built with ❤️ by [samurai007ak](https://github.com/Samurai007AK)**
