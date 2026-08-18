# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [3.0.0] — 2026-08-18

> **Why 3.0.0 and not 2.1.0:** under the project's stability policy
> (`docs/stability.md`), changing a flag's default is a **MAJOR** change, and
> this release changes the `--db` default (below). PyPI's 2.0.0 is also
> already published and immutable, so the unreleased work since it cannot ship
> under that number. This release additionally carries the AGPL relicensing,
> which applies going forward only.

### Fixed
- **Claim token no longer echoed in the radar flow.** `select_worker_for_claim`
  read the worker token with `input()`, echoing it in plaintext into the
  terminal and its scrollback. It now uses `getpass.getpass()`, matching the
  existing pattern elsewhere in the CLI; the peer-selection prompt is
  unchanged, and the non-interactive (EOF/`Ctrl+C`) paths behave identically.
- **Coordinator startup errors now say what to do.** `gpumesh serve` used to
  report every bind failure as "port already in use". Three common failures
  are now tailored: an unwritable `--db` path names the path (it previously
  escaped the OSError handler entirely as a raw `sqlite3.OperationalError`
  traceback, because that error is not an OSError); binding a privileged
  port (<1024) on POSIX explains that ports below 1024 need elevation
  instead of suggesting another port; and a `--host-ip` that is not an
  address of this machine is refused before binding, with the machine's
  actual addresses listed.

### Changed
- **The project is now licensed under the GNU AGPL-3.0** (previously MIT).
  The goal is copyleft protection for a network service: anyone who modifies
  gpumesh and runs it as a service must offer their modified source in turn
  (AGPL section 13). Already-released versions remain under the MIT terms
  they were published under; the new license applies going forward.
- **The container image moved to `k4-labs/gpumesh` and is now published from
  CI.** It was hand-published on a personal namespace and had drifted two
  minor versions behind the repository (the documented compose file pinned a
  tag that never existed). A new `docker` workflow builds on every PR that
  touches the image or package, and publishes on a release; `latest` moves
  only on a real release. The old `samurai007ak/gpumesh` tags remain up as
  they are, frozen.

### Added
- **CodeQL static analysis (SAST) in CI.** A new `codeql` workflow runs
  GitHub's taint/data-flow analysis on every push and PR and on a weekly
  schedule. For a project that deserializes callables received over a
  socket and hands script paths to subprocesses, this is the layer that
  catches the injection-shaped bugs style lint cannot.
- **A test-coverage floor in CI.** `pytest` now runs under `pytest-cov` with
  `--cov-fail-under=80`, so a PR that shrinks the suite's ~81% coverage
  fails instead of silently regressing. The floor is a guard, not a target;
  raise it as the low-coverage entry points get covered.
- **Dependency vulnerability scanning in CI.** A new `pip-audit` workflow
  builds the real wheel, installs it into an isolated venv, and scans the
  installed tree for known CVEs on every push/PR and weekly. This catches
  advisories against transitive dependencies, which Dependabot's update
  proposals never surface on their own.

### Fixed
- **The coordinator's job queue was lost on restart from a different
  directory.** `gpumesh serve --db` defaulted to the RELATIVE path
  `gpumesh.db`, so the database lived in whatever folder the coordinator
  happened to be started from; restart it from anywhere else and it opened a
  fresh, empty database, silently discarding every queued job. The default is
  now the stable per-user path `~/.gpumesh/gpumesh.db` (next to the saved
  connection), so jobs submitted while no worker is online wait in the queue
  and run when a worker joins — and survive coordinator restarts from any
  directory. An explicit `--db` still wins. Applies to `gpumesh serve`, the
  setup wizard, and `GPUMesh.start_coordinator()`.
- **Two coordinators could silently bind the same port on Windows.** A second
  `gpumesh serve` on an occupied port used to start anyway: `HTTPServer`
  inherits `allow_reuse_address = 1`, which on Windows lets a new process bind
  a port another process is actively listening on, with no error. The second
  coordinator then told its own worker "authentication failed" — connections
  reached the first coordinator, which held a different token — and any
  process on the machine, even one running as a different user, could bind
  over a live coordinator and receive its traffic. Both the coordinator and
  the claim server now set `allow_reuse_address = os.name != "nt"`, so on
  Windows a second bind is refused with a clear "port in use" error instead
  of silently succeeding.

## [2.0.0] — 2026-08-15

The theme of this release is **the defaults stop assuming you meant to be
reachable**. A coordinator's port is a remote-code-execution surface whose
only guard is the token, and it used to be handed to every machine on the
LAN — hotel and conference wifi included — by anyone who ran `gpumesh serve`
without thinking about the network.

The second theme is **failures say what is wrong**. A worker one release out
of step used to fail as a `NameError` three calls after joining; a mesh nobody
could reach used to fail as a firewall mystery; a task nobody could run used to
fail as a timeout naming neither the task nor the reason. This release adds a
wire-protocol handshake, a `gpumesh doctor` command, and a set of refusals that
name the machine, the version and the fix.

### Changed
- **BREAKING: `gpumesh serve` now binds `127.0.0.1` instead of `0.0.0.0`.**
  A mesh that worked before will stop accepting remote workers until the
  coordinator is restarted with `--host 0.0.0.0` (or `GPUMESH_HOST=0.0.0.0`).
  The reasoning is that a worker executes whatever Python the coordinator
  sends it, as the OS user that started the worker, with that user's files,
  GPUs, network and credentials — so "reachable from the LAN" means "arbitrary
  code execution as that user for anyone on the LAN holding the token". That
  is the same shape as TorchServe's CVE-2023-43654 and Dask's CVE-2021-42343.
  Exposure is now something you ask for and are told about: a wider bind
  prints a banner naming the user and device that tasks will run as.

  The migration is designed to be readable at the moment it bites. On a
  loopback bind the coordinator does **not** print a LAN join URL, because
  that URL cannot connect and printing it anyway is what sends someone off to
  debug a firewall for an hour. It says the coordinator is unreachable from
  other machines, why that is the default, and the exact command to change it.

  `GPUMesh.start_coordinator()` takes the same new `host` parameter, also
  defaulting to loopback.
- `docker-compose.yaml` passes `--host 0.0.0.0` (the container must bind
  wildcard for the compose network to reach it) while publishing to
  `127.0.0.1` on the host by default. `GPUMESH_BIND=0.0.0.0` is the explicit
  opt-in to LAN reachability, and the previous `"8732:8732"` — which bound
  every host interface — is gone.
