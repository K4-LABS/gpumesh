# gpumesh

> Borrow your friends' GPUs. A distributed compute mesh that lets you share GPU power across machines on your network -- with a single decorator, one CLI command, or a Python API.

[![PyPI version](https://img.shields.io/pypi/v/gpumesh.svg)](https://pypi.org/project/gpumesh/)
[![Python](https://img.shields.io/pypi/pyversions/gpumesh.svg)](https://pypi.org/project/gpumesh/)
[![License](https://img.shields.io/pypi/l/gpumesh.svg)](https://github.com/Samurai007AK/gpumesh/blob/main/LICENSE)
[![Tests](https://img.shields.io/badge/tests-137%20passed-brightgreen)](https://github.com/Samurai007AK/gpumesh)
[![Status](https://img.shields.io/badge/status-beta-blue)](https://github.com/Samurai007AK/gpumesh)

---

```ascii
  ╔═══════════════════════════════════════════════════════════╗
  ║              gpumesh - GPU Mesh Network                   ║
  ║        "like Bluetooth, but for your GPUs"                ║
  ╚═══════════════════════════════════════════════════════════╝

         ┌─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┐
         │          NETWORK TRAFFIC FLOW              │
         │                                           │
         │   ┌──────────┐     ┌──────────┐           │
         │   │ RTX 4090 │◄───►│ RTX 3080 │           │
         │   │  Server  │     │  Laptop  │           │
         │   │120.5 G/s │     │ 85.2 G/s │           │
         │   └────┬─────┘     └────┬─────┘           │
         │        │                │                  │
         │   ┌────▼────────────────▼─────┐            │
         │   │        T4 (12.0)          │            │
         │   │      running tasks        │            │
         │   └───────────────────────────┘            │
         │                                           │
         │   >>> results collected automatically     │
         └─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┘
```

---

## What is gpumesh?

gpumesh turns multiple machines into a single, unified GPU compute pool. Start a **coordinator** on one machine, join **workers** from other machines (laptops, desktops, servers -- anything with Python), and run code across all of them as if they were one device.

**Use cases:**
- Hyperparameter search across multiple GPUs
- Data preprocessing sharded across machines
- Model training on a pool of consumer GPUs
- Any embarrassingly parallel workload

---

## Quick Demo

```python
from gpumesh import GPUMesh, accelerate

mesh = GPUMesh("http://coordinator:8000", token="mysecret")

@accelerate(mesh)
def train(lr, epochs):
    return {"accuracy": 0.95}

result = train(lr=0.01, epochs=100)        # best local device

results = train.map([                       # all mesh devices
    {"lr": 0.01, "epochs": 100},
    {"lr": 0.05, "epochs": 200},
])
```

---

## Features

### Transparent Acceleration

```python
@accelerate(mesh)
def preprocess(chunk_id, data_path):
    return {"chunk": chunk_id, "rows": len(df)}

result = preprocess(chunk_id=0, data_path="data.parquet")

results = preprocess.map([
    {"chunk_id": 0, "data_path": "part0.parquet"},
    {"chunk_id": 1, "data_path": "part1.parquet"},
])
```

### Smart Routing

```
+---------------------------+-------------------------------------------+
| Scenario                  | What happens                              |
+---------------------------+-------------------------------------------+
| func(x)                   | Runs on best LOCAL device (CPU/GPU)       |
| func.map()                | Spreads across ALL mesh devices           |
| Mesh unreachable          | Falls back to LOCAL execution silently    |
| GPUMESH_LOCAL=1           | Forces local-only (no mesh)               |
| GPUMESH_VERBOSE=1         | Prints which device handled each task     |
+---------------------------+-------------------------------------------+
```

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

### Auto Device Placement

PyTorch models are automatically placed on the best available device:

```python
@accelerate(mesh)
def train_model(model, data):
    return model(data)    # model auto-moved to best GPU
```

### Fault Tolerance

```
  +----------------------------------------------------+
  |  [] Dead workers detected and tasks re-queued       |
  |  [] Straggler workers deprioritized                 |
  |  [] Graceful fallback to local execution            |
  |  [] Crash diagnostics on worker failures            |
  |  [] TTL-based worker expiry (auto-prune)            |
  |  [] Memory-aware scheduling (VRAM tracking)         |
  +----------------------------------------------------+
```

### Benchmark Scoring

Each worker runs a benchmark on join and gets a 0-100 score:

```
  Score      Typical GPU        Use Case
  -----      ------------        --------
  80-100     RTX 4090, A100     Heavy training, large models
  50-80      RTX 3080, 3090     Medium training, inference
  20-50      RTX 3060, T4       Light tasks, preprocessing
  0-20       CPU only           Very light tasks
```

### Live Radar (Network Discovery)

```ascii
   RADAR - Scanning for nearby workers
  ╔══════════════════════════════════════════╗
  ║  [+] laptop-a    RTX 3080   85.2 GFLOP/s║
  ║  [+] server-b    RTX 4090   120.5 GFLOP/s║
  ║  [ ] desktop-c   T4         12.0 GFLOP/s║
  ║  [+] macbook     MPS        45.3 GFLOP/s║
  ╚══════════════════════════════════════════╝
   Network: 4 nodes | 3 GPUs | 262.0 GFLOP/s
```

The `gpumesh radar` command scans your local network and shows nearby devices with live updates -- no manual configuration needed.

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

**Requires:** Python 3.9+, cloudpickle (auto-installed). PyTorch optional for GPU detection.

---

## Quick Start

### 1. Start a coordinator (one machine)

```bash
gpumesh setup
```

The wizard detects your hardware, generates a token, and shows connection info. Or start directly:

```bash
gpumesh serve --port 8000 --token mysecret
```

> Windows: Run `gpumesh serve` as Administrator for automatic firewall rules.

### 2. Join a worker (another machine)

```bash
gpumesh setup   # choose Worker, enter URL + token
gpumesh join http://coordinator-ip:8000 --token mysecret
gpumesh quickjoin http://coordinator-ip:8000 --token mysecret  # one-click
```

### 3. Use your mesh

```python
from gpumesh import GPUMesh, accelerate

mesh = GPUMesh("http://coordinator:8000", token="mysecret")

# Option A: @accelerate decorator
@accelerate(mesh)
def train(lr, epochs):
    return {"accuracy": 0.95}

result = train(lr=0.01, epochs=100)
results = train.map([{"lr": 0.01}, {"lr": 0.05}, {"lr": 0.1}])

# Option B: Python API
results = mesh.distribute(
    function=train_model,
    params=[{"lr": 0.01, "epochs": 100}, {"lr": 0.05, "epochs": 200}],
)

# Option C: CLI
# gpumesh submit train.py --payloads payloads.json --wait
```

---

## CLI Commands

### Server & Connection

```
  Command                    Description
  -------------------------  --------------------------------------------
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

```
  Command                    Description
  -------------------------  --------------------------------------------
  gpumesh submit SCRIPT      Submit a Python script job
  gpumesh status JOB_ID      Check job progress
  gpumesh cancel JOB_ID      Cancel a running job
  gpumesh kill               Kill all tasks
```

### Monitoring

```
  Command                    Description
  -------------------------  --------------------------------------------
  gpumesh workers            List connected workers
  gpumesh devices            Show all GPUs as one pool
```

---

## Python API

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
devices = mesh.devices()       # unified pool view
count   = mesh.device_count()  # total GPUs
total   = mesh.total_score()   # combined compute score
best    = mesh.auto_device()   # pick most powerful
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
job_id = mesh.submit(name="preprocess", script="process.py",
                     payloads=[{"file": "data.csv"}])
status = mesh.status(job_id)
df     = mesh.results_to_dataframe(results)  # requires pandas
```

### From Python (non-blocking)

```python
GPUMesh.start_coordinator(port=8000, token="mysecret")
GPUMesh.add_worker("http://coordinator:8000", token="mysecret")
```

---

## @accelerate Patterns

### Basic

```python
@accelerate(mesh)
def preprocess(chunk_id, data_path):
    import pandas as pd
    df = pd.read_parquet(data_path)
    return {"chunk": chunk_id, "rows": len(df)}
```

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

### Global Install (Import Hook)

```python
from gpumesh import accelerate

accelerate.install(mesh)

@accelerate     # no parentheses needed
def train(lr, epochs):
    return {"accuracy": 0.95}
```

### Bind to Device

```python
gpu_predict = predict.to("cuda")
result = gpu_predict(x)
```

### Mesh Fallback

If the mesh is unreachable, @accelerate falls back to local execution silently:

```python
@accelerate(mesh)
def train(lr, epochs):
    return {"accuracy": 0.95}

result = train(lr=0.01, epochs=100)   # works even offline
```

---

## Network Options

```
  Method       Setup            Best For              Encrypted
  ------       -----            --------              ---------
  LAN          None             Same Wi-Fi, fastest   No
  Tailscale    Install TS       Remote teams           Yes
  ngrok        pip install      Public access, demos   Yes
```

### LAN (Default)

No setup required. Workers discover the coordinator automatically via UDP broadcast.

```bash
# Coordinator
gpumesh serve --port 8000

# Worker
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
# Prints: ngrok tunnel -> https://abc123.ngrok.io

# Worker
gpumesh join https://abc123.ngrok.io --token mysecret
```

---

## Architecture

```ascii
  ╔══════════════════════════════════════════════════════════════╗
  ║                    SYSTEM ARCHITECTURE                       ║
  ╚══════════════════════════════════════════════════════════════╝

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

  JOB FLOW:
  ┌──────┐   ┌──────┐   ┌──────┐   ┌──────┐   ┌──────┐   ┌──────┐
  │Submit│──►│Queue │──►│Claim │──►│Execute│──►│Report│──►│Collect│
  └──────┘   └──────┘   └──────┘   └──────┘   └──────┘   └──────┘
      │          │          │          │          │          │
      ▼          ▼          ▼          ▼          ▼          ▼
   Python     SQLite     Worker     Subprocess   JSON       Client
   script     storage    pulls      runs task    result     receives
```

---

## Security

```
  Feature                    Status
  -----------------------    ----------------------------
  Token authentication       All API requests
  Timing-safe comparison    HMAC compare_digest
  Rate limiting             5 failures -> blocked
  Process isolation         Tasks in subprocesses
  File permissions          0o600 on token files
  Token hashing             SHA-256 + salt
```

> Workers execute code from the coordinator. Only share your URL and token with people you trust. gpumesh is designed for trusted networks (home labs, team clusters).

---

## Troubleshooting

```
  Problem                            Fix
  ------------------------           ------------------------------------
  command not found: gpumesh         Use python -m gpumesh or check PATH
  401 bad token                      Same token on coordinator and worker
  coordinator unreachable            Check firewall, is coordinator running?
  task timed out                     Increase --timeout or split tasks
  Windows connection error           Run as Administrator for firewall
  Worker not showing                 Both on same network? Try gpumesh radar
  ModuleNotFoundError: torch         pip install gpumesh[gpu]
  UDP broadcast not working          Use gpumesh join URL directly
```

### Verbose Logging

```bash
GPUMESH_VERBOSE=1 gpumesh serve
GPUMESH_VERBOSE=1 gpumesh join http://coordinator:8000 --token mysecret
```

### Force Local-Only

```bash
GPUMESH_LOCAL=1 python my_script.py
```

---

## Development

```bash
git clone https://github.com/Samurai007AK/gpumesh.git
cd gpumesh
pip install -e ".[dev]"
pytest
```

### Running Tests

```bash
pytest                    # all tests
pytest tests/test_api.py  # specific file
pytest -v                 # verbose
```

### Building

```bash
python -m build
twine check dist/*
```

---

## Limitations

- Python only -- tasks must be Python functions or scripts
- No GPU memory sharing -- each task gets its own process
- No model sharding -- each task runs on one machine at a time
- Single coordinator -- single point of failure (use Tailscale for reliability)
- No built-in encryption -- use Tailscale for encrypted tunnels

---

## License

MIT License. See [LICENSE](LICENSE) for details.

---

[GitHub](https://github.com/Samurai007AK/gpumesh) - [Issues](https://github.com/Samurai007AK/gpumesh/issues) - [PyPI](https://pypi.org/project/gpumesh/)
