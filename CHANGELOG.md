# Changelog

All notable changes to gpumesh are documented here.

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
