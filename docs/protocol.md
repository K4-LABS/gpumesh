# The gpumesh protocol

Everything gpumesh does crosses one interface: JSON over HTTP, on a single
port, authenticated by one shared token in a header. There is no binary
channel, no second socket, no message broker. If you can send an HTTP request
you can be a worker, submit jobs, or build a dashboard.

This page documents that interface as the code implements it, against
gpumesh 1.3.0. Where the server's own module docstring and the code disagree,
the code wins and the difference is noted.

> [!CAUTION]
> This protocol has no sandbox. `POST /api/jobs` with
> `script: "__gpumesh_function__"` sends pickled bytecode that every worker
> will execute as the OS user that started it. Anyone who can reach the port
> and holds the token has arbitrary code execution on every machine in the
> mesh — and, because results are deserialized by the submitter, a hostile
> worker has the same over the submitter. Read
> [SECURITY.md](../SECURITY.md) and [THREAT_MODEL.md](../THREAT_MODEL.md)
> before you expose a coordinator.

---

## Transport and authentication

| | |
|---|---|
| Base URL | `http://<coordinator-host>:<port>` — or `https://` when the coordinator was started with `--tls`. 8000 for `gpumesh serve`, 8732 in the Docker image |
| Transport | HTTP/1.1. **Unencrypted by default.** `gpumesh serve --tls` wraps the listener in TLS 1.2+ with a self-signed certificate, which is enough to keep the token out of a LAN packet capture. Put the mesh on Tailscale or an ngrok tunnel if it crosses a network you do not control |
| Auth header | `X-Auth-Token: <token>` on **every** request, including `GET` |
| Content type | `application/json; charset=utf-8` both ways |
| Max request body | 10 MB (`CoordinatorHandler.MAX_CONTENT_LENGTH`). Oversized bodies are drained and answered `413` so the connection stays usable |
| Server | `ThreadingHTTPServer`; one thread per request, no keep-alive assumptions |

The token is compared with `hmac.compare_digest` against a
PBKDF2-HMAC-SHA256 hash (random per-token salt, 200 000 iterations by
default) held in memory only — it is never written to the database, and it is
re-derived from the token every time the coordinator starts. Hashes in the
older `salt:hash` single-round format still verify, so a pinned or persisted
hash keeps working; set `GPUMESH_AUTH_KDF=sha256` to go back to producing
them.

A successful verification is memoised in memory, keyed by an HMAC of the
token under a random per-process key, so a worker polling several times a
second pays the derivation once rather than on every request. Failures are
never cached — caching them would let a brute-forcer replay a wrong guess for
free, in front of the rate limiter that exists to count those guesses.

Failed attempts from non-loopback addresses are rate limited per IP, and
loopback is exempt, because anyone who can open a socket from `127.0.0.1` can
already read the token out of the process and the config file rather than
guess it.

Clients talking to a `--tls` coordinator need to trust its certificate:
point `GPUMESH_TLS_CA` at a copy of it (verified), or set
`GPUMESH_TLS_INSECURE=1` (encrypted, but nothing checks who answered).

### Status codes

| Code | Means |
|------|-------|
| `200` | Success, JSON body |
| `204` | `POST /api/lease` only: no task available. **Empty body** |
| `400` | Malformed request — bad JSON, non-object body, bad `Content-Length`, missing required field |
| `401` | Auth failed. The body's `error` string distinguishes a wrong token, a rate-limited IP, and an address not on the allowlist. Treat the string as human-readable, not as a stable API |
| `403` | `--safe-mode` coordinator refusing a function job |
| `404` | Unknown endpoint, or unknown job id |
| `409` | `POST /api/result` for a task this worker no longer holds (its lease expired and someone else took it) |
| `413` | Request body over 10 MB |
| `426` | `POST /api/register` only: the worker's `protocol_version` is outside this coordinator's support window. The body carries `protocol_version`, `min_protocol_version` and `worker_protocol_version` alongside `error`. See [stability.md](stability.md) |
| `500` | Unhandled server error; the traceback is printed on the coordinator's console |
| `503` | Coordinator is shutting down |

