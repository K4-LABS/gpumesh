<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/img/gpumesh-logo.png">
    <img alt="gpumesh — GPU Mesh Network" src="docs/img/gpumesh-logo-light.png" width="240">
  </picture>
</p>

<p align="center">
  <b>Borrow your friends' GPUs.</b> A distributed compute mesh that lets you share GPU power
  across machines on your network — with one decorator, one CLI command, or a Python API.
</p>

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
> Trust runs in both directions.
>
> gpumesh provides **no sandbox** and does not try to. Use it on machines and
> with people you actually trust. Read [SECURITY.md](SECURITY.md) and
> [THREAT_MODEL.md](THREAT_MODEL.md) before exposing a coordinator to a
> network.

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
| **Benchmark scoring** | Every worker gets a relative compute score from a join-time benchmark; the scheduler routes work to the strongest hardware |
| **Memory-aware scheduling** | VRAM is tracked; a payload's `gpu_memory_mb` hint keeps a task off workers without that much free memory |
| **Live radar** | `gpumesh radar` discovers nearby devices on your network — no config needed |
| **Isolated execution** | Every task runs in its own subprocess; a crashing task can't take down a worker |
| **Token security** | All API calls require a token; rate-limited, timing-safe verification |
| **Jupyter support** | `%%mesh` cell magic wraps every function in a cell automatically |

---

## Quick demo

```python
from gpumesh import GPUMesh, accelerate

mesh = GPUMesh("http://coordinator:8000", token=TOKEN)

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

**One machine is enough.** `gpumesh serve` joins your own CPU/GPU to the pool
by default, so a mesh of one runs exactly the same code path as a mesh of ten
— same scheduler, same wire format, same results. Get that working first;
adding a second machine is one flag and one command.

All terminal output below is real, copied from actual runs, with addresses
and hostnames replaced by placeholders.

### 1. Generate a token

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Do this once and reuse the value. It is not decoration: the token is the only
thing standing between anyone who can reach the port and code execution as
you. Short, memorable tokens are guessable, and the coordinator's hash is a
single SHA-256 round with no KDF — the entropy of the token is the whole
defence.

### 2. Start a coordinator

```bash
pip install gpumesh
gpumesh serve --port 8000 --token $TOKEN
```

```console
[gpumesh] config saved to ~/.gpumesh/config.json
[OK] Coordinator listening on 127.0.0.1:8000
   Token: <your token>

   Join from THIS machine:   gpumesh join http://127.0.0.1:8000 --token <your token>
   Other machines CANNOT reach this coordinator (bound to 127.0.0.1 only).
   That is the default now: a worker executes code as the user who started it.
   To let other machines join, re-run with: gpumesh serve --host 0.0.0.0 --port 8000 --token <your token>
   (or set GPUMESH_HOST=0.0.0.0). Read the warning it prints before you do.
   Ctrl+C to stop
[OK] Self-check: server is up (tested on 127.0.0.1 only)
[OK] Self-worker started — this machine's CPU/GPU is part of the pool
   (disable with --no-self-worker)
[worker] device=CPU Intel64 Family 6 Model 154 Stepping 3, GenuineIntel score=0.373 GFLOP/s
[mesh] worker joined: laptop-a (Intel64 Family 6 Model 154 Stepping 3, GenuineIntel, score=0.373)
[worker] joined mesh as 0afecb2dfd44
```

You now have a working mesh. Confirm it in another terminal:

```console
$ gpumesh workers

  WORKERS
  ------------------------------------------------------
  CPU  0afecb2d    laptop-a          score=0.373     [alive]
```

Prefer a guided wizard? Run `gpumesh setup`.

### 3. Run a distributed task

Save as `demo.py`:

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
[gpumesh] connected to coordinator at http://127.0.0.1:8000
{'lr': 0.01, 'accuracy': 0.91}
[{'lr': 0.01, 'accuracy': 0.91}, {'lr': 0.05, 'accuracy': 0.95}]
```

That is the whole workflow. Kill the coordinator and run it again — you get
the identical output, computed locally. Your script does not break because
the mesh went away.

More runnable examples, all verified: [`examples/`](examples/README.md).

### 4. Add a second machine

The coordinator binds to loopback, so nothing outside your machine can reach
it. That is the default, and opening it up is a decision rather than an
oversight — a worker runs whatever code the coordinator hands it, as the OS
user who started that worker. Restart with `--host 0.0.0.0` when you mean it:

