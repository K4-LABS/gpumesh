# Stability and compatibility

Semantic Versioning opens with an obligation most projects skip:

> Software using Semantic Versioning MUST declare a public API.

gpumesh reached 1.3.0 without declaring one. That is not a paperwork problem.
Until a project says what its public API is, *every* rename is arguably
breaking and no rename is definitely breaking, so "is this a MAJOR bump?"
has no answer and the version number stops carrying information. This page is
the declaration.

There are three surfaces, and they are versioned differently on purpose:

| Surface | Versioned by | Breaking change means |
|---|---|---|
| The Python API | `gpumesh.__version__` (SemVer) | MAJOR bump |
| The CLI | `gpumesh.__version__` (SemVer) | MAJOR bump |
| The HTTP protocol | `gpumesh.PROTOCOL_VERSION` (integer) | The old version leaves the support window |

The third one is separate because the coordinator and the workers are
installed on different machines, by different people, at different times. A
mesh where every machine must run the same patch release is not a mesh of
borrowed laptops, it is a cluster with extra steps.

---

## 1. The Python API

**The public API is exactly the names in `gpumesh.__all__`.** Everything else
in the package is internal and may change, move, or vanish in any release,
including submodules you can import today.

```python
>>> import gpumesh; gpumesh.__all__
['GPUMesh', 'GPUMeshError', 'accelerate', 'accelerate_install',
 'PlacementUnsupportedError', 'mesh', 'torch', 'load_ipython_extension',
 'unload_ipython_extension', 'ProtocolVersionMismatch', 'PROTOCOL_VERSION',
 'MIN_PROTOCOL_VERSION', '__version__']
```

Every exception gpumesh raises **at** a caller is in that list. That rule is
worth stating separately, because it is the one an incomplete `__all__` breaks
first: an exception a user must write in an `except` clause, and cannot find in
the declared public API, is a promise nobody can rely on. Exceptions that never
cross the API boundary, such as internal control flow like
`accelerate._MeshUnavailable`, which is raised and caught two frames apart
inside one module, are not listed, and are not raised at you either.

| Name | What is promised |
|---|---|
| `GPUMesh` | The class, its constructor signature, and its documented methods: `workers`, `distribute`, `submit`, `submit_job`, `status`, `job_status`, `devices`, `device_count`, `gpu_count`, `total_score`, `auto_device`, `results_to_dataframe`, and the static `start_coordinator` / `add_worker` |
| `GPUMeshError` | The exception type raised by `GPUMesh` operations. Catchable, and it stays a subclass of `Exception` |
| `accelerate`, `accelerate_install` | The decorator and the installer |
| `PlacementUnsupportedError` | Raised by `.map()` when the mesh object cannot carry a `gpu=`/`cores=`/`memory=` constraint. See the fallback contract below. Subclass of `RuntimeError`, never narrowed |
| `mesh` | The `@mesh` **decorator**. See the warning below |
| `torch` | The lazy `gpumesh.torch` integration module, including the names it documents: `setup_torch`, `device`, and the `RemoteDeviceError` its `device()` raises |
| `load_ipython_extension`, `unload_ipython_extension` | So `%load_ext gpumesh` keeps working |
| `ProtocolVersionMismatch` | Raised when a worker and a coordinator cannot talk. Subclass of `Exception`, never narrowed |
| `PROTOCOL_VERSION`, `MIN_PROTOCOL_VERSION` | Integers. See §3 |
| `__version__` | A PEP 440 string |

`GPUMesh` currently exposes two pairs of near-duplicate methods,
`status`/`job_status` and `submit`/`submit_job`. Both pairs are supported;
neither is deprecated today. If one is ever removed it goes through §5 first.

> [!WARNING]
> `gpumesh.mesh` is the decorator, not the `gpumesh.mesh` submodule, and the
> binding in `__init__.py` is load-bearing. The import machinery pins
> `gpumesh.mesh` to the submodule the first time anything does
> `import gpumesh.mesh`, which would permanently shadow a lazy `__getattr__`
> and make `from gpumesh import mesh` return a module, *depending on import
> order*. The submodule is imported eagerly and the name re-bound immediately,
> so both `from gpumesh import mesh` (decorator) and
> `from gpumesh.mesh import connect` (module) are safe. Do not "simplify" it.

### The `@accelerate` fallback contract

