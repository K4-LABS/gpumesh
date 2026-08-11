# 🖥️ gpumesh — Borrow Your Friends' GPUs

> *Like Bluetooth, but for your GPUs*

[![PyPI version](https://img.shields.io/pypi/v/gpumesh.svg)](https://pypi.org/project/gpumesh/)
[![Python](https://img.shields.io/pypi/pyversions/gpumesh.svg)](https://pypi.org/project/gpumesh/)
[![License](https://img.shields.io/pypi/l/gpumesh.svg)](https://github.com/Samurai007AK/gpumesh/blob/main/LICENSE)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/r/samurai007ak/gpumesh)

---

```
  ╔═══════════════════════════════════════════════════════════════╗
  ║              🖥️  gpumesh — GPU Mesh Network                  ║
  ║        "like Bluetooth, but for your GPUs"                    ║
  ╚═══════════════════════════════════════════════════════════════╝

         ┌─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┐
         │          NETWORK TRAFFIC FLOW                │
         │                                             │
         │   ┌──────────┐     ┌──────────┐             │
         │   │ RTX 4090 │◄───►│ RTX 3080 │             │
         │   │  Server  │     │  Laptop  │             │
         │   │120.5 G/s │     │ 85.2 G/s │             │
         │   └────┬─────┘     └────┬─────┘             │
         │        │                │                    │
         │   ┌────▼────────────────▼─────┐              │
         │   │        T4 (12.0)          │              │
         │   │      running tasks        │              │
         │   └───────────────────────────┘              │
         │                                             │
         │   >>> results collected automatically       │
         └─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┘
```

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

## ⚡ Quick Start (30 seconds)

### 1️⃣ Start a Coordinator

```bash
docker run -d \
  --name gpumesh-coordinator \
  -p 8732:8732 \
  -e GPUMESH_TOKEN=mysecret \
  samurai007ak/gpumesh:latest \
  serve --port 8732 --token mysecret
```

### 2️⃣ Join a Worker

```bash
docker run -d \
  --name gpumesh-worker \
  -e GPUMESH_URL=http://coordinator-ip:8732 \
  -e GPUMESH_TOKEN=mysecret \
  samurai007ak/gpumesh:latest \
  join http://coordinator-ip:8732 --token mysecret
```

### 3️⃣ Use Your Mesh

```python
from gpumesh import GPUMesh, accelerate

mesh = GPUMesh("http://coordinator:8732", token="mysecret")

@accelerate(mesh)
def train(lr, epochs):
    return {"accuracy": 0.95}

result = train(lr=0.01, epochs=100)
results = train.map([{"lr": 0.01}, {"lr": 0.05}, {"lr": 0.1}])
```

---

## 🐳 Docker Images

| Tag | Description | Size |
|-----|-------------|------|
| `latest` | Latest stable release | ~50 MB (compressed) |
| `1.1.0` | Version 1.1.0 (robustness release) | ~50 MB (compressed) |

### Pull Commands

```bash
# Latest version
docker pull samurai007ak/gpumesh:latest

# Specific version
docker pull samurai007ak/gpumesh:1.1.0
```

---

## 🎯 Docker Compose (Recommended)

Create a `docker-compose.yml`:

```yaml
services:
  coordinator:
    image: samurai007ak/gpumesh:latest
    ports:
      - "8732:8732"
      - "48900:48900/udp"
    environment:
      - GPUMESH_COLOR=1
    # `serve` does not read GPUMESH_TOKEN from the environment (unlike
    # `join`), so pass it explicitly — otherwise the coordinator starts with a
    # random token and no worker can authenticate against it.
    command: serve --port 8732 --color --token mysecret
    volumes:
      - gpumesh_data:/root/.gpumesh
    healthcheck:
      test: ["CMD", "nc", "-z", "localhost", "8732"]
      interval: 10s
      timeout: 3s
      retries: 3

  worker:
    image: samurai007ak/gpumesh:latest
    depends_on:
      coordinator:
        condition: service_healthy
    environment:
      - GPUMESH_TOKEN=mysecret
      - GPUMESH_COLOR=1
    # The coordinator URL is a required positional argument — `join` on its
    # own exits with "the following arguments are required: url".
    command: join http://coordinator:8732 --color
    deploy:
      replicas: 2
    restart: unless-stopped

volumes:
  gpumesh_data:
```

Then run:

```bash
# Start coordinator + 2 workers
docker compose up -d

# Scale to 4 workers
docker compose up -d --scale worker=4

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
| `func(x)` | Runs on best LOCAL device |
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

Each worker runs a benchmark on join and gets a **0-100 score**:

```
  Score      Typical GPU        Use Case
  ─────      ───────────        ────────
  80-100     RTX 4090, A100     Heavy training, large models
  50-80      RTX 3080, 3090     Medium training, inference
  20-50      RTX 3060, T4       Light tasks, preprocessing
  0-20       CPU only           Very light tasks
```

---

## 🛠️ Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GPUMESH_TOKEN` | (required) | Authentication token |
| `GPUMESH_URL` | (worker only) | Coordinator URL |
| `GPUMESH_PORT` | `8732` | Coordinator port |
| `GPUMESH_COLOR` | `1` | Enable colored output |
| `GPUMESH_VERBOSE` | `0` | Verbose logging |
| `GPUMESH_LOCAL` | `0` | Force local-only mode |
| `WORKER_REPLICAS` | `2` | Number of workers (compose) |

---

## 🔐 Security

| Feature | Status |
|---------|--------|
| Token authentication | ✅ All API requests |
| Timing-safe comparison | ✅ HMAC compare_digest |
| Rate limiting | ✅ 5 failures → blocked |
| Process isolation | ✅ Tasks in subprocesses |
| File permissions | ✅ 0o600 on token files |
| Token hashing | ✅ SHA-256 + salt |

> ⚠️ Workers execute code from the coordinator. Only share your URL and token with people you trust.

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

- **GitHub:** [github.com/Samurai007AK/gpumesh](https://github.com/Samurai007AK/gpumesh)
- **PyPI:** [pypi.org/project/gpumesh](https://pypi.org/project/gpumesh/)
- **Issues:** [github.com/Samurai007AK/gpumesh/issues](https://github.com/Samurai007AK/gpumesh/issues)

---

## 🤝 Contributing

Contributions are welcome! See the [GitHub repository](https://github.com/Samurai007AK/gpumesh) for guidelines.

---

## 📄 License

MIT License — see [LICENSE](https://github.com/Samurai007AK/gpumesh/blob/main/LICENSE) for details.

---

**Built with ❤️ by [samurai007ak](https://github.com/Samurai007AK)**
