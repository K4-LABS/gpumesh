# Changelog

All notable changes to gpumesh will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0] - 2026-07-12

### Added
- **Python API for Jupyter/Colab notebooks** — `GPUMesh` class with `distribute()`, `workers()`, `start_coordinator()`, `add_worker()`, `results_to_dataframe()`
- **Function serialization** — cloudpickle primary, inspect.getsource fallback for sending functions to workers
- **Smart scheduling** — Heavy tasks automatically go to fast GPUs, light tasks to slow GPUs
- **CLI progress bar** — Real-time progress display with visual bars, per-worker status, and recent results
- **Enhanced Python API docs** — Comprehensive Jupyter/Colab examples in README including closures, lambdas, task costs, and error handling
- **Unit tests for progress bar** — 24 tests covering `_esc()`, `_bar()`, `_get_workers()`, `print_job()`, and `wait_for_job()`
- **Edge case tests** — Timeout, function errors, empty params, multi-worker distribute, closures, lambdas, auth failures
- **New test files** — `tests/test_api.py`, `tests/test_serializer.py`, `tests/test_api_edge_cases.py`, `tests/test_client.py`

### Changed
- Worker wraps function exceptions in `sandbox.TaskError` for proper error reporting
- Database queries use `ORDER BY rowid` for consistent task ordering
- `pyproject.toml` now includes `notebook = ["cloudpickle", "pandas"]` optional dependency
- README rewritten to be simpler, more precise, and include all limitations
- Added `device_name` column to workers table for better progress display
- Refactored `_get_lan_ip()` to shared utility `utils.py` to remove duplication across api.py, setup_wizard.py, and cli.py

### Fixed
- Results ordering bug in `distribute()` method (added `_task_index` to result)
- Empty params list handling (returns empty list early)
- Function timeout detection in worker
- Removed dead code (`run_function()`) from `sandbox.py`
- Fixed flaky Windows socket test with `@pytest.mark.xfail`

## [0.4.1] - 2026-07-12

### Added
- **Saved connections** - After first `gpumesh join`, coordinator URL and token are saved to `~/.gpumesh/config.json` so subsequent commands don't need `--url`/`--token` flags
- **Unit tests for connection_manager** - 20 tests covering save, load, clear, and get_connection functions

### Changed
- Updated README with saved connections documentation and examples
- Updated command examples to show `--url`/`--token` are optional after first join
- Clarified that coordinators need environment variables or explicit flags since they don't run `gpumesh join`

### Fixed
- Fixed docstring in connection_manager.py to show correct config path (`~/.gpumesh/config.json` instead of `~/.gpumesh_config`)

## [0.4.0] - 2026-07-11

### Added
- **Comprehensive beginner-friendly README** - Complete rewrite with step-by-step guides, all commands documented, troubleshooting section, and examples
- **Security hardening** - Removed hardcoded example tokens from documentation

### Changed
- Version bumped to 0.4.0 for PyPI release
- README now includes: installation guide, quick start, complete user guide, all commands reference, task script writing guide, network options, security best practices, troubleshooting, and architecture details

## [0.3.0] - 2026-07-11

### Added
- **Tailscale auto-detection** - Auto-detect Tailscale and use its private network
- `--tailscale` flag for `serve` and `quickjoin` commands
- `--port` flag for `quickjoin` command (configurable port)
- Improved tunnel error handling with graceful fallbacks
- 6 new tunnel tests (ngrok success, timeout, OS error)

### Changed
- Updated `pyproject.toml` URLs to use actual GitHub repository (Samurai007AK)
- Added CHANGELOG.md
- Updated CLI docstring with new options

### Fixed
- Fixed ngrok download error on Windows (now falls back gracefully)
- Fixed tunnel.py to handle `PyngrokNgrokInstallError` and `OSError`
- Fixed URL validation for `quickjoin --tailscale`

## [0.2.0] - 2026-07-11

### Added
- **Security features**:
  - Token hashing with SHA-256 and random salt
  - Rate limiting (5 attempts per 5 minutes, 15 minute lockout)
  - Optional IP allowlist for access control
- **Job cancellation** - Cancel running jobs with `gpumesh cancel JOB_ID`
- **One-click join** - `gpumesh quickjoin` command for easy worker setup
- **Auto GPU detection** - Detects NVIDIA GPUs via nvidia-smi
- **Auto PyTorch installation** - Installs PyTorch with CUDA when GPU detected
- **Version bump script** - `scripts/bump_version.py` for easy version management
- **Security tests** - 23 new tests for security features
- **PyPI metadata** - Added classifiers, keywords, and project URLs

### Changed
- Updated `tunnel.py` with better error handling
- Updated `README.md` with security documentation
- Updated `pyproject.toml` with MIT license (PEP 639 format)

### Fixed
- Fixed Windows process isolation in `sandbox.py`
- Fixed Unicode crash in `bump_version.py` on Windows

## [0.1.0] - 2026-07-10

### Added
- **Initial release**
- Coordinator server with threaded HTTP API
- Worker agent with heartbeat and task leasing
- SQLite database with WAL mode
- Capability-based scheduler (GFLOP/s scoring)
- Sandboxed subprocess execution
- Job submission and status polling
- Optional ngrok tunneling for NAT traversal
- CLI interface (serve, join, submit, status, workers)
- Example hyperparameter search task
- Basic tests

### Architecture
- Pure Python implementation (stdlib only for core)
- Optional dependencies: torch, psutil, pyngrok
- Process isolation with timeout and CPU limits
- Fault tolerance with lease reaper and bounded retries
- Pull-based model (workers fetch work)
