# gpumesh

Borrow your friends' GPUs. A distributed compute mesh that lets you share GPU power across machines using Python.

```
Machine A (coordinator)              Machine B (worker)
┌─────────────────────┐   HTTP/JSON   ┌─────────────────────┐
│  job queue           │◄────────────►│  hardware probe      │
│  scheduler           │  (optional   │  matmul benchmark    │
│  SQLite (WAL)        │   ngrok)     │  sandboxed execution │
│  heartbeat + reaper  │              │                      │
└──────────▲───────────┘              └──────────────────────┘
           │ submit / status
       CLI or Python API
```

---

## What can you do with this?

- Run hyperparameter searches across multiple GPUs
- Process data in parallel on different machines
- Split any data-parallel workload across a team's hardware
- Use idle GPUs sitting around in your lab or office

**What it is not:**
- It does not shard model training (no data-parallel PyTorch training)
- It does not handle GPU memory sharing
- It does not replace SLURM or Kubernetes for production clusters
- It is designed for small teams (2-20 machines), not large data centers

---

## Installation

```bash
pip install gpumesh
```

Optional extras:

```bash
pip install gpumesh[gpu]       # GPU detection and CUDA benchmarks
pip install gpumesh[tunnel]    # ngrok support for public URLs
pip install gpumesh[sysinfo]   # RAM reporting
pip install gpumesh[notebook]  # Python API for Jupyter/Colab
pip install gpumesh[gpu,tunnel,sysinfo,notebook]  # everything
```

Requires Python 3.9+.

---

## Quick Start

**1. Start a coordinator (one machine):**

```bash
gpumesh serve --token mySecret
```

Output:

```
[mesh] coordinator listening on 0.0.0.0:8000
[mesh] token: mySecret
[mesh] LAN join command:
       gpumesh join http://192.168.1.10:8000 --token mySecret
[mesh] Ctrl+C to stop
```

**2. Join a worker (another machine):**

```bash
gpumesh join http://192.168.1.10:8000 --token mySecret
```

Output:

```
[worker] device=cuda (NVIDIA GeForce RTX 3080) score=85.234 GFLOP/s
[worker] joined mesh as w1
```

**3. Submit a job:**

```bash
export GPUMESH_URL=http://192.168.1.10:8000
export GPUMESH_TOKEN=mySecret

gpumesh submit examples/grid_search.py --payloads examples/payloads.json --wait
```

---

## Setup Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          GPUMESH SETUP FLOW                                 │
│                          gpumesh setup                                      │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
                    ┌───────────────────────────────┐
                    │   What do you want to do?     │
                    │                               │
                    │   1) Coordinator (Manage)     │
                    │   2) Worker (Join)            │
                    └───────────────────────────────┘
                           │                 │
            ┌──────────────┘                 └──────────────┐
            ▼                                               ▼
┌───────────────────────────┐                 ┌───────────────────────────┐
│    COORDINATOR FLOW       │                 │       WORKER FLOW         │
│                           │                 │                           │
│  "How will others         │                 │  "How are you             │
│   connect to this one?"   │                 │   connecting?"            │
│                           │                 │                           │
│   1) Same WiFi/LAN       │                 │   1) Same WiFi/LAN       │
│   2) Tailscale            │                 │   2) Tailscale            │
└───────────────────────────┘                 └───────────────────────────┘
            │                                               │
     ┌──────┴──────┐                                 ┌──────┴──────┐
     ▼             ▼                                 ▼             ▼
┌─────────┐   ┌─────────┐                       ┌─────────┐   ┌─────────┐
│ SAME    │   │ TAIL-   │                       │ SAME    │   │ TAIL-   │
│ NETWORK │   │ SCALE   │                       │ NETWORK │   │ SCALE   │
└─────────┘   └─────────┘                       └─────────┘   └─────────┘
     │             │                                 │             │
     ▼             ▼                                 ▼             ▼
