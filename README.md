# gpumesh

**Borrow your friends' GPUs.** A terminal-based distributed compute mesh written in
pure Python (stdlib only — `torch`, `psutil`, `pyngrok` are optional extras).

You have ML work to run but a weak laptop. Your friend has a strong GPU sitting
idle. `gpumesh` turns any group of machines into a small compute cluster: one
machine coordinates, any number of machines join with a single command, and work
is split between them **proportionally to how fast each machine is**.

```
 your laptop (coordinator)              friend's laptop (worker)
┌───────────────────────┐    HTTP/JSON  ┌────────────────────────┐
│  job queue            │◄─────────────►│  hardware probe        │
│  capability scheduler │   (optional   │  matmul benchmark      │
│  SQLite (WAL)         │    ngrok      │  sandboxed subprocess  │
│  heartbeats + reaper  │    tunnel)    │  executor              │
└──────────▲────────────┘               └────────────────────────┘
           │ submit / status
       client CLI
```

## Quick start

```bash
pip install -e .
```

**Machine 1 — start the coordinator:**

```bash
gpumesh serve --port 8000
# prints a token and a ready-made join command, e.g.
#   gpumesh join http://192.168.1.5:8000 --token Kv3xP9qL2mNa
```

Add `--public` (with `pip install pyngrok`) to get a public URL friends can
reach over the internet.

**Machine 2..N — join as a worker (one command, as promised):**

```bash
gpumesh join http://192.168.1.5:8000 --token Kv3xP9qL2mNa
```

The worker probes its hardware (CUDA GPU → Apple Silicon → CPU), benchmarks
itself with a matmul, and starts pulling tasks. Your own laptop can join too —
then both machines compute in parallel.

**Submit work:**

```bash
gpumesh submit examples/grid_search.py --payloads examples/payloads.json \
    --url http://192.168.1.5:8000 --token Kv3xP9qL2mNa --wait
```

A job is a Python script plus a JSON list of payloads (shards). Each payload
becomes one task; an optional `"cost"` field marks how heavy it is. Heavy
tasks go to fast machines, light tasks to slow ones.

**Watch the mesh:**

```bash
gpumesh workers --url ... --token ...
gpumesh status JOB_ID --url ... --token ...
```

## Writing your own task script

The contract is two lines of glue:

```python
import json, sys
payload = json.load(sys.stdin)        # your shard's parameters
# ... do the work (os.environ["GPUMESH_DEVICE"] tells you cuda/mps/cpu) ...
print(json.dumps({"answer": 42}))     # result = last stdout line, as JSON
```

Anything data-parallel fits: hyperparameter search, batch inference,
cross-validation folds, rendering chunks, brute-force search shards.

## How the scheduler splits work

1. Every worker benchmarks itself at join time → a GFLOP/s **score**.
2. Pending tasks are sorted by `cost`.
3. When a worker asks for work, its score percentile among live workers picks
   the matching percentile of task costs — the strongest machine gets the
   heaviest remaining task, the weakest gets the lightest.
4. Tasks are leased, not given away: if a worker dies (heartbeats stop) or the
   lease expires, the reaper re-queues the task for someone else (max 3
   attempts, then marked failed).

## Design notes (the interview section)

- **Networking** — hand-rolled JSON-over-HTTP protocol on `http.server`
  (no framework), worker heartbeats, poll-based task leasing that survives
  flaky tunnels, shared-token auth, ngrok tunneling for NAT traversal.
- **DBMS** — SQLite in WAL mode; schema with foreign keys and indexes; atomic
  lease acquisition via a conditional `UPDATE ... WHERE status='pending'` so
  two workers can never grab the same task; transactions around multi-row
  writes.
- **Operating systems** — every task runs in a fresh subprocess in its own
  session (process group); timeouts kill the whole process tree with
  `SIGKILL`; POSIX `RLIMIT_CPU` caps runaway loops; coordinator uses threads
  with a lock around the shared DB connection.
- **Distributed systems** — capability-based scheduling, lease/heartbeat
  failure detection, idempotent re-queue with bounded retries, work-stealing
  style pull model (workers pull; the coordinator never needs to reach them
  through NAT).
- **Task-parallel, not tensor-parallel** — splitting a single model's tensors
  across machines over the internet dies on latency (that's what NCCL +
  InfiniBand are for). Sharding independent work units is the model serverless
  GPU platforms (e.g. RunPod) actually use.

## Security warning

Workers execute code submitted to the coordinator. That is the point of the
tool, and it is also remote code execution by design. Only share your
coordinator URL + token with people you trust, treat the token as a password,
and don't leave a public tunnel running unattended. The subprocess sandbox
limits accidents, not attackers.

## Layout

```
gpumesh/
  cli.py         command line entry point (serve / join / submit / status / workers)
  server.py      coordinator: threaded HTTP JSON API + lease reaper
  db.py          SQLite layer: workers, jobs, tasks, atomic leasing
  worker.py      agent loop: register, heartbeat, lease, execute, report
  sandbox.py     subprocess isolation: timeouts, process-group kill, rlimits
  capability.py  hardware probe + matmul benchmark -> capability score
  client.py      job submission and status polling
  tunnel.py      optional ngrok public URL
examples/
  grid_search.py hyperparameter-search demo task (pure Python)
  payloads.json  six shards with varying cost
```