- **A script task that fails deterministically now fails once instead of three
  times.** A non-zero *positive* exit status, no output at all, or a last
  stdout line that is not JSON are all the script's own verdict on itself: an
  uncaught exception exits 1, and re-running it produces the identical failure.
  Those are now marked `user_error` and failed immediately, which function
  tasks have done since they got a subprocess and script tasks were simply left
  behind. The sign matters and is not an oversight: a *negative* status is
  POSIX for "killed by a signal", which is the OOM killer's verdict on the
  machine rather than the code's on itself, so those keep the full retry
  budget — as do timeouts and lease expiry. Retries exist for flaky
  infrastructure, not for bugs.
- **BREAKING: `@accelerate(...).map()` no longer falls back to local execution
  once the coordinator has accepted the job.** It used to fall back on *any*
  exception from `distribute()`, so code could rely on "a mesh call always
  returns something". Two cases now raise instead:

  - A **`TimeoutError`** propagates. A job no worker could take quietly
    becoming a local run after 300 seconds is worse than an error — the caller
    waited out the whole timeout precisely because they wanted mesh execution.
  - A **`gpumesh.PlacementUnsupportedError`** (new, and in `__all__`) is raised
    when the mesh object's `distribute()` cannot carry the `gpu=`/`cores=`/
    `memory=` keywords at all. Against an older mesh object that raised
    `TypeError: distribute() got an unexpected keyword argument 'gpu'`, the
    blanket handler read it as "mesh trouble" and ran the batch locally behind
    a warning — so `@accelerate(mesh, gpu="A100").map(...)` *guaranteed* the
    work never touched an A100. A placement constraint is a correctness
    statement, not a preference.

  The line is **acceptance**, and it is not arbitrary: everything that fails
  before `POST /api/jobs` succeeds — unreachable coordinator, refused
  connection, no alive workers, an unserializable function — still falls back
  to local with a warning, because that is what makes a mesh optional. After
  acceptance the coordinator owns a job that may still run to completion, and
  running the same batch locally on top of it executes the work twice. For
  anything with a side effect that is worse than any exception.

  Migration: catch `TimeoutError` and `gpumesh.PlacementUnsupportedError` where
  you previously relied on a value always coming back, or set `GPUMESH_LOCAL=1`
  to opt out of the mesh entirely. The full split — which failure does which,
  for both `.map()` and a single call — is now written down in
  `docs/stability.md` §1 rather than left to be inferred from the code.

### Added
- `gpumesh serve --host <ADDR>` and the `GPUMESH_HOST` environment variable
  set the bind address. This is distinct from the existing `--host-ip`, which
  only changes the address *printed* for workers to dial; the two are now
  documented side by side because confusing them costs an hour of firewall
  debugging.
- **Tasks nobody can run are now failed instead of queued forever.** A task
  carrying a `gpu`, `gpu_memory_mb` or `cpu_cores` hint that no live worker
  satisfies used to sit pending until the client's own 300s timeout fired and
  reported "mesh task did not complete", which names neither the task nor the
  reason. `fail_unsatisfiable_tasks()` runs in the reaper, waits 60s (long
  enough for a GPU box that boots just after submission), and fails the task
  with a message naming the requirement that could not be met.
- `docs/protocol.md`: the HTTP API, every endpoint with its request and
  response shape, the task payload keys, the function and result envelopes,
  and the exact cross-Python-version function-shipping rules.
- **A versioned wire protocol, so a version skew fails as a version skew.**
  `gpumesh.PROTOCOL_VERSION` is an integer, deliberately not `__version__`.
  The coordinator and the workers are installed on different machines, by
  different people, at different times, and they drift — that is the normal
  state of a mesh of borrowed laptops, not a misconfiguration, so tying the
  handshake to the package version would mean every patch release refused every
  worker that had not been upgraded that afternoon. It moves only when the
  *wire* changes.

  A worker sends `protocol_version` on `POST /api/register`; the coordinator
  answers with its own `protocol_version` and `min_protocol_version`, and
  reports both on `GET /api/health` so an operator can read the window without
  first owning a worker that can join it. An out-of-window worker is refused at
  registration with **HTTP 426 Upgrade Required** and a message naming both
  versions, which side is behind, and the command that fixes it — before any
  row is written, so a refused worker leaves no ghost in `/api/workers`. The
  reverse case is checked by the worker itself, because a coordinator older
  than the worker has no gate at all. Both surface as
  `gpumesh.ProtocolVersionMismatch`, not through the worker's generic "failed
  to register" branch, whose firewall advice is entirely wrong here.

  It starts at **2**, not 1. Every gpumesh released up to and including 1.3.0
  sends no version at all, and that unversioned protocol is named 1
  retroactively — so the N−1 branch is exercised by every worker in the field
  on the day it ships, rather than being dead code. An absent
  `protocol_version` maps to the fixed constant 1, never to "whatever the
  current minimum happens to be": a peer that sent nothing is telling you it
  predates versioning. That is the same lesson the serializer learned the hard
  way with `python_version` in 1.2.1 — a missing field means *unknown*, and
  treating unknown as a mismatch broke real users.
- **`gpumesh doctor`** — read-only environment diagnostics, formatted for
  pasting into a bug report. Nearly every support thread on this project turns
  out to be one machine's environment disagreeing with another's: two Python
  minors, so a pickled function will not load on the far side; a CPU-only torch
  wheel on a box with a 4090 in it; a coordinator advertising an address no
  worker can route to; a firewall quietly dropping the port. Each of those cost
  a long round of "run this, paste the output" before anyone could name the
  problem. `doctor` is that whole conversation in one command.

  Two rules hold it together. It **changes nothing** — no firewall rule is
  added, no connection saved, nothing installed, because a diagnostic that
  changes the system destroys the evidence it was run to collect. And it
  **never prints the token**, because the token grants code execution on every
  machine in the mesh and the entire point is that this output gets pasted in
  public. It exits `1` only for a fault on *this* machine; no coordinator
  configured, or an unreachable one, is a warning and still exits `0`, so it
  works as a pre-flight check in a script. `--json` emits the same report as a
  parseable document, with collection-time warnings diverted to stderr so they
  can never land mid-document.
