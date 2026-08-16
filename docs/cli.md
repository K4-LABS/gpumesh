# CLI reference

Two flags are global and go *before* the subcommand: `-v` / `--verbose` for
debug-level logging, and `--json-logs` to emit logs as JSON lines (for Docker).

## Server & connection

| Command | Description |
|---------|-------------|
| `gpumesh setup` | Interactive setup wizard (coordinator or worker) |
| `gpumesh serve` | Start the coordinator (`--host`, `--port`, `--token`, `--db`, `--host-ip`, `--public`, `--tailscale`, `--no-discovery`, `--safe-mode`, `--no-self-worker`, `--color`/`--no-color`) |
| `gpumesh join URL` | Join a mesh as a worker (`--token`, `--timeout`, `--safe-mode`, `--color`/`--no-color`) |
| `gpumesh quickjoin [URL]` | One-click: install, detect GPU, join (`--token`, `--tailscale`, `--port`, `--timeout`, `--safe-mode`) |
| `gpumesh worker` | Broadcast presence and wait to be claimed (`--token`, `--claim-port`, `--timeout`, `--safe-mode`) |
| `gpumesh radar` | Scan for nearby devices (live radar; `--mode coordinator` \| `worker`) |
| `gpumesh doctor` | Check this machine's environment and print a report (`--json`) |
| `gpumesh show-connection` | Show the saved URL + token |
| `gpumesh disconnect` | Clear the saved connection |

`serve` and `join` also take `--color` / `--no-color`. They are the two
commands that keep printing for hours into a Docker log or a systemd journal,
which is exactly where `isatty()` says "no terminal" and you may still want
colour — or emphatically not want it.

**`gpumesh doctor`** is read-only: it adds no firewall rules, saves no
connection, installs nothing, and never prints the token. It reports which
gpumesh is imported and from where, the Python interpreter, torch/CUDA (and
whether `nvidia-smi` disagrees with it), the cloudpickle version, the saved
coordinator and its workers, the addresses this machine would bind and
advertise, and on Windows whether a firewall rule for the port exists. It
exits `1` only for a fault on *this* machine — no coordinator configured, or
an unreachable one, is a warning and still exits `0`, so it works as a
pre-flight check in a script. `--json` emits the same report as a document
(warnings are diverted to stderr so the stdout stays parseable).

**`--host` and `--host-ip` are different things**, and mixing them up costs an
hour of firewall debugging:

| Flag | Controls | Default |
|------|----------|---------|
| `--host` | **The bind address** — who can open a connection at all. A security boundary | `127.0.0.1` (this machine only) |
| `--host-ip` | **The advertised address** — which address is printed for workers to dial. Cosmetic | auto-detected LAN IP |

Setting `--host-ip` alone never opens the port up. Use `--host 0.0.0.0` for
that, and read the banner it prints. `--host-ip` is for when auto-detection
picks a VPN or hypervisor adapter and remote workers time out against an
address only your machine can route to.

## Jobs

| Command | Description |
|---------|-------------|
| `gpumesh submit SCRIPT --payloads FILE` | Submit a script job (`--name`, `--wait` blocks until done, `--wait-timeout`) |
| `gpumesh status JOB_ID` | Show job progress and results |
| `gpumesh cancel JOB_ID` | Cancel a running job |
| `gpumesh retry JOB_ID` | Re-queue failed/timed-out tasks |
| `gpumesh kill [--force]` | Kill all tasks (graceful or immediate) |

Each payload object is handed to your script as JSON on stdin. Four keys are
read by the scheduler instead of being left to your code:

| Payload key | Effect |
|-------------|--------|
| `cost` | Relative task weight (default `1.0`); heavier tasks are routed to stronger workers |
| `gpu` | Device kind (`cuda`, `cpu`, `mps`) or model substring (`A100`); only matching workers are offered the task |
| `gpu_memory_mb` | Minimum free VRAM; a worker reporting less free memory than this skips the task |
| `cpu_cores` | Minimum CPU cores; a worker reporting fewer skips the task. This is the wire name for `@accelerate(cores=...)` |

These are **filters, not preferences**. A task nobody can satisfy stays
pending until the coordinator gives up on it (60s) and fails it with a message
naming the requirement, rather than queueing forever.

All four also apply to payloads passed to `mesh.submit(...)` from the Python
API, and to the payload dicts you hand to `.map()`. Full shapes in
[`docs/protocol.md`](protocol.md); what is versioned and what a bump
promises is in [`docs/stability.md`](stability.md).

## Monitoring

| Command | Description |
|---------|-------------|
| `gpumesh workers` | List connected workers and their status |
| `gpumesh devices` | Show all GPUs/CPUs as one unified pool |

Job and monitoring commands (`submit`, `status`, `cancel`, `retry`, `workers`, `devices`, `kill`) accept `--url URL --token TOKEN`. If you omit them, gpumesh falls back to the `GPUMESH_URL` / `GPUMESH_TOKEN` environment variables, then to the connection saved by `join`/`serve`.

## Environment variables

| Variable | Where it applies | Effect |
|----------|------------------|--------|
| `GPUMESH_URL` | Job/monitoring commands | Coordinator URL when `--url` is omitted |
| `GPUMESH_TOKEN` | Job/monitoring commands, `serve`, `join` | Auth token when `--token` is omitted (`quickjoin` and `worker` always require the flag) |
| `GPUMESH_HOST` | `serve` | Bind address when `--host` is omitted. `0.0.0.0` opens the port to other machines |
| `GPUMESH_HOST_IP` | `serve` | Pin the address advertised to workers (same as `--host-ip`) — does **not** change the bind. Must be an IP literal; a hostname is rejected with a warning and auto-detection is used instead |
| `GPUMESH_CLAIM_HOST` | `worker`, and `setup` in worker mode | Bind address for the **claim** port. Defaults to all interfaces, unlike the coordinator — a claim server exists to be reached by another machine, so loopback would not make it safer, only broken. Narrow it to one address (your tailnet, say) if you do not need all of them |
| `GPUMESH_LOCAL=1` | `@mesh` / `@accelerate` | Force local execution, never touch the mesh |
| `GPUMESH_VERBOSE=1` | `@mesh` / `@accelerate` | Print which device handled each task |
| `GPUMESH_COLOR` | All output | `1` forces colour, `0` disables it, `auto` (default) checks whether stdout is a TTY |

**`GPUMESH_CLAIM_HOST` is deliberately not `GPUMESH_HOST`.** The two answer
different questions and are routinely set on the same machine:
`GPUMESH_HOST=127.0.0.1` is a perfectly sensible coordinator setting, and if
the claim port read it too, every worker on that box would silently become
unclaimable. A non-loopback claim bind prints a banner naming the OS user that
claims will run as.

`GPUMESH_COLOR` is the environment form. `gpumesh serve` and `gpumesh join`
also take `--color` (force it on) and `--no-color` (force it off), which are
mutually exclusive. The flags work by setting `GPUMESH_COLOR` and re-running
the detection, so the choice reaches this process *and* the function-task
subprocess a worker spawns, which inherits the environment. (Script tasks run
with a deliberately minimal allowlisted environment and never see it.)

One wrinkle: the auto-detection probes **stdout**, while `-v` log lines are
written to **stderr**. For the uncommon shape where only stderr is redirected
(`gpumesh serve 2> run.log` from a terminal), the detection says "terminal" and
the log file gets escape sequences. `--no-color` or `GPUMESH_COLOR=0` is the
escape hatch.