```bash
gpumesh serve --host 0.0.0.0 --port 8000 --token $TOKEN
```

```console
   Join from THIS machine:   gpumesh join http://127.0.0.1:8000 --token <your token>
   Join from ANOTHER machine: gpumesh join http://192.0.2.10:8000 --token <your token>
   (127.0.0.1 always works locally; the LAN IP is for other machines)

  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
  !! NETWORK-EXPOSED COORDINATOR
  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
   Bound to:      0.0.0.0:8000 (not loopback — other machines can connect)
   Tasks run as:  alex
   On device:     CPU Intel64 Family 6 Model 154 Stepping 3, GenuineIntel

   Anyone who can reach this port AND has the token can run
   arbitrary code on this machine as alex.
   Only do this on a network you trust, and keep the token secret.
   Loopback-only (the default) is: gpumesh serve --port 8000
  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!

   Reachability of http://192.0.2.10:8000 from other machines is not tested here — a worker joining confirms it.
```

**Copy the `Join from ANOTHER machine` line** — that is the exact command the
second machine needs.

```bash
# on machine B
pip install gpumesh
gpumesh join http://192.0.2.10:8000 --token $TOKEN
```

```console
[worker] device=CPU Intel64 Family 6 Model 154 Stepping 3 score=0.310 GFLOP/s
[worker] joined mesh as 7c1e04ab93f2
```

```console
$ gpumesh workers

  WORKERS
  ------------------------------------------------------
  CPU  0afecb2d    laptop-a          score=0.373     [alive]
  CPU  7c1e04ab    laptop-b          score=0.310     [alive]
```