- **`--no-color`** on `serve` and `join`, as a mutually exclusive partner to
  `--color`. Those two commands are the ones that keep printing for hours into
  a Docker log, a systemd journal or a CI pane — exactly where `isatty()` says
  "no terminal" and the operator still wants, or emphatically does not want,
  colour. `GPUMESH_COLOR=0` remains the environment form.
- **`GPUMESH_CLAIM_HOST`** narrows the bind of `gpumesh worker`'s claim port.
  The default is still every interface, and deliberately so: unlike the
  coordinator — where running a coordinator and a worker on one machine is a
  real and common case — a claim server exists solely to be reached by a
  coordinator on *another* machine, so a loopback claim server is not a safer
  one, it is a broken one that fails as an unreachable-worker mystery. What was
  missing was never a smaller default, it was the ability to say something more
  precise than "everything" — a tailnet address, say. A non-loopback bind now
  prints a banner naming the OS user that claims will run as.

  It is a separate variable from `GPUMESH_HOST` on purpose. The two answer
  different questions and are routinely set on the same machine, and
  `GPUMESH_HOST=127.0.0.1` — an entirely sensible coordinator setting — would
  otherwise have quietly rendered every worker on that box unclaimable.
- `docs/stability.md`: what gpumesh's public API actually is (`__all__`, and
  nothing else), what a MAJOR/MINOR bump promises for the Python API and the
  CLI, `@accelerate`'s fallback contract — which failures fall back to local
  execution and which propagate, and why the line is drawn at job acceptance —
  the protocol compatibility window, the Python version support policy,
  and the two-release deprecation path. SemVer requires a project to declare a
  public API; until this page existed, every internal rename was arguably a
  breaking change and none was definitely one.
- `docs/why-not-ray-or-dask.md`: an honest comparison, including the cases
  where Ray or Dask is the right answer.
- `examples/README.md` and four new runnable examples — `hello_mesh.py`,
  `second_machine.py`, `param_sweep.py`, `numpy_result.py`, `worker_leaves.py`
  — covering the questions that come up in the first hour.

### Fixed
- **The scheduler handed the heaviest task to the weakest worker on a
  two-worker mesh** — the exact inverse of what it promises. Worker rank was
  computed by counting scores `<= mine`, which made the line 1-based: the
  weakest of N workers scored `1/N` rather than 0, shifting every worker one
  slot up the cost-sorted queue. Rank now counts strictly weaker peers, and
  tied scores take the midpoint of the slots they jointly occupy instead of
  all piling onto the lightest task.
- **`@accelerate(cores=...)` was advertised but never enforced.** The
  requirement was validated against the pool at call time and then dropped;
  nothing stopped the task from being leased by a worker with fewer cores.
  Workers now report `cpu_cores` and `gpu_memory_total_mb` at registration,
  and `_worker_can_run()` is a single source of truth consulted both by
  `lease_task()` (to pick eligible tasks) and by the unsatisfiable detector
  (to decide nobody can) — so the two can never disagree. A worker that
  reported no capacity reads back as 0, which cannot satisfy a positive
  requirement: a machine that never said how many cores it has is not one we
  can promise 64 of.
- **Straggler detection was silently disabled on two-worker meshes.** The
  median helper indexed the upper middle sample instead of averaging the two,
  making the slower of two workers its own yardstick, so "slower than twice
  the median" could never be true. The requesting worker is also now excluded
  from the median it is measured against, which is what the Petals-style rule
  actually means.
- **`NameError: name 'mesh' is not defined` on a task that ran fine locally.**
  When laptops run different Python minors the function ships as source text
  and is rebuilt with `exec()` — but the captured source included its own
  decorator lines, and `@mesh` is not a name in the shipped metadata. Stripping
  gpumesh's own decorators costs nothing, since the function runs bare inside
  the worker's subprocess and the decorator would add nothing there. (`dd9f0dd`)
- **A cross-version function with no source fallback crashed the worker
  instead of failing.** A function defined in a heredoc or an interactive
  session has no source file, so `inspect.getsource()` captures nothing and the
  metadata carries no `source`. On a worker running a different Python,
  `cloudpickle.loads()` of that foreign bytecode can *succeed* and hand back a
  function whose bytecode kills the interpreter the moment it is called — a
  native fault (`0xC0000005` on Windows) inside the task subprocess, with no
  traceback and nothing to diagnose. Deserialization now refuses up front,
  naming both Python versions and saying to define the function in a `.py`
  file. A clean error beats a dead worker. (`96eee91`)
- **GPU workers on torch >= 2.8 died at startup.** `get_device_properties()`
  renamed `total_mem` to `total_memory`, so the capability probe raised
  `AttributeError` before the worker ever registered. Both names are now read,
  and `AttributeError` joins the other GPU-probe failures as best-effort rather
  than fatal — a machine whose VRAM cannot be measured is still a usable
  worker. (`5b7dbcd`)
- **A multi-line decorator could be truncated into a `SyntaxError`.** When a
  function ships as source text (cross-version workers), gpumesh strips its
  own `@mesh` / `@accelerate` decorators and keeps every other one. The span
  search followed bracket nesting without tracking string state, so a
  decorator argument containing brackets — a triple-quoted note holding
  `) ) )` — ended the span early and left the closing quotes glued to the
  front of the `def`. Quote state is now threaded from line to line.
- **The source fallback silently produced the wrong callable.** A callable
  object is serialized under its *class* name, so rebuilding from source
  produced the class; the worker's `func(**params)` then constructed an
  instance and returned it as the task's result. Classes, async functions and
  bound methods are now rejected up front with a message explaining what to do
  instead, rather than failing confusingly deep inside the subprocess.
- **Shutting down a coordinator could throw away completed work.** The
  database was closed before the accept loop stopped, with a 1s sleep standing
  in for "handlers have drained". A handler still running after that second
  hit `sqlite3.ProgrammingError: Cannot operate on a closed database` and
  returned a 500, and every request arriving during that second was answered
  503 — including `/api/result`, so a task a worker had already finished was
  rejected at the door. The accept loop is now stopped first, collapsing the
  window to microseconds.
- **`shutdown()` left the listening socket bound.** A coordinator that had
  "shut down" still owned its port: restarting on the same port failed with
  `EADDRINUSE` on Linux, and on Windows the old socket lingered as a leaked
  descriptor. `server_close()` now runs on the shutdown path.
- **`shutdown()` before `serve_forever()` blocked forever.** `BaseServer.shutdown()`
  waits on an event only the accept loop sets, so calling it on a coordinator
  whose loop had not started yet hung — but skipping it when the loop *was*
  running left the coordinator serving after shutdown returned. Both cases are
  now tracked explicitly instead of guessed.