`@accelerate` runs your function locally when the mesh cannot take it. That
fallback is the reason a mesh is *optional*, and it is also the only part of
this API that can turn a failure into a wrong answer, so exactly which failures
fall back and which propagate is public, and changing the split is a MAJOR
change.

**The line is acceptance.** A failure that happens *before* the coordinator has
accepted the job falls back to local. A failure *after* it has accepted the job
propagates.

The reason is not a preference for strictness. Once `POST /api/jobs` has
succeeded, the coordinator owns a job that may be pending, may be running on
a worker at this moment, and may finish after your call has returned. Running
the same batch locally on top of that executes the work **twice**, which for
anything with a side effect, whether a file written, a row inserted or a model
checkpointed, is worse than any exception.

| What happened | `@accelerate(...)(...)` | `.map([...])` |
|---|---|---|
| `GPUMESH_LOCAL=1` | Local | Local |
| No alive workers | Local, silently | Local, with a `UserWarning` |
| Coordinator unreachable, connection refused, job submission rejected | Local, silently | Local, with a `UserWarning` |
| Function cannot be serialized | Local, silently | Local, with a `UserWarning` |
| No worker satisfies `gpu=` / `cores=` / `memory=` | **`ValueError`** | **`ValueError`** |
| The mesh object's `distribute()` cannot carry the constraint | n/a | **`PlacementUnsupportedError`** |
| Job accepted, then timed out | **`TimeoutError`** | **`TimeoutError`** |
| Job accepted, task failed on a worker | **`RuntimeError`** | An `{"_error": ...}` entry in that task's slot |

A single call raises on a failed task because it has exactly one result to
return and no way to express half of it; `.map()` returns one entry per
parameter set and reports the failure in the slot it belongs to, so one bad
parameter set does not discard the other ninety-nine.

Two rows deserve their reasoning spelled out, because both used to fall back
and no longer do:

- **`TimeoutError` propagates.** An unschedulable job quietly becoming a local
  run after 300 seconds is a worse outcome than an error: the caller waited out
  the entire timeout precisely because they wanted mesh execution, and the job
  is still on the coordinator. Note the corollary: a `TimeoutError` here does
  not always mean "your function is slow". It also covers a coordinator that
  became unreachable for the whole polling window, and the message says which
  case it was.
- **`PlacementUnsupportedError` propagates.** A `gpu=`/`cores=`/`memory=`
  requirement is a correctness statement, not a preference; the whole point of
  `@accelerate(mesh, gpu="A100")` is that the work does *not* run on the laptop
  that happens to be handy. Falling back there delivers the exact outcome the
  constraint exists to prevent.

Resource validation (`gpu=`, `cores=`, `memory=`) is a snapshot taken at call
time, and it is deliberately **fail-open on ignorance and fail-closed on
measurement**: a worker that reports no capacity for a resource is unknown and
passes, because the coordinator's scheduler is the real enforcement point and
refusing here only stops the task from reaching it. But a worker that reports
`0` has been measured, and 0 satisfies no positive requirement. Validation is
also never more permissive than the coordinator's scheduler, so a task that
passes it cannot then be un-leasable.

### Not public

- Every submodule not reachable through a name above: `gpumesh.server`,
  `gpumesh.db`, `gpumesh.worker`, `gpumesh.sandbox`, `gpumesh.serializer`,
  `gpumesh.capability`, `gpumesh.claimer`, `gpumesh.discovery`,
  `gpumesh.security`, `gpumesh.ansi`, `gpumesh.utils`, `gpumesh.cli`, …
  They are importable because Python has no way to stop you, not because they
  are supported. `gpumesh.worker.MeshClient` in particular is a convenience
  used by this project's own tests; if you want a stable client, use the HTTP
  protocol directly (§3). It *is* versioned.
- Any name beginning with `_`.
- The SQLite schema in `gpumesh.db`. It is an implementation detail of one
  process, is not a migration target, and is not readable across versions.
- Console output, log line formats, and the wording of exception messages.
  Match on exception *types*, never on message text.

---

## 2. The CLI

**Human-readable stdout is explicitly unstable.** Column widths, colours,
emoji, box drawing, progress lines and phrasing change whenever they get
better. Do not parse them. Where a command offers `--json`, that shape is
public and changes only by adding keys.

**Exit codes are public API.** Changing the meaning of one is a MAJOR change.