> **Workers never die.** They survive laptop sleep, WiFi drops and coordinator
> restarts, and reconnect on their own.
>
> Windows: run `gpumesh serve` as Administrator so the firewall rule is added
> for you. Without it, workers on other machines may be blocked.
>
> If the worker reports a timeout instead, it could not reach that address —
> see [Troubleshooting](#troubleshooting).

`examples/second_machine.py` reads your live mesh and prints the exact
command, filled in with your address, port and token.

### Prefer the CLI?

Submit a script instead of decorating a function. (The `examples/` files ship
with the repository, not the pip package — `git clone` first, or point the
command at any script of your own that reads a JSON payload on stdin.)

```console
$ gpumesh submit examples/grid_search.py --payloads examples/payloads.json --wait
[OK] Submitted job c3f81cc498f0
Job: examples/grid_search.py (c3f81cc498f0)

  #################### 100%  6/6 done  (10s)

  Status: finished
  Counts: {'done': 6}

  ✓ task 463a58e92490 [done]  cost=1.0  worker=0afecb2dfd44
    result: {"lr": 0.01, "epochs": 100, "l2": 0.0, "val_accuracy": 0.948, "weights": [0.2271, -0.2757, 0.0019]}
  ✓ task 7fa79a95725f [done]  cost=10.0  worker=0afecb2dfd44
    result: {"lr": 0.3, "epochs": 1000, "l2": 0.01, "val_accuracy": 0.952, "weights": [2.1907, -2.9448, 0.0191]}
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

Two flags are global and go *before* the subcommand: `-v` / `--verbose` for
debug-level logging, and `--json-logs` to emit logs as JSON lines (for Docker).

### Server & connection

| Command | Description |
|---------|-------------|
| `gpumesh setup` | Interactive setup wizard (coordinator or worker) |
| `gpumesh serve` | Start the coordinator (`--host`, `--port`, `--token`, `--db`, `--host-ip`, `--public`, `--tailscale`, `--no-discovery`, `--safe-mode`, `--no-self-worker`, `--color`/`--no-color`) |
| `gpumesh join URL` | Join a mesh as a worker (`--token`, `--timeout`, `--safe-mode`, `--color`/`--no-color`) |
| `gpumesh quickjoin [URL]` | One-click: install, detect GPU, join (`--token`, `--tailscale`, `--port`, `--timeout`, `--safe-mode`) |
| `gpumesh worker` | Broadcast presence and wait to be claimed (`--token`, `--claim-port`, `--timeout`, `--safe-mode`) |
| `gpumesh radar` | Scan for nearby devices (live radar; `--mode coordinator` \| `worker`) |
| `gpumesh doctor` | Check this machine's environment and print a report (`--json`) |
| `gpumesh show-connection` | Show the saved URL + token |
| `gpumesh disconnect` | Clear the saved connection |

`serve` and `join` also take `--color` / `--no-color`. They are the two
commands that keep printing for hours into a Docker log or a systemd journal,
which is exactly where `isatty()` says "no terminal" and you may still want
colour — or emphatically not want it.

**`gpumesh doctor`** is read-only: it adds no firewall rules, saves no
connection, installs nothing, and never prints the token. It reports which
gpumesh is imported and from where, the Python interpreter, torch/CUDA (and
whether `nvidia-smi` disagrees with it), the cloudpickle version, the saved
coordinator and its workers, the addresses this machine would bind and
advertise, and on Windows whether a firewall rule for the port exists. It
exits `1` only for a fault on *this* machine — no coordinator configured, or
an unreachable one, is a warning and still exits `0`, so it works as a
pre-flight check in a script. `--json` emits the same report as a document
(warnings are diverted to stderr so the stdout stays parseable).

**`--host` and `--host-ip` are different things**, and mixing them up costs an
hour of firewall debugging:

| Flag | Controls | Default |
|------|----------|---------|
| `--host` | **The bind address** — who can open a connection at all. A security boundary | `127.0.0.1` (this machine only) |
| `--host-ip` | **The advertised address** — which address is printed for workers to dial. Cosmetic | auto-detected LAN IP |

Setting `--host-ip` alone never opens the port up. Use `--host 0.0.0.0` for
that, and read the banner it prints. `--host-ip` is for when auto-detection
picks a VPN or hypervisor adapter and remote workers time out against an
address only your machine can route to.

### Jobs

| Command | Description |
|---------|-------------|
| `gpumesh submit SCRIPT --payloads FILE` | Submit a script job (`--name`, `--wait` blocks until done, `--wait-timeout`) |
| `gpumesh status JOB_ID` | Show job progress and results |
| `gpumesh cancel JOB_ID` | Cancel a running job |
| `gpumesh retry JOB_ID` | Re-queue failed/timed-out tasks |
| `gpumesh kill [--force]` | Kill all tasks (graceful or immediate) |

Each payload object is handed to your script as JSON on stdin. Four keys are
read by the scheduler instead of being left to your code:

| Payload key | Effect |
|-------------|--------|
| `cost` | Relative task weight (default `1.0`); heavier tasks are routed to stronger workers |
| `gpu` | Device kind (`cuda`, `cpu`, `mps`) or model substring (`A100`); only matching workers are offered the task |
| `gpu_memory_mb` | Minimum free VRAM; a worker reporting less free memory than this skips the task |
| `cpu_cores` | Minimum CPU cores; a worker reporting fewer skips the task. This is the wire name for `@accelerate(cores=...)` |

These are **filters, not preferences**. A task nobody can satisfy stays
pending until the coordinator gives up on it (60s) and fails it with a message
naming the requirement, rather than queueing forever.

All four also apply to payloads passed to `mesh.submit(...)` from the Python
API, and to the payload dicts you hand to `.map()`. Full shapes in
[`docs/protocol.md`](docs/protocol.md); what is versioned and what a bump
promises is in [`docs/stability.md`](docs/stability.md).

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
| `GPUMESH_TOKEN` | Job/monitoring commands, `serve`, `join` | Auth token when `--token` is omitted (`quickjoin` and `worker` always require the flag) |
| `GPUMESH_HOST` | `serve` | Bind address when `--host` is omitted. `0.0.0.0` opens the port to other machines |
| `GPUMESH_HOST_IP` | `serve` | Pin the address advertised to workers (same as `--host-ip`) — does **not** change the bind. Must be an IP literal; a hostname is rejected with a warning and auto-detection is used instead |
| `GPUMESH_CLAIM_HOST` | `worker`, and `setup` in worker mode | Bind address for the **claim** port. Defaults to all interfaces, unlike the coordinator — a claim server exists to be reached by another machine, so loopback would not make it safer, only broken. Narrow it to one address (your tailnet, say) if you do not need all of them |
| `GPUMESH_LOCAL=1` | `@mesh` / `@accelerate` | Force local execution, never touch the mesh |
| `GPUMESH_VERBOSE=1` | `@mesh` / `@accelerate` | Print which device handled each task |
| `GPUMESH_COLOR` | All output | `1` forces colour, `0` disables it, `auto` (default) checks whether stdout is a TTY |

**`GPUMESH_CLAIM_HOST` is deliberately not `GPUMESH_HOST`.** The two answer
different questions and are routinely set on the same machine:
`GPUMESH_HOST=127.0.0.1` is a perfectly sensible coordinator setting, and if
the claim port read it too, every worker on that box would silently become
unclaimable. A non-loopback claim bind prints a banner naming the OS user that
claims will run as.

`GPUMESH_COLOR` is the environment form. `gpumesh serve` and `gpumesh join`
also take `--color` (force it on) and `--no-color` (force it off), which are
mutually exclusive. The flags work by setting `GPUMESH_COLOR` and re-running
the detection, so the choice reaches this process *and* the function-task
subprocess a worker spawns, which inherits the environment. (Script tasks run
with a deliberately minimal allowlisted environment and never see it.)

One wrinkle: the auto-detection probes **stdout**, while `-v` log lines are
written to **stderr**. For the uncommon shape where only stderr is redirected
(`gpumesh serve 2> run.log` from a terminal), the detection says "terminal" and
the log file gets escape sequences. `--no-color` or `GPUMESH_COLOR=0` is the
escape hatch.

---

## Python API

```python
from gpumesh import GPUMesh

mesh = GPUMesh("http://coordinator:8000", token=TOKEN)
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
GPUMesh.start_coordinator(port=8000, token=TOKEN)   # binds 127.0.0.1; pass host="0.0.0.0" to open it up
GPUMesh.add_worker("http://coordinator:8000", token=TOKEN)
```

---

## `@accelerate` patterns

```python
from gpumesh import GPUMesh, accelerate

mesh = GPUMesh("http://coordinator:8000", token=TOKEN)

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

A prebuilt image is available on Docker Hub ([samurai007ak/gpumesh](https://hub.docker.com/r/samurai007ak/gpumesh)).

First, a token:

```bash
export GPUMESH_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
```

There are **two** host decisions in a container, and they are not the same
one:

1. **What the coordinator binds to inside the container.** It must be
   `0.0.0.0`, because anything reaching it arrives from outside the container
   — a loopback bind inside a container answers nothing, including the port
   Docker published.
2. **What Docker publishes on the machine running Docker.** This is the one
   that decides who can reach you. `-p 8732:8732` binds `0.0.0.0` on the host,
   which puts a coordinator on every network the machine is attached to.
   Prefix the host side with `127.0.0.1` unless you mean otherwise.

```bash
# Coordinator — reachable from this machine only
docker run -d --name gpumesh-coordinator \
  -p 127.0.0.1:8732:8732 -p 127.0.0.1:48900:48900/udp \
  -e GPUMESH_TOKEN \
  samurai007ak/gpumesh:1.3.0 \
  serve --host 0.0.0.0 --port 8732

# Worker
docker run -d --name gpumesh-worker \
  -e GPUMESH_TOKEN \
  samurai007ak/gpumesh:1.3.0 \
  join http://coordinator-ip:8732
```

Drop the `127.0.0.1:` prefix — or better, name a specific interface, e.g.
`-p 192.0.2.10:8732:8732` — only when you deliberately want other machines to
join. That is the same decision `gpumesh serve --host 0.0.0.0` asks you to
make on the host, and it deserves the same pause.

Both `serve` and `join` read `GPUMESH_TOKEN` from the environment, so
`-e GPUMESH_TOKEN` is enough and the token never appears in `docker ps`
output. `join` takes the coordinator URL as a positional argument, so there is
no `GPUMESH_URL` to set here — that variable is only read by the job and
monitoring commands.

Or use the included [`docker-compose.yaml`](docker-compose.yaml) for a
coordinator + N workers, with a healthcheck the workers wait on before
starting. It pulls the published image, so the file works on its own:

```bash
GPUMESH_TOKEN=$TOKEN docker compose up -d                      # localhost only
WORKER_REPLICAS=4 GPUMESH_TOKEN=$TOKEN docker compose up -d    # scale workers
GPUMESH_BIND=0.0.0.0 GPUMESH_TOKEN=$TOKEN docker compose up -d # reachable from the LAN
```

`GPUMESH_TOKEN` is required — compose stops with a clear message if it is
unset, rather than starting a coordinator with a random token that no worker
could authenticate against. `GPUMESH_BIND` defaults to `127.0.0.1` and is the
host-side publish address from decision (2) above.

**Ports:** `8732` (TCP API) and `48900/udp` (LAN discovery). Persistent state
(the SQLite database and `~/.gpumesh/config.json`) lives under `/data`; the
compose file mounts a named volume there.

> The container listens on **8732**, while `gpumesh serve` on the host defaults to **8000**. That is deliberate — the image pins an explicit port so published `docker run` and compose recipes stay stable. Both are just defaults: pass `--port` to use whatever you like, and make sure workers point at the same number the coordinator is listening on.

---

## Network options

| Method | Setup | Best for | Encrypted |
|--------|-------|----------|-----------|
| **LAN** | None | Same Wi-Fi, fastest | No |
| **Tailscale** | Install Tailscale | Remote teams | Yes |
| **ngrok** | `pip install gpumesh[tunnel]` | Public access, demos | Yes |

- **LAN:** `gpumesh serve --host 0.0.0.0` + `gpumesh join http://192.0.2.10:8000 --token $TOKEN`. UDP broadcast helps workers *find* a coordinator, but joining still needs the token and a coordinator that is actually bound beyond loopback.
- **Tailscale:** `gpumesh serve --host 0.0.0.0 --port 8000 --tailscale`, then join via the Tailscale IP. This is the option to reach for if the mesh crosses any network you do not control — the tunnel, not the token, becomes the boundary.
- **ngrok:** `gpumesh serve --host 0.0.0.0 --port 8000 --public` prints a public `https://...` URL that workers anywhere can join. "Anywhere" includes people you did not invite; the token is the only thing keeping them out.

The default — no flag at all — is **loopback only**. Every row above is an
opt-in to a wider audience.

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
| Loopback bind by default | `gpumesh serve` listens on `127.0.0.1`; exposure needs `--host 0.0.0.0` and prints a banner |
| Token authentication | All API requests, including reads |
| Timing-safe comparison | HMAC `compare_digest` |
| Rate limiting | 5 failures -> 15 min IP lockout (loopback exempt — see below) |
| Process isolation | Tasks in subprocesses |
| File permissions | 0o600 on the saved config (`~/.gpumesh/config.json`) |
| Token hashing | SHA-256, in memory only — the token is never written to the database |

Read that table as "what raises the cost of an attack", not as "what makes
this safe". None of it is a sandbox.

> **The token is a licence to execute code, not a password guarding data.**
> Anyone holding your URL and token can run arbitrary Python on every machine
> in your mesh, as the user who started each worker. And it runs both ways: a
> worker's result is deserialized on the submitting machine, so a hostile
> worker executes code on you. gpumesh is built for trusted networks — home
> labs, lab benches, your own machines, a team you know.
>
> Use a real token: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
> The coordinator hashes it with a single SHA-256 round and a deterministic
> salt, which is fine for 250 bits of entropy and useless for a token you
> chose by hand. Entropy is the whole defence.
>
> Run the coordinator with `--safe-mode` to refuse function distribution and
> accept submitted scripts only. That closes the pickle path; it does not stop
> a submitted script from doing anything a script can do.
>
> Rate limiting exempts loopback deliberately. Anyone who can open a socket
> from `127.0.0.1` can read the token out of the process and the config file
> rather than guess it, so a lockout there costs an attacker nothing and costs
> you your own mesh.
>
> Traffic is **not encrypted** on a plain LAN. Use `--tailscale` or `--public`
> (ngrok) when the mesh crosses a network you do not control.

Full detail: **[SECURITY.md](SECURITY.md)** (what a token grants, how to
report a vulnerability) and **[THREAT_MODEL.md](THREAT_MODEL.md)** (the same
flow traced through the code, file and line).

---

## Benchmark scoring

Each worker runs a benchmark on join and gets a score of
`gflops * 0.7 + bandwidth_gbps * 0.3`. It is a **relative, unbounded** number —
there is no normalisation and no ceiling, so it only means anything next to the
other workers in your pool. The scheduler uses it to rank workers, nothing else.

Rough magnitudes, to read the numbers `gpumesh workers` prints:

| Typical hardware | Score, roughly |
|------------------|----------------|
| RTX 4090, A100 | ~100+ |
| RTX 3080, 3090 | ~50–100 |
| RTX 3060, T4 | ~10–50 |
| CPU only | under 1 |

Those are illustrative, not a scale — a laptop CPU commonly lands around `0.24`,
and a faster GPU than anything listed here would simply score higher.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `command not found: gpumesh` | Use `python -m gpumesh` or check your PATH |
| HTTP 401, `Invalid token` | Use the same token on coordinator and worker |
| HTTP 401, `Too many attempts` | Not a token rejection — the token was not checked. Wait out the lockout and retry with the same token |
| **Another machine cannot connect at all** | The coordinator binds `127.0.0.1` by default. Restart it with `--host 0.0.0.0` (or `GPUMESH_HOST=0.0.0.0`). Check this **before** the firewall — a loopback bind and a blocked firewall look identical from the other machine |
| `--host-ip` set but still unreachable | `--host-ip` only changes the address that is *printed*. `--host` is the bind. Setting the first never opens the port |
| `GPUMESH_HOST_IP` seems to be ignored | It takes an IP literal, not a hostname — almost any typo is a syntactically valid hostname, so it is validated and a non-IP value is discarded with a `WARNING` line at startup. `gpumesh doctor` prints the address actually being advertised |
| Coordinator unreachable | Check firewall; is the coordinator running? |
| Task timed out | Increase `--timeout` or split tasks |
| Windows connection error | Run `gpumesh serve` as Administrator for firewall rules |
| Worker not showing up | Both on the same network? Try `gpumesh radar` |
| `ModuleNotFoundError: torch` | `pip install gpumesh[gpu]` |
| UDP broadcast not working | Use `gpumesh join URL` directly |
| `ModuleNotFoundError` inside a task | Install that package on the **worker** too — gpumesh ships your code, not your environment |
| `cannot send result of type ...` | Return plain data. Sockets, locks, database handles and live GPU handles can't cross machines |
| `NameError` inside a function that works locally | The worker is on a different Python minor version, so the function shipped as source text and lost its module-level constants and closures. Match versions, or move the constants inside the function |
| `Cannot run this task on a Python X worker` | Same cause, no source to fall back to — the function was defined in a REPL or heredoc. Put it in a `.py` file |
| No colour in a Docker log or CI pane | `isatty()` is false there, which is the correct default. Force it with `gpumesh serve --color` / `gpumesh join --color`, or `GPUMESH_COLOR=1` |
| `Refusing to register this worker: incompatible gpumesh protocol version` | The two machines are more than one wire-protocol version apart. The message names both numbers and which side is behind; `pip install -U gpumesh` on that side. Details in [`docs/stability.md`](docs/stability.md) |
| Results differ from a local run | They shouldn't — file an issue. Confirm with `GPUMESH_LOCAL=1 python your_script.py` |

**Start with `gpumesh doctor`.** It is read-only, it never prints the token,
and its output is meant to be pasted into an issue. For *"workers can't
connect"* it shows in one screen whether the coordinator binds loopback, which
address this machine would advertise (and whether that address belongs to a
virtual adapter no other machine can route to), whether a Windows firewall
rule for the port exists, and whether the saved coordinator answers at all.
For *"NameError on a task"* it prints this machine's Python, gpumesh and
cloudpickle versions and flags any connected worker reported as differing —
which is the cause nearly every time. If the coordinator does not report
worker versions it says so and tells you to run `gpumesh doctor` on each
machine and compare the Python lines by hand. `gpumesh doctor --json` gives
the same report as a parseable document.

**Verbose logging:** `gpumesh -v serve` (the flag is global, so it works on any command) — **decorator routing messages:** `GPUMESH_VERBOSE=1 python my_script.py` — **force local-only:** `GPUMESH_LOCAL=1 python my_script.py`

---

## Development

```bash
git clone https://github.com/K4-LABS/gpumesh.git
cd gpumesh
pip install -e ".[dev]"
pytest                 # on Windows a few skip and one xpasses
python -m build        # build wheel + sdist
```

CI runs the suite on Linux (Python 3.9, 3.11, 3.12), Windows and macOS for
every push and pull request — the [badge](https://github.com/K4-LABS/gpumesh/actions/workflows/tests.yml)
at the top of this page is the live count, so it cannot drift the way a
hand-written number does.

Contributions are welcome — issues and pull requests both. See
[CONTRIBUTING.md](CONTRIBUTING.md) for setup, how the pieces fit together,
and what makes a change easy to review.

### Documentation

| Page | What it covers |
|------|----------------|
| [`docs/protocol.md`](docs/protocol.md) | The HTTP API, every endpoint, the task payload, the function and result envelopes, cross-Python-version behaviour |
| [`docs/stability.md`](docs/stability.md) | What counts as gpumesh's public API, what a version bump promises, the wire-protocol compatibility window, and the deprecation policy |
| [`docs/why-not-ray-or-dask.md`](docs/why-not-ray-or-dask.md) | An honest comparison, including when Ray or Dask is the right answer |
| [`examples/`](examples/README.md) | Runnable scripts for the first hour: hello-mesh, a second machine, a `.map()` sweep, non-JSON return values, a worker that disappears |
| [SECURITY.md](SECURITY.md) · [THREAT_MODEL.md](THREAT_MODEL.md) | What a token grants, and the same flow traced through the code |

---

## Limitations

- Python only — tasks must be Python functions or scripts
- Arguments and return values must be picklable. Anything tied to a live process — open files, sockets, locks, database handles, CUDA handles — cannot cross machines. Return plain data (numbers, arrays, tensors, DataFrames) instead
- Every worker needs the imports your function uses already installed; gpumesh ships your code, not your environment
- Workers should run the same Python minor version as the submitter — cloudpickle falls back to source when they differ, which does not cover every function
- No GPU memory sharing — each task gets its own process
- No model sharding — each task runs on one machine at a time
- No task graphs — every task is independent; if task B needs task A's output, that plumbing is yours
- No distributed data structures — no shared arrays, dataframes or object store. If your data does not fit on one machine, gpumesh will not help
- Built and tested for meshes of roughly 2–20 machines. Nobody has run it at hundreds of workers
- Single coordinator — single point of failure (use Tailscale for reliability)
- No built-in encryption — use Tailscale for encrypted tunnels
- Trusted networks only — workers run whatever code the coordinator sends, and submitters deserialize whatever workers return

If several of those are dealbreakers, read
[**Why not Ray or Dask?**](docs/why-not-ray-or-dask.md) — it says plainly
which tool to use instead.

---

## Prior art and credits

gpumesh borrows shamelessly, at the level of **API shape and scheduling
strategy**. No code was copied from any of these projects; what was taken is
the idea of what a good interface looks like, and each one is credited in the
source where its influence lands.

[**exo**](https://github.com/exo-explore/exo) (Apache-2.0) is the closest
relative in spirit — running AI workloads on the consumer hardware you
already own. Its runner-supervisor pattern is why every gpumesh task runs in
its own subprocess, and its crash diagnostics and topology-change events are
reflected in `worker.py` and `db.py`.
[**hivemind**](https://github.com/learning-at-home/hivemind) (MIT) supplied
the TTL-based worker expiry that prunes a machine that has stopped
heartbeating. [**Petals**](https://github.com/bigscience-workshop/petals)
(MIT) supplied straggler deprioritisation — a worker slower than twice its
peers' median gets lighter tasks rather than holding up the batch.
[**cudf.pandas**](https://github.com/rapidsai/cudf) (Apache-2.0) is where
`accelerate.install(mesh)` comes from: an import hook that makes existing
code use the accelerator without editing it.
[**Hugging Face Accelerate**](https://github.com/huggingface/accelerate)
(Apache-2.0) is the source of the `.to(device)` placement idea.
[**clustrix**](https://github.com/ContextLab/clustrix) (MIT) is where
`@accelerate(cores=8, memory="16GB")` gets its shape — declaring resource
requirements on the decorator rather than in a separate config.
[**burla**](https://github.com/Burla-Cloud/burla) contributed the
`remote_parallel_map` batch pattern behind `.map()` and the `func_gpu="A100"`
hardware-selection idea; note that burla is licensed **FSL-1.1-Apache-2.0**,
which is *source-available, not OSI open source*, and reads back to Apache-2.0
after two years. Two smaller influences round it out: **distry**, for the
plain single-function decorator, and **ezpz**, for `setup_torch()`-style
automatic backend detection.

If you maintain one of these and think the credit is wrong, or the influence
is closer to copying than we believe,
[open an issue](https://github.com/K4-LABS/gpumesh/issues) and it will be
corrected.

---

## License

MIT License. See [LICENSE](LICENSE) for details.

---

[GitHub](https://github.com/K4-LABS/gpumesh) · [Issues](https://github.com/K4-LABS/gpumesh/issues) · [PyPI](https://pypi.org/project/gpumesh/)