- **`--color` was accepted and read by nothing.** The flag had been declared on
  `serve` and `join` since it was added and never consulted, so the one
  situation it exists for — forcing colour into a log where `isatty()` is false
  — was the one situation where it did nothing. It is fiddlier than it looks,
  which is why it sat there: `ansi._SUPPORTS_COLOR` is evaluated once at import
  time, long before argparse has seen a command line, and several modules bind
  their own copy of that bool with `from .ansi import _SUPPORTS_COLOR`. Setting
  the environment variable alone would have fixed nothing in-process; rebinding
  only `ansi`'s global would have left the copies stale and the flag
  half-working, which is worse than not working. The flag now sets
  `GPUMESH_COLOR` (so the choice reaches the function-task subprocess a worker
  spawns, and any gpumesh module imported later), re-runs the detection, and
  rebinds every already-imported module holding a copy.
- **`cost` in a `.map()` payload broke the local fallback.** `cost` is a
  scheduler hint that rides at the payload's top level and is never an argument
  for your function — `GPUMesh.distribute` has stripped it since 1.1.0. But the
  local fallbacks in `.map()` handed the raw payload straight to the function,
  so a payload annotated exactly the way `examples/payloads.json` and the docs
  teach worked on the mesh and raised `unexpected keyword argument 'cost'` the
  instant the mesh was unavailable. That is precisely backwards: a fallback
  that only works when the thing it replaces is available is not a fallback.
  All three of `@accelerate`'s local paths and `@mesh`'s now share one helper,
  so a mesh call and a local call take identical arguments.

  Two deliberate non-changes. `__call__` does *not* strip `cost`, because there
  the payload is built from the caller's own arguments — `train(lr=0.1,
  cost=2.0)` can only mean the function declares a `cost` parameter, and
  stripping it would break that function on the local path, the mirror image of
  the bug above. And the placement hints (`gpu`, `gpu_memory_mb`, `cpu_cores`)
  are *not* stripped from `.map()` payloads: those are written at the top level
  by `distribute()` from the decorator's own keywords, so a key of that name in
  a user payload is an ordinary parameter and reaches the function on the mesh
  path too. Dropping them locally would invent a fresh asymmetry rather than
  remove one.
- **Log lines wrote raw escape bytes into redirected files.** The coloured
  formatter emitted ANSI unconditionally, so `gpumesh serve 2> run.log` filled
  the log with escape sequences. Colour is now gated on the project's single
  existing capability probe rather than a second detection scheme. One wrinkle
  worth knowing: that probe asks `stdout` while the handler writes to `stderr`,
  so for the uncommon shape where only stderr is redirected, `--no-color` or
  `GPUMESH_COLOR=0` is the escape hatch. Log handlers also no longer stack on
  repeated setup calls, and `--json-logs` now carries exception type, message
  and traceback instead of dropping them.
- **`GPUMESH_HOST_IP` accepted a typo and failed minutes later on another
  machine.** The value is copied verbatim into every advertised URL and every
  claim payload, so a bad one surfaced as a connection error on a machine whose
  operator never set the variable and had no way to guess what it was. It is
  now validated where it is read: an IP literal is required — almost any typo
  is a syntactically valid single-label hostname, which is exactly what let
  them through — and anything else prints one warning naming the value and
  falls back to auto-detection. A detected address that works beats a pinned
  one that cannot.
- **Address filtering excluded `100.0.0.0/8` when it meant `100.64.0.0/10`.**
  The check for addresses not worth advertising matched the string prefix
  `"100."`, which covers the Tailscale/CGNAT range but also eight times more
  ordinary space besides. Loopback, link-local and CGNAT are now tested as
  networks rather than as string prefixes. Duplicate candidate URLs are also
  filtered out.
- **`setup_torch()` returned `cuda:0` on a multi-GPU machine no matter which
  card the mesh had picked**, so it and `device()` disagreed about the same
  inventory row and one of them was always wrong. Both now map the inventory
  row to a local CUDA index, and validate it against `torch.cuda.device_count()`
  — a stale inventory row (a card since removed, an inventory predating a
  reboot) raises a message naming both numbers instead of surfacing much later
  as an opaque invalid-device error from torch.
- **`setup_torch(min_memory_mb=...)` could route to the one card with no room.**
  A GPU reporting 0 MB free was treated as having reported nothing and fell
  back to its *total*, so a fully occupied 24 GB card sailed through a
  `min_memory_mb=8000` filter. Absent now means unknown; zero means zero.
- **`@accelerate(memory=...)` still routed work onto a GPU with no free
  memory.** The same bug, in the other file, surviving the fix above: the
  capacity check read `gpu_memory_free_mb` through a truthiness chain, so a
  *reported* 0 was treated as "said nothing" and fell through to
  `gpu_memory_total_mb`, and a fully occupied 24 GB card satisfied
  `memory="8GB"`. The asymmetry that kept the chain alive here — `accelerate`
  validates a *request* and must not reject a mesh it cannot measure, while
  `torch` chooses a *device* and must not pick one it cannot verify — is sound
  for an **absent** key and does not extend to a reported zero. A worker that
  explicitly answers "0 MB free" has been measured, and it has none.

  Absent still means unknown and still passes validation, so a 1.3.0
  coordinator that reports no capacity keys at all is unaffected, and so is a
  NULL `cpu_cores` on a worker row written before the capacity migration.
  Validation also stays no more permissive than the coordinator's scheduler, so
  a task still cannot pass validation and then turn out to be un-leasable. One
  case is genuinely ambiguous and is now named in the error rather than guessed
  at: a worker reporting 0 free alongside a large total is either a full card
  or one that registered moments ago and has not sent its first heartbeat, and
  the message says that retrying in a few seconds tells them apart.
- **A mesh call that outlived its coordinator blamed the task.** Every failed
  status poll was swallowed, so a coordinator that shut down or dropped off the
  network mid-task ended as a bare `mesh task did not complete within 300s` —
  a message that sends the reader to look at their own function. Swallowing the
  individual failures is still right (one dropped packet must not fail a
  five-minute job), but the last one is now kept and reported, so the timeout
  says the coordinator became unreachable and names the error.
- **`device(mesh, -1)` quietly returned the last device.** Negative indices
  fell through into Python's own negative indexing, which is not a selection
  anybody asked for from an API whose indices name inventory entries and wrap.
  It now raises `ValueError` naming the index and the rule.
- **A failed `import torch` hid its own cause.** The `ImportError` gpumesh
  raised in its place said "install torch" and discarded the original message,
  which is unhelpful precisely when torch *is* installed and failing to load
  (a CUDA driver mismatch, a broken wheel). The original is now included and
  chained.
- **The token file's permissions could fail silently.** `~/.gpumesh/config.json`
  holds the token in plaintext at 0600; when the `chmod` failed, or the Windows
  `icacls` call could not run, gpumesh said nothing. It now names the file, says
  what failed, and says why it matters. It also warns when the file is found
  group- or other-readable on load.

### Security
- **The coordinator's request handling is bounded on all three axes.** A 60 s
  socket timeout closes a slowloris hole — without one, `rfile` reads block
  forever and, because this is a `ThreadingHTTPServer`, a peer opening
  connections that declare a body they never send pinned one thread per
  connection until the process died. Oversized bodies are refused on the
  declared `Content-Length` and then drained under both a byte cap and a
  wall-clock budget, so `Content-Length: 10000000000` buys a closed connection
  rather than a 10 GB read; chunked and length-less bodies are refused `411`
  rather than half-understood.
- **The worker claim port is hardened past what the coordinator needs.** It is
  the widest-open surface gpumesh ships and, unlike the coordinator, it
  physically cannot authenticate before parsing — the claim protocol carries
  the token in the request body. So the parse is made small, bounded and
  rate-limited instead: the rate limiter is consulted before `Content-Length`
  is read at all, the body cap is 16 KB (down from a 1 MB limit inherited from
  the coordinator, which carries serialized task payloads — nothing on this
  port ever does) and is refused on the declared length before any allocation
  proportional to it, draining is bounded at 256 KB with a 2 s timeout, and a
  15 s socket timeout closes the same slowloris hole. Rate-limit accounting is
  centralised in the one method both the stock and the worker-patched handler
  funnel through, so neither can drift from the policy. `GPUMESH_CLAIM_HOST`
  (above) is the one control here that reduces attack *surface* rather than
  cost per attempt.
- **Loopback is exempt from the token rate limiter.** Five mistyped tokens used
  to wedge a healthy mesh for fifteen minutes on the one machine that cannot be
  told to come from a different IP — and the coordinator's own self-worker and
  the operator's own CLI both arrive over loopback. The lockout had nothing
  left to protect there: rate limiting exists to stop someone *guessing* the
  token over the network, and anyone who can open a socket from `127.0.0.1` is
  already executing code on the coordinator host, where the token sits in argv,
  in the environment, and in `~/.gpumesh/config.json`. They read it; they do
  not guess it. Exempted outright rather than given a higher ceiling, because a
  higher ceiling still ends in a lockout on the one path where a lockout is
  never the right answer — it only moves the outage from five typos to fifty.
- **Loopback detection now covers the whole `127.0.0.0/8` range, `::1`, and
  IPv4-mapped forms such as `::ffff:127.0.0.1`.** A dual-stack listener on
  Windows and Linux reports loopback connections in exactly that mapped form,
  and `ipaddress.IPv6Address.is_loopback` is `False` for it. Anything
  unparseable — an empty string, a hostname, a spoofed header value — is not
  loopback: this function decides who skips rate limiting, so it fails closed.
- `SECURITY.md` and `THREAT_MODEL.md` document the trust boundaries, the
  personas and their granted capabilities, and a STRIDE table with residual
  risk; `SECURITY-INSIGHTS.yml` states the same in-scope/out-of-scope split in
  the OpenSSF machine-readable form. The short version has not changed and is
  not a bug: **a token is a license to execute arbitrary code as the worker's
  OS user**, and it runs in both directions, because results are deserialized
  by whoever submitted the task.

## [1.3.0] — 2026-08-14

The theme of this release is **the coordinator no longer guesses how workers
reach it**. Which of a coordinator's addresses is usable is a property of the
network between two machines, not of the coordinator alone — so a machine with
a VPN, hypervisor or container adapter could advertise an address only it could
route to, and every worker would time out against it.

### Fixed
- **The claim handshake asserted a reachability it never tested.** The
  coordinator sent one guessed address; the worker acked as soon as the token
  matched and only *then* tried to register. So the coordinator printed
  "claimed successfully!" while the worker was seconds away from timing out
  against an unusable address, and the failure appeared solely in the worker's
  own log where the coordinator never saw it. The coordinator now sends a
  ranked list of candidate URLs, the worker probes them and registers with the
  first that answers, and the ack reports which one worked. An unreachable
  coordinator returns HTTP 502 listing every address tried, leaving the worker
  claimable so a corrected retry succeeds. Coordinators sending only the old
  `coordinator_url` field are still accepted.
- **The advertised address was picked without consulting the routing table.**
  When the peer is known — which it is throughout the claim flow — the kernel
  can say exactly which local address reaches it. `utils.local_ip_for_peer()`
  asks it (a UDP `connect()`, which transmits nothing), and that answer now
  leads the candidate list instead of a guess drawn from an interface list.
- **`gpumesh serve` reported a self-check it had not performed.** It probed
  `127.0.0.1`, which always succeeds, and printed a green line that read as
  confirmation the advertised address worked. It now states that only loopback
  was tested.
- **A connection timeout was reported as a connection refusal.** WinError 10060
  (timed out) surfaces as Python's `TimeoutError`, which the worker's error
  classifier lumped in with `ConnectionRefusedError`, so a worker that could
  not reach the coordinator printed advice headed `10061 / ConnectionRefused`.
  The two need opposite advice — refused means nothing is listening there,
  timed out means the packets never arrived and the coordinator may be running
  perfectly — so users were sent to restart a healthy server instead of
  checking routing and firewalls. The two cases are now classified and
  explained separately, and the suggested `curl` check uses the URL that
  actually failed.
- `get_lan_ip()` accepted any private-ranged address, so on a machine running
  VirtualBox, VMware, Hyper-V, Docker or Windows connection sharing it could
  pick a host-only adapter. Known virtual adapter ranges are now ranked below
  real LAN addresses. This is a heuristic and cannot be complete — several
  ranges (`172.16/16` among them) are used by both real LANs and virtual
  adapters — so it only improves the fallback used where no peer is known.
  The candidate list above is what actually fixes the failure.
- `_is_private_ip()` no longer raises `ValueError` on a malformed `172.*`
  address.
- Five setup-wizard tests failed on a clean checkout. They assert on strings
  containing numbers and URLs, which rich highlights automatically, injecting
  ANSI escapes inside the asserted text. The test console is now plain, so the
  suite is green from a fresh clone.

- **`docker compose up` could never have worked.** The worker service ran
  `join --color` with no URL, but the coordinator URL is a required positional
  argument, so every worker exited immediately with an argparse error. Even
  with that fixed the mesh could not form: unlike `join`, `gpumesh serve` does
  not read `GPUMESH_TOKEN` from the environment, so the coordinator generated
  a random token while workers authenticated with `$GPUMESH_TOKEN` — a
  guaranteed 401. Both compose recipes now pass the URL and the token
  explicitly, and fail with a readable message when `GPUMESH_TOKEN` is unset.
  The same broken recipe was published on the Docker Hub page and is corrected
  there too.

- **Tasks could hang forever on Linux and macOS.** Both subprocess launch
  paths used `preexec_fn`, which Python documents as unsafe when the parent
  has threads — and the worker always has them (heartbeat thread, plus the
  coordinator's HTTP threads when running as a self-worker). A child forked
  while another thread held an internal lock could block between `fork` and
  `exec`, hanging the task where neither the task timeout nor the lease
  reaper could reach it. Windows was unaffected, having no `fork`, which is
  why this survived undetected. `worker.py` now uses `start_new_session=True`
  (the same `setsid`, performed safely by CPython), and `sandbox.py` applies
  the CPU rlimit inside the child through a launcher that preserves the
  script's own line numbers in tracebacks.
- **gpumesh did not work on Python 3.9 at all**, despite
  `requires-python = ">=3.9"` and a 3.9 classifier on PyPI. `capability.py`,
  `claimer.py` and `worker.py` used PEP 604 (`X | None`) annotations without
  `from __future__ import annotations`, so on 3.9 they were evaluated at
  definition time and raised `TypeError` on import — the package could not be
  imported, let alone run. Found by the new CI matrix within minutes of it
  being added. All three modules now defer annotation evaluation.
- **`gpumesh serve` ignored `GPUMESH_TOKEN` while `gpumesh join` honoured it.**
  A deployment that set the variable once for both roles gave the coordinator
  a random token while workers authenticated with the variable, so every
  worker was rejected with a 401 and nothing explained why. `serve` now reads
  it too; an explicit `--token` still wins.
- Tests no longer inherit `GPUMESH_*` variables from the developer's shell. A
  contributor who exported `GPUMESH_TOKEN` for their own mesh would have seen
  unrelated CLI tests fail.
- **Coordinator and worker startup stalled ~35s on machines with a broken
  reverse-DNS resolver.** Every `serve()`, `start_coordinator()` and claim
  server paid a ~35s `gethostbyaddr()` block: CPython's
  `HTTPServer.server_bind()` calls `socket.getfqdn(host)`, and `utils` asked
  `getaddrinfo(gethostname())` for the LAN-IP candidates. On the macOS CI
  runner — where the resolver stalls — the suite took over 25 minutes and hit
  the job cap; the same machine now runs it in ~7 minutes. The bind path uses
  a `ThreadingHTTPServer` subclass that skips the reverse lookup, and the
  hostname enumeration is time-boxed to 2s so a slow resolver degrades
  startup instead of freezing it.
- **A worker beacon crashed at startup when the network had no route for the
  `255.255.255.255` fallback broadcast.** The redundant send to the global
  broadcast address raised `OSError` (errno 65 on macOS) and killed the
  beacon before it ever advertised. That send is now tolerated — a machine
  with no route for it still broadcasts on its own subnet.
- **A `Listener` could leak its UDP port after `stop()`.** If the receive
  thread was slow to wake, `stop()` returned while the socket was still
  bound, and the next listener on the same fixed port (48900) failed with
  `EADDRINUSE`. `stop()` now closes the socket synchronously instead of
  waiting on the thread.

### Added
- Continuous integration: the suite runs on Linux (Python 3.9, 3.11, 3.12),
  Windows and macOS on every push and pull request. The README test badge is
  now live rather than a hand-edited number that could drift from reality.
- `CONTRIBUTING.md`: setup, a map of how the components fit together, the
  invariants that are easy to break unknowingly, and what to include in a bug
  report.
- GitHub issue templates for bug reports and feature requests. The bug
  template asks for the reachability check that distinguishes a firewall drop
  from a wrong token, because that is the question that resolves most reports.
- A Quickstart in the README that carries the real terminal output of each
  step, so a reader can tell a working run from a broken one.
- `gpumesh serve --host-ip <IP>` and the `GPUMESH_HOST_IP` environment
  variable pin the address advertised to workers. Auto-detection cannot always
  distinguish a real LAN interface from a VPN or hypervisor one, so a manual
  override is available when it guesses wrong.
- `gpumesh serve` and the setup wizard now list this machine's other addresses
  when more than one is available, flagging any that belong to a virtual
  adapter — shown up front rather than after a worker has already timed out.

## [1.2.0] — 2026-08-10

The theme of this release is **a mesh call behaves exactly like a local call**.
Previously the same function could return a different value — or fail outright
— depending on whether a worker happened to be connected.

### Fixed
- **Non-JSON return values failed the task.** Results crossed the wire as plain
  JSON, so returning a numpy scalar, numpy array, torch tensor or DataFrame —
  the most ordinary thing an ML function can do — failed with
  `result not JSON-serializable`, three times over after retries. Results now
  travel in an envelope (`serializer.encode_result`) that passes JSON values
  through untouched and cloudpickles anything else.
- **Mesh and local runs returned different shapes.** A function returning a
  non-dict (`return a + b`, `return [1, 2, 3]`) came back wrapped as
  `{"result": 5}` from the mesh but bare from a local run, so results silently
  changed shape depending on whether a worker was alive. Both paths now return
  exactly what the function returned.
- **`%load_ext gpumesh` raised `AttributeError`.** The documented Jupyter magic
  never worked: IPython looks up `load_ipython_extension` on the package, and
  only `gpumesh.jupyter_magic` exposed it. Both hooks are now re-exported from
  the package.
- **Any command with `--url`/`--token` overwrote the saved connection.**
  Resolving a connection is a read, but it persisted unconditionally, so one
  mistyped URL on a read-only command (`gpumesh workers --url ...`) silently
  destroyed a working saved connection. Only `serve`, `join`, `quickjoin` and
  `setup` persist now.
- **`mesh.device_count()` reported 0 on a CPU-only mesh** while `mesh.devices()`
  listed live machines, because it counted GPUs. It now counts every alive
  compute device; `mesh.gpu_count()` is the new GPU-only counter.
- **Deterministic failures were retried three times.** A task raising
  `ValueError` was re-run twice more to fail identically, tripling the error
  output and the time to see it. Failures caused by the task's own code now
  fail immediately; infrastructure failures keep the full retry budget.

### Changed
- **Mesh calls are roughly 4x faster to return.** A flat 2s worker lease poll
  plus a 1s client poll put a ~2s floor under every call. Both now start fast
  and back off, and the worker polls rapidly for a window after each task. A
  single round trip measured 2.1s → 0.5s, `.map()` of 3 tasks 4.1s → 1.2s. Idle
  workers settle back to a 1s poll.
- `GPUMesh.distribute(poll_interval=...)` now defaults to adaptive polling.
  Passing an explicit value still pins a fixed interval.
- Failures that cannot be sent back report the value's *type* and how to fix it
  instead of dumping an unreadable repr.
- README corrected throughout: single-call routing now matches the code, the
  security table no longer overstates token hashing, the Docker/host port
  difference is explained, and serialization and environment constraints are
  documented under Limitations and Troubleshooting.

### Added
- 25 new tests (590 passing, up from 565): result-envelope round trips
  including numpy, non-dict and container returns over a live mesh, retry
  classification, config-persistence safety, and package-level Jupyter hook
  registration.

## [1.1.0] — 2026-08-08

### Fixed
- **Windows: non-ASCII task results crashed with UnicodeEncodeError.** Task
  subprocesses wrote stdout in the ANSI code page (cp1252) while the parent
  read UTF-8, so any result containing e.g. "✓" or "é" failed spuriously.
  Subprocesses now run with `PYTHONIOENCODING=utf-8` (both script tasks in
  `sandbox.py` and function tasks in `worker.py`).
- **`distribute()` forwarded the scheduler's `cost` hint into the function**
  as a kwarg, so fixed-signature functions failed with "unexpected keyword
  argument 'cost'" whenever a payload carried the documented cost weight.
  The hint is now stripped from the function's args (it still controls
  scheduling at the payload level).
- **`@mesh` functions decorated before `connect()` stayed local forever.**
  `MeshFunction` now resolves the mesh lazily on first use, so the
  documented "decorate first, connect later" flow works.
- **Docker build would fail**: `.dockerignore` excluded `README.md`, which
  `pyproject.toml` references as the package readme (required by the wheel
  build inside the image).

### Changed
- README rewritten: cleaner, comprehensive, and PyPI-friendly, verified
  against the actual CLI/API.
- Dockerfile image label bumped to 1.1.0.

### Added
- 3 regression tests for the fixes above (565 passing).

## [1.0.0] — 2026-08-03

### Added
- **`@mesh` decorator + `from gpumesh import mesh`** — the "connect once,
  code normally" API. One import, one decorator line per heavy function;
  everything else stays normal Python. Works in VS Code, Jupyter, PyCharm,
  or a terminal.
- **`mesh.connect()` / `mesh.devices()` / `mesh.device_count()` /
  `mesh.total_score()`** — auto-connect from the saved config with explicit
  helpers on the exported `mesh` object.
- **Jupyter magics** — `%load_ext gpumesh` injects `@mesh` into the notebook
  and provides `%mesh_devices`, `%mesh_status`, `%mesh_connect`, and the
  **`%%mesh` cell magic** (wraps every function in a cell, like `%%time`).
- **Self-worker** — `gpumesh serve` and `GPUMesh.start_coordinator` now
  automatically add the coordinator's own machine (CPU/GPU) to the pool.
- **`--self-worker` / `--no-self-worker` / `--color` CLI flags** for `serve`,
  `--color` for `join`, `--safe-mode` for `quickjoin`.
- **`map()` fast-fail** — falls back to local execution with a clear warning
  when no workers are alive, instead of hanging.
- Worker resilience tests (outage -> coordinator restart -> task runs),
  dev-mode example (`examples/dev_mode.py`), and hermetic setup-wizard tests.

### Changed
- **Workers never die** — removed the permanent-exit thresholds. Workers
  retry with capped exponential backoff, auto re-register when the
  coordinator returns (laptop sleep, WiFi drops, coordinator restarts are
  all survived), and no exception type can crash the worker thread.
- **`from gpumesh import mesh` import-order bug fixed** — the package now
  binds `mesh` eagerly, so `import gpumesh.mesh` after `from gpumesh
  import mesh` (and vice versa) both resolve consistently.
- Setup wizard token validation now lives in wizard logic (not only the UI
  callback), and wizard tests are console-independent (no more
  `NoConsoleScreenBufferError` under git-bash/Windows).

### Fixed
- 19 setup-wizard test failures caused by prompt_toolkit on Windows.

## [0.9.0] — 2026-07-26

### Fixed
- **14 critical bugs fixed** across the codebase.
- Heartbeat `task_id` omission causing jobs to be silently dropped.
- `_remote_call` crash when peer connection returns `None`.
- Job polling retry on transient network errors (no more premature `None` result).
- Discovery not enabled on `serve` by default.
- Windows Firewall UDP 48900 rule not auto-created.
- `worker_stats` table column mismatch with new telemetry.
- Cost parameter accidentally stripped from job payloads.
- JSON serialization failure when sending `Path` objects to subprocess.
- `cloudpickle` import error not caught gracefully.
- Legacy token verification using wrong comparison.
- `quickjoin` failing due to `pip install` path issue.
- Worker peer key collision when multiple workers share a machine.
- `api_port=0` not allowed during development/testing.
- Graceful shutdown missing timeout, leaving zombie processes.
- Setup wizard firewall rule using wrong port.
- Token hash salt not deterministic.

### Added
- **Process isolation for remote jobs** — Tasks run in separate subprocesses (inspired by Exo).
- **Benchmark scoring system** — `run_benchmark()` returns a 0-100 score per worker (inspired by Exo).
- **Crash diagnostics** — Structured error reporting on worker failures (inspired by Exo).
- **TTL-based worker expiry** — Stale workers automatically pruned (inspired by Hivemind).
- **Straggler deprioritization** — Slow workers get fewer jobs (inspired by Hivemind).
- **GPU memory tracking** — `get_gpu_memory_usage()` reports VRAM usage (inspired by HAMi).
- **Memory-aware scheduling** — Workers selected by available VRAM (inspired by HAMi).
- **Node join/leave events** — `GET /api/events` endpoint for real-time mesh changes (inspired by Exo).
- **Graceful fallback** — `@accelerate` falls back to local execution if mesh is unavailable (inspired by PartaGPU).
- **Worker stats tracking** — `worker_stats` table records jobs, errors, latency per worker (inspired by Swarm-Tune).
- `submit()` and `status()` convenience methods on `GPUMesh` API.
- `GET /api/events` endpoint returns recent join/leave/error events.
- New `--no-discovery` flag for `gpumesh serve`.
- `_function_subprocess.py` for isolated task execution.

### Changed
- `@accelerate` now checks mesh for alive workers before falling back to local.
- Benchmark re-runs on worker reconnection.
- Improved topology logging on mesh join.
- Version bump to 0.9.0.
- Removed legacy agent worktrees, old setup docs, and build artifacts.

## [0.8.1] — 2026-07-14

### Fixed
- Setup wizard crashed on Python 3.10 due to import issues. Fixed module-level imports.
- Setup wizard showed "requires optional dependencies" even when packages were installed.
- ANSI escape codes displayed incorrectly on Windows terminals.
- Welcome messages in `__init__.py` now use proper color helpers.

### Changed
- Version bump to 0.8.1.

## [0.8.0] — 2026-07-14

### Added
- **`@accelerate` decorator** — Add `@accelerate(mesh)` to any function to use all connected GPUs automatically. Single calls run locally; `.map([...])` spreads across all mesh devices.
- **Hardware selection** — `@accelerate(mesh, gpu="A100")` targets specific GPU types.
- **Resource specs** — `@accelerate(mesh, cores=8, memory="16GB", timeout=300)` declares resource requirements.
- **Resource validation** — Checks worker capabilities before distributing; raises `ValueError` with clear message when requirements can't be met.
- **GPU memory detection** — `probe_device()` now reports `gpu_memory_total_mb`, `gpu_memory_free_mb`, `cpu_cores`. New `get_memory_info(device_index)` function for CUDA devices.
- **Auto device placement** — PyTorch `nn.Module` arguments are automatically placed on the best device.
- **`.to(device)` method** — `func.to("cuda")` returns new function bound to specific device.
- **`install(mesh)` hook** — Thread-safe global mesh setter; after `accelerate.install(mesh)`, use `@accelerate` without parentheses.
- **`setup_torch(mesh, min_memory_mb=0)`** — Auto-detects best device, optionally filtering by minimum free VRAM.
- **Safe printing for Windows** — `safe_print()` and `_safe_str()` handle cp1252 encoding gracefully; Unicode chars fall back to ASCII on terminals that don't support UTF-8.
- **Setup wizard UI polish** — Replaced raw ANSI escape codes with `rich` (panels, tables, live radar) and `questionary` (select, text, confirm prompts) for a beautiful terminal experience.
- **Claim-based connection model** — Workers broadcast their presence; coordinator discovers and claims them. No more manual IP configuration.
- **Timing-safe token comparison** — Uses `hmac.compare_digest()` to prevent timing attacks on token verification.
- **Race condition fixes** — Fixed claim port probing, worker re-claim, and shutdown handling.
- **Worker key collision fix** — Workers now use unique keys to prevent collisions when multiple workers join.

### Changed
- Version bump to 0.8.0.
- README rewritten for PyPI with accurate CLI commands and API examples.

## [0.7.4] — 2026-07-13

### Fixed
- Coordinator lifecycle issues. Server now stays alive after setup wizard completes.

### Changed
- Version bump to 0.7.4.

## [0.7.3] — 2026-07-13

### Fixed
- Worker heartbeat reliability. Workers now maintain stable connection to coordinator.

### Changed
- Version bump to 0.7.3.

## [0.7.2] — 2026-07-12

### Added
- Connection manager for persistent URL/token storage.
- Auto-reconnection on worker disconnect.
- Better error messages for common issues.

### Changed
- Version bump to 0.7.2.

## [0.7.1] — 2026-07-12

### Added
- Claim-based connection model for easier setup.
- Worker broadcasting on local network.
- Coordinator auto-discovery of workers.

### Changed
- Version bump to 0.7.1.

## [0.7.0] — 2026-07-11

### Added
- Basic distributed compute mesh.
- Coordinator/worker architecture.
- Task distribution and result collection.
- Token authentication.

### Changed
- Initial stable release.

<!--
Version links.

Only v0.5.0, v1.2.0 and v1.3.0 exist as git tags. v0.7.0 through v1.1.0 were
published to PyPI but never tagged, so `compare/` links for those versions
cannot resolve — they point at the PyPI release page instead of being
invented. MAINTAINER: tagging the historical release commits would let every
version get a real diff link:

    git tag v1.1.0 <sha> && git push origin v1.1.0     (and so on)

then replace the PyPI links below with compare/ links.
-->

[Unreleased]: https://github.com/K4-LABS/gpumesh/compare/v3.0.0...HEAD
[3.0.0]: https://github.com/K4-LABS/gpumesh/compare/v2.0.0...v3.0.0
[2.0.0]: https://github.com/K4-LABS/gpumesh/compare/v1.3.0...v2.0.0
[1.3.0]: https://github.com/K4-LABS/gpumesh/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/K4-LABS/gpumesh/releases/tag/v1.2.0
[1.1.0]: https://pypi.org/project/gpumesh/1.1.0/
[1.0.0]: https://pypi.org/project/gpumesh/1.0.0/
[0.9.0]: https://pypi.org/project/gpumesh/0.9.0/
[0.8.1]: https://pypi.org/project/gpumesh/0.8.1/
[0.8.0]: https://pypi.org/project/gpumesh/0.8.0/
[0.7.4]: https://pypi.org/project/gpumesh/0.7.4/
[0.7.3]: https://pypi.org/project/gpumesh/0.7.3/
[0.7.2]: https://pypi.org/project/gpumesh/0.7.2/
[0.7.1]: https://pypi.org/project/gpumesh/0.7.1/
[0.7.0]: https://pypi.org/project/gpumesh/0.7.0/
