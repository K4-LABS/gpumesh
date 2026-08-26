# CLI reference

Three flags are global and go *before* the subcommand: `-v` / `--verbose` for
debug-level logging, `--json-logs` to emit logs as JSON lines (for Docker), and
`--strict` to refuse pickled results (see [Strict results](#strict-results)).

## Server & connection

| Command | Description |
|---------|-------------|
| `gpumesh setup` | Interactive setup wizard (coordinator or worker) |
| `gpumesh serve` | Start the coordinator (`--host`, `--port`, `--token`, `--db`, `--host-ip`, `--public`, `--tailscale`, `--no-discovery`, `--safe-mode`, `--tls`, `--tls-cert`, `--tls-key`, `--no-self-worker`, `--color`/`--no-color`) |
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

## TLS

The mesh speaks plain HTTP by default. `gpumesh serve --tls` makes *that one
coordinator* speak HTTPS instead; every URL it prints, saves to the connection
file, or self-checks switches to `https://` to match.

| Flag | Effect |
|------|--------|
| `--tls` | Serve HTTPS. With no `--tls-cert`/`--tls-key`, generate a self-signed pair under `~/.gpumesh/tls/` on first use and reuse it on every later start |
| `--tls-cert PATH` | Serve this certificate instead of a generated one |
| `--tls-key PATH` | The private key for `--tls-cert` |

**`--tls-cert` and `--tls-key` must be given together.** One without the other
is a startup error, raised deliberately after the socket is created but before
it starts serving, so the operator sees it on screen immediately rather than
discovering it as a per-connection failure on a worker an hour later.

The generated certificate is valid for 825 days, is regenerated once it is
within 30 days of expiry, and its key is written 0600 in a 0700 directory. Its
SAN covers `localhost`, this machine's hostname, and every local address the
machine can see, so workers can dial it by any of them. Generation uses the
`cryptography` library if it is installed (`pip install gpumesh[tls]`) and the
`openssl` binary otherwise; with neither, `serve` refuses to start and names
both plus `--tls-cert` as the fixes. At startup the coordinator prints the
certificate path and its SHA-256 fingerprint.

On the worker side, three ways to trust it:

| Setting | Result |
|---------|--------|
| `GPUMESH_TLS_CA=/path/to/coordinator-cert.pem` | Encrypted and **verified** against the certificate you copied over. Check it against the fingerprint the coordinator printed |
| A certificate from a real CA passed to `--tls-cert` | Encrypted and verified with nothing to set on the worker. An internal CA counts, and so does `tailscale cert` |
| `GPUMESH_TLS_INSECURE=1` | Encrypted, **unauthenticated**. An active on-path attacker can still substitute a certificate. Better than plain HTTP, worse than either row above |

With none of them set, the system trust store is used, which correctly refuses
a self-signed certificate. That failure is rewritten into an instruction naming
`GPUMESH_TLS_CA` and `GPUMESH_TLS_INSECURE`, because "certificate verify
failed" on its own sends people to the wrong fix.

**What `--tls` is worth.** It closes passive eavesdropping on a LAN: the token
stops travelling in cleartext and pickled payloads cannot be rewritten in
flight. It does not authenticate the coordinator unless you moved the
certificate by hand, and it does not make gpumesh safe to face the internet.
Cross-network use still wants a tunnel — `--tailscale` or `--public`.

A coordinator and one worker, end to end:

```bash
# On the coordinator. Prints the certificate path and its SHA-256 fingerprint.
gpumesh serve --host 0.0.0.0 --tls --token SHARED_TOKEN
#   [mesh] TLS enabled, certificate /home/you/.gpumesh/tls/coordinator-cert.pem
#   [mesh] self-signed SHA-256 3A:7F:...
#   Workers: gpumesh join https://192.168.1.10:8000 --token SHARED_TOKEN

# Copy the certificate to the worker (scp, a USB stick, anything you trust).
scp you@192.168.1.10:~/.gpumesh/tls/coordinator-cert.pem ./coordinator-cert.pem

# On the worker. Verify the fingerprint matches what the coordinator printed.
openssl x509 -in coordinator-cert.pem -noout -fingerprint -sha256

export GPUMESH_TLS_CA=$PWD/coordinator-cert.pem
gpumesh join https://192.168.1.10:8000 --token SHARED_TOKEN
```

The URL must say `https://`. A worker dialling `http://` at a TLS listener gets
a connection reset and no explanation.

## Strict results

A result comes back either as JSON or, for anything JSON cannot carry, as
cloudpickled bytes. Decoding the second kind runs the worker's code on the
machine that submitted the job.

`gpumesh --strict` refuses to do that: a pickled result raises instead of being
unpickled, and the job output shows a `_gpumesh_strict` marker where the value
would be. JSON-encodable results still work.

It is a **top-level flag**, so it goes before the subcommand:

```bash
gpumesh --strict status JOB_ID      # correct
gpumesh status --strict JOB_ID      # error: unrecognized argument
```

`GPUMESH_STRICT_RESULTS=1` is equivalent and works anywhere, including in the
environment of a script that never types the flag.

The cost is not hypothetical: under `--strict`, a task that returns a tensor, a
numpy array or a DataFrame stops working. That is the trade. With strict mode
off, the first pickled decode in a process emits a one-time `RuntimeWarning`.

`--strict` and `--safe-mode` point in opposite directions and are not
substitutes for each other. `--safe-mode` is set on the coordinator and stops
functions going *out* to workers; `--strict` is set on the submitting client
and stops pickled results coming *back*.

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
| `GPUMESH_TLS_CA` | Anything that dials a coordinator | Path to the coordinator's certificate (or a CA bundle). Verification is on, against that file. A path that does not exist is refused at startup rather than silently ignored |
| `GPUMESH_TLS_INSECURE=1` | Anything that dials a coordinator | Encrypt but do not verify. An active on-path attacker can still impersonate the coordinator |
| `GPUMESH_STRICT_RESULTS=1` | Anything that decodes a result | Refuse to unpickle results; same as the top-level `--strict` |
| `GPUMESH_AUTH_KDF` | `serve` | Token hashing scheme: `pbkdf2_sha256` (default) or `sha256` (legacy single round). An unknown value is an error, not a fallback |
| `GPUMESH_AUTH_KDF_ITERATIONS` | `serve` | PBKDF2 cost factor. Default `200000`; anything below `1000` is refused rather than clamped, so a typo cannot quietly turn the KDF back into one round. Ignored under `GPUMESH_AUTH_KDF=sha256` |

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

**The two `GPUMESH_AUTH_KDF*` variables are for compatibility and tuning, not
for turning security on.** The coordinator derives a PBKDF2-HMAC-SHA256 hash of
the token at every start, keeps it in memory, and never writes it to the
database — that is the default and needs no configuration. `sha256` exists
because tokens hashed by an older gpumesh must keep verifying; it is a
downgrade to a single round, so set it only if something outside gpumesh reads
the hash format. Raising the iteration count does not slow a busy mesh down:
a successful verification is memoised in memory, so a polling worker pays the
derivation once rather than on every request.
