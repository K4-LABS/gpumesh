# Contributing to gpumesh

Thanks for taking a look. gpumesh is a small, actively developed project and
contributions of every size are welcome — a typo fix, a bug report, or a new
scheduling strategy.

If you are unsure whether something is worth doing, open an issue and ask
before writing code. A short conversation is cheaper than a rewritten PR.

If you have a *question* rather than a change — "how do I", "is this supposed
to work like this" — [SUPPORT.md](SUPPORT.md) says where it goes and what to
include. Participation here is covered by the
[Code of Conduct](CODE_OF_CONDUCT.md), and [GOVERNANCE.md](GOVERNANCE.md)
describes who decides what.

## Expect rough edges

gpumesh is early-stage — `Development Status :: 4 - Beta`. It works, it is
tested, and it is used, but it is not settled:

- Internal APIs change without deprecation cycles. The public surface
  (`@mesh`, `@accelerate`, `GPUMesh`, the CLI) is more stable than the rest.
- The hardest problems are in networking, and they are hard to reproduce.
  Most remaining bugs surface only across two real machines with a VPN,
  hypervisor adapter or firewall in the way — not on loopback.
- CI runs the full suite on Linux, Windows and macOS for every pull request
  and every push to `master` — but **please run `pytest` locally before
  opening a PR** anyway.
  The suite is real servers and sockets, and a red local run will be a red
  CI run.
- Some errors still tell you the wrong thing. Fixing a misleading message is a
  genuinely useful contribution, not a nitpick.

