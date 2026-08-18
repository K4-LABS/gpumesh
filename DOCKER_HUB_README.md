<div align="center">

<img src="https://raw.githubusercontent.com/K4-LABS/gpumesh/master/docs/img/gpumesh-logo.png" alt="gpumesh logo" width="300">

# gpumesh

**Borrow your friends' GPUs.**

Like Bluetooth — but for your compute. Share GPU power between machines on your network with one decorator, one CLI command, or a Python API.

[![PyPI](https://img.shields.io/pypi/v/gpumesh?style=flat-square&logo=pypi&logoColor=white)](https://pypi.org/project/gpumesh/)
[![Python](https://img.shields.io/pypi/pyversions/gpumesh?style=flat-square)](https://pypi.org/project/gpumesh/)
[![License](https://img.shields.io/badge/License-AGPL--3.0-blue?style=flat-square)](https://github.com/K4-LABS/gpumesh/blob/master/LICENSE)
[![Tests](https://img.shields.io/github/actions/workflow/status/K4-LABS/gpumesh/tests.yml?style=flat-square&label=tests)](https://github.com/K4-LABS/gpumesh/actions/workflows/tests.yml)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?style=flat-square&logo=docker&logoColor=white)](https://hub.docker.com/r/samurai007ak/gpumesh)

[Quickstart](#-quick-start) · [Docker Compose](#-docker-compose-recommended) · [Features](#-features) · [Security](#-security) · [GitHub](https://github.com/K4-LABS/gpumesh)

</div>

---

> [!CAUTION]
> **gpumesh runs code you send it — there is no sandbox.** A worker executes
> arbitrary Python as the OS user that started it, and results are deserialized
> by the submitter — trust runs **both ways**. Use it only with machines and
> people you trust. See [SECURITY.md](https://github.com/K4-LABS/gpumesh/blob/master/SECURITY.md).

---

## What is gpumesh?

**gpumesh** turns multiple machines into a single, unified compute pool. Start a **coordinator** on one machine, join **workers** from other machines (laptops, desktops, servers — anything with Python), and run code across all of them as if they were one device.

```
┌──────────────┐         ┌──────────────┐
│  Coordinator │◄───────►│   Worker 1   │
│  (your Mac)  │         │ RTX 4090     │
│  Port 8732   │         │ Score: 120   │
└──────┬───────┘         └──────────────┘
       │
       │                 ┌──────────────┐
       ├────────────────►│   Worker 2   │
       │                 │ RTX 3080     │
       │                 │ Score: 85    │
       │                 └──────────────┘
       │
       │                 ┌──────────────┐
       └────────────────►│   Worker 3   │
                         │ Laptop CPU   │
                         │ Score: 0.5   │
                         └──────────────┘
```

**Use cases:**
- Hyperparameter search across multiple GPUs
- Data preprocessing sharded across machines
- Model training on a pool of consumer GPUs
- Any embarrassingly parallel workload

---

## 🆕 What's New in 3.0.0

- **Job queue persistence** — submitted jobs survive coordinator restarts (SQLite-backed)
- **AGPL-3.0-or-later license** — proper open-source copyleft
- **Loopback by default** — coordinator binds `127.0.0.1` unless you opt in
- **Tailored startup errors** — clear messages for port conflicts, bad `--host-ip`, unwritable DB
- **Radar token via getpass** — interactive claim tokens never echoed to terminal
- **CI hardening** — compat tests verify version skew, pip-audit retries
- **Threat model + SECURITY-INSIGHTS** — machine-readable security docs

See [CHANGELOG](https://github.com/K4-LABS/gpumesh/blob/master/CHANGELOG.md) for full details.

---

## ⚡ Quick Start

### 0. Generate a Token

```bash
export GPUMESH_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
```

### 1. Start a Coordinator

```bash
docker run -d \
  --name gpumesh-coordinator \
  -p 127.0.0.1:8732:8732 \
  -e GPUMESH_TOKEN \
  samurai007ak/gpumesh:3.0.0 \
  serve --host 0.0.0.0 --port 8732
```

> **Why `serve --host 0.0.0.0`?** Inside a container, the loopback address (`127.0.0.1`) answers nothing — even the port Docker published. The `--host` flag controls what the coordinator binds to *inside* the container. The `-p` flag controls what Docker publishes on the *host machine*.

### 2. Join a Worker (from another machine)

```bash
docker run -d \
  --name gpumesh-worker \
  -e GPUMESH_TOKEN \
  samurai007ak/gpumesh:3.0.0 \
  join http://coordinator-ip:8732
```

### 3. Use Your Mesh

```python
from gpumesh import GPUMesh, accelerate

mesh = GPUMesh("http://coordinator:8732", token=TOKEN)

@accelerate(mesh)
def train(lr, epochs):
    return {"accuracy": 0.95}

# Single call → best available worker (or local if none)
result = train(lr=0.01, epochs=100)

# Map call → spread across ALL connected workers
results = train.map([
    {"lr": 0.01, "epochs": 100},
    {"lr": 0.05, "epochs": 200},
])
```

---

## 🐳 Docker Images

| Tag | Description |
|-----|-------------|
| `latest` | Latest stable release (currently 3.0.0) |
| `3.0.0` | AGPL-licensed with queue persistence, loopback default, security hardening |

```bash
docker pull samurai007ak/gpumesh:3.0.0
# or
docker pull samurai007ak/gpumesh:latest
```

### Verify

```bash
docker run --rm samurai007ak/gpumesh:3.0.0 --version
# gpumesh 3.0.0 (3.11.9, linux)

docker run --rm samurai007ak/gpumesh:3.0.0 doctor --json
# {"version": "3.0.0", "python": "3.11.9", "status": "ok"}
```

---

## 🎯 Docker Compose (Recommended)

Create a `docker-compose.yml`:

```yaml
services:
  coordinator:
    image: samurai007ak/gpumesh:3.0.0
    ports:
      - "${GPUMESH_BIND:-127.0.0.1}:${GPUMESH_PORT:-8732}:8732"
      - "${GPUMESH_BIND:-127.0.0.1}:48900:48900/udp"
    environment:
      - GPUMESH_TOKEN=${GPUMESH_TOKEN:?set GPUMESH_TOKEN}
      - GPUMESH_HOST=0.0.0.0
    command: serve --host 0.0.0.0 --port 8732
    volumes:
      - gpumesh_data:/data
    healthcheck:
      test: ["CMD", "nc", "-z", "localhost", "8732"]
      interval: 10s
      timeout: 3s
      retries: 3
      start_period: 10s
    restart: unless-stopped
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

Run:

```bash
# Generate a token
export GPUMESH_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")

# Start coordinator + 2 workers
docker compose up -d

# Scale to 4 workers
WORKER_REPLICAS=4 docker compose up -d

# Reachable from other machines on your LAN
GPUMESH_BIND=0.0.0.0 docker compose up -d

# View logs
docker compose logs -f

# Stop
docker compose down
```

---

## 🌟 Features

### Transparent Acceleration

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

### Smart Routing

| Scenario | What Happens |
|----------|--------------|
| `func(x)`, workers alive | Runs on one mesh worker |
| `func(x)`, no workers | Runs locally (CPU/GPU) |
| `func.map()` | Spreads across ALL mesh devices |
| Mesh unreachable | Falls back to local execution |
| `GPUMESH_LOCAL=1` | Forces local-only |

### Hardware Selection

```python
@accelerate(mesh, gpu="A100")
def train(model):
    return model.cuda().forward(x)
```

### Resource Specs

```python
@accelerate(mesh, cores=8, memory="16GB", timeout=300)
def heavy_computation(data):
    return processed
```

### Fault Tolerance

- Dead workers detected & tasks re-queued
- Straggler workers deprioritized
- Graceful fallback to local execution
- Crash diagnostics on worker failures
- TTL-based worker expiry (auto-prune)
- Memory-aware scheduling (VRAM tracking)

---

## 📈 Benchmark Scoring

Each worker runs a benchmark on join and gets a score of `gflops * 0.7 + bandwidth_gbps * 0.3` — a **relative, unbounded** number used purely to rank workers.

```
  Score       Typical GPU        Use Case
  ─────       ───────────        ────────
  ~100+       RTX 4090, A100     Heavy training, large models
  ~50-100     RTX 3080, 3090     Medium training, inference
  ~10-50      RTX 3060, T4       Light tasks, preprocessing
  under 1     CPU only           Very light tasks
```

---

## 🛠️ Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GPUMESH_TOKEN` | (required) | Authentication token. Read by `serve` and `join` |
| `GPUMESH_HOST` | `127.0.0.1` | Bind address inside container. Must be `0.0.0.0` for anything outside to connect |
| `GPUMESH_HOST_IP` | (auto) | Advertised address — which IP is printed for workers to dial. Does **not** change the bind |
| `GPUMESH_BIND` | `127.0.0.1` | Compose only: host-side publish address. This decides who can reach the coordinator |
| `GPUMESH_URL` | (unset) | Coordinator URL for CLI commands (`submit`, `status`, `workers`). `join` takes URL as a positional argument |
| `GPUMESH_PORT` | `8732` | Host port published by compose; container always listens on 8732 |
| `GPUMESH_COLOR` | `auto` | `1` forces color, `0` disables, `auto` checks TTY |
| `GPUMESH_CLAIM_HOST` | `0.0.0.0` | Bind for claim server. Defaults to all interfaces (a claim server exists to be reached) |
| `GPUMESH_VERBOSE` | `0` | `1` makes `@mesh`/`@accelerate` print which device handled each task |
| `GPUMESH_LOCAL` | `0` | Force local-only mode |
| `WORKER_REPLICAS` | `2` | Number of workers (compose) |

---

## 🔐 Security

| Feature | Status |
|---------|--------|
| Loopback by default | `serve` binds `127.0.0.1`; compose publishes to `127.0.0.1` |
| Non-root container | Runs as UID 10001, `cap_drop: ALL`, `no-new-privileges` |
| Token authentication | All API requests, including reads |
| Timing-safe comparison | HMAC `compare_digest` |
| Rate limiting | 5 failures → 15 min lockout (loopback exempt) |
| Process isolation | Tasks in subprocesses |
| File permissions | 0o600 on config files |
| Token hashing | SHA-256, in memory only — never written to database |

> ⚠️ **A token is a licence to execute code, not a password guarding data.** Anyone holding your URL and token runs arbitrary Python on every machine in the mesh. Traffic is **not encrypted**. Use `--tailscale` or `--public` (ngrok) when crossing untrusted networks.
>
> Read [SECURITY.md](https://github.com/K4-LABS/gpumesh/blob/master/SECURITY.md) and [THREAT_MODEL.md](https://github.com/K4-LABS/gpumesh/blob/master/THREAT_MODEL.md).

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

**Job flow:** Jobs are stored in SQLite. Workers pull tasks over HTTP with a lease (a crashed worker's task is automatically re-queued). Each task runs in an isolated subprocess. Results are posted back. The scheduler routes heavier tasks to stronger workers based on benchmark scores.

See [architecture.md](https://github.com/K4-LABS/gpumesh/blob/master/docs/architecture.md) for the full diagram.

---

## 📚 Documentation

- **GitHub:** [github.com/K4-LABS/gpumesh](https://github.com/K4-LABS/gpumesh)
- **PyPI:** [pypi.org/project/gpumesh](https://pypi.org/project/gpumesh/)
- **Issues:** [github.com/K4-LABS/gpumesh/issues](https://github.com/K4-LABS/gpumesh/issues)
- **HTTP API:** [docs/protocol.md](https://github.com/K4-LABS/gpumesh/blob/master/docs/protocol.md)
- **Stability:** [docs/stability.md](https://github.com/K4-LABS/gpumesh/blob/master/docs/stability.md)
- **Why not Ray or Dask?** [docs/why-not-ray-or-dask.md](https://github.com/K4-LABS/gpumesh/blob/master/docs/why-not-ray-or-dask.md)
- **Examples:** [examples/](https://github.com/K4-LABS/gpumesh/tree/master/examples)

---

## 🤝 Contributing

Contributions welcome! See [CONTRIBUTING.md](https://github.com/K4-LABS/gpumesh/blob/master/CONTRIBUTING.md) for guidelines.

---

## 📄 License

GNU AGPL-3.0 — see [LICENSE](https://github.com/K4-LABS/gpumesh/blob/master/LICENSE) for details.

---

<div align="center">

**Built with ❤️ by [Samurai007AK](https://github.com/Samurai007AK)**

</div>
