# gpumesh — Pre-Launch Checklist

Everything that must be true before gpumesh is advertised publicly and people
are invited to contribute.

Legend: **V** = verified by running it, **F** = fixed during this pass,
**U** = user action required.

---

## A. Repository metadata & GitHub settings

- [x] **Repo topics** — 10 set (distributed-computing, gpu, machine-learning,
  python, mlops, distributed-systems, homelab, self-hosted, cli, docker). **V**
- [x] **Labels** — 11 project labels present (bug, friction, feature-request,
  network, windows, docker, gpu-detection, scheduling, docs, good first
  issue, help wanted). **V**
- [x] **Good-first-issues** — issues #1–#5 created with labels. **V**
- [x] **Discussions** — enabled; welcome post live at
  https://github.com/Samurai007AK/gpumesh/discussions/6. **V**
- [ ] **Pin the welcome discussion** — GitHub has no API for this; owner must
  open the discussion → `...` → *Pin discussion*. **U**
- [x] **Repo description** — set: *"Borrow your friends' GPUs: a distributed
  compute mesh in pure Python"*. **F** (via API)
- [x] **Homepage** — set to the GitHub repo. **F** (via API)
- [x] **SECURITY.md** — added: supported versions, private-vulnerability
  reporting, threat model, what counts as a vulnerability. **F**
- [x] **PR template** — `.github/pull_request_template.md` with the three
  questions CONTRIBUTING asks every PR to answer + a checklist. **F**
- [x] **Issue templates** — bug + feature templates exist and are form-based
  (`.github/ISSUE_TEMPLATE/*.yml`). **V**

## B. Documentation accuracy

- [x] **README** — Quickstart output is real captured terminal output; feature
  table, API examples, Security, Troubleshooting, Limitations all match the
  code. **V**
- [x] **README CLI table** — flag lists updated to match argparse exactly
  (`--db`, `--host-ip`, `--port`, `--timeout` additions); the blanket "all
  commands accept --url/--token" claim replaced with the accurate statement
  (job/monitoring commands only) plus a verified environment-variable table
  (`GPUMESH_URL`, `GPUMESH_TOKEN`, `GPUMESH_HOST_IP`, `GPUMESH_LOCAL`,
  `GPUMESH_VERBOSE`, `GPUMESH_COLOR`). **F**
- [x] **README test count** — Development section now says 649 (Linux) /
  645 + 3 skips + 1 xpass (Windows), matching the measured numbers. **F**
- [x] **CONTRIBUTING test count** — "642 passed" → 649 with the Windows
  caveat. **F**
- [x] **CONTRIBUTING "no CI yet"** — rewritten: CI runs on Linux/Windows/macOS
  for every push and PR; contributors should still run pytest locally. **F**
- [x] **CHANGELOG [Unreleased]** — added the reverse-DNS startup stall, the
  beacon crash on unroutable broadcasts, and the listener port leak. **F**