| Code | Meaning |
|---|---|
| `0` | Success. Also a clean interrupt of a long-running command (`serve`, `join`). A worker you stopped with Ctrl+C did not fail |
| `1` | The command failed: coordinator unreachable, token rejected, job failed, port in use, no such job. Also `gpumesh` with no subcommand (it prints help), and a *second* Ctrl+C during shutdown, which abandons the graceful path on purpose |
| `2` | Usage error, emitted by `argparse` for an unknown subcommand, a missing required argument or a bad flag |

Scripts should test for zero versus non-zero. gpumesh does not currently
allocate distinct codes per failure kind; if it ever does, new codes will be
carved out of the "non-zero" space, which is a MINOR change, and `1` will keep
meaning "failed".

**Flags.**

- Adding a subcommand, or adding an optional flag, is **MINOR**.
- Removing a subcommand or a flag, renaming one, or **changing a flag's
  default**, is **MAJOR**. A default is not a detail: `gpumesh serve` binding
  to loopback instead of `0.0.0.0` changed which machines could join, and a
  script that relied on the old default silently stopped working.
- Making a previously optional argument required is **MAJOR**.
- Environment variables read by the CLI (`GPUMESH_TOKEN`, `GPUMESH_HOST`,
  `GPUMESH_HOST_IP`, `GPUMESH_CLAIM_HOST`, `GPUMESH_COLOR`) follow the same
  rules as flags, including the clause above about defaults. Narrowing the set
  of values one accepts counts: `GPUMESH_HOST_IP` began rejecting hostnames,
  which is a behaviour change for anyone who had one there, even though every
  value that ever *worked* still works.

---

## 3. The HTTP protocol

Versioned by an integer, `gpumesh.PROTOCOL_VERSION`, deliberately independent
of `__version__`. It moves only when the wire changes: a new required field, a
changed meaning for an existing field, a new endpoint a peer must have. A
patch release does not move it, which is the point. Otherwise every Tuesday's
bugfix would refuse every worker that had not been upgraded that afternoon.

The endpoint-by-endpoint reference is [protocol.md](protocol.md). This section
covers only compatibility.

### The window

**A coordinator supports protocol version N and N−1. So does a worker.**

Today: `PROTOCOL_VERSION = 2`, `MIN_PROTOCOL_VERSION = 1`.

One version of slack is what lets an operator upgrade machines one at a time
instead of holding a flag day, which matters enormously here, because the
machines belong to other people and get upgraded when they feel like it. It is
not wider than one because every supported version is a branch the code has to
keep implementing and CI has to keep exercising (`.github/workflows/compat.yml`
doubles in size per extra version), and "supported forever" is exactly how a
project ends up with the undiagnosable cross-version misbehaviour this
handshake was added to delete.

### Version 1 is the protocol with no version in it

Every gpumesh up to and including 1.3.0 sends no `protocol_version` at all.
That unversioned protocol is named, retroactively, **version 1**. Protocol 2 is
the same protocol plus the handshake fields, so the numbering starts at 2 with
a live N−1 branch rather than at 1 with a dead one.

**An absent `protocol_version` is mapped to 1**, a fixed constant, never
"whatever the current minimum happens to be". The distinction bites the day
`PROTOCOL_VERSION` becomes 3: a peer that sent no version is telling you it
predates versioning, which is evidence for 1 and never for 2. Pinning the
mapping to a moving minimum would silently re-admit ancient peers forever.

This is the same lesson the serializer learned the hard way with
`python_version` (see the comment block in `serializer.deserialize_function`,
and commit dd9f0dd): a missing field means *unknown*, treating unknown as a
mismatch broke real users, and the fix was to map the unknown to something
concrete and then apply the ordinary rule to it. Absent is not special-cased
into a refusal here, and it is not special-cased into an exemption either.

### The handshake

- A worker sends `protocol_version` in its `POST /api/register` body. Older
  coordinators ignore unknown keys, so sending it is safe against every
  gpumesh ever released.
- A coordinator answers a successful registration with its own
  `protocol_version` and `min_protocol_version`, and reports both on
  `GET /api/health`, so an operator can read the window without owning a
  worker that can join.
