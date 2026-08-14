# Changelog

All notable changes to gpumesh are documented here.

## [Unreleased]

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

### Tests
- 590 tests passing, up from 565 (25 new: result-envelope round trips
  including numpy, non-dict and container returns over a live mesh, retry
  classification, config-persistence safety, and package-level Jupyter hook
  registration).

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

### Tests
- 565 tests passing (3 new regression tests for the fixes above).

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
