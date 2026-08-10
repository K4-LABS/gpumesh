# gpumesh — Borrow Your Friends' GPUs

> *Like Bluetooth, but for your GPUs*

[![PyPI version](https://img.shields.io/pypi/v/gpumesh.svg)](https://pypi.org/project/gpumesh/)
[![Python](https://img.shields.io/pypi/pyversions/gpumesh.svg)](https://pypi.org/project/gpumesh/)
[![License](https://img.shields.io/pypi/l/gpumesh.svg)](https://github.com/Samurai007AK/gpumesh/blob/main/LICENSE)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/r/samurai007ak/gpumesh)

```
   ╔════════════════════════════════════════════════════════╗
   ║   gpumesh — one compute pool out of many machines      ║
   ║   "like Bluetooth, but for your GPUs"                  ║
   ╚════════════════════════════════════════════════════════╝
```

**gpumesh** turns multiple machines into one compute pool. Start a **coordinator** on one machine, join **workers** from other machines, and run code across all of them as if they were one device.

- **Source and full docs:** [github.com/Samurai007AK/gpumesh](https://github.com/Samurai007AK/gpumesh)
- **Python package:** [pypi.org/project/gpumesh](https://pypi.org/project/gpumesh/)

---

## Read this before you start: the image is CPU-only

This image is built `FROM python:3.11-slim` and installs gpumesh **without** the `gpu` extra. That means:

- **No CUDA runtime and no `torch` inside the container.** Adding `--gpus all` gives the container a GPU device, but nothing in the image can use it.
- A containerized worker is detected as **CPU-only** and benchmarks with a deliberately slow pure-Python fallback, so it scores near zero and the scheduler will only send it the lightest tasks.

The image is a good fit for the **coordinator** (it is pure scheduling and bookkeeping) and for CPU-side tasks. For real GPU workers, pick one of:

1. Run `pip install "gpumesh[gpu]"` and `gpumesh join ...` **directly on the host** — the simplest option.
2. Build your own GPU image:

   ```dockerfile
   FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04
   RUN apt-get update && apt-get install -y python3-pip && rm -rf /var/lib/apt/lists/*
   RUN pip3 install "gpumesh[gpu]"
   ENTRYPOINT ["gpumesh"]
   ```

   then `docker run --gpus all your-image join http://coordinator:8732 --token mysecret`.

---

## Quick start

### 1. Start a coordinator

```bash
docker run -d \
  --name gpumesh-coordinator \
  -p 8732:8732 \
  -p 48900:48900/udp \
  -e GPUMESH_HOST_IP=192.168.1.10 \
  samurai007ak/gpumesh:latest \
  serve --port 8732 --token mysecret
```

Two things matter here:

- **Pass `--token` explicitly.** `gpumesh serve` does *not* read `GPUMESH_TOKEN`; without `--token` it generates a random one and no worker will be able to authenticate.
- **Set `GPUMESH_HOST_IP` to the host's LAN address.** Otherwise the coordinator advertises its container-internal IP, which workers on other machines cannot reach.

### 2. Join a worker

```bash
docker run -d \
  --name gpumesh-worker \
  -e GPUMESH_TOKEN=mysecret \
  samurai007ak/gpumesh:latest \
  join http://192.168.1.10:8732 --token mysecret
```

Unlike `serve`, `join` *does* fall back to `GPUMESH_TOKEN`, so `--token` is optional when the environment variable is set. The URL is always required.

### 3. Use your mesh

```python
from gpumesh import GPUMesh, accelerate

mesh = GPUMesh("http://192.168.1.10:8732", token="mysecret")

@accelerate(mesh)
def train(lr, epochs):
    return {"accuracy": 0.95}

result  = train(lr=0.01, epochs=100)                        # one worker
results = train.map([{"lr": 0.01}, {"lr": 0.05}, {"lr": 0.1}])  # every worker
```

---

## Tags

| Tag | Description |
|-----|-------------|
| `latest` | Latest stable release |
| `1.2.0` | Current release |
| `1.1.0` | Previous release |

```bash
docker pull samurai007ak/gpumesh:latest
docker pull samurai007ak/gpumesh:1.2.0
```

The entrypoint is `gpumesh`, so anything after the image name is a gpumesh subcommand:

```bash
docker run --rm samurai007ak/gpumesh:latest --help
docker run --rm samurai007ak/gpumesh:latest --version
```

---

## Docker Compose

```yaml
services:
  coordinator:
    image: samurai007ak/gpumesh:latest
    ports:
      - "${GPUMESH_PORT:-8732}:8732"
      - "48900:48900/udp"
    environment:
      - GPUMESH_COLOR=1
      - GPUMESH_HOST_IP=${GPUMESH_HOST_IP:-}
    command: serve --port 8732 --token ${GPUMESH_TOKEN:?set GPUMESH_TOKEN} --color
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
      - GPUMESH_TOKEN=${GPUMESH_TOKEN:?set GPUMESH_TOKEN}
      - GPUMESH_COLOR=1
    command: join http://coordinator:8732 --color
    deploy:
      replicas: ${WORKER_REPLICAS:-2}
    restart: unless-stopped

volumes:
  gpumesh_data:
```

```bash
GPUMESH_TOKEN=mysecret docker compose up -d              # coordinator + 2 workers
GPUMESH_TOKEN=mysecret docker compose up -d --scale worker=4
docker compose logs -f
docker compose down
```

Note that the worker's `command` includes the coordinator URL. `gpumesh join` takes the URL as a required positional argument and will exit with an argparse error if it is missing — `GPUMESH_URL` covers the client commands (`workers`, `status`, `submit`, …), not `join`.

Set `GPUMESH_HOST_IP` to the host's LAN address if machines outside this compose network need to join.

---

## Environment variables

| Variable | Read by | Effect |
|----------|---------|--------|
| `GPUMESH_TOKEN` | `join`, client commands | Auth token when `--token` is omitted. **Not read by `serve`** |
| `GPUMESH_URL` | client commands (`workers`, `status`, `submit`, …) | Coordinator URL when `--url` is omitted. **Not read by `join`** |
| `GPUMESH_HOST_IP` | `serve` | Address the coordinator advertises to workers — set this in containers |
| `GPUMESH_COLOR` | all output | `1` forces color, `0` disables it, `auto` (default) checks for a TTY |
| `GPUMESH_VERBOSE` | `@mesh` / `@accelerate` | `1` prints which device handled each task |
| `GPUMESH_LOCAL` | `@mesh` / `@accelerate` | `1` forces local execution, never touching the mesh |
| `GPUMESH_PORT` | compose file only | Host port mapped to the container's 8732 |
| `WORKER_REPLICAS` | compose file only | Number of worker replicas |

---

## Ports and volumes

| Port | Protocol | Purpose |
|------|----------|---------|
| `8732` | TCP | Coordinator HTTP API |
| `48900` | UDP | LAN peer discovery beacons |

The image pins **8732**, while `gpumesh serve` on a bare host defaults to **8000** — deliberate, so published `docker run` and compose recipes stay stable. Both are just defaults; `--port` overrides either, as long as coordinator and workers agree.

| Path | Contents |
|------|----------|
| `/root/.gpumesh` | Saved connection config (`config.json`) |
| working directory | `gpumesh.db`, the coordinator's SQLite job store |

Mount a volume at `/root/.gpumesh` to keep the saved connection across restarts. To keep job history too, pass `serve --db /root/.gpumesh/gpumesh.db` so the database lands on the same volume.

---

## Features

### Transparent acceleration

```python
@accelerate(mesh)
def preprocess(chunk_id, data_path):
    import pandas as pd
    return {"chunk": chunk_id, "rows": len(pd.read_parquet(data_path))}

result  = preprocess(chunk_id=0, data_path="data.parquet")   # one worker
results = preprocess.map([                                   # every worker
    {"chunk_id": 0, "data_path": "part0.parquet"},
    {"chunk_id": 1, "data_path": "part1.parquet"},
])
```

### Smart routing

| Scenario | What happens |
|----------|--------------|
| `func(x)`, workers alive | Runs as a single task on one mesh worker |
| `func(x)`, no workers | Runs on the best LOCAL device |
| `func.map([...])` | Spreads across ALL mesh devices |
| Mesh unreachable | Falls back to LOCAL execution silently |
| `GPUMESH_LOCAL=1` | Forces local-only |
| `GPUMESH_VERBOSE=1` | Prints which device handled each task |

Both paths return the identical value, so switching between them never changes your results.

### Hardware selection and resource specs

```python
@accelerate(mesh, gpu="A100")
def train(model):
    return model.cuda().forward(x)

@accelerate(mesh, cores=8, memory="16GB", timeout=300)
def heavy_computation(data):
    return processed
```

### Fault tolerance

- Dead workers are detected and their tasks re-queued
- Task leases expire after 300s and return to the pending queue
- Straggler workers (over 2× the median task time) are deprioritized to lighter work
- Every task runs in its own subprocess, so a crash cannot take down a worker
- Stale worker rows are pruned after a 300s TTL
- Memory-aware scheduling via a `gpu_memory_mb` payload hint
- Graceful fallback to local execution when the mesh is unreachable

---

## Benchmark scoring

On join — and every 10 minutes after — each worker runs a 1024×1024 matmul and a 256 MB memory copy:

```
score = 0.7 × GFLOP/s  +  0.3 × memory bandwidth (GB/s)
```

The score is **unbounded and relative**, not a 0–100 rating. The scheduler ranks each worker's score against the others and assigns task cost by percentile: the strongest machine gets the heaviest queued work. A container without `torch` falls back to a pure-Python benchmark and scores near zero — see the CPU-only note at the top.

Inspect the real numbers with `docker exec gpumesh-coordinator gpumesh workers`.

---

## Architecture

gpumesh is a **pull-based work queue**. The coordinator never pushes to workers, so a worker needs no inbound ports and no stable address — which is exactly why containers, laptops, and NAT'd machines all work.

```
 ╔══════════════════════════════════════════════════════════════════╗
 ║  COORDINATOR                        gpumesh serve --port 8732    ║
 ╟──────────────────────────────────────────────────────────────────╢
 ║   HTTP API  (threaded, X-Auth-Token on every call)               ║
 ║       ├──▶ auth + per-IP rate limiting                           ║
 ║       ├──▶ scheduler: score percentile + VRAM filter             ║
 ║       ├──▶ SQLite (WAL): jobs, tasks, workers, stats             ║
 ║       └──▶ reaper: expire leases, prune dead workers             ║
 ║   UDP :48900  discovery beacon listener                          ║
 ╚══════════════════════════════════════════════════════════════════╝
        ▲                    ▲                    ▲
        │  register          │  heartbeat  10s    │  lease / result
        │                    │                    │
 ┌──────┴───────┐     ┌──────┴───────┐     ┌──────┴───────┐
 │  WORKER A    │     │  WORKER B    │     │  WORKER C    │
 │  poll loop   │     │  poll loop   │     │  poll loop   │
 │  subprocess  │     │  subprocess  │     │  subprocess  │
 │  per task    │     │  per task    │     │  per task    │
 └──────────────┘     └──────────────┘     └──────────────┘

 submit ─▶ pending ─▶ leased/running ─▶ done
                           └─ crash, timeout, or lost lease ─▶ pending
```

Jobs and results live in the coordinator's SQLite file, so a coordinator restart resumes in-flight work. Workers hold no durable state — they re-register and re-benchmark on reconnect.

The [full architecture section](https://github.com/Samurai007AK/gpumesh#architecture) on GitHub covers the module map, HTTP API, and scheduling rules.

---

## Security

| Feature | Detail |
|---------|--------|
| Token authentication | Required on every API request |
| Timing-safe comparison | `hmac.compare_digest` |
| Rate limiting | 5 failed attempts within 300s → 15 min IP lockout |
| Process isolation | Every task runs in its own subprocess |
| File permissions | `0o600` on the saved config |
| Token hashing | SHA-256 with a salt, in memory only — never written to the database |

> **Workers execute code sent by the coordinator.** Anyone holding your URL and token can run arbitrary code on every machine in your mesh. Treat the token like a password and only share it with people you trust. gpumesh is built for trusted networks; it is not a sandbox and is not designed to run untrusted code.
>
> Run the coordinator with `serve --safe-mode` to refuse pickled functions and accept submitted scripts only. Traffic on a plain LAN is **not encrypted** — use Tailscale or ngrok when the mesh crosses a network you do not control. Do not publish port 8732 to the open internet.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Worker container exits immediately with an argparse error | `gpumesh join` needs the URL as a positional argument: `join http://coordinator:8732` |
| `401 bad token` | `serve` ignores `GPUMESH_TOKEN` — pass `serve --token ...` explicitly |
| Workers on other machines cannot connect | Set `-e GPUMESH_HOST_IP=<host LAN IP>` on the coordinator and publish `-p 8732:8732` |
| Discovery finds nothing | UDP broadcast rarely crosses a Docker bridge network — join by explicit URL |
| Worker scores near zero | Expected: the image has no `torch`. See the CPU-only note above |
| `ModuleNotFoundError` inside a task | Install that package in the **worker** image — gpumesh ships your code, not your environment |
| Job history lost on restart | `serve --db /root/.gpumesh/gpumesh.db` with a volume mounted at `/root/.gpumesh` |

---

## Links

- **GitHub:** [github.com/Samurai007AK/gpumesh](https://github.com/Samurai007AK/gpumesh)
- **PyPI:** [pypi.org/project/gpumesh](https://pypi.org/project/gpumesh/)
- **Issues:** [github.com/Samurai007AK/gpumesh/issues](https://github.com/Samurai007AK/gpumesh/issues)
- **Changelog:** [CHANGELOG.md](https://github.com/Samurai007AK/gpumesh/blob/main/CHANGELOG.md)

## License

MIT License — see [LICENSE](https://github.com/Samurai007AK/gpumesh/blob/main/LICENSE).
