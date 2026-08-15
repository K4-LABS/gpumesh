# Good first issues — drafts to paste

This file is a staging area for the maintainer, not documentation for users.

GitHub's contribution-discovery surfaces (the `good first issue` filter, the
personalised "contribute" recommendations, and third-party sites that scrape
them) only see *issues*. A repository with a welcoming CONTRIBUTING.md and an
empty issue tracker is invisible to all of them. The five drafts below take the
"Good places to start" bullets in [CONTRIBUTING.md](../CONTRIBUTING.md) and make
them concrete enough to open as-is — each names the file and function to change,
how to verify it, and where to ask for help.

**How to use this file:** paste each block into a new issue, apply the labels
listed, then delete the block from here — or leave it, and this becomes the
record of what was seeded.

---

## 1. Name the coordinator address in "Could not reach coordinator" errors

**Labels:** `good first issue`, `bug`, `area:cli`

> ### What is wrong
>
> Six CLI commands report an unreachable coordinator like this:
>
> ```
> [ERROR] Could not reach coordinator: <urlopen error [WinError 10061] ...>
> ```
>
> The one fact that would let you fix it yourself — *which address and port were
> actually tried* — is missing. If you have joined a mesh before, the URL comes
> from the saved connection rather than from anything you typed, so "which
> coordinator?" is a real question and the message does not answer it.
>
> ### Where
>
> `gpumesh/cli.py`. The affected handlers are `cmd_status`, `cmd_cancel`,
> `cmd_retry`, `cmd_workers`, `cmd_devices` and `cmd_kill` — each has an
> `except (urllib.error.URLError, OSError) as exc:` branch printing either
> "Could not reach coordinator" or "Could not communicate with coordinator".
> Each of them already has `url` in scope from `_resolve_conn(args)`.
>
> ### What to change
>
> Include the URL in the message, and follow it with a hint on the next line —
> the existing `dim()` helper is used for hints elsewhere in the same file:
>
> ```
> [ERROR] Could not reach coordinator at http://192.168.1.10:8000: <exc>
>    Check the coordinator is still running, or 'gpumesh show-connection'
>    to see which URL is saved.
> ```
>
> Keep the two distinct phrasings ("reach" vs "communicate with") if they are
> distinguishing something real; unify them if they are not. Say which you
> concluded in the PR.
>
> ### How to verify
>
> `tests/test_cli.py` already exercises these handlers. Add a test that points a
> command at an address that cannot route — use `192.0.2.1` (reserved by
> RFC 5737 and guaranteed not to route; the test suite uses `192.0.2.x` for
> exactly this) — and assert the printed output contains that address. Then run
> `pytest tests/test_cli.py -v`.
>
> ### Help
>
> Comment here if any part is unclear and I will walk you through it — including
> where the tests capture stdout. This one touches no networking logic, so it is
> a safe first change.

---

## 2. Preserve the cause when `wait_for_job` gives up

**Labels:** `good first issue`, `bug`, `area:api`

> ### What is wrong
>
> `gpumesh/client.py`, in `wait_for_job()`:
>
> ```python
> if job is None:
>     raise RuntimeError(f"Failed to get status for job {job_id}")
> ```
>
> This is the end of a polling loop that has been swallowing `URLError` and
> `OSError` and retrying (`except (urllib.error.URLError, OSError): time.sleep(poll); continue`).
> By the time it raises, every piece of evidence about *why* the coordinator
> stopped answering has been discarded. The user gets one sentence that could
> mean a dead coordinator, a dropped Wi-Fi link, or a coordinator restarted with
> a fresh database.
>
> Compare the 404 and 401 branches a few lines above — those messages are good,
> and say what to do next. This one should match them.
>
> ### What to change
>
> Keep the last exception seen during polling and include it in the final
> `RuntimeError` — including how long the wait went on for, since `start_time`
> is already in scope. Something like:
>
> ```
> Gave up waiting for job abc123 after 120s. The coordinator at
> http://192.168.1.10:8000 stopped responding: <last error>.
> ```
>
> Use `raise ... from last_exc` so the traceback keeps the chain.
>
> ### How to verify
>
> Add a test in `tests/test_client.py` that starts a real server bound to
> `127.0.0.1:0` (see `tests/test_connect_integration.py` for the pattern), stops
> it mid-wait, and asserts the raised message names both the job and the
> underlying error. Give the docstring the reason the test exists, per the
> testing conventions in CONTRIBUTING.md.
>
> ### Help
>
> Ask here if the "bind to port 0 and shut it down mid-test" pattern is new to
> you — it is the standard shape in this suite and worth learning once.

