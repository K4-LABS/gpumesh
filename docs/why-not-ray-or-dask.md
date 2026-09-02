# Why not Ray or Dask?

Short answer: **often you should use Ray or Dask.** They are mature, well
funded, well documented, and they solve a superset of what gpumesh solves. If
you already run one of them, gpumesh has nothing to offer you.

gpumesh exists for a narrower situation that those tools are not shaped for:
you have a few consumer machines that happen to be sitting on the same
network, you want to use them for an afternoon, and you are not willing to
stand up a cluster to do it.

This page tries to be honest about where that line is. If it reads as
partisan, [open an issue](https://github.com/K4-LABS/gpumesh/issues).
a comparison nobody trusts is worth less than no comparison.

---

## The short version

| | gpumesh | Ray | Dask |
|---|---|---|---|
| Time to first distributed call | one command, one decorator | cluster start + `ray.init()` | scheduler + workers + client |
| Assumes a homogeneous cluster | no | mostly | mostly |
| Long-running daemon | no, the coordinator *is* the process you started | yes (GCS, raylets) | yes (scheduler) |
| Scheduler | ~200 lines of SQL over SQLite | distributed, sophisticated | distributed, sophisticated |
| Distributed data structures | none | actors, object store | arrays, dataframes, bags, futures |
| Model sharding / tensor parallel | no | yes (with libraries) | via extensions |
| Dependency management | you install packages on each machine | runtime environments, container images | you install packages on each machine |
| Trust model | mutual: both sides execute the other's code, no sandbox | same shape, plus cluster auth | same shape |
| Falls back to local when the cluster is gone | yes, silently, same return value | no | no |
| Scale it is built for | 2–20 machines you know by name | thousands | thousands |

---

## Use Ray when

- **You need actors or shared state.** Ray's object store and stateful actors
  are the reason Ray exists. gpumesh has no equivalent and no plan for one,
  a task takes its arguments and returns a value, full stop.
- **You are doing distributed training or serving that shards a model.**
  gpumesh runs one task on one machine. It cannot split a model across
  devices, so anything needing tensor or pipeline parallelism is out of
  scope by construction.
- **You want RLlib, Tune, Serve, or Data.** Those libraries are a large part
  of Ray's value, and gpumesh has no ecosystem at all.
- **Your cluster is elastic and managed.** Kubernetes, autoscaling, spot
  instances. Ray has real answers there. gpumesh assumes machines you start
  by hand.
- **You need dependency isolation per job.** Ray runtime environments solve
  the "worker doesn't have my package" problem. gpumesh's answer is "install
  it on the worker", which is fine for four laptops and miserable for forty.

## Use Dask when

- **Your problem is a big array or dataframe**, not a pile of independent
  function calls. `dask.dataframe` and `dask.array` give you a partitioned,
  lazily-evaluated collection with a familiar API. gpumesh has nothing like
  it: if your data does not fit on one machine, gpumesh will not help you.
- **You want a task graph with dependencies.** Dask schedules a DAG. gpumesh
  schedules a flat list. Every task is independent, and if task B needs task
  A's output you are writing that plumbing yourself.
- **You are already in the PyData stack** and want distributed to be a
  one-line change to code you have.
- **You need out-of-core computation** on data larger than memory.

## Use gpumesh when

- **Your work is embarrassingly parallel.** A hyperparameter sweep, a batch
  of preprocessing shards, N independent simulations. This is the shape
  gpumesh is built around, and it is the shape where a simple scheduler loses
  nothing to a sophisticated one.
- **The machines are heterogeneous consumer hardware.** A 4090 desktop, a
  laptop with a 3060, an old box with no GPU at all. gpumesh benchmarks each
  worker at join and routes heavier tasks to stronger machines, and treats a
  worker that turns out to be slow as a straggler and gives it lighter work.
  Ray and Dask both assume, by default, that a worker is a worker.
- **The machines come and go.** Laptops sleep, wifi drops, someone closes the
  lid. gpumesh treats that as the normal case: a lease expires, the task is
  re-queued, the worker reconnects on its own when it wakes up. There is no
  state to recover because there is no state.
- **You want the code to still run when the mesh is not there.** A
  `@mesh`-decorated function with no reachable coordinator runs locally and
  returns the identical value. You can develop on a plane and distribute in
  the lab without changing a line. Neither Ray nor Dask offers this, and it
  is arguably the single most useful thing gpumesh does.
- **You do not want a daemon.** There is no service to install, no config
  file to write, no scheduler process to keep alive. `gpumesh serve` is the
  coordinator; Ctrl+C is the shutdown.

---

## Where the real differences are

### Setup cost

The thing gpumesh optimises hardest is the distance between "I have two
machines" and "my function ran on both of them".

```bash
# machine A
gpumesh serve --host 0.0.0.0 --token "$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"

# machine B
pip install gpumesh && gpumesh join http://<coordinator-ip>:8000 --token <TOKEN>
```

```python
from gpumesh import mesh

@mesh
def train(lr):
    return {"lr": lr, "acc": 0.9}

train.map([{"lr": 0.01}, {"lr": 0.05}])
```

That is the whole thing. No cluster config, no address file, no
`ray.init(address=...)`, no `Client(scheduler_address)`. Whether that
difference matters depends entirely on how often you set clusters up. If the
answer is "once, and then it runs for a year", this is not a real advantage.

### The scheduler is not sophisticated, deliberately

gpumesh's scheduler is a few SQL queries. It sorts pending tasks by `cost`,
places the requesting worker on a 0..1 line from weakest to strongest by
benchmark score, and hands it the task at the matching position. Placement
hints (`gpu`, `gpu_memory_mb`, `cpu_cores`) filter the queue first. Stragglers
get the lighter half. That is all of it.
see [protocol.md](protocol.md).

For a flat list of independent tasks on a handful of machines, that is close
to what a smarter scheduler would do anyway. For a DAG with data locality
constraints across a hundred nodes, it is nowhere near, and you should be
using something else.

### No cluster assumption

Ray and Dask both, reasonably, assume you have a *cluster*: a set of machines
that are alike, that stay up, that were provisioned together. gpumesh assumes
the opposite: that you have whatever is on the network right now, that it is
mismatched, and that some of it will disappear mid-job.

This shows up in small places. Every worker is benchmarked at join rather
than assumed equal. A worker that vanishes is expected rather than
exceptional. The `--self-worker` default means the coordinator's own machine
is part of the pool, so a "cluster" of one is a normal configuration and the
quickstart needs no second machine.

### The trust model is the same shape, and it is worth saying out loud

This is not a gpumesh advantage. Ray, Dask and gpumesh all execute code sent
over the network, and none of them sandboxes it. Ray had CVE-2023-48022,
Dask had CVE-2021-42343. Both, in essence, were "the port was reachable and
running code was the intended behaviour". gpumesh is the same category of
software.

gpumesh's differences are of degree:

- It requires a token on every request, and compares it timing-safely.
- The coordinator binds `127.0.0.1` by default, so exposure is something you
  opt into with `--host 0.0.0.0` and are warned about, loudly, at the moment
  you do.
- It says so loudly. See [SECURITY.md](../SECURITY.md) and
  [THREAT_MODEL.md](../THREAT_MODEL.md).

None of that is a sandbox. A token is a license to execute code as the
worker's user, and the worker's results are deserialized by whoever submitted
the task, so the trust runs both ways. If you need real isolation, you need
containers or VMs, from any of these three tools.

### Heterogeneous hardware

Worth a concrete example. Suppose you have an RTX 4090 (score ~120), an RTX
3060 laptop (score ~25) and an old CPU-only desktop (score ~0.3), and a sweep
of 30 configs of varying expense.

gpumesh sorts the tasks by `cost` and matches worker strength to task weight,
so the 4090 pulls the expensive configs and the CPU box takes the cheap ones.
If the laptop turns out to be thermally throttled and its average task time
exceeds twice its peers' median, it gets moved to the lighter half of the
queue without you doing anything.

You can get the same outcome from Ray or Dask with resource annotations and
custom resources. You just have to know to do it, and to know the numbers
in advance. gpumesh measures them at join time because on borrowed hardware
you usually do not know them.

---

## What gpumesh does not do

Stated plainly, so you can rule it out fast:

- No distributed data structures. No shared arrays, dataframes, or object
  store
- No task graphs. Every task is independent
- No model sharding, no tensor or pipeline parallelism
- No GPU memory sharing between tasks; each task is its own process
- No dependency shipping. Your imports must already exist on the worker
- No encryption on a plain LAN. Use Tailscale or ngrok if the network is not
  yours
- Single coordinator, single point of failure
- Python only
- No sandbox

The [Limitations section of the README](../README.md#limitations) is the
canonical list.

---

## Honest sizing

gpumesh has been exercised on meshes of a handful of machines. The
coordinator is a threaded `HTTPServer` over SQLite; the workers poll for
leases. That is fine at 2–20 workers and a few thousand tasks. It is not
designed for hundreds of workers, and nobody has tested it there.

If you outgrow it, you have outgrown it. Port the sweep to Ray or Dask.
Since the unit of work is a plain function taking keyword arguments and
returning a value, that port is usually mechanical.

---

## Related

- [protocol.md](protocol.md), the HTTP API and wire format
- [../README.md#limitations](../README.md#limitations), the full limitations list
- [SECURITY.md](../SECURITY.md), [THREAT_MODEL.md](../THREAT_MODEL.md)
