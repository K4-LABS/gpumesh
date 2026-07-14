# Changelog

All notable changes to gpumesh are documented here.

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
