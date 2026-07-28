# ⚡ gpumesh

> **Borrow your friends' GPUs.** A distributed compute mesh that lets you share GPU power across machines on your network — with a single decorator, one CLI command, or a Python API.

[![PyPI version](https://img.shields.io/pypi/v/gpumesh.svg)](https://pypi.org/project/gpumesh/)
[![Python](https://img.shields.io/pypi/pyversions/gpumesh.svg)](https://pypi.org/project/gpumesh/)
[![License](https://img.shields.io/pypi/l/gpumesh.svg)](https://github.com/Samurai007AK/gpumesh/blob/main/LICENSE)
[![Tests](https://img.shields.io/badge/tests-137%20passed-brightgreen)](https://github.com/Samurai007AK/gpumesh)
[![Downloads](https://img.shields.io/pypi/dm/gpumesh)](https://pypi.org/project/gpumesh/)
[![Status](https://img.shields.io/badge/status-beta-blue)](https://github.com/Samurai007AK/gpumesh)

---

```ascii
                    ╔═══════════════════════════════════╗
                    ║       ⚡ gpumesh MESH ⚡           ║
                    ║   "Like Bluetooth for your GPUs"  ║
                    ╚═══════════════════════════════════╝

      ┌──────────────────┐          ┌──────────────────┐
      │  🖥️  RTX 4090     │          │  💻  RTX 3080     │
      │  Score: 120.5    │◄────────►│  Score: 85.2      │
      │  ─────────────── │          │  ────────────────  │
      │  @accelerate     │          │  runs tasks        │
      │  def train():    │          │  returns results   │
      └────────┬─────────┘          └────────┬───────────┘
               │                             │
               │        ┌──────────────────┐ │
               │        │  💻  T4          │ │
               │        │  Score: 12.0     │ │
               └────────┤  ─────────────── ├──┘
                        │  runs tasks      │
                        └──────────────────┘
                      ╔══════════════════════╗
                      ║  Results collected   ║
                      ║  automatically       ║
                      ╚══════════════════════╝
```

---

## 🚀 Quick Demo

```python
from gpumesh import GPUMesh, accelerate

mesh = GPUMesh("http://coordinator:8000", token="mysecret")

@accelerate(mesh)
def train(lr, epochs):
    return {"accuracy": 0.95}

# Single call → best local device
result = train(lr=0.01, epochs=100)

# Batch call → spread across ALL mesh devices
results = train.map([
    {"lr": 0.01, "epochs": 100},
    {"lr": 0.05, "epochs": 200},
])
```

---

## ✨ Features

### 🎯 Transparent Acceleration

```python
# One decorator turns any function into a mesh-distributed task:
@accelerate(mesh)
def preprocess(chunk_id, data_path):
    return {"chunk": chunk_id, "rows": len(df)}

# Automatically runs on the best device (CPU/GPU)
result = preprocess(chunk_id=0, data_path="data.parquet")

# Spread across ALL mesh devices in parallel
results = preprocess.map([
    {"chunk_id": 0, "data_path": "part0.parquet"},
    {"chunk_id": 1, "data_path": "part1.parquet"},
])
```

### 🔧 Smart Routing

```
┌─────────────┬─────────────────────────────────────────────┐
│  Scenario   │              What happens                    │
├─────────────┼─────────────────────────────────────────────┤
│ func(x)     │ Runs on best LOCAL device (CPU/GPU)          │
│ func.map()  │ Spreads across ALL mesh devices               │
│ Mesh down   │ Falls back to LOCAL execution silently        │
│ GPUMESH_LOCAL│ Forces local-only (no mesh)                  │
│ GPUMESH_VERBOSE│ Prints which device handled each task      │
└─────────────┴─────────────────────────────────────────────┘
```

### 🎛️ Hardware Selection

```python
# Target specific GPU types:
@accelerate(mesh, gpu="A100")
def train(model):
    return model.cuda().forward(x)

# Specify resource requirements:
@accelerate(mesh, cores=8, memory="16GB", timeout=300)
def heavy_computation(data):
    return processed
```

### 🧠 Auto Device Placement

PyTorch models are automatically placed on the best available device:

```python
@accelerate(mesh)
def train_model(model, data):
    return model(data)  # model auto-moved to best GPU
```

### 🛡️ Fault Tolerance

```
┌──────────────────────────────────────────────┐
│  ✅ Dead workers detected & tasks re-queued   │
│  ✅ Straggler workers deprioritized            │
│  ✅ Graceful fallback to local execution       │
│  ✅ Crash diagnostics on worker failures       │
│  ✅ TTL-based worker expiry (auto-prune)       │
│  ✅ Memory-aware scheduling (VRAM tracking)    │
└──────────────────────────────────────────────┘
```

### 📊 Benchmark Scoring

Each worker runs a benchmark on join and gets a 0-100 score:

```ascii
  Score Range    Typical GPU        Use Case
  ─────────────────────────────────────────────────
  80-100         RTX 4090, A100     Heavy training, large models
  50-80          RTX 3080, RTX 3090 Medium training, inference
  20-50          RTX 3060, T4       Light tasks, preprocessing
  0-20           CPU only           Very light tasks
```

---

## 📦 Installation

### Basic

```bash
pip install gpumesh
```

### With extras

```bash
pip install gpumesh[gpu]       # GPU detection + CUDA benchmarks (requires torch)
pip install gpumesh[tunnel]    # ngrok for public URLs
pip install gpumesh[sysinfo]   # System info (psutil)
pip install gpumesh[notebook]  # DataFrame support (pandas)
pip install gpumesh[ui]        # Beautiful setup wizard (rich + questionary)
pip install gpumesh[all]       # Everything above
```

### Requirements

- **Python 3.9+**
- **cloudpickle** (automatically installed)
- Optional: **PyTorch** for GPU detection and CUDA benchmarks

---

## 🏃 Quick Start

### Step 1: Start a coordinator (one machine)

```bash
gpumesh setup
```

The wizard will:
1. Detect your hardware (CPU cores, GPU model, VRAM)
2. Ask if you want to be a **coordinator** or **worker**
3. Generate a token and show connection info
4. Display a live radar of connected workers

Or start directly:

```bash
gpumesh serve --port 8000 --token mysecret
```

> **⚠️ Windows users**: Run `gpumesh serve` as **Administrator** for automatic firewall rules.

### Step 2: Join a worker (another machine)

```bash
gpumesh setup
```

Choose **Worker** and enter the coordinator's URL and token. Or:

```bash
gpumesh join http://coordinator-ip:8000 --token mysecret
gpumesh quickjoin http://coordinator-ip:8000 --token mysecret  # One-click
```

### Step 3: Use your mesh

```python
from gpumesh import GPUMesh, accelerate

mesh = GPUMesh("http://coordinator:8000", token="mysecret")

# -- Option A: @accelerate decorator (recommended) --

@accelerate(mesh)
def train(lr, epochs):
    return {"accuracy": 0.95}

result = train(lr=0.01, epochs=100)
results = train.map([{"lr": 0.01}, {"lr": 0.05}, {"lr": 0.1}])

# -- Option B: Python API --

def train_model(lr, epochs):
    return {"accuracy": 0.95, "lr": lr}

results = mesh.distribute(
    function=train_model,
    params=[{"lr": 0.01, "epochs": 100}, {"lr": 0.05, "epochs": 200}],
)

# -- Option C: CLI job submission --

# gpumesh submit train.py --payloads payloads.json --wait
# gpumesh status JOB_ID
# gpumesh cancel JOB_ID
```

---

## 📖 CLI Commands

### Server & Connection

```ascii
  Command                    Description
  ─────────────────────────────────────────────────────
  gpumesh setup              Interactive setup wizard
  gpumesh serve              Start coordinator server
  gpumesh join URL           Join mesh as a worker
  gpumesh quickjoin URL      One-click: detect GPU + join
  gpumesh worker             Start worker broadcasting
  gpumesh radar              Scan for nearby devices
  gpumesh show-connection    Show saved URL + token
  gpumesh disconnect         Clear saved connection
```

### Job Management

```ascii
  Command                    Description
  ─────────────────────────────────────────────────────
  gpumesh submit SCRIPT     Submit a Python script job
  gpumesh status JOB_ID     Check job progress
  gpumesh cancel JOB_ID     Cancel a running job
  gpumesh kill              Kill all tasks
```

### Monitoring

```ascii
  Command                    Description
  ─────────────────────────────────────────────────────
  gpumesh workers           List connected workers
  gpumesh devices           Show all GPUs as one pool
```

---

## 🔌 Python API

### GPUMesh Client

```python
from gpumesh import GPUMesh

mesh = GPUMesh("http://coordinator:8000", token="mysecret")
```

### Workers

```python
workers = mesh.workers()
# [
#     {'id': 'w1', 'device': 'cuda', 'device_name': 'RTX 3080',
#      'hostname': 'laptop-a', 'score': 85.0, 'alive': True},
#     {'id': 'w2', 'device': 'cuda', 'device_name': 'T4',
#      'hostname': 'server-b', 'score': 12.0, 'alive': True},
# ]
```

### Devices

```python
# List all devices (local + remote) as one unified pool
devices = mesh.devices()

# Count GPUs
count = mesh.device_count()

# Total compute score
total = mesh.total_score()

# Auto-pick best device
best = mesh.auto_device()
```

### Distribute Functions

```python
results = mesh.distribute(
    function=train_model,
    params=[{"lr": 0.01, "epochs": 100}, {"lr": 0.05, "epochs": 200}],
    timeout=600,
)
```

### Job Management

```python
# Submit a job
job_id = mesh.submit(name="preprocess", script="process.py",
                     payloads=[{"file": "data.csv"}])

# Check status
status = mesh.status(job_id)

# Convert to DataFrame
df = mesh.results_to_dataframe(results)
```

### Start Coordinator from Python

```python
GPUMesh.start_coordinator(port=8000, token="mysecret")
# Prints: http://192.168.1.10:8000
```

### Join as Worker from Python

```python
info = GPUMesh.add_worker("http://coordinator:8000", token="mysecret")
# {'device': 'cuda', 'device_name': 'RTX 3080', 'score': 85.234}
```

---

## 🎨 @accelerate Patterns

### Pattern 1: Basic

```python
@accelerate(mesh)
def preprocess(chunk_id, data_path):
    import pandas as pd
    df = pd.read_parquet(data_path)
    return {"chunk": chunk_id, "rows": len(df)}
```

### Pattern 2: Hardware Selection

```python
@accelerate(mesh, gpu="A100")
def train(model):
    return model.cuda().forward(x)
```

### Pattern 3: Resource Specs

```python
@accelerate(mesh, cores=8, memory="16GB", timeout=300)
def heavy_computation(data):
    return processed
```

### Pattern 4: Global Install (Import Hook)

```python
from gpumesh import accelerate

accelerate.install(mesh)  # Set global mesh

@accelerate  # No parentheses needed!
def train(lr, epochs):
    return {"accuracy": 0.95}
```

### Pattern 5: Bind to Device

```python
@accelerate(mesh)
def predict(x):
    return model(x)

gpu_predict = predict.to("cuda")
result = gpu_predict(x)
```

### Pattern 6: Mesh Fallback

```python
# If mesh is unreachable, falls back to local execution silently
@accelerate(mesh)
def train(lr, epochs):
    return {"accuracy": 0.95}

# Works even if coordinator is down — runs locally
result = train(lr=0.01, epochs=100)
```

---

## 🌐 Network Options

```ascii
  Method       Setup            Best For              Encrypted
  ─────────────────────────────────────────────────────────────
  LAN          None             Same Wi-Fi, fastest   No
  Tailscale    Install TS       Remote teams           Yes ✅
  ngrok        pip install      Public access, demos   Yes ✅
```

### LAN (Default)

No setup required. Workers discover the coordinator automatically via UDP broadcast.

```bash
# Coordinator
gpumesh serve --port 8000

# Worker (on another machine)
gpumesh join http://192.168.1.10:8000 --token mysecret
```

### Tailscale (Encrypted)

```bash
# Coordinator
gpumesh serve --port 8000 --tailscale

# Worker
gpumesh join http://tailscale-ip:8000 --token mysecret
```

### ngrok (Public URL)

```bash
# Coordinator
gpumesh serve --port 8000 --public
# Prints: ngrok tunnel → https://abc123.ngrok.io

# Worker
gpumesh join https://abc123.ngrok.io --token mysecret
```

---

## 🏗️ Architecture

```ascii
┌──────────────────────────────────────────────────────────────────┐
│                        COORDINATOR                                │
│                                                                   │
│   ┌──────────┐    ┌──────────┐    ┌──────────┐   ┌──────────┐    │
│   │Job Queue │    │ Task DB  │    │ Workers  │   │ Events   │    │
│   │          │    │ (SQLite) │    │ Registry │   │ Log      │    │
│   └────┬─────┘    └──────────┘    └────┬─────┘   └──────────┘    │
│        │                               │                          │
│        └───────────────┬───────────────┘                          │
│                        │                                          │
│               HTTP API (port 8000)                                │
└────────────────────────┼──────────────────────────────────────────┘
                         │
            ┌────────────┼────────────┐
            │            │            │
      ┌─────▼────┐ ┌────▼────┐ ┌────▼────┐
      │ Worker 1 │ │Worker 2 │ │Worker 3 │
      │ RTX 4090 │ │RTX 3080 │ │   T4    │
      │Score:120 │ │Score:85 │ │Score:12 │
      └──────────┘ └─────────┘ └─────────┘
```

### Job Flow

```ascii
  Submit → Queue → Claim → Execute → Report → Collect
    │        │        │        │        │        │
    ▼        ▼        ▼        ▼        ▼        ▼
  Python   SQLite   Worker   Subprocess JSON    Client
  script   storage  pulls    runs task  result  receives
           │        │        │          │        │
           └────────┴────────┴──────────┴────────┘
                Fully automated pipeline
```

---

## 🔒 Security

```ascii
  Feature                    Status
  ─────────────────────────────────────────────
  Token authentication      ✅ All API requests
  Timing-safe comparison    ✅ HMAC compare_digest
  Rate limiting             ✅ 5 failures → blocked
  Process isolation         ✅ Tasks in subprocesses
  File permissions          ✅ 0o600 on token files
  Token hashing             ✅ SHA-256 + salt
```

> **⚠️ Workers execute code from the coordinator.** Only share your URL and token with people you trust. gpumesh is designed for **trusted networks** (home labs, team clusters).

---

## 🧰 Troubleshooting

```ascii
  Problem                            Fix
  ────────────────────────────────────────────────────────────
  command not found: gpumesh         Use python -m gpumesh or check PATH
  401 bad token                      Same token on coordinator & worker
  coordinator unreachable            Check firewall, is coordinator running?
  task timed out                     Increase --timeout or split tasks
  Windows connection error           Run as Administrator for firewall rules
  Worker not showing                 Both on same network? Try gpumesh radar
  ModuleNotFoundError: torch         pip install gpumesh[gpu]
  UDP broadcast not working          Use gpumesh join URL directly
```

### Verbose Logging

```bash
GPUMESH_VERBOSE=1 gpumesh serve
GPUMESH_VERBOSE=1 gpumesh join http://coordinator:8000 --token mysecret
```

### Force Local-Only Mode

```bash
GPUMESH_LOCAL=1 python my_script.py
```

---

## 🧪 Development

```bash
git clone https://github.com/Samurai007AK/gpumesh.git
cd gpumesh
pip install -e ".[dev]"
pytest
```

### Running Tests

```bash
pytest                    # Run all tests
pytest tests/test_api.py  # Run specific test file
pytest -v                 # Verbose output
```

### Building

```bash
python -m build
twine check dist/*
```

---

## ⚠️ Limitations

- **Python only** — Tasks must be Python functions or scripts
- **No GPU memory sharing** — Each task gets its own process
- **No model sharding** — Each task runs on one machine at a time
- **Single coordinator** — Single point of failure (use Tailscale for reliability)
- **No built-in encryption** — Use Tailscale for encrypted tunnels

---

## 📄 License

MIT License. See [LICENSE](LICENSE) for details.

---

<div align="center">

**Made with ⚡ by the gpumesh community**

[GitHub](https://github.com/Samurai007AK/gpumesh) • [Issues](https://github.com/Samurai007AK/gpumesh/issues) • [PyPI](https://pypi.org/project/gpumesh/)

</div>