┌───────────────────────────┐                 ┌───────────────────────────┐
│ COORDINATOR:              │                 │ WORKER:                   │
│                           │                 │                           │
│ Shows:                    │                 │ SAME NETWORK:             │
│ ┌───────────────────────┐ │                 │   Enter coordinator IP    │
│ │ YOUR CONNECTION INFO  │ │                 │   (e.g. 192.168.1.10)    │
│ │                       │ │                 │   Then enter token        │
│ │ URL: http://IP:8000   │ │                 │                           │
│ │ Token: xyz123         │ │                 │ TAILSCALE:                │
│ └───────────────────────┘ │                 │   Auto-detect coordinator │
│                           │                 │   OR enter URL manually   │
│ Command to run:           │                 │   Then enter token        │
│ $ gpumesh serve \         │                 └───────────────────────────┘
│   --port 8000 \           │                                 │
│   --token xyz123          │                                 ▼
│                           │                 ┌───────────────────────────┐
│ For friends to join:      │                 │ WORKER JOINS MESH         │
│ $ gpumesh quickjoin \     │                 │                           │
│   http://IP:8000 \        │                 │ Shows:                    │
│   --token xyz123          │                 │ - Device detected         │
└───────────────────────────┘                 │ - GPU info                │
                                              │ - Score (GFLOP/s)         │
                                              │ - Status: Connected ✓     │
                                              └───────────────────────────┘
```

### Quick Cheat Sheet

| Step | Coordinator | Worker |
|------|-------------|--------|
| **Setup** | `gpumesh setup` → Choose 1 | `gpumesh setup` → Choose 2 |
| **Start** | `gpumesh serve --port 8000 --token TOKEN --tailscale` | (auto-joins) |
| **Connect** | (runs server) | `gpumesh quickjoin --token TOKEN --tailscale` |
| **Check** | `gpumesh workers` | `gpumesh status` |
| **Share** | `gpumesh show-connection` | - |
| **Submit** | `gpumesh submit script.py --payloads data.json --wait` | - |

---

## CLI Commands

| Command | What it does |
|---------|-------------|
| `gpumesh serve` | Start coordinator on this machine |
| `gpumesh join URL` | Join a mesh as a worker |
| `gpumesh quickjoin URL` | One-click setup: install, detect GPU, join |
| `gpumesh submit SCRIPT --payloads FILE` | Submit a job |
| `gpumesh status JOB_ID` | Check job progress |
| `gpumesh cancel JOB_ID` | Cancel a running job |
| `gpumesh workers` | List connected workers |
| `gpumesh disconnect` | Clear saved connection |
| `gpumesh setup` | Interactive setup wizard |
| `gpumesh --version` | Show version |
| `gpumesh --help` | Show help |

### serve options

| Option | Default | Description |
|--------|---------|-------------|
| `--port` | 8000 | Port to listen on |
| `--token` | random | Auth token |
| `--db` | gpumesh.db | Database file path |
| `--public` | off | Expose via ngrok |
| `--tailscale` | off | Auto-detect Tailscale IP |

### join options

| Option | Default | Description |
|--------|---------|-------------|
| `--token` | "" | Auth token |
| `--timeout` | 240 | Per-task timeout (seconds) |

### submit options

| Option | Default | Description |
|--------|---------|-------------|
| `--payloads` | required | JSON file with task parameters |
| `--url` | "" | Coordinator URL |
| `--token` | "" | Auth token |
| `--name` | script filename | Job name |
| `--wait` | off | Wait for completion |

After first `gpumesh join`, `--url` and `--token` are saved automatically to `~/.gpumesh/config.json`. You can skip them on future commands.

**Priority order:** Explicit `--url`/`--token` flags > environment variables (`GPUMESH_URL`, `GPUMESH_TOKEN`) > saved config file.

To clear the saved connection: `gpumesh disconnect`

> **Note for coordinators:** The coordinator machine does not run `gpumesh join`, so saved connections don't apply. Set environment variables or use `--url`/`--token` flags.

---

## Writing Task Scripts

Your script receives parameters on stdin and prints results as JSON on stdout.

```python
import json, sys, os

# Read parameters
payload = json.load(sys.stdin)