- [x] **DOCKER_HUB_README** — image table updated to 1.2.0 (stale size claim
  dropped — measured 206 MB uncompressed); "Token hashing ✅ SHA-256 + salt"
  corrected to match the README ("in memory only, never written to the
  database"). **F**
- [x] **README links** — all 12 real URLs return 200 (the rest are example
  addresses in code blocks). **V**
- [x] **`.env.example`** — already clean (no stray header). **V**
- [x] **Version strings** — 1.2.0 consistent in `pyproject.toml`,
  `gpumesh/__init__.py`, Dockerfile (now an ARG), README sample output;
  `gpumesh --version` prints exactly the README's sample. **V**

## C. Packaging & distribution

- [x] **Build** — `python -m build` produces a clean wheel + sdist. **V**
- [x] **`twine check`** — PASSED on both artifacts. **V**
- [x] **Artifact contents** — sdist includes README, LICENSE, CHANGELOG (added
  to MANIFEST.in) and examples; wheel carries all 26 modules. **F**
- [x] **Fresh-venv smoke install** — clean venv, wheel installed: import OK,
  `gpumesh --version` OK, `python -m gpumesh` OK, all 16 `--help` exit 0. **V**
- [x] **Python 3.9** — importability enforced by the green CI 3.9 job. **V**
- [x] **bump_version.py** — now also updates the Dockerfile version (ARG +
  label); verified with a same-version run. **F**
- [x] **Dockerfile** — version is a build `ARG` (`--build-arg VERSION=x.y.z`
  override); labels can no longer drift. **F**
- [x] **Docker image** — builds clean; full in-container e2e passed: coordinator
  + worker containers formed a mesh and a 3-payload script job completed with
  correct results. **V**
- [x] **docker-compose** — `docker compose config` validates; recipes match
  the image. **V**

## D. Tests & CI

- [x] **Full suite green** — Windows: 645 passed, 3 skipped, 1 xpassed in 406s;
  Linux (WSL, earlier run): 649 passed. No runtime code changed in this pass.
  **V**
- [x] **CI matrix green** — 5/5 jobs (Linux 3.9/3.11/3.12, Windows 3.11,
  macOS 3.12), macOS ~421s. **V** (run 31784135543)
- [x] **Hang watchdog** — `faulthandler_timeout = 300` present and must stay.
  **V**
- [x] **CI workflow** — clean; the temporary macOS diagnostic commits were
  reverted and the workflow matches the pre-diagnostic state. **V**
- [x] **No flaky tests** — the 3 Windows skips are intentional (2 pandas
  importorskips, 1 Windows SO_REUSEADDR) and the xpass is the documented
  non-strict Windows xfail. **V**

## E. Code hygiene & security

- [x] **No TODO/FIXME/HACK** in the codebase. **V**
- [x] **Optional dependencies lazy** — no top-level `import torch/rich/
  psutil/pyngrok/pandas` anywhere in `gpumesh/`. **V**
- [x] **Python 3.9 annotations** — `from __future__ import annotations` where
  PEP 604 is used (CI 3.9 job enforces). **V**
- [x] **Terminal output** — color helpers from `gpumesh.ansi`; `safe_print`
  guards the legacy-console path (verified: the em-dash mangle seen during
  testing was a capture-pipeline artifact — Python writes correct cp1252
  bytes). **V**
- [x] **Secrets scan** — no keys, tokens, or credentials in tracked files.
  **V**
- [x] **Dev artifacts removed** — `ci.log`, `ux_auth.db`, and the stale
  `.claude/worktrees` checkout deleted. **F**
- [x] **`.dockerignore`** — excludes the AI-tooling dirs, tests/scripts, and
  dev scratch so the build context stays lean. **F**
- [x] **dist/** — gitignored; regenerated during this pass, will rebuild at
  release. **V**

## F. Examples

- [x] **grid_search.py** — runs standalone (`echo payload | python
  examples/grid_search.py` → one JSON result line) and against every entry in
  `payloads.json` (rc=0). **V**
- [x] **dev_mode.py** — runs with `GPUMESH_LOCAL=1` (no coordinator needed):
  single call, `.map()`, and sweep all return correct values. **V**
- [x] **README example references** — commands match the files. **V**

## G. Release steps (owner actions, after everything above is green)

- [ ] **Version bump** to 1.3.0 (`python scripts/bump_version.py 1.3.0`),
  CHANGELOG entry finalized, tag `v1.3.0`. **U**
- [ ] **Publish to PyPI** — `python -m build && twine upload dist/*` (1.2.0
  already taken; this release carries the Unreleased fixes). **U**
- [ ] **Publish Docker image** — build + push `samurai007ak/gpumesh:1.3.0`
  and `latest`, update DOCKER_HUB_README tag table. **U**
- [ ] **Pin the welcome discussion.** **U**
- [ ] **Announce** — pick the channel (X, Reddit r/LocalLLaMA, r/Python,
  HN Show HN). **U**