New here? Issues labelled
[`good first issue`](https://github.com/K4-LABS/gpumesh/labels/good%20first%20issue)
are scoped so you do not need to understand the whole codebase, and
[`help wanted`](https://github.com/K4-LABS/gpumesh/labels/help%20wanted)
marks the ones where input is most valuable.

**Claiming one:** comment on the issue saying you are taking it. That is the
whole process — there is no assignment queue and you do not need to wait for a
reply before starting. It exists so that two people do not spend an evening on
the same fix without knowing. If an issue has a claim comment older than about
two weeks with nothing since, assume it lapsed, say so in a new comment, and
take it. If you claim one and then decide against it, please say that too; it
costs you nothing and it un-blocks the next person. You do not need to claim
anything to open a PR — a small fix arriving unannounced is fine.

---

## Setup

```bash
git clone https://github.com/K4-LABS/gpumesh.git
cd gpumesh
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

You should see well over a thousand tests pass (Windows shows a few skips and
one xpass, all intentional). The count grows steadily, so do not compare it
against any number written down here — a *failure* is what matters. If anything
fails, please open an issue with your OS and Python version before doing
anything else.

Python 3.9 through 3.14 — every one of those is a cell in the CI matrix, and
3.9 is kept deliberately (the long note on `requires-python` in
`pyproject.toml` says why). The only required runtime dependency is
`cloudpickle`; everything else (`torch`, `psutil`, `rich`, `questionary`,
`pyngrok`, `pandas`) is optional and guarded by a lazy import. Install the
extras you need:

```bash
pip install -e ".[all]"         # everything
pip install -e ".[ui]"          # just the setup wizard deps
```

`dev` is deliberately small — `pytest`, `pytest-cov`, `build`, `twine` — because
it is installed by every cell of the matrix. That does mean a bare
`pip install -e ".[dev]"` silently *skips* the tests guarded by
`importorskip`. To run the same set CI runs:

```bash
pip install -e ".[dev,ui,notebook]" numpy
```

`numpy` is not a gpumesh extra; several tests use it to check that a non-JSON
return value survives the round trip through the coordinator, which is exactly
the coverage most worth having.

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

Since 2.0.0 `gpumesh serve` binds `127.0.0.1` by default, so the loopback
recipe above works as written — but a second machine will not reach it until
you start it with `--host 0.0.0.0` (or `GPUMESH_HOST=0.0.0.0`) and read the
warning that prints.

`gpumesh doctor` is the fastest way to see what the code thinks your machine
looks like: the install, the interpreter, torch/CUDA, cloudpickle, the saved
coordinator, and every address this machine would advertise. It is read-only,
and `gpumesh doctor --json` gives the same report as a document you can diff
between two machines.

Useful environment variables while developing:

| Variable | Effect |
|---|---|
| `GPUMESH_LOCAL=1` | Force `@mesh` / `@accelerate` to run locally, bypassing the mesh |
| `GPUMESH_VERBOSE=1` | Log routing decisions and mesh submissions |
| `GPUMESH_HOST=<ip>` | Bind address for `gpumesh serve`, beating the `127.0.0.1` default. Same thing as `--host`; the flag wins |
| `GPUMESH_HOST_IP=<ip>` | Pin the address the coordinator advertises to workers. An IP literal, not a hostname — anything else is discarded with a warning |
| `GPUMESH_CLAIM_HOST=<ip>` | Bind address for the claim port opened by `gpumesh worker` (and by `gpumesh setup` in worker mode). Defaults to all interfaces (a loopback claim server cannot be claimed, which is its entire job), so this is how you narrow it to one. Separate from `GPUMESH_HOST` on purpose — a loopback coordinator is sensible, a loopback claim port is broken |
| `GPUMESH_COLOR=0` | Disable ANSI colour. `serve` and `join` also take `--color` / `--no-color` |

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
| `__init__.py` | The public surface (`__all__`), `__version__`, and `PROTOCOL_VERSION` / `MIN_PROTOCOL_VERSION` — the wire handshake, which is *not* the package version |
| `server.py` | Coordinator HTTP API. Thin — logic belongs in `db.py` |
| `db.py` | All persistence and the scheduler (`lease_task`), plus `_worker_can_run` — the single placement predicate shared by `lease_task` and `fail_unsatisfiable_tasks` so the two can never disagree about whether a task is runnable |
| `worker.py` | Worker lifecycle: register, heartbeat, lease, execute, report |
| `sandbox.py` | Script tasks — payload on stdin, JSON result on last stdout line |
| `_function_subprocess.py` | Function tasks. **Standalone script**, cannot import gpumesh |
| `serializer.py` | cloudpickle function transport, and the result envelope |
| `accelerate.py` | The real decorator implementation |
| `mesh.py` | `@mesh` — zero-config wrapper over `accelerate.py` |
| `api.py` | `GPUMesh` — the notebook-facing class (`workers()`, `distribute()`) |
| `client.py` | Submit / poll / render helpers shared by the CLI and `api.py` |
| `cli.py` | Every subcommand, including `gpumesh doctor`. By far the largest module; new commands go here |
| `connection_manager.py` | The saved coordinator in `~/.gpumesh/config.json`, so commands work without `--url` / `--token` |
| `security.py` | Coordinator-side token hashing, rate limiting and loopback/IP checks |
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

The project runs **ruff** and **mypy**, both configured in `pyproject.toml`.
They are not in the `dev` extra, so install them explicitly:

```bash
pip install -e ".[dev,lint]"
ruff check .
mypy
```

There is also a `.pre-commit-config.yaml`, which is optional but cheap:

```bash
pip install pre-commit && pre-commit install
pre-commit run --all-files      # to run the whole set once, on purpose
```

It is **not** a formatter either — the hooks either only touch files you are
already committing (trailing whitespace, end-of-file) or only report. What
earns its place is the rest: `check-yaml` over `.github/**` (a workflow that
does not parse silently does not run, and GitHub does not tell you loudly),
`check-toml` over `pyproject.toml`, `ruff --no-fix`, and two credential
scanners — `detect-private-key` and `gitleaks`. That last pair matters here
specifically: this repo has files that carry a live coordinator token, and a
`.gitignore` protects against `git add .` but not against `git add -f`.
Nothing in CI runs pre-commit, so it is a local convenience, not a gate.

**There is still no formatter.** `ruff format` and black are deliberately not
run over this repo, and `E501` (line-too-long) is in ruff's ignore list for that
reason. So the old rule survives intact:

> Match the style of the file you are editing.

Please do not add a formatter, reformat files you are not otherwise changing, or
bulk-fix whitespace. A diff that mixes a real change with a reformat is hard to
review and hard to revert. The line-length setting is 120 because that is what
the code already is — it was measured, not chosen as an ideal — so it flags the
genuine outliers and leaves everything else alone.

### What the linters are actually for

`ruff check` selects pycodestyle, pyflakes, isort, pyupgrade, bugbear,
comprehensions, **flake8-bandit (`S`)** and ruff's own rules. The `S` rules are
the reason the config exists at all. gpumesh accepts pickled callables over a
socket and runs them, so pickle, `eval`/`exec`, `subprocess` and URL-open call
sites are the project's central risk. The rule is not "make the warning go
away":

- Where a hit is intentional, silence it **on the line, with a reason**:
  `# noqa: S301  - payload is authenticated upstream by the token check`.
  That turns an unexamined risk into a documented one, which is the entire
  point.
- Where a hit is a surprise, it is probably a real finding. Say so in the PR.
- Blanket `# noqa`, file-level disables, or widening `ignore` in
  `pyproject.toml` will get a review comment. Per-file ignores already exist for
  `serializer.py` and `_function_subprocess.py`, where pickling *is* the design.

`mypy` is deliberately gradual, not strict: `check_untyped_defs` and
`disallow_untyped_defs` are both off, because the codebase is only partly
annotated and flipping strict on today would produce a lint job nobody reads.
What is on catches the things that are always bugs — unreachable code, missing
returns, bad casts, implicit `Optional`. The package ships `py.typed`, so the
annotations you write are read by downstream users' type checkers; that makes
annotations on the public surface worth more than annotations anywhere else.

A module being annotated well enough to tighten is a good PR on its own.

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

## What CI does to your PR

Five workflows fire on every pull request. Knowing which of them can go red for
a reason that is not your fault saves a confusing afternoon, so here is the
whole set — the files are in `.github/workflows/` if you want the reasoning,
which is written in the comments.

| Workflow | What it does | Red means |
|---|---|---|
| `tests` | `pytest` on Ubuntu × Python 3.9–3.14, plus Windows 3.11, Windows 3.13 and macOS 3.12 | A real failure. Nine cells, `fail-fast: false`, so you see all of them |
| `tests` (lint job) | `ruff`, then `mypy` | Mostly advisory — see below |
| `build` | Builds the sdist and wheel **twice** and compares wheel hashes, then `twine check --strict`, `check-wheel-contents`, `check-manifest`, and asserts `py.typed` shipped. Then installs the sdist on Ubuntu 3.9, Ubuntu 3.13 and Windows 3.12 and imports it from a directory that is not the repo | Usually a packaging change: a new data file that `MANIFEST.in` does not carry, or a build that is not reproducible |
| `codeql` | GitHub's taint/data-flow analysis. This project deserializes callables off a socket, so it is the layer that catches injection-shaped bugs lint cannot | A finding to answer, not necessarily to fix — say which in the PR |
| `pip-audit` | Builds the real wheel, installs it into a clean venv, and audits the *installed* tree for known CVEs | Almost always an upstream advisory, not your patch. Say so and it will be handled |
| `compat` | Runs a real task between your tree and a **released** gpumesh, in both role assignments, and checks that an unsupported version is refused cleanly | You changed the wire. See `PROTOCOL_VERSION` below |

**The coverage floor is 80%** — the test job runs
`pytest --cov=gpumesh --cov-fail-under=80` and a PR that drops below it fails
even when every test passes. The suite sits around 81%, so the floor is a
regression guard rather than a target: it exists to stop coverage sliding, not
to demand you push it up. If your PR adds a module with no tests, this is the
check that will tell you.

**The lint job is deliberately split, and only half of it is a gate.** The
hard step is `ruff check --select E9,F63,F7,F82` — undefined names, syntax
errors, the handful of checks that are never a style opinion and always a bug
at runtime. It reports zero and must stay at zero. The full ruleset, the `S`
security summary and `mypy` all run with `continue-on-error: true`, because
the repo carries a lint backlog from before the config existed and a required
check that is red on day one is a check people learn to ignore. Their
annotations still appear inline in your diff, which is where they are useful.
So: **do not feel obliged to burn down unrelated ruff findings to get green** —
but do not add new ones either, and read the annotations on the lines you
touched.

To reproduce locally what actually blocks:

```bash
pytest --cov=gpumesh --cov-fail-under=80
ruff check --select E9,F63,F7,F82 .
python -m build          # needs pip install -e ".[dev]"
```

---

## Pull requests

1. Branch from `master`: `git checkout -b fix/short-description`
2. Make the change, with tests
3. Run the full suite — `pytest` must be green — plus `ruff check .` and `mypy`
   on the files you touched (see [what CI does](#what-ci-does-to-your-pr) for
   which of those actually block)
4. Update `CHANGELOG.md` under `## [Unreleased]`
5. **If you changed the wire, bump `PROTOCOL_VERSION`** — see below
6. Sign off your commits — `git commit -s` (see [below](#sign-your-commits-dco))
7. Open the PR

**Changing the wire means bumping `gpumesh.PROTOCOL_VERSION`.** The coordinator
and the workers are installed on different machines by different people, so
they drift by default; the integer in `gpumesh/__init__.py` is what turns that
drift into a clear refusal at registration instead of a mystery failure three
calls later. A new *required* field, a changed meaning or type for an existing
one, a removed endpoint, or a changed status code for an existing condition all
bump it. A new *optional* field either side may ignore, and a new endpoint old
peers never call, do not. The support window is exactly N and N−1, so each bump
drops a version out of it — which is the deprecation, and
[`docs/stability.md`](docs/stability.md) §3 and §5 are the rules for doing it
deliberately. `.github/workflows/compat.yml` runs a real task between your tree
and a released gpumesh in both role assignments; if you bump the integer, look
at what that workflow now has to cover.

`docs/stability.md` is also the answer to "is this a breaking change?" for the
Python API and the CLI. If your change renames or removes anything in
`gpumesh.__all__`, or changes a CLI flag's default, read §5 before writing the
code — the deprecation path is two releases long and starts *before* the
removal.

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

### Sign your commits (DCO)

Every commit needs a `Signed-off-by:` line. Adding one is a single flag:

```bash
git commit -s -m "fix: report the address that was actually tried"
```

which appends

```
Signed-off-by: Your Name <your.email@example.com>
```

using your configured `user.name` and `user.email`. Set those once with
`git config --global user.name` / `user.email` and you can forget about it.

**Forgot on a few commits?** Fix them in place — no need to redo the work:

```bash
git rebase --signoff HEAD~3      # last 3 commits; use the count you need
git push --force-with-lease
```

`--force-with-lease` rather than `--force`, so you cannot clobber something
pushed to your branch in the meantime.

**What you are certifying.** The sign-off is the
[Developer Certificate of Origin](https://developercertificate.org/) — a short,
public statement that you wrote the change, or that you have the right to submit
it under this project's licence. It is not a copyright assignment and it is not
a CLA. You keep your copyright, you sign nothing, and there is no account to
create. It costs you one flag and it means that a future question about where a
line of code came from has a recorded answer.

### AI-assisted contributions

AI-assisted contributions are welcome. Two conditions:

- **Say so in the PR description.** One line is enough — which tool, and roughly
  what it did ("Claude wrote the first draft of the retry test"). If you prefer a
  trailer, `Assisted-by: <tool>` in the commit message works.
- **Confirm you have read and run the code yourself.** You are the author for
  every purpose that matters here: you sign it off, you answer review comments on
  it, and you are the one who understands why it works.

**PRs that appear to be unreviewed model output will be closed** without a
line-by-line review. That is not a judgement about the tool — it is that this
codebase's hard parts are the ones a model gets confidently wrong: the
`_function_subprocess.py` / `serializer.py` duplication, the retry
classification, and the coordinator's refusal to guess reachability all look
like redundancy or missing error handling if you have not read the reasoning
behind them. A patch that "cleans those up" costs more to review than to write.

### What to expect after you open it

**You can expect a response within 7 days.** If a week goes by with nothing,
ping the thread — that is the intended behaviour, not a nuisance. Two people
maintain this in their own time, so an honest slow answer is the one being
promised here rather than a fast one that is not kept.

Reviews are usually a conversation rather than a verdict. A request for changes
almost always means "this is worth landing, here is what is in the way".

---

## Reporting bugs

Networking bugs are the hardest to diagnose remotely, so please include:

- OS and Python version on **both** machines
- `gpumesh --version` — or better, `gpumesh doctor`, which is read-only and
  prints exactly this set of fields in one block meant for pasting
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
[Apache License 2.0](LICENSE) that covers this project.

The `Signed-off-by:` line on each commit ([DCO](#sign-your-commits-dco)) is the
record of that agreement. There is no CLA and no copyright assignment — you keep
the copyright in what you write, and it is licensed out under the same Apache-2.0
terms as everything else here.
