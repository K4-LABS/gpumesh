# gpumesh

> Borrow your friends' GPUs. A distributed compute mesh that lets you share GPU power across machines.

[![PyPI version](https://img.shields.io/pypi/v/gpumesh.svg)](https://pypi.org/project/gpumesh/)
[![Python](https://img.shields.io/pypi/pyversions/gpumesh.svg)](https://pypi.org/project/gpumesh/)
[![License](https://img.shields.io/pypi/l/gpumesh.svg)](https://github.com/Samurai007AK/gpumesh/blob/main/LICENSE)

---

## Features

- **Transparent acceleration** — Add `@accelerate` to any function, it uses all connected GPUs automatically
- **One-line setup** — `gpumesh setup` handles everything
- **Auto-discovery** — Workers find coordinators on the same network
- **Smart scheduling** — Fast GPUs get heavy tasks, slow ones get light tasks
- **Fault tolerance** — Dead workers are detected, tasks are re-queued
- **Python API** — Distribute functions from Jupyter notebooks

---

## Installation

```bash
pip install gpumesh
```

Optional extras:

```bash
pip install gpumesh[gpu]       # GPU detection + CUDA benchmarks
pip install gpumesh[tunnel]    # ngrok for public URLs
pip install gpumesh[ui]        # Beautiful setup wizard (rich + questionary)
pip install gpumesh[all]       # everything
```

Requires Python 3.9+.

---

## Quick Start

### 1. Start a coordinator (one machine)

```bash
gpumesh setup
# Choose option 1 (Coordinator)
# The wizard detects your hardware and shows a radar
```

### 2. Join a worker (another machine)

```bash
gpumesh setup
# Choose option 2 (Worker)
# Enter a token, start broadcasting
# The coordinator claims you from the radar
```

### 3. Use transparent acceleration

```python
from gpumesh import GPUMesh, accelerate

mesh = GPUMesh("http://coordinator:8000", token="mysecret")

@accelerate(mesh)
def train(lr, epochs):
    # Your code here — runs on all connected GPUs automatically
    return {"accuracy": 0.95}

# Single call → best local device
result = train(lr=0.01, epochs=100)

# Batch call → spread across all mesh devices
results = train.map([
    {"lr": 0.01, "epochs": 100},
    {"lr": 0.05, "epochs": 200},
])
```

---

## Transparent Acceleration

The `@accelerate` decorator makes your mesh resources transparent to your code:

```python
from gpumesh import GPUMesh, accelerate

mesh = GPUMesh("http://coordinator:8000", token="mysecret")

@accelerate(mesh)
def preprocess(chunk_id, data_path):
    import pandas as pd
    df = pd.read_parquet(data_path)
    return {"chunk": chunk_id, "rows": len(df)}

# Single call → runs locally on best device
result = preprocess(chunk_id=0, data_path="data.parquet")

# Batch call → spreads across ALL mesh devices
results = preprocess.map([
    {"chunk_id": 0, "data_path": "part0.parquet"},
    {"chunk_id": 1, "data_path": "part1.parquet"},
])
```

### How it works

| Scenario | What happens |
|----------|-------------|
| Single call `func(x)` | Runs on best **local** device (CPU/GPU) |
| Batch call `func.map([...])` | Spreads across **all** mesh devices |
| Mesh unreachable | Falls back to **local** execution silently |
| `GPUMESH_LOCAL=1` | Forces local-only (no mesh) |
| `GPUMESH_VERBOSE=1` | Prints which device handled each task |

---

## CLI Commands

| Command | Description |
|---------|-------------|
| `gpumesh setup` | Interactive setup wizard |
| `gpumesh serve` | Start coordinator |
| `gpumesh join URL` | Join as worker |
| `gpumesh quickjoin` | One-click: detect GPU and join mesh |
| `gpumesh radar` | Scan for nearby gpumesh devices |
| `gpumesh worker` | Start a worker that broadcasts and waits to be claimed |
| `gpumesh submit SCRIPT --payloads FILE` | Submit a job |
| `gpumesh status JOB_ID` | Check job progress |
| `gpumesh cancel JOB_ID` | Cancel a job |
| `gpumesh workers` | List connected workers |
| `gpumesh devices` | Show all GPUs as one pool |
| `gpumesh show-connection` | Show saved URL and token for sharing |
| `gpumesh disconnect` | Clear saved connection |
| `gpumesh kill` | Kill all gpumesh tasks (graceful or force) |

---

## Python API

```python
from gpumesh import GPUMesh

mesh = GPUMesh("http://coordinator:8000", token="mysecret")

# List workers
mesh.workers()

# Distribute a function across all workers
results = mesh.distribute(
    function=train_model,
    params=[
        {"lr": 0.01, "epochs": 100},
        {"lr": 0.05, "epochs": 200},
    ],
)
```

---

## Network Options

| Method | Setup | Best for |
|--------|-------|----------|
| **LAN** | None | Same Wi-Fi, fastest |
| **Tailscale** | Install Tailscale | Remote teams |
| **ngrok** | `pip install gpumesh[tunnel]` | Public access |

---

## Limitations

- **Python only** — Tasks must be Python scripts
- **No GPU memory sharing** — Each task gets its own process
- **No model sharding** — Each task runs on one machine
- **Single coordinator** — Single point of failure

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `command not found` | Use `python -m gpumesh` instead |
| `401 bad token` | Check coordinator and worker tokens match |
| `coordinator unreachable` | Check firewall and that coordinator is running |
| `task timed out` | Increase `--timeout 600` or split into smaller tasks |

---

## Security

- Token authentication on all API requests
- Tokens stored with restricted file permissions (0o600 on Unix, icacls on Windows)
- Rate limiting after 5 failed attempts
- Process isolation for all tasks

**Important:** Workers execute code from the coordinator. Only share your URL and token with people you trust.

### Security Model

gpumesh is designed for **trusted networks** (home labs, team clusters). Key security considerations:

- **Code execution**: Function tasks (`@accelerate`) execute in the worker's process. Only connect to machines you trust.
- **Plaintext HTTP**: All communication uses HTTP. Use Tailscale (`--tailscale`) for encrypted tunnels across untrusted networks.
- **Token authentication**: A shared token authenticates all communication. Keep it secret.
- **No sandbox**: Tasks have full access to the worker machine. Do not run untrusted code.

---

## Contributing

```bash
git clone https://github.com/Samurai007AK/gpumesh.git
cd gpumesh
pip install -e ".[dev]"
pytest
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.