Every non-2xx body is `{"error": "<message>"}`.

---

## Endpoints

The module docstring in `gpumesh/server.py` lists ten endpoints. There are
thirteen: `/api/devices`, `/api/health` and `/api/kill` are implemented but
undocumented there.

### Worker lifecycle

#### `POST /api/register`

A worker announces itself and the capacity it has. Called on first join and
again after any reconnect.

```json
{
  "hostname": "laptop-a",
  "device": "cuda",
  "device_name": "NVIDIA GeForce RTX 3080",
  "score": 85.2,
  "cpu_cores": 16,
  "gpu_memory_total_mb": 10240.0,
  "protocol_version": 2
}
```

| Field | Type | Notes |
|-------|------|-------|
| `hostname` | string | Display only. Defaults to `"unknown"` |
| `device` | string | `"cpu"`, `"cuda"`, `"mps"`. Matched against a task's `gpu` hint |
| `device_name` | string | Model string. Also matched against the `gpu` hint, so `gpu: "A100"` works |
| `score` | number | Benchmark result, `gflops * 0.7 + bandwidth_gbps * 0.3`. Must parse as a float or the request is `400` |
| `cpu_cores` | integer | Falls back to `cpu_count`. Anything unparseable becomes `0` |
| `gpu_memory_total_mb` | number | Unparseable becomes `0.0` |
| `protocol_version` | integer | Optional. The wire protocol this worker speaks. **Absent means `1`** — every gpumesh up to and including 1.3.0 sends nothing here, and that unversioned protocol is named 1 retroactively. A value outside the coordinator's window is `426`; a value that is not an integer is `400` |

Workers post their whole capability probe here; unknown extra keys are
ignored. **`0` is not "unknown", it is "cannot satisfy any positive
requirement"** — a worker that never reports its core count will never
receive a task asking for cores.

Response:

```json
{"worker_id": "0afecb2dfd44", "protocol_version": 2, "min_protocol_version": 1}
```

`worker_id` is a 12-hex-character id. Each call creates a *new* row, so a
reconnecting worker gets a new id and the old row ages out.

The two version fields are the coordinator's half of the handshake: they let a
worker that is *newer* than the coordinator refuse the pairing itself, which no
amount of gating on the coordinator can do — an old coordinator has no gate at
all. A response without them is exactly the absent case, and means protocol 1.
The rules for both directions are in [stability.md](stability.md).

Version negotiation happens before anything about the worker is written down,
so a refused worker leaves no row, fires no `worker_joined` event, and never
appears in `/api/workers`.

#### `POST /api/heartbeat`

```json
{"worker_id": "0afecb2dfd44", "task_id": "91abb320f959",
 "score": 85.2, "gpu_memory_free_mb": 8192.0}
```

`worker_id` is required; everything else is optional. `task_id` names the
task currently in flight so its lease is extended. `gpu_memory_free_mb` is
what the memory-aware scheduler reads — until a heartbeat supplies it, the
scheduler falls back to the total reported at registration.

Response `{"ok": true}`, or `404` with `{"ok": false}` if the coordinator has
no such worker (it restarted, or the row was reaped). A worker seeing that
re-registers.

Timings, from `gpumesh/db.py`: a worker is **dead** after 30s without a
heartbeat and its leases are re-queued; its row is **deleted** after 300s.
The reaper runs every 5s.

#### `POST /api/lease`

```json
{"worker_id": "0afecb2dfd44"}
```

Returns `204` with an empty body when there is nothing to run — poll, do not
retry. On success:

```json
{
  "task_id": "91abb320f959",
  "job_id":  "50e029946961",
  "payload": {"n": 4, "cost": 1.0},
  "cost": 1.0,
  "script": "import json,sys\np=json.load(sys.stdin)\n..."
}
```

The lease lasts 300s and is renewed by heartbeats carrying this `task_id`.
If it expires the task returns to `pending` and another worker gets it; the
`attempts` counter in the job status is how you see that happened. A task
that has been pending and unmatched for 60s with no worker able to satisfy
its hints is failed with a message naming the requirement, rather than
queued forever.

