# Troubleshooting

| Problem | Fix |
|---------|-----|
| `command not found: gpumesh` | Use `python -m gpumesh` or check your PATH |
| HTTP 401, `Invalid token` | Use the same token on coordinator and worker |
| HTTP 401, `Too many attempts` | Not a token rejection — the token was not checked. Wait out the lockout and retry with the same token |
| **Another machine cannot connect at all** | The coordinator binds `127.0.0.1` by default. Restart it with `--host 0.0.0.0` (or `GPUMESH_HOST=0.0.0.0`). Check this **before** the firewall — a loopback bind and a blocked firewall look identical from the other machine |
| `--host-ip` set but still unreachable | `--host-ip` only changes the address that is *printed*. `--host` is the bind. Setting the first never opens the port |
| `GPUMESH_HOST_IP` seems to be ignored | It takes an IP literal, not a hostname — almost any typo is a syntactically valid hostname, so it is validated and a non-IP value is discarded with a `WARNING` line at startup. `gpumesh doctor` prints the address actually being advertised |
| Coordinator unreachable | Check firewall; is the coordinator running? |
| Task timed out | Increase `--timeout` or split tasks |
| Windows connection error | Run `gpumesh serve` as Administrator for firewall rules |
| Worker not showing up | Both on the same network? Try `gpumesh radar` |
| `ModuleNotFoundError: torch` | `pip install gpumesh[gpu]` |
| UDP broadcast not working | Use `gpumesh join URL` directly |
| `ModuleNotFoundError` inside a task | Install that package on the **worker** too — gpumesh ships your code, not your environment |
| `cannot send result of type ...` | Return plain data. Sockets, locks, database handles and live GPU handles can't cross machines |
| `NameError` inside a function that works locally | The worker is on a different Python minor version, so the function shipped as source text and lost its module-level constants and closures. Match versions, or move the constants inside the function |
| `Cannot run this task on a Python X worker` | Same cause, no source to fall back to — the function was defined in a REPL or heredoc. Put it in a `.py` file |
| No colour in a Docker log or CI pane | `isatty()` is false there, which is the correct default. Force it with `gpumesh serve --color` / `gpumesh join --color`, or `GPUMESH_COLOR=1` |
| `Refusing to register this worker: incompatible gpumesh protocol version` | The two machines are more than one wire-protocol version apart. The message names both numbers and which side is behind; `pip install -U gpumesh` on that side. Details in [`docs/stability.md`](stability.md) |
| Results differ from a local run | They shouldn't — file an issue. Confirm with `GPUMESH_LOCAL=1 python your_script.py` |

**Start with `gpumesh doctor`.** It is read-only, it never prints the token,
and its output is meant to be pasted into an issue. For *"workers can't
connect"* it shows in one screen whether the coordinator binds loopback, which
address this machine would advertise (and whether that address belongs to a
virtual adapter no other machine can route to), whether a Windows firewall
rule for the port exists, and whether the saved coordinator answers at all.
For *"NameError on a task"* it prints this machine's Python, gpumesh and
cloudpickle versions and flags any connected worker reported as differing —
which is the cause nearly every time. If the coordinator does not report
worker versions it says so and tells you to run `gpumesh doctor` on each
machine and compare the Python lines by hand. `gpumesh doctor --json` gives
the same report as a parseable document.

**Verbose logging:** `gpumesh -v serve` (the flag is global, so it works on any command) — **decorator routing messages:** `GPUMESH_VERBOSE=1 python my_script.py` — **force local-only:** `GPUMESH_LOCAL=1 python my_script.py`