# Check what device you're on
device = os.environ.get("GPUMESH_DEVICE", "cpu")

# Do your work
result = {"answer": 42, "device": device}

# Print result as JSON (last line of stdout)
print(json.dumps(result))
```

### Payload format

A JSON file with a list of objects. Each object becomes one task:

```json
[
    {"lr": 0.01, "epochs": 100, "cost": 1},
    {"lr": 0.05, "epochs": 200, "cost": 2},
    {"lr": 0.1, "epochs": 500, "cost": 5}
]
```

The `cost` field (default 1) tells the scheduler how heavy the task is. Higher cost = assigned to faster machines.

### Rules

- Input: JSON on stdin
- Output: JSON on the **last line** of stdout
- Errors: print to stderr or exit non-zero
- Timeout: tasks are killed after timeout (default 240s)
- Environment: `GPUMESH_DEVICE` is set to `cuda`, `mps`, or `cpu`

---

## Python API (Jupyter / Colab)

The Python API lets you distribute compute from Jupyter notebooks, Colab, or any Python script — no CLI required.

```bash
pip install gpumesh[notebook]
```

### Quick Start

```python
from gpumesh import GPUMesh

# Connect to a running coordinator
mesh = GPUMesh("http://192.168.1.10:8000", token="mySecret")

# List connected workers
mesh.workers()
# [{'id': 'w1', 'device': 'cuda', 'hostname': 'GamingPC', 'score': 85.2},
#  {'id': 'w2', 'device': 'cuda', 'hostname': 'LabServer', 'score': 42.1}]

# Distribute a function across all workers
def train(lr, epochs):
    import time
    time.sleep(1)  # simulate training
    return {"accuracy": 0.95, "lr": lr, "epochs": epochs}

results = mesh.distribute(
    function=train,
    params=[
        {"lr": 0.01, "epochs": 100},
        {"lr": 0.05, "epochs": 200},
        {"lr": 0.1, "epochs": 300},
    ]
)

print(results)
# [{'accuracy': 0.95, 'lr': 0.01, 'epochs': 100},
#  {'accuracy': 0.95, 'lr': 0.05, 'epochs': 200},
#  {'accuracy': 0.95, 'lr': 0.1, 'epochs': 300}]
```

### Jupyter Notebook Example

```python
# Cell 1: Install (run once)
# !pip install gpumesh[notebook]

# Cell 2: Connect to coordinator
from gpumesh import GPUMesh

mesh = GPUMesh("http://192.168.1.10:8000", token="mySecret")

# Cell 3: Check connected workers
workers = mesh.workers()
print(f"{len(workers)} workers connected:")
for w in workers:
    print(f"  {w['hostname']}: {w['device']} (score={w['score']:.1f})")

# Cell 4: Define your workload
def hyperparameter_search(lr, weight_decay, batch_size):
    """Train a model with given hyperparameters."""
    import os
    device = os.environ.get("GPUMESH_DEVICE", "cpu")
    # Your training code here
    return {
        "lr": lr,
        "weight_decay": weight_decay,
        "batch_size": batch_size,
        "accuracy": 0.95 + lr * 10,  # placeholder
        "device": device,
    }

# Cell 5: Generate hyperparameter grid
import itertools

param_grid = {
    "lr": [0.001, 0.01, 0.1],
    "weight_decay": [0.0, 0.001, 0.01],
    "batch_size": [32, 64, 128],
}

params = [
    dict(zip(param_grid.keys(), v))
    for v in itertools.product(*param_grid.values())
]
print(f"Testing {len(params)} combinations...")

# Cell 6: Distribute across workers
results = mesh.distribute(
    function=hyperparameter_search,
    params=params,
    name="hyperparam_search",
    timeout=600,  # 10 minutes total
)

# Cell 7: Analyze results
import pandas as pd

df = mesh.results_to_dataframe(results)
df = df.sort_values("accuracy", ascending=False)
print(df.head(10))