---

## 3. Add a test that the version number agrees in every place it is written

**Labels:** `good first issue`, `bug` (no `area:` — this one spans the repo)

> ### What is wrong
>
> gpumesh's version is written down in **three** files that must agree, and
> nothing checks that they do:
>
> | File | Field |
> |---|---|
> | `gpumesh/__init__.py` | `__version__ = "1.3.0"` — what `gpumesh --version` prints |
> | `pyproject.toml` | `version = "1.3.0"` — what PyPI and `pip` see |
> | `CITATION.cff` | `version: 1.3.0` — what GitHub's "Cite this repository" renders |
>
> A release that bumps two of the three ships a package whose installed version
> disagrees with the version it reports about itself. That is a nasty one to
> notice, because everything still works — it only shows up in bug reports,
> where `gpumesh --version` is the field being used to decide whether a reporter
> is on the release that contains the fix. The bug report form asks for that
> value specifically, so it needs to be true.
>
> ### What to add
>
> A small test — `tests/test_version_consistency.py` — that reads all three and
> asserts they match. Give it a docstring saying which failure it prevents, per
> the testing conventions in CONTRIBUTING.md.
>
> One wrinkle worth knowing before you start: gpumesh supports **Python 3.9**,
> and `tomllib` only arrived in 3.11. Options, roughly in order of preference:
>
> - Read the `version = "..."` line out of `pyproject.toml` with a regex. Boring,
>   dependency-free, works everywhere, and this is a three-line file section that
>   is not going to grow a complicated grammar.
> - Use `importlib.metadata.version("gpumesh")` — but note this reads the
>   *installed* metadata, which in an editable install can lag behind the file
>   you just edited. That makes it a weaker check than reading the file.
>
> `CITATION.cff` is YAML; a regex on the `version:` line keeps the test free of a
> PyYAML dependency, which the suite does not otherwise need.
>
> ### How to verify
>
> `pytest tests/test_version_consistency.py -v` passes. Then confirm the test can
> actually fail: temporarily change one of the three to `9.9.9`, watch it go red,
> and change it back. A consistency test that cannot fail is worse than no test,
> so please do check this and say in the PR that you did.
>
> ### Help
>
> Ask here if you would rather do it a different way — there is no single right
> answer and I am happy to talk through the tradeoff.

---

## 4. Fix a README instruction that sent you the wrong way

**Labels:** `good first issue`, `documentation`, `help wanted`, `area:docs`

> ### What this is
>
> An open invitation rather than a specific defect. The README is written by
> someone who already knows how gpumesh works, which makes it structurally
> unable to notice its own gaps. If you followed it and something did not
> happen the way it said — a command that needed a flag the README omits, a step
> that assumed both machines were already on the same subnet, an output sample
> that no longer matches — that is the bug.
>
> ### What to change
>
> Open a PR against `README.md` with the correction. In the description, say
> **what you expected from reading it and what actually happened**. That sentence
> is the valuable part; it is the thing that cannot be recovered later, once you
> know how it works.
>
> Small is fine. One wrong flag is a real fix.
>
> ### How to verify
>
> Follow your own corrected instructions from a clean shell — ideally a fresh
> virtualenv — and confirm they work start to finish.
>
> ### Help
>
> If you are not sure whether something is wrong or you just misread it, comment
> here and ask. If two people misread it the same way, it is wrong.

---

## 5. Run gpumesh across two real machines and report what breaks

**Labels:** `help wanted`, `needs-repro`, `area:networking`

> ### Why this is worth a whole issue
>
> Not a beginner code task — this needs two machines rather than any particular
> skill, which is precisely why it is unfilled. The maintainer has a limited set
> of hardware and one network, and CI runs everything on loopback within a single
> runner. Nearly every remaining rough edge in gpumesh lives in the gap between
> those two facts.
>
> The setups most likely to surface something new:
>
> - A machine with a **VPN** running — Tailscale, WireGuard, corporate
> - A machine with **hypervisor or container adapters** — VirtualBox, VMware,
>   Hyper-V, Docker Desktop, WSL. These add local addresses that only the host
>   itself can route to
> - Two machines on **different subnets**, or on Wi-Fi with client isolation
> - **Mixed operating systems** — Windows coordinator with a Linux or macOS
>   worker, or the reverse
> - **Different Python versions on each machine.** This is the single most common
>   real-world failure, because `@mesh` ships your function across
>
> ### What to report
>
> Open a bug report per distinct failure, not one issue for the whole session.
> The bug form asks for exactly the right things. Please include:
>
> - `gpumesh --version` and `python --version` from **both** machines
> - The full output from both sides, tokens redacted
> - From the worker: `curl http://<COORDINATOR-IP>:<PORT>/api/workers`. HTTP 401
>   means reachable-but-wrong-token, which is a completely different bug from a
>   timeout
> - `ipconfig` / `ip addr` from the coordinator, so the adapter set is visible
>
> **A report that it worked fine is also useful** — comment here with the two
> setups. Knowing which combinations are clean is how the broken ones get
> isolated.
>
> ### Help
>
> If you get a failure and want help reading it before writing it up, paste it
> here. Half of these turn out to be one address being advertised that the other
> machine cannot route to, and that is diagnosable from the output.