**Which task you get is not FIFO.** The coordinator places this worker on a
0..1 line from weakest to strongest by score among live workers, then reads
off the task at the same position in the queue sorted by ascending `cost`.
Strong workers get heavy tasks. A solo worker always gets the heaviest. A
worker whose `avg_time` exceeds twice the median of its *peers* is treated as
a straggler and restricted to the lighter half of the queue. Before any of
that, tasks whose placement hints this worker cannot satisfy are filtered
out entirely.

#### `POST /api/result`

```json
{
  "task_id": "91abb320f959",
  "worker_id": "0afecb2dfd44",
  "ok": true,
  "result": {"n": 4, "sq": 16},
  "elapsed": 0.31
}
```

`ok` must be a JSON boolean — a string `"true"` is `400`. On failure send
`ok: false` with `error` (string) and optionally `diagnostics` (an object the
worker builds: error type, elapsed, GPU memory delta).

`user_error: true` marks a **deterministic** failure: re-running it produces
the identical failure, so it is failed immediately instead of consuming the
retry budget. Infrastructure failures omit it and keep the full budget (3
attempts — the counter increments per *lease*, so it is three leases, not
three re-runs after the first).

The real worker sets it in these cases, and the split is worth knowing because
it decides whether a task comes back:

| Task kind | Deterministic — fails once | Retried |
|---|---|---|
| Function | the function raised; the return value could not be serialized; the payload was refused for a cross-Python-version reason | timeout, lease expiry, worker death |
| Script | exit status **positive** (an uncaught exception exits 1, `sys.exit(2)` exits 2 — the script's own verdict on itself); no output at all; last stdout line is not JSON | timeout; exit status **negative** (POSIX: killed by a signal — SIGKILL from the OOM killer says the machine was full, not that the code is wrong); lease expiry |

A hand-rolled worker chooses for itself; the coordinator only reads the flag.

Response `{"ok": true}`, or `409` `{"ok": false}` if this worker no longer
holds the task.

### Jobs

#### `POST /api/jobs`

```json
{"name": "sweep", "script": "<source text or __gpumesh_function__>",
 "payloads": [{"lr": 0.01}, {"lr": 0.05}]}
```

`script` must be non-empty and `payloads` a non-empty list, or `400`. One
task is created per payload. Response: `{"job_id": "50e029946961"}`.

A coordinator started with `--safe-mode` answers `403` when `script` contains
`__gpumesh_function__`.

#### `GET /api/jobs/<job_id>`

Real response, captured from a live coordinator:

```json
{
  "id": "50e029946961",
  "name": "proto-demo",
  "created_at": 1786744960.0770698,
  "finished": true,
  "counts": {"done": 1},
  "tasks": [
    {
      "id": "91abb320f959",
      "status": "done",
      "cost": 1.0,
      "worker_id": "0afecb2dfd44",
      "result": {"n": 4, "sq": 16},
      "error": null,
      "attempts": 1
    }
  ]
}
```

`status` is one of `pending`, `running`, `done`, `failed`. `finished` is true
when every task is `done` or `failed` — note that a job of all-failed tasks
is also "finished". `counts` only contains the statuses actually present.
`404` `{"error": "no such job"}` for an unknown id.

#### `POST /api/cancel` · `POST /api/retry` · `POST /api/kill`

| Endpoint | Body | Response |
|----------|------|----------|
| `/api/cancel` | `{"job_id": "..."}` | `{"pending": 2, "running": 1}` — counts moved to `failed` with `error: "cancelled"` |
| `/api/retry` | `{"job_id": "..."}` | `{"requeued": 3, "counts": {...}}` — failed/timed-out tasks back to `pending` |
| `/api/kill` | `{"force": false}` | Cancels tasks across **all** jobs. `force` must be a boolean or `400` |

Cancelling does not reach into a worker that is mid-task; it marks the rows.
The worker's own result for a cancelled task arrives to a `409`.

### Read-only views

#### `GET /api/workers`

```json
{"workers": [{"id": "0afecb2dfd44", "hostname": "laptop-a",
              "device": "cpu", "device_name": "Intel64 Family 6 Model 154",
              "score": 0.373, "alive": true}]}
```

#### `GET /api/devices`

The same machines, aggregated. **Note the spelling difference:** this view
reports liveness as a `status` string, while `/api/workers` reports an
`alive` boolean. Two views, two conventions; both are load-bearing.

```json
{
  "total_devices": 1, "alive_devices": 1,
  "total_gpus": 0, "total_cpus": 1, "total_score": 0.37,
  "devices": [{"index": 0, "id": "0afecb2dfd44", "hostname": "laptop-a",
               "device": "cpu", "device_name": "Intel64 Family 6 Model 154",
               "score": 0.373, "status": "alive",
               "cpu_cores": 16, "gpu_memory_total_mb": 0.0,
               "gpu_memory_free_mb": 0.0}]
}
```

#### `GET /api/health`

```json
{"status": "ok", "version": "1.3.0",
 "protocol_version": 2, "min_protocol_version": 1,
 "uptime_seconds": 471.62,
 "workers_alive": 1, "workers_dead": 0,
 "jobs_pending": 0, "jobs_running": 0, "jobs_done": 45, "jobs_failed": 2,
 "total_score": 0.37}
```

`protocol_version` and `min_protocol_version` are reported here as well as on
a registration response, so an operator debugging "my worker will not join" can
read the coordinator's support window without first owning a worker that can
join it.

Still requires the token, so it is not usable as an unauthenticated liveness
probe. The Docker healthcheck uses a bare TCP connect for that reason.

#### `GET /api/events`

`{"events": [{"type": "worker_joined", "worker_id": "...", "time": 1786744440.59}]}`

Last 100 join/leave events, newest first. Polled, not streamed.

---

## The task payload

A payload is an arbitrary JSON object, with a handful of keys the coordinator
reads before your code ever sees it.

### Scheduler keys

| Key | Type | Read by | Effect |
|-----|------|---------|--------|
| `cost` | number | `create_job`, `lease_task` | Relative task weight, default `1.0`. Sorts the queue; heavy tasks go to strong workers. Unparseable values fall back to `1.0` |
| `gpu` | string | `_worker_can_run` | Device kind (`cuda`, `cpu`, `mps`, `gpu`) or model substring (`A100`). Matched against the worker's `device` and `device_name` |
| `gpu_memory_mb` | number | `_worker_can_run` | Minimum free VRAM. A worker reporting less is not offered the task |
| `cpu_cores` | integer | `_worker_can_run` | Minimum cores. Note the name: `@accelerate(cores=8)` and `distribute(cores=8)` both travel as `cpu_cores` |

Hints are **filters, not preferences**. A worker that cannot satisfy one is
skipped and the task stays pending for someone who can. If nobody can, the
unsatisfiable detector eventually fails the task with a message naming the
requirement, rather than leaving it queued forever.

`cost` is stripped from a function task's arguments before your function is
called. The placement hints ride at the payload's top level, next to `cost`,
and are likewise never passed to your function.

### Script tasks

For a script job, the **whole payload** — scheduler keys included — is
written to the script's stdin as JSON. The script prints its result as JSON
on stdout; the last line is taken as the result. `examples/grid_search.py`
is the reference implementation of that contract, and it simply ignores the
`cost` key it receives.

### Function tasks

When `script == "__gpumesh_function__"`, the payload carries the function
itself:

| Key | Type | Meaning |
|-----|------|---------|
| `_func` | string | Base64 serialized function (below) |
| `_params` | object | Keyword arguments — this becomes `func(**_params)` |
| `_task_index` | integer | Position in the submitted list, so results can be re-ordered |

---

## The function envelope

`_func` decodes to a length-prefixed frame:

```
[4 bytes big-endian metadata length][UTF-8 JSON metadata][cloudpickle bytes]
```

The metadata, captured from a real `serialize_function` call:

```json
{
  "method": "cloudpickle",
  "modules": ["base64", "gpumesh", "json", "time", "urllib"],
  "module_globals": {"base64": "base64", "json": "json",
                     "serializer": "gpumesh.serializer"},
  "func_name": "as_array",
  "python_version": "3.11",
  "source": "def as_array(n):\n    import numpy as np\n    return ..."
}
```

| Field | Purpose |
|-------|---------|
| `method` | `"cloudpickle"` or `"source"` |
| `modules` | Best-effort list of top-level modules to import before unpickling. Import failures are tolerated |
| `module_globals` | Maps the *names the function used* to importable module names, so `import numpy as np` rebinds as `np` on the source path |
| `func_name` | Used to find the function after `exec`-ing the source, and to strip gpumesh's own decorators |
| `python_version` | `"3.11"` — `major.minor` of the sender |
| `source` | `inspect.getsource()` text, when available. Absent for interactive sessions and heredocs |

When `method == "source"` there are no cloudpickle bytes after the metadata.

### Cross-version behaviour: Python

Two different things can be "out of version" on a mesh, and they fail in
completely different places. This subsection is about the **Python** version of
the machine that defined the function versus the machine that runs it — a
per-task, per-function property carried in the envelope above. The next one is
about the **wire protocol** version of the two processes, which is settled once
at registration. Neither implies the other: matched Pythons on skewed protocols
never get as far as a task, and matched protocols on skewed Pythons register
fine and then fail per function.

This is the part most likely to bite, and the rules are exact.

| Sender's `python_version` | Worker's Python | What happens |
|---|---|---|
| Same | — | cloudpickle. Full fidelity: closures, globals, bound methods, callable objects |
| Different, `source` present | — | Falls back to rebuilding from source text |
| Different, no `source` | — | **Refused** with a `ValueError` naming both versions |
| Absent (pre-0.8.1 sender) | — | Treated as unknown, *not* as a mismatch: cloudpickle is attempted, with a source fallback if it raises |

The refusal is deliberate. Cross-version `cloudpickle.loads()` can succeed
and hand back a function whose bytecode crashes the interpreter when called —
a native segfault inside the task subprocess with no traceback. A clean error
up front beats a worker crash.

The source path is a genuine downgrade, not a transparent equivalent. It
re-execs the text into a namespace holding only the *module-valued* globals
from `module_globals`, so these are gone:

- module-level constants (`SCALE = 3` becomes `NameError` at call time)
- closure variables
- bound methods — refused outright, naming `self`/`cls`
- callable objects — refused, because the source rebuilds the *class*
- `async def` functions — refused; gpumesh never awaits

gpumesh's own `@mesh` / `@accelerate` decorators are stripped from the source
before exec. **Every other decorator is preserved**, on purpose: silently
dropping `@torch.no_grad()` would change what the function does depending on
which worker ran it. If a preserved decorator's name is not importable on the
worker, the error says so rather than leaving a bare `NameError`.

The practical advice: run the same Python minor version everywhere, and
define mesh functions in a `.py` file rather than a REPL.

### Cross-version behaviour: the protocol

Everything above is per-function. The *wire* is versioned separately, by
`gpumesh.PROTOCOL_VERSION` — an integer, deliberately not `__version__`,
because the coordinator and the workers are installed on different machines by
different people and drift is the normal state of a mesh rather than a
misconfiguration.

The short form, so this reference is self-contained:

| | |
|---|---|
| Current | `PROTOCOL_VERSION = 2`, `MIN_PROTOCOL_VERSION = 1` |
| Window | Exactly N and N−1, on both sides |
| Absent `protocol_version` | Means **1**, a fixed constant — not "the current minimum". Every gpumesh through 1.3.0 lands here, and 1 is inside the window |
| Worker outside the coordinator's window | `426` at `POST /api/register`. No row, no event, no `/api/workers` entry |
| Coordinator outside the worker's window | Refused by the worker, reading `protocol_version` off the registration response |
| Either refusal, in Python | `gpumesh.ProtocolVersionMismatch` — never a bare `ValueError`, and never the worker's generic "failed to register" branch, whose firewall advice is entirely wrong for a version skew |

Only the **worker** handshake is version-gated. Job submitters
(`POST /api/jobs`, `GET /api/jobs/<id>`) are not — they are frequently a
notebook on a fourth machine, and their real compatibility constraint is the
Python version in the function envelope above.

[stability.md](stability.md) is the authority here: what counts as a wire
change, what moves the integer, and the deprecation path a version follows on
its way out of the window.

---

## The result envelope

A result crosses worker subprocess → worker → coordinator → you. Only the
middle hop constrains anything: the coordinator persists results as JSON in
SQLite. So every *function* result is wrapped:

```json
{"__gpumesh_result__": {"encoding": "json", "value": 5}}
```

```json
{"__gpumesh_result__": {"encoding": "cloudpickle",
                        "value": "gAWVnAAAAAAAAACME251bXB5..."}}
```

`encoding: "json"` when `json.dumps(value)` succeeds — the value passes
through verbatim. Otherwise the value is cloudpickled and base64'd. Decoding
is exact: `np.arange(5)` returns as `array([0, 1, 2, 3, 4])`, not as a list.

Two consequences worth stating plainly:

- **The envelope preserves the shape of non-dict returns.** `return 5` comes
  back as `5`, not `{"result": 5}`, so a mesh call and a local call return
  the identical object.
- **Decoding runs `cloudpickle.loads` on the submitting machine.** That is
  code execution, driven by whatever the worker sent. Trust is not one-way.

Anything that is not an envelope is passed through unchanged, which is what
keeps script tasks (plain JSON on stdout) and results from older workers
working.

Values that cannot be pickled at all — sockets, locks, database connections,
live CUDA handles — fail the task with a message naming the type and marking
it a `user_error`, so it is not retried. Note that an *open file object* is a
near miss: cloudpickle reads it and hands back a `StringIO`, so the bytes
arrive but the file does not.

---

## Discovery (UDP 48900)

Separate from the HTTP API. Workers broadcast a presence beacon on UDP port
48900; a coordinator started without `--no-discovery` listens and prints
nearby machines. `gpumesh radar` uses the same channel. Discovery only makes
machines *visible* — joining still requires the token over HTTP, and a
coordinator bound to loopback cannot be joined regardless of what discovery
shows.

---

## A minimal worker, end to end

```python
import json, time, urllib.request

URL, TOKEN = "http://127.0.0.1:8000", "<your token>"

def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        URL + path, data=data, method=method,
        headers={"X-Auth-Token": TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else None      # 204 -> None

reg = call("POST", "/api/register", {
    "hostname": "my-worker", "device": "cpu",
    "device_name": "hand-rolled", "score": 1.0, "cpu_cores": 4,
    # Say which wire protocol you speak. Omitting it is legal and means 1;
    # sending it is what gets you a 426 with both numbers in it instead of an
    # unexplained failure three calls later. A coordinator too old to know the
    # key ignores it, so this is safe against every gpumesh ever released.
    "protocol_version": 2,
})
# Absent in the response means the coordinator predates the handshake, i.e. 1.
if not (1 <= reg.get("protocol_version", 1) <= 2):
    raise SystemExit(f"coordinator speaks protocol {reg['protocol_version']}, "
                     f"this worker speaks 2")
wid = reg["worker_id"]

while True:
    task = call("POST", "/api/lease", {"worker_id": wid})
    if task is None:
        call("POST", "/api/heartbeat", {"worker_id": wid})
        time.sleep(1)
        continue
    # Script tasks only — running task["script"] is arbitrary code execution.
    result = {"echo": task["payload"]}
    call("POST", "/api/result", {
        "task_id": task["task_id"], "worker_id": wid,
        "ok": True, "result": result, "elapsed": 0.0,
    })
```

That is the entire worker contract. Everything else the real worker does —
benchmarking, subprocess isolation, backoff, reconnection, crash
diagnostics — is policy on top of these four calls.

---

## Related

- [stability.md](stability.md) — what is public API, and how `PROTOCOL_VERSION` moves
- [SECURITY.md](../SECURITY.md) — what a token actually grants
- [THREAT_MODEL.md](../THREAT_MODEL.md) — the same flow traced through the code
- [why-not-ray-or-dask.md](why-not-ray-or-dask.md) — when this protocol is the wrong one
- [../examples/README.md](../examples/README.md) — runnable clients