# Cell 8: Visualize (optional)
# import matplotlib.pyplot as plt
# plt.figure(figsize=(10, 6))
# plt.scatter(df['lr'], df['accuracy'], c=df['batch_size'], cmap='viridis')
# plt.xlabel('Learning Rate')
# plt.ylabel('Accuracy')
# plt.colorbar(label='Batch Size')
# plt.show()
```

### Google Colab Example

```python
# Cell 1: Install
!pip install gpumesh[notebook]

# Cell 2: Connect to your coordinator (use public URL or Tailscale)
from gpumesh import GPUMesh

# Option A: Public ngrok URL (use a strong token!)
mesh = GPUMesh("https://your-coordinator.ngrok-free.app", token="mySecret")

# Option B: Tailscale URL
# mesh = GPUMesh("http://100.x.x.x:8000", token="mySecret")

# Cell 3: Verify connection
workers = mesh.workers()
print(f"Connected to {len(workers)} workers")

# Cell 4: Run distributed workload
def process_batch(batch_id, data_range):
    """Process a batch of data."""
    import os
    device = os.environ.get("GPUMESH_DEVICE", "cpu")
    # Simulate heavy computation
    result = sum(i ** 2 for i in range(data_range))
    return {
        "batch_id": batch_id,
        "device": device,
        "result": result,
    }

params = [{"batch_id": i, "data_range": 1000000} for i in range(100)]

results = mesh.distribute(
    function=process_batch,
    params=params,
    name="data_processing",
)

# Cell 5: Process results
df = mesh.results_to_dataframe(results)
print(f"Processed {len(df)} batches")
print(df.head())
```

### Using Closures and Lambdas

The API supports closures and lambdas via cloudpickle:

```python
from gpumesh import GPUMesh

mesh = GPUMesh("http://192.168.1.10:8000", token="mySecret")

# Closures work
def make_trainer(model_name):
    def train(learning_rate):
        # model_name is captured by the closure
        return {"model": model_name, "lr": learning_rate, "accuracy": 0.95}
    return train

resnet_trainer = make_trainer("resnet50")

results = mesh.distribute(
    function=resnet_trainer,
    params=[{"learning_rate": 0.01}, {"learning_rate": 0.001}]
)

# Lambdas work too
results = mesh.distribute(
    function=lambda x, y: {"sum": x + y},
    params=[{"x": 1, "y": 2}, {"x": 3, "y": 4}]
)
```

### Task Costs (Prioritization)

Use the `cost` field to prioritize heavy tasks for faster GPUs:

```python
results = mesh.distribute(
    function=train,
    params=[
        {"lr": 0.01, "cost": 5},  # Heavy task → fast GPU
        {"lr": 0.05, "cost": 1},  # Light task → any GPU
    ]
)
```

### Error Handling

Failed tasks include error details in the results. The `distribute()` method also raises `TimeoutError` if the job exceeds the timeout:

```python
try:
    results = mesh.distribute(
        function=some_function,
        params=params,
        timeout=300,  # seconds
    )
except TimeoutError as e:
    print(f"Job timed out: {e}")
    results = []

for r in results:
    if "_error" in r:
        print(f"Task failed: {r['_error']}")
    else:
        print(f"Success: {r}")
```

### Setup from Python

You can start a coordinator and join workers directly from Python:

```python
from gpumesh import GPUMesh

# Start a coordinator (non-blocking) - returns the token
token = GPUMesh.start_coordinator(port=8000, token="mySecret")
print(f"Coordinator started. Token: {token}")