- A worker outside the coordinator's window is refused **at registration**
  with **HTTP 426 Upgrade Required** and a message naming both versions, which
  side is behind, and the command that fixes it. No row is written, no
  `worker_joined` event fires, and the worker never appears in `/api/workers`.
  426 rather than 400 on purpose: a worker seeing 400 cannot tell a version
  skew from a malformed body.
- A coordinator outside the *worker's* window is refused by the worker, on the
  registration response. This half cannot be delegated: a coordinator older
  than the worker has no gate at all. It accepts anything and then behaves
  however the difference makes it behave.
- Both refusals surface as `gpumesh.ProtocolVersionMismatch`, not as a bare
  `ValueError` and not through the worker's generic "failed to register"
  branch, whose troubleshooting advice (firewalls, 10061 vs 10060, "is the
  coordinator running?") is entirely wrong for a version skew.

Only the **worker** handshake is version-gated. Job submitters
(`POST /api/jobs`, `GET /api/jobs/<id>`) are not: they are frequently a
notebook on a fourth machine, and their real compatibility constraint is the
function envelope's Python version, which has its own rules in
[protocol.md](protocol.md).

### What moves `PROTOCOL_VERSION`

| Change | Effect |
|---|---|
| New optional field either side may ignore | No bump |
| New endpoint that old peers never call | No bump |
| New **required** field | Bump |
| Changed meaning or type of an existing field | Bump |
| Removed endpoint or field a peer relies on | Bump |
| Changed status code for an existing condition | Bump |

Each bump drops the oldest version out of the window, so peers that far behind
start being refused, clearly, at registration, naming both versions. That is
the deprecation, and it is why the number is allowed to move at all.

---

## 4. Python version support

`requires-python = ">=3.9"`. See the long note above it in `pyproject.toml`:
3.9 is kept deliberately, because the machines that make this project's pitch
true ("works on the junk you already own") are exactly the ones still on a
distro Python of 3.9.

- Dropping a Python version is a **MINOR** change, announced in the changelog.
  This follows the ecosystem's convention rather than SemVer's letter: the
  package still works identically on every interpreter it still supports, and
  `requires-python` means pip resolves an older gpumesh for you instead of
  installing a broken one.
- A version is dropped when the code actually needs a feature from a newer
  one, or when upstream security support for it ends, **not** because the
  calendar moved. Raising the floor also means dropping the classifier and the
  matrix entry in `.github/workflows/tests.yml`.
- Every supported version is in that matrix. A version not tested in CI is not
  supported, whatever `requires-python` says.

---

## 5. Deprecation policy

Nothing in `__all__`, no CLI flag, and no protocol version is removed without
a deprecation period first.

**Two releases minimum**, and the warning class changes between them:

1. **First release:** `DeprecationWarning`. This is nearly invisible on
   purpose. Python hides it by default outside `__main__`, so library users
   do not get scolded for their dependencies' choices. Test suites (pytest
   turns it back on) and developers running scripts directly do see it, which
   is the right first audience.
2. **Last release before removal:** `FutureWarning`. Shown by default,
   everywhere, to everyone. If a user is going to be broken by the next
   release, they get told by the current one.
3. **Then** it is removed, in a MAJOR release, listed in the changelog.

Every deprecation warning:

- uses **`stacklevel=2`**, so the file and line reported are the *caller's*,
  not a line inside gpumesh. A warning pointing at gpumesh's own source tells
  the reader nothing they can act on;
- states **what** is deprecated, **which version removes it**, and **what to
  use instead**. All three, in the message, because the message is often the
  only thing the user will read.

```python
import warnings

def submit_job(self, script, payloads, name=""):
    warnings.warn(
        "GPUMesh.submit_job() is deprecated and will be removed in gpumesh "
        "2.0. Use GPUMesh.submit(name=..., script=..., payloads=...) instead.",
        DeprecationWarning,   # FutureWarning in the release before removal
        stacklevel=2,
    )
    ...
```

CLI flags follow the same shape: the flag keeps working, and using it prints a
line to **stderr** naming the replacement and the removal version. Removing a
protocol version follows §3: it leaves the window and peers get the 426.

---

## Related

- [protocol.md](protocol.md), the endpoint-by-endpoint HTTP reference
- [../CHANGELOG.md](../CHANGELOG.md), what actually changed, per release
- [../SECURITY.md](../SECURITY.md), what a token grants
- `.github/workflows/compat.yml`: CI that runs a real task between the
  current tree and a released gpumesh, in both role assignments