---

# Label taxonomy

Create these before opening the issues above. Nothing here is exotic; the value
is in using a fixed set consistently, so that a filter is trustworthy.

## Type — exactly one per issue

| Label | Meaning | Notes |
|---|---|---|
| `bug` | Does not behave the way the docs say | Applied automatically by `bug_report.yml` |
| `enhancement` | A capability that does not exist yet | Applied automatically by `feature_request.yml`. **Use `enhancement`, not `feature-request`** — it is a GitHub default label that already exists, and an issue form referencing a label that does not exist silently drops it |
| `documentation` | README, CONTRIBUTING, docstrings, examples | |
| `question` | Should probably have been a Discussion | Answer it, link [SUPPORT.md](../SUPPORT.md), convert to a Discussion |

## Status — added and removed as an issue moves

| Label | Meaning |
|---|---|
| `needs-triage` | Not yet looked at. Applied by both issue forms; **remove it** once read, or it means nothing |
| `needs-repro` | Cannot reproduce with what is in the issue. The most common reason a gpumesh bug stalls, so it is worth naming rather than leaving the thread silent |
| `blocked` | Waiting on an upstream fix or on information from the reporter |
| `wontfix` | Deliberate design boundary. Say which one, in the closing comment |
| `duplicate` | Link the original |

## Contribution signals — spelling matters

| Label | Meaning |
|---|---|
| `good first issue` | Scoped so it can be done without understanding the whole codebase |
| `help wanted` | Input is wanted from outside — including things that are not beginner-friendly |

These two are the exact strings GitHub matches for its global
contribution-discovery feeds: the repository's own "Contribute" panel, the
personalised recommendations at <https://github.com/contribute>, and the
`good-first-issue` topic feeds that most "find an open source project" sites
scrape. **Lowercase, spaces not hyphens, singular `issue`.** `good-first-issue`,
`Good First Issue` and `good first issues` are all invisible to those feeds — the
issue still exists, it just never gets recommended to anyone, which defeats the
point of labelling it.

They are also the two labels [CONTRIBUTING.md](../CONTRIBUTING.md) links to by
name, so the links break if the spelling drifts.

## Area — zero or one per issue

Matched to this codebase's actual modules, so that an area label points at
files rather than at a vibe.

| Label | Modules |
|---|---|
| `area:networking` | `discovery.py`, `claimer.py`, `connection_manager.py`, `tunnel.py`, `utils.py` |
| `area:coordinator` | `server.py`, `db.py` (routes, persistence, the reaper thread) |
| `area:scheduling` | `db.py` (`lease_task`), `capability.py` — which worker gets which task |
| `area:worker` | `worker.py`, `sandbox.py`, `_function_subprocess.py` |
| `area:serialization` | `serializer.py`, `_function_subprocess.py` — the cross-version function transport, which is where the most common real-world failures land |
| `area:api` | `api.py`, `accelerate.py`, `mesh.py`, `client.py`, `jupyter_magic.py`, `torch.py` |
| `area:cli` | `cli.py`, `status.py`, `radar.py`, `setup_wizard.py`, `ansi.py` |
| `area:docker` | `Dockerfile`, `docker-compose.yaml`, the published image |
| `area:docs` | `README.md`, `CONTRIBUTING.md`, `examples/`, docstrings |

`area:serialization` and `area:worker` overlap on `_function_subprocess.py`
because that file genuinely belongs to both — it duplicates the encoding half of
`serializer.py` on purpose, since it runs as a standalone script with no gpumesh
package on its path. Pick whichever half the issue is about.

## Platform — only when a problem is OS-specific

`os:windows`, `os:macos`, `os:linux`. gpumesh supports all three and CI tests all
three, so "only on Windows" is a genuinely useful filter and not an excuse.