# Join as a worker (from another machine or same machine)
info = GPUMesh.add_worker("http://192.168.1.10:8000", token="mySecret")
print(f"Worker joined: {info['device']} ({info['device_name']})")
```

### API Reference

| Method | Signature | Description |
|--------|-----------|-------------|
| `GPUMesh` | `GPUMesh(url, token)` | Connect to a coordinator |
| `workers` | `mesh.workers()` | List connected workers |
| `distribute` | `mesh.distribute(function, params, name="", timeout=300.0, poll_interval=2.0)` | Run a function across workers |
| `submit_job` | `mesh.submit_job(script, payloads, name="")` | Submit a script-based job |
| `job_status` | `mesh.job_status(job_id)` | Get job status |
| `results_to_dataframe` | `GPUMesh.results_to_dataframe(results)` | Convert results to pandas DataFrame (requires pandas) |
| `start_coordinator` | `GPUMesh.start_coordinator(port=8000, token=None, db_path="gpumesh.db", tailscale=False)` | Start a coordinator server |
| `add_worker` | `GPUMesh.add_worker(url, token, timeout=240.0)` | Join as a worker |


---

## Network Options

| Method | Setup | Best for |
|--------|-------|----------|
| **LAN** | None | Same Wi-Fi, fastest |
| **Tailscale** | Install Tailscale on all machines | Remote teams |
| **ngrok** | `pip install gpumesh[tunnel]`, free account | Public access |

### LAN

```bash
# Coordinator
gpumesh serve --port 8000 --token mySecret

# Worker (use LAN IP from coordinator output)
gpumesh join http://192.168.1.10:8000 --token mySecret
```

### Tailscale

```bash
# Coordinator
gpumesh serve --port 8000 --token mySecret --tailscale

# Worker (auto-detects coordinator)
gpumesh quickjoin --token mySecret --tailscale
```

### ngrok

```bash
# Coordinator
gpumesh serve --port 8000 --token mySecret --public
# prints: [mesh] public URL: https://abc123.ngrok-free.app

# Worker
gpumesh join https://abc123.ngrok-free.app --token mySecret
```

---

## Limitations

Be aware of these constraints before using gpumesh:

1. **No GPU memory sharing.** Each task gets its own process. Two tasks cannot share VRAM on the same GPU.

2. **No model sharding.** This is not distributed training. Each task is independent and runs on one machine.

3. **Task communication is HTTP.** For very large inputs/outputs (multi-GB), the HTTP overhead matters. Keep payloads small and results compact.

4. **Windows has weaker sandboxing.** No `RLIMIT_CPU` on Windows. Tasks still get killed on timeout, but runaway loops may consume more CPU before that.

5. **No authentication by default on LAN.** Anyone on the same network who knows the token can connect. Use a strong token.

6. **Single coordinator.** The coordinator is a single point of failure. If it crashes, workers stop getting tasks. The SQLite database is local to one machine.

7. **Python only.** Tasks must be Python scripts. No native binaries, no Docker containers.

8. **Function serialization uses cloudpickle.** The Python API (`mesh.distribute()`) requires cloudpickle. Functions with closures and lambdas work, but objects that cannot be pickled will fail.

9. **Tasks must be data-parallel.** If your tasks depend on each other (e.g., pipeline stages), gpumesh is not the right tool.

10. **No persistent storage.** Results are in the SQLite database only while the coordinator runs. Export results before shutting down.

---

## Security

Workers execute code submitted to the coordinator — that is the core functionality. Only share your coordinator URL and token with people you trust.

### Security features

- **Token authentication** — All API requests require a shared secret token
- **Token hashing** — Tokens are hashed with SHA-256 + random salt before storage (never stored in plain text)
- **Rate limiting** — After 5 failed token attempts in 5 minutes, the IP is locked out for 15 minutes
- **IP allowlist** — Optional feature to restrict access to specific IP addresses
- **Subprocess sandboxing** — Each task runs in its own isolated subprocess with:
  - Process group isolation (kills entire tree on timeout)
  - Wall-clock timeout (default 240 seconds)
  - CPU time limits via `RLIMIT_CPU` on POSIX systems

### Security best practices

1. **Use a strong token** — Treat it like a password. Generate one with:
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(16))"
   ```
2. **Don't leave public tunnels running unattended** — Shut down the coordinator when not in use
3. **Only join meshes you trust** — Workers execute arbitrary code from the coordinator
4. **Use Tailscale for team access** — More secure than public tunnels

---

## How the Scheduler Works

