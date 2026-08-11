# Contributing to gpumesh

Thanks for taking a look. gpumesh is a small, actively developed project and
contributions of every size are welcome — a typo fix, a bug report, or a new
scheduling strategy.

If you are unsure whether something is worth doing, open an issue and ask
before writing code. A short conversation is cheaper than a rewritten PR.

## Expect rough edges

gpumesh is early-stage — `Development Status :: 4 - Beta`. It works, it is
tested, and it is used, but it is not settled:

- Internal APIs change without deprecation cycles. The public surface
  (`@mesh`, `@accelerate`, `GPUMesh`, the CLI) is more stable than the rest.
- The hardest problems are in networking, and they are hard to reproduce.
  Most remaining bugs surface only across two real machines with a VPN,
  hypervisor adapter or firewall in the way — not on loopback.
- There is no CI yet, so **please run `pytest` locally before opening a PR**.
- Some errors still tell you the wrong thing. Fixing a misleading message is a
  genuinely useful contribution, not a nitpick.

New here? Issues labelled
[`good first issue`](https://github.com/Samurai007AK/gpumesh/labels/good%20first%20issue)
are scoped so you do not need to understand the whole codebase, and
[`help wanted`](https://github.com/Samurai007AK/gpumesh/labels/help%20wanted)
marks the ones where input is most valuable.

---

## Setup

```bash
git clone https://github.com/Samurai007AK/gpumesh.git
cd gpumesh
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

You should see **642 passed**. If you do not, that is a bug — please open an
issue with your OS and Python version before doing anything else.

Python 3.9 or newer. The only required runtime dependency is `cloudpickle`;
everything else (`torch`, `psutil`, `rich`, `questionary`, `pyngrok`, `pandas`)
is optional and guarded by a lazy import. Install the extras you need:

```bash
pip install -e ".[all]"         # everything
pip install -e ".[ui]"          # just the setup wizard deps
```

---

## Running it locally

You do not need two machines to develop. A coordinator and a worker on
loopback exercise almost every path:

```bash
# terminal 1
gpumesh serve --port 8000 --token devtoken

# terminal 2
gpumesh join http://127.0.0.1:8000 --token devtoken

# terminal 3
gpumesh workers
gpumesh submit examples/grid_search.py --payloads examples/payloads.json --wait
```

`gpumesh serve` already starts a self-worker, so terminal 2 is only needed
when you want to watch a worker's own output.

Useful environment variables while developing:

| Variable | Effect |
|---|---|
| `GPUMESH_LOCAL=1` | Force `@mesh` / `@accelerate` to run locally, bypassing the mesh |
| `GPUMESH_VERBOSE=1` | Log routing decisions and mesh submissions |
| `GPUMESH_HOST_IP=<ip>` | Pin the address the coordinator advertises to workers |
| `GPUMESH_COLOR=0` | Disable ANSI colour |

---

## How the pieces fit together

Worth reading before changing anything non-trivial.

```
client (@mesh / @accelerate / GPUMesh / CLI)
    |  HTTP + JSON, X-Auth-Token on every request
    v
coordinator  ── server.py (routes) ── db.py (SQLite: workers, jobs, tasks)
    ^                                   reaper thread: expired leases, dead workers
    |  workers poll for leases; coordinator never calls a worker
worker ── worker.py ── subprocess per task (sandbox.py / _function_subprocess.py)
```

| File | Responsibility |
|---|---|
| `server.py` | Coordinator HTTP API. Thin — logic belongs in `db.py` |
| `db.py` | All persistence and the scheduler (`lease_task`) |
| `worker.py` | Worker lifecycle: register, heartbeat, lease, execute, report |
| `sandbox.py` | Script tasks — payload on stdin, JSON result on last stdout line |
| `_function_subprocess.py` | Function tasks. **Standalone script**, cannot import gpumesh |
| `serializer.py` | cloudpickle function transport, and the result envelope |
| `accelerate.py` | The real decorator implementation |
| `mesh.py` | `@mesh` — zero-config wrapper over `accelerate.py` |
| `discovery.py` / `claimer.py` | UDP beacons and the claim handshake |
| `capability.py` | Hardware probe and benchmark scoring |

### Invariants worth knowing

Breaking one of these will fail tests in a way that looks unrelated, so they
are called out here.

- **A mesh call must return exactly what a local call returns.** Results cross
  a JSON-only hop through SQLite, so they travel in an envelope
  (`serializer.encode_result`). JSON-encodable values pass through untouched;
  everything else is cloudpickled.
- **`_function_subprocess.py` duplicates the encoding half of
  `serializer.py` on purpose.** It runs as a standalone script with no gpumesh
  package on its path, so it cannot import the module. Change one, change both.
- **A worker never exits on its own after registering.** Coordinator outages,
  laptop sleep and WiFi drops are survived with capped backoff and automatic
  re-registration. Only an explicit signal or a 401 stops it.
- **Deterministic failures are not retried.** A task whose own code raises is
  marked `user_error` and fails immediately; retries exist for flaky
  infrastructure, not for bugs.
- **The coordinator never guesses reachability.** It cannot know which of its
  addresses a given worker can route to, so it offers candidates and the
  worker picks. Do not reintroduce a single-address assumption.
- **Optional dependencies stay optional.** Import `torch`, `rich`, `psutil`
  and friends inside the function that needs them, never at module top level.

---

## Tests

```bash
pytest                                  # everything
pytest tests/test_db.py -v              # one file
pytest -k "lease" -v                    # by name
```

Every behavioural change needs a test. A few conventions:

- **Test the behaviour, not the implementation.** Assert on what a user
  observes, not on which private helper was called.
- **Docstrings say why the test exists.** Especially for regression tests —
  name the bug being prevented, so a future reader knows what breaks if the
  assertion is loosened.
- **Nothing may touch the real network or the real `~/.gpumesh/config.json`.**
  `tests/conftest.py` redirects the config directory automatically. For
  network paths, bind a real server to `127.0.0.1:0` (see
  `tests/test_connect_integration.py`) rather than mocking HTTP.
- **Use `192.0.2.x` when you need an unreachable address.** It is reserved by
  RFC 5737 and guaranteed not to route, unlike an arbitrary private IP.
- Tests must pass on Windows, macOS and Linux. Avoid POSIX-only assumptions,
  hardcoded path separators, and anything requiring a real GPU.

---

## Code style

**There is no linter or formatter configured on this project.** No black, no
ruff, no flake8, no mypy, no pre-commit hooks. That is deliberate for now, and
it means one thing for you:

> Match the style of the file you are editing. That is the whole rule.

Please do not add a formatter, reformat files you are not otherwise changing,
or bulk-fix whitespace. A diff that mixes a real change with a reformat is
hard to review and hard to revert. If you think the project should adopt a
formatter, open an issue and make the case — do not open a PR that applies
one.

What the existing code does, so you can match it:

- 4-space indent, double quotes. Most lines stay under 80 characters and
  almost none exceed 100 — no hard limit is enforced
- `from __future__ import annotations` at the top of modules using modern
  type-hint syntax on Python 3.9
- Type hints on public functions; not required on locals or tests
- Docstrings on modules, classes and non-obvious functions. They explain
  *why* something works the way it does, not what the next line says
- Optional dependencies imported inside the function that needs them, never
  at module top level
- Terminal output goes through `gpumesh.ansi.safe_print` and the colour
  helpers, never bare `print` with escape codes

---

## Pull requests

1. Branch from `main`: `git checkout -b fix/short-description`
2. Make the change, with tests
3. Run the full suite — `pytest` must be green
4. Update `CHANGELOG.md` under `## [Unreleased]`
5. Open the PR

**Branch naming:** `fix/`, `feat/`, `docs/`, `test/` or `chore/` followed by a
short hyphenated description — `fix/worker-timeout-message`,
`docs/windows-setup`.

**A good PR description answers three questions:**

- *What was wrong before?* The behaviour you observed, not the code you read.
- *What does this change?* One or two sentences.
- *How do you know it works?* The test you added, or the manual steps you ran
  — especially for networking changes, where "I ran it across two machines
  and here is the output" is worth more than any unit test.

Link the issue if there is one (`Fixes #12`).

**Commit messages:** explain *why*, not just what. The subject line says what
changed; the body says what was wrong before and why this is the right fix.
Look at recent commits for the house style.

**Scope:** one concern per PR. A bug fix bundled with a refactor and a
reformat is hard to review and hard to revert.

**Comments:** explain reasoning that is not evident from the code. Skip
comments that restate the line below them.

---

## Reporting bugs

Networking bugs are the hardest to diagnose remotely, so please include:

- OS and Python version on **both** machines
- `gpumesh --version`
- The exact command you ran and the full output
- For connection failures: the output of `ipconfig` / `ip addr` on the
  coordinator, and whether `curl http://<COORDINATOR>:8000/api/workers` from
  the worker machine returns anything (HTTP 401 means reachable)

An error message that is misleading is itself a bug worth reporting, even if
you worked out the real cause.

---

## Good places to start

- Run it on two real machines and report what breaks — most remaining rough
  edges are in networking, and they are hard to reproduce on loopback
- Improve an error message that sent you the wrong way
- Add an example under `examples/`
- Docs: anything in the README that turned out to be wrong or unclear

---

## Security

Do not open a public issue for a security problem. Use GitHub's private
vulnerability reporting instead — the **Security** tab on this repository,
then *Report a vulnerability*.

Please note what gpumesh already documents about its threat model: there is
no transport encryption, and workers execute whatever code the coordinator
sends. It is designed for trusted networks, with Tailscale for anything
beyond a LAN. Reports that restate those known properties are not
vulnerabilities — but a way to bypass token authentication, or to make a
worker execute code from an unauthenticated source, certainly is.

---

## License

By contributing you agree that your contributions are licensed under the
[MIT License](LICENSE) that covers this project.