1. Each worker benchmarks itself at join time (matmul GFLOP/s score)
2. Tasks are sorted by cost
3. Fast workers get heavy tasks, slow workers get light tasks
4. Tasks are leased, not permanently assigned
5. If a worker dies, its tasks get re-queued
6. Failed tasks retry up to 3 times

```
Worker asks for task
        │
        ▼
┌──────────────────┐
│  Scheduler picks │  Fast worker → heavy task
│  matching task   │  Slow worker → light task
└──────────────────┘
        │
        ▼
┌──────────────────┐
│  Task is leased  │  240s timeout, heartbeat required
└──────────────────┘
        │
    ┌───┴───┐
    │       │
  Done    Failed
    │       │
    ▼       ▼
 Result   Re-queue (max 3x)
 posted   then mark failed
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `gpumesh: command not found` | Use `python -m gpumesh.cli` instead |
| `401 bad or missing token` | Token mismatch. Check coordinator and worker match exactly |
| `coordinator unreachable` | Coordinator not running, or firewall blocks the port |
| `task timed out` | Increase `--timeout 600` on the worker, or split into smaller tasks |
| `task exited with code 1` | Check stderr. Your script has an error |
| Worker shows `[dead]` | Worker disconnected. It will re-join if restarted |
| `No module named 'gpumesh'` | Run `pip install gpumesh` |
| `No module named 'torch'` | Run `pip install gpumesh[gpu]` for GPU detection |

Windows: use `python -m gpumesh.cli` if `gpumesh` is not recognized.

---

## Design Principles

- **Networking** — Hand-rolled JSON-over-HTTP protocol on `http.server` with no external frameworks. Worker heartbeats, poll-based task leasing that survives flaky connections, shared-token auth, and optional ngrok/Tailscale tunneling for NAT traversal.
- **Database** — SQLite in WAL mode with foreign keys, indexes, and atomic lease acquisition via conditional `UPDATE ... WHERE status='pending'` to prevent duplicate task assignments. Transactions protect multi-row writes.
- **Process isolation** — Every task runs in a fresh subprocess in its own process group. Timeouts kill the entire process tree; POSIX `RLIMIT_CPU` caps runaway loops. The coordinator uses threads with a lock around the shared DB connection.
- **Fault tolerance** — Capability-based scheduling, lease/heartbeat failure detection, idempotent re-queue with bounded retries, and a pull-based model where workers fetch work instead of the coordinator pushing it.
- **Task parallelism** — Independent work units are sharded across machines, not model tensors. This avoids internet latency bottlenecks and matches how production serverless GPU platforms operate.

---

## Layout

```
gpumesh/
  cli.py               command line entry point
  server.py            coordinator: threaded HTTP JSON API + lease reaper
  db.py                SQLite layer: workers, jobs, tasks, atomic leasing
  worker.py            agent loop: register, heartbeat, lease, execute, report
  sandbox.py           subprocess isolation: timeouts, process-group kill, rlimits
  capability.py        hardware probe + matmul benchmark -> capability score
  api.py               Python API (GPUMesh class)
  serializer.py        function serialization (cloudpickle + inspect fallback)
  client.py            job submission and status polling
  tunnel.py            optional ngrok/Tailscale support
  security.py          token hashing, rate limiting, IP allowlist
  connection_manager.py saved connections (~/.gpumesh/config.json)
  setup_wizard.py      interactive setup wizard
examples/
  grid_search.py       hyperparameter-search demo task (pure Python)
  payloads.json        six shards with varying cost
```

---

## Contributing

Contributions are welcome! Here is how to get started:

```bash
# Clone the repository
git clone https://github.com/Samurai007AK/gpumesh.git
cd gpumesh

# Install in development mode with dev extras
pip install -e ".[dev]"

# Run the tests
pytest
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.

## Links

- **GitHub**: [github.com/Samurai007AK/gpumesh](https://github.com/Samurai007AK/gpumesh)
- **Issues**: [github.com/Samurai007AK/gpumesh/issues](https://github.com/Samurai007AK/gpumesh/issues)
- **PyPI**: [pypi.org/project/gpumesh](https://pypi.org/project/gpumesh)
