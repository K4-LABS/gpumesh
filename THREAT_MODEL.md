# gpumesh Threat Model

Every claim below is grounded in code that was read, cited as `file:line`.
See [Provenance](#provenance) for the commit these citations were taken
against.

Read `SECURITY.md` first. It defines the personas and the granted
capabilities; this document traces them through the code.

## 1. System context

gpumesh is a coordinator plus N workers, all speaking JSON over plain HTTP,
authenticated by one shared bearer token in an `X-Auth-Token` header.

**The default deployment is one machine.** The coordinator binds `127.0.0.1`
unless the operator passes `--host` or sets `GPUMESH_HOST`
(`DEFAULT_BIND_HOST`, `cli.py:347`; `_resolve_bind_host`, `cli.py:359`;
`GPUMesh.start_coordinator(host="127.0.0.1")`, `api.py:203`). Everything below
that involves a network attacker presumes the operator opted out of that
default — an opt-in that prints a full-width banner naming the OS user and
device that submitted code will run as (`cli.py:435`, called at `cli.py:804`;
the API equivalent at `api.py:285-297`).

Two listeners are still wide by design, and both are noted where they appear
below: the worker claim server (`claimer.py:645`) and `gpumesh setup` in LAN
auto-discovery mode (`setup_wizard.py:455`).

### The real data flow of a function task

This is the path that matters, because it is the one that moves executable
code. Follow a single `@mesh`-decorated function from the user's laptop and
back:

```
CLIENT (submitting laptop)
  1. user function
  2. cloudpickle.dumps(func)            serializer.py:57   (_serialize_with_cloudpickle)
     + JSON metadata: required modules, module globals,
       func name, python_version, and inspect.getsource() text
  3. base64 -> job "script" == "__gpumesh_function__",
     payload["_func"] == the blob         api.py / accelerate.py
  4. POST /api/jobs  with X-Auth-Token    worker.py MeshClient

COORDINATOR
  5. _authed()  -> SecurityManager.verify_request   server.py:591, server.py:526
                                                    security.py:239
  6. safe-mode gate: 403 if script contains __gpumesh_function__   server.py:766
  7. db.create_job -> payload stored as JSON text in SQLite  db.py:841, db.py:890

WORKER
  8. POST /api/lease  -> task JSON                  server.py:711
  9. safe-mode gate: refuse function tasks          worker.py:1034
 10. serializer.deserialize_function(payload["_func"])   worker.py:184
       -> cloudpickle.loads(rest)                   serializer.py:217
       -> OR exec(compile(source, ...), namespace)  serializer.py:265
          (cross-version fallback)
       -> OR refuse, user_error=True                worker.py:199
 11. cloudpickle.dumps((func, params)) -> subprocess stdin   worker.py:219, worker.py:240
 12. subprocess: cloudpickle.loads(stdin)           _function_subprocess.py:65
 13. subprocess: func(**params)                     _function_subprocess.py:72
 14. subprocess: print(json.dumps(_encode_result(result)))   _function_subprocess.py:84
       non-JSON values are cloudpickled + base64'd  _function_subprocess.py:30-45
 15. POST /api/result  with the envelope            worker.py:637

COORDINATOR
 16. db.complete_task stores result as JSON text    db.py:1254, db.py:1270

CLIENT
 17. GET /api/jobs/<id>                             server.py:571
 18. serializer.decode_result(raw)                  serializer.py:498
       -> cloudpickle.loads(base64decode(value))    serializer.py:520
     call sites: api.py:624, accelerate.py:514, client.py:37
 19. the object is handed to the user's code
```

Steps **10/12** and **18** are the two deserialization points. They face in
opposite directions. Step 10 trusts the coordinator; step 18 trusts the
worker.

Script tasks (`gpumesh submit`) take a shorter path: the script text is
written to a temp directory and run with `sys.executable`
(`sandbox.run_task`, `sandbox.py:59`; temp dir `sandbox.py:73`; `Popen`
`sandbox.py:107`), and its last stdout line is parsed as JSON
(`sandbox.py:167`). A script may still emit a result envelope, so step 18
applies to script tasks too.

A script task that exits with a **positive** status, produces no output, or
produces unparseable output is now failed with `user_error=True`
(`sandbox.py:156`, `sandbox.py:165`, `sandbox.py:171`), matching the function
path. `db.complete_task` treats `user_error` as final and skips the retry
budget (`db.py:1254`), so a deterministically broken task burns one attempt
rather than three. That is a correctness fix with a denial-of-service
side-benefit: it removes a 3x amplifier on repeated failing submissions. It
does not constrain an authenticated submitter, who can simply submit three
times — see STRIDE row 9.

The sign of the exit status is load-bearing and is not a detail. A positive
status is the script's own verdict on itself — an uncaught exception exits 1 —
so re-running it is pure waste. A *negative* status is POSIX for "killed by a
signal", which is the OOM killer's verdict on the *machine*, not the code's on
itself, so those keep the full retry budget and may well succeed on another
worker (`sandbox.py:150-157`). Timeouts likewise stay retryable
(`sandbox.py:138`). Read the amplifier reduction as covering deterministic
failures only; resource-exhaustion failures still cost three leases each, which
is the correct trade and is why row 9 is still "accepted by design".

## 2. Trust domains

| Domain | Members | Boundary with the outside |
| --- | --- | --- |
| **The mesh** | Coordinator, every worker, every token holder, every submitting client | The token check. Nothing inside it. |
| **This machine** | The default. Coordinator bound to `127.0.0.1`, self-worker, operator's CLI | The OS. No remote peer exists. |
| **The LAN** | Any host that can reach an exposed coordinator port, a worker claim port, or UDP 48900 | Untrusted. Present only after the operator opts into a non-loopback bind, or runs `gpumesh worker` / `gpumesh setup`. |
| **The internet** | Present only if the operator ran `--public` (ngrok) or joined via Tailscale | Untrusted. |

There is exactly one *authorization* boundary and it is one bit wide: token, or
no token. Ahead of it now sits a reachability boundary — the coordinator's bind
address — which is not an access control (it distinguishes no callers) but does
decide whether the authorization boundary is exposed to anyone at all. In the
default deployment it is not.

Inside the mesh there are no roles, no per-worker keys, no per-job isolation,
and no asymmetry between "who may submit" and "who may execute". A mesh is a
single trust domain — the same framing vLLM uses for a Ray cluster: every
member is as trusted as the least trustworthy member.

## 3. Assets

Ordered by what an attacker actually wants.

| Asset | Where it lives | Why it is worth taking |
| --- | --- | --- |
| **The worker host's OS account** | Whichever user ran `gpumesh join` | Code execution as that user is the granted capability (`_function_subprocess.py:72`). This is the whole prize. |
| **SSH keys, cloud credentials, `.env` files** | `$HOME` on every worker | Reachable by any task; there is no filesystem confinement. A dev laptop worker usually holds credentials to things far more valuable than the GPU. |
| **The GPU itself** | Worker hosts | Theft of compute: crypto mining, model training on someone else's electricity, or a foothold that looks like legitimate load. gpumesh has no quota mechanism, so a submitter can take all of it (`SECURITY.md`, "Not a vulnerability"). |
| **The token** | `--token` argv (on `serve` and every other subcommand's parser), stdout at `cli.py:766`, `GPUMESH_TOKEN` env, `~/.gpumesh/config.json` at 0600 (`connection_manager.py:23`, `connection_manager.py:121`), and the coordinator's memory as a hash (`hash_token`, `security.py:38`) | It is the single credential. Holding it is equivalent to being the operator. |
| **Code in flight** | The cloudpickle blob in `payload["_func"]`, stored as JSON text in SQLite (`db.py:890`) and moving over plain HTTP | Reading it discloses proprietary model code; modifying it is remote code execution on every worker. |
| **Data in flight and results** | Payloads and result envelopes, plain HTTP, stored in SQLite (`db.py:1270`) | Training data, inference outputs. Also the injection vector for step 18. |
| **The submitting client** | The operator's laptop, usually running a notebook | Compromised via a malicious result at `serializer.py:520`. Attacking the client is often easier than attacking a worker, and lands on a better machine. |

## 4. Actors

| Actor | Position | Capability |
| --- | --- | --- |
| Mesh operator | Runs `gpumesh serve` | Total, by definition. Not defended against. |
| Token holder | Has URL + token | Arbitrary code execution on every worker; full job control. |
| Malicious worker | Joined the mesh with a valid token, or was compromised afterwards | Returns a poisoned result envelope; gets code execution on every client that reads it (`serializer.py:520`). Also sees every task it is leased. |
| Compromised coordinator | Owns the queue | Serves arbitrary `_func` blobs to every worker. Total mesh compromise. |
| Unauthenticated LAN peer | Can reach the ports — **only after the operator opted into a non-loopback bind, or started `gpumesh worker` / `gpumesh setup`** | Should get nothing but a 401 (coordinator) or a 401/429 (claim port). This is the boundary under test. Absent entirely from a default `gpumesh serve`. |
| Local user on a mesh host | Shell on a worker or the coordinator, different account | Can read `--token` from the process table (`ps`); can read `~/.gpumesh/config.json` only if they are the same user or root. |
| Network observer | On the path | Plain HTTP: reads the token from the header, reads code and data, and can modify both. Mitigated only by choosing the network, or `--tailscale`. |

## 5. Entry points and trust boundaries

### 5.1 Coordinator HTTP port (default 8000, bound `127.0.0.1`)

`cmd_serve` resolves the bind through `_resolve_bind_host` (`cli.py:359`) —
`--host`, then `GPUMESH_HOST`, then `DEFAULT_BIND_HOST = "127.0.0.1"`
(`cli.py:347`) — and passes it to `server.serve` (`cli.py:713`), where the
socket is bound (`server.py:936`). `GPUMesh.start_coordinator` mirrors the
default in its signature (`api.py:203`) and calls `serve` by keyword
specifically so a future parameter reorder cannot silently bind the wrong
argument (`api.py:259`).

Every route is gated by `_authed()` (`server.py:523`), which runs **before**
the body is read on both verbs (`server.py:542` for GET, `server.py:591` for
POST, with `_read_json` at `server.py:431`). Bodies are capped at 10 MB
(`server.py:302`) and oversized ones are drained rather than left to desync the
connection (`server.py:479-483`).

- **Boundary:** yes, the primary one — **but on a default install there is
  nothing on the far side of it.** A loopback bind means no remote peer can
  complete a TCP handshake, so the token check is a second line rather than
  the first.
- **Pre-auth attack surface:** the HTTP request line and headers parsed by
  `BaseHTTPRequestHandler`, plus the `Content-Length` handling. No JSON is
  parsed pre-auth.
- **Exposure is opt-in and loud.** A non-loopback bind prints
  `_print_exposure_warning` (`cli.py:435`, called at `cli.py:804`), naming the
  OS user and device. A loopback bind deliberately refuses to print a LAN join
  URL, and prints the exact command to open up instead (`cli.py:783-796`), so
  the new default fails legibly rather than looking like a firewall problem.
  Firewall rules are only attempted when the bind is non-loopback
  (`cli.py:694`).
- **Consistency check:** `_is_loopback_bind` (`cli.py:347`) treats an *empty*
  host as non-loopback, because `bind(("", port))` is `INADDR_ANY`. The saved
  connection URL follows the bind (`cli.py:762-764`), so a loopback
  coordinator does not persist a LAN IP that answers nothing.
- **Caveat — `gpumesh setup`.** The wizard's LAN auto-discovery mode overrides
  the default with `WIZARD_LAN_BIND_HOST = "0.0.0.0"` (`setup_wizard.py:455`),
  resolved through the same `_resolve_bind_host` so `GPUMESH_HOST` still wins
  (`setup_wizard.py:466-475`), bound at `setup_wizard.py:581`. This is
  deliberate — UDP radar, the claim POST, and the worker dialling back all
  need a reachable socket — and it announces itself with the same banner
  (`_announce_bind_host`, `setup_wizard.py:478`, called at
  `setup_wizard.py:615`). If `GPUMESH_HOST` pins loopback, the wizard says so
  and explains why the flow will find nothing (`setup_wizard.py:483-495`).
  The wizard's Tailscale and manual modes only print instructions; they do not
  bind at all.
- **Caveat — containers.** The published image's `ENTRYPOINT` is `gpumesh`
  with no default subcommand (`Dockerfile:122-123`), so the bind comes from
  the arguments the operator supplies. Documented container usage passes
  `--host 0.0.0.0`, which is correct inside a container and pushes the real
  boundary out to the Docker port publish.

### 5.2 Worker claim port (`gpumesh worker`, ephemeral by default)

`claimer.start_claim_server` (`claimer.py:595`) resolves its bind address
through `_resolve_claim_bind_host` (`claimer.py:40`) and binds it at
`claimer.py:645`. The precedence is deliberately the same shape as the
coordinator's `cli._resolve_bind_host` — explicit argument, then
`GPUMESH_CLAIM_HOST`, then the default — and only the default and the variable
name differ, so an operator who has learned `--host`/`GPUMESH_HOST` does not
have to learn a second set of rules.

**The default is still every interface** (`DEFAULT_CLAIM_BIND_HOST = "0.0.0.0"`,
`claimer.py:37`), and that is a decision, not an oversight. Unlike the
coordinator — where running a coordinator and a worker on one machine is a
real, common case that loopback serves perfectly — a claim server has exactly
one purpose: to be reached by a coordinator on *another* machine. A loopback
claim server is not a safer claim server, it is a broken one, and it fails as
an unreachable-worker mystery rather than as a refusal. Discovery mode is
already an explicit opt-in the operator chose; narrowing the default would
silently undo that choice.

What changed is that the bind is now **addressable**. `GPUMESH_CLAIM_HOST=<addr>`
pins the listener to one interface — a tailnet or VPN address is the usual
reason — and that is the only control on this port that reduces the attack
*surface* rather than the cost per attempt. It is a separate variable from
`GPUMESH_HOST` on purpose: the two answer different questions, are frequently
set on the same machine, and `GPUMESH_HOST=127.0.0.1` — an entirely reasonable
coordinator setting — would otherwise quietly render every worker on that box
unclaimable.

A non-loopback bind announces itself: `_print_claim_exposure_warning`
(`claimer.py:73`, called at `claimer.py:657`) prints a banner naming the
listening address, the scope, and the OS user that claims will run as. It is
modelled on `cli._print_exposure_warning` but is deliberately not that
function: the coordinator's banner closes by telling the reader loopback is the
default and how to get back to it, which is actively wrong here. Note the
ordering — the banner is printed *after* `bind()` succeeds, so a listener that
failed to start never claims to be exposed.

This remains the widest-open surface gpumesh ships, and the one that has been
hardened most.

`POST /api/claim` compares the presented token with `hmac.compare_digest`
(`claimer.py:538`). `gpumesh worker` monkey-patches `ClaimHandler.do_POST`
with its own `_intercepted_do_POST` (`worker.py:1196`, installed at
`worker.py:1276`); that patched handler calls the *same* `_read_json` and
`_send`, and repeats the same `hmac.compare_digest` (`worker.py:1231`), so
every control below applies to both handlers.

- **Boundary:** yes, and unlike §5.1 it is always live while a worker runs.
- **Pre-auth attack surface is structurally larger here than on the
  coordinator, and cannot be made smaller by reordering.** The claim protocol
  carries the token in the request *body*, so this handler physically cannot
  authenticate before parsing something. The response is to make the parse
  small, bounded, and rate-limited rather than to pretend it can be moved:
  - **Rate limit first.** `_read_json` (`claimer.py:386`) consults
    `RateLimiter.is_allowed` before it reads `Content-Length` at all
    (`claimer.py:402`), answering 429 and closing. A locked-out peer therefore
    cannot drive the JSON parser.
  - **16 KB body cap, refused on the declared length.** `MAX_CONTENT_LENGTH`
    is `16 * 1024` (`claimer.py:238`) and an oversized declaration is rejected
    at `claimer.py:467` **before any allocation proportional to it**. The
    old 1 MB cap was inherited from the coordinator, which carries serialized
    task payloads; nothing on this port ever does. Draining an oversized body
    is itself bounded at 256 KB with a 2 s timeout (`claimer.py:247-248`,
    enforced in `_drain` at `claimer.py:351-384`), so
    `Content-Length: 10000000000` buys a closed connection, not a 10 GB read.
  - **No chunked, no length-less requests.** `BaseHTTPRequestHandler` does not
    decode chunked transfer encoding, so both are refused with 411
    (`claimer.py:441`, `claimer.py:449`) rather than half-understood.
  - **15 s socket timeout** (`timeout = 15`, `claimer.py:257`). This closes a
    slowloris hole: without it, `rfile` reads block forever, and because this
    is a `ThreadingHTTPServer`, an unauthenticated peer opening connections
    that declare a body they never send pinned one worker thread per
    connection until the process died.
  - **Rate-limit accounting is centralised in `_send`** (`claimer.py:278`,
    accounting at `claimer.py:296-300`), the one method both `do_POST`
    implementations funnel through, so the patched worker handler is governed
    by the same policy without knowing it exists. 401 records a failure, 200
    clears it.
- **The limiter is `security.RateLimiter`, not a second implementation**
  (imported `claimer.py:22`, instantiated `claimer.py:228`) — same defaults
  (5 failures / 300 s window -> 900 s lockout), same loopback exemption, one
  policy for an operator to learn. A fresh listener gets a fresh limiter
  (`claimer.py:642`), on the reasoning that a restart is a deliberate act at
  the worker's own console and a remote attacker cannot cause one.
- On success the worker calls out to a coordinator URL taken from the request
  body (`find_reachable_coordinator`, called at `claimer.py:564`;
  `worker.py:1256` in the patched handler) and only then joins it
  (`claimer.py:642`; `worker.py:1273` acknowledges). That is an outbound fetch
  to a network-supplied URL, gated on the worker's token. A token bypass here
  is therefore both RCE and SSRF — which is why this port, not the
  coordinator's, is where the hardening effort went.
- **Residual, and narrowing the bind does not remove it.** The controls above
  are limits on the *cost* of an unauthenticated request, not gates on making
  one: the claim protocol carries the token in the body, so the gate cannot be
  moved earlier than the parse. Concretely, what an unauthenticated non-locked-
  out peer still reaches is the `Content-Length` handling and `json.loads` over
  at most 16 KB. `json.loads` on bounded input is not a code-execution
  primitive, but this remains the one place in the codebase where an
  unauthenticated body is structurally parsed, and it is where a future
  maintainer is most likely to add something worse. The lockout is per source
  IP and in memory, so it slows a single attacker rather than a distributed or
  spoofing one, and loopback is exempt. `GPUMESH_CLAIM_HOST` is the only lever
  that changes *who can open the socket at all* — it is a real reduction in
  surface and it is the reason the knob exists, but on a mesh that spans
  machines the port is still reachable by everything on the chosen interface.
  **The token is the security boundary. Treat it like one.**

### 5.3 UDP discovery beacon (broadcast, port 48900)

`BROADCAST_PORT`, `discovery.py:30`. Workers broadcast every 2 s
(`BEACON_INTERVAL`, `discovery.py:31`); the coordinator listens and prints
nearby peers (`_discovery_printer`, `server.py:897`).

- **Boundary:** no. It is unauthenticated in both directions by design.
- **What leaks:** hostname, device type, device name, score, api/claim ports,
  and `platform.platform()` (`discovery.py:146-155`) — an OS and version
  fingerprint, broadcast to the whole subnet.
- **What does not leak:** the token. It is not in the payload.
- **Spoofing:** trivial. Anyone can broadcast a beacon claiming to be a
  worker. The consequence is bounded: a discovered peer is only *printed*
  (`server.py:918`), never auto-claimed, and field values are length- and
  range-clamped when parsed (`Peer.__init__`, `discovery.py:254-271`). Disable
  with `--no-discovery`.
- **Note on the loopback default:** discovery is unaffected by the bind
  address — it is a separate UDP socket. A coordinator bound to loopback still
  broadcasts and still listens, so a beacon may advertise a coordinator that
  no remote worker can actually join.

### 5.4 The deserialization boundary, both directions

The most important row in this document.

| Direction | Call site | Trusts | Gate |
| --- | --- | --- | --- |
| Coordinator -> worker | `serializer.py:217` (`cloudpickle.loads`), `serializer.py:265` (`exec` of source), reached from `worker.py:184` | The coordinator, and therefore every token holder | Token, plus `--safe-mode` (`worker.py:1034`, `server.py:766`) |
| Worker -> worker subprocess | `_function_subprocess.py:65` | The worker process itself | None needed; same trust |
| **Worker -> client** | **`serializer.py:520`** (inside `decode_result`, `serializer.py:498`), reached from `api.py:624`, `accelerate.py:514`, `client.py:37` | **Every worker in the mesh** | **Token only. `--safe-mode` does not cover this.** |

The third row is the one that is easy to miss and the reason this document
exists. `--safe-mode` is a control on *what a submitter may push*; it says
nothing about *what a worker may return*. A worker that has been admitted to
the mesh — or a worker host that was compromised after joining — can put an
arbitrary pickle in the result envelope and get code execution on the
submitting laptop the moment `decode_result` runs.

There is no signature on results, no allowlist of pickle classes, and no
`safe_mode`-equivalent on the client. Fixing this properly means either
restricting the client to JSON results, or authenticating results per worker;
both are behaviour changes and neither is implemented.

### 5.5 The token file on disk

`~/.gpumesh/config.json` (`connection_manager.py:23`) holds the plaintext URL
and token. It is written atomically via `mkstemp` + `os.replace`
(`connection_manager.py:97-101`), chmod'd 0600 (`connection_manager.py:121`),
and on Windows given a best-effort `icacls /inheritance:r`
(`connection_manager.py:137-141`).

- **Boundary:** OS file permissions.
- **Failure is no longer silent.** Both the chmod and the `icacls` call route
  their failures to `_warn_permissions` (`connection_manager.py:64`), which
  prints the file path, what specifically failed, and what the token grants.
  Four distinct failures are surfaced: chmod raising
  (`connection_manager.py:122-123`), `USERNAME` unset so `icacls` cannot be
  targeted (`connection_manager.py:129-134`), `icacls` exiting non-zero with
  its own stderr quoted (`connection_manager.py:143-147`), and `icacls` not
  being runnable at all (`connection_manager.py:148-149`).
- **Permissions are also re-checked on read.** `load_connection` calls
  `_warn_if_world_readable` (`connection_manager.py:155`, called at
  `connection_manager.py:202`), which warns if `st_mode & 0o077` is set. This
  catches the cases a write-time check cannot: upgrades from versions that
  never chmod'd the file, restores from backup, and copies made under a
  permissive umask. It warns once per process, guarded by a module-level flag
  (`connection_manager.py:152`), so the warning is not tuned out by repetition.
- **POSIX only, deliberately.** The read-time check returns early on Windows
  (`connection_manager.py:176`) because `st_mode` there carries no real ACL
  information — a wide-open file still reports `0o666`, so the check would be
  simultaneously always-on and evidence-free. Windows protection comes from
  the `icacls` call, whose failure is reported. **Residual:** a Windows config
  file whose ACL was widened after `save_connection` ran is not detected.
- The temp file is created inside `~/.gpumesh` with default `mkstemp`
  permissions (0600) before the rename, so there is no world-readable window.
- **Not covered:** a failed write is still fatal and re-raised
  (`connection_manager.py:102-107`), which is correct — a partial config is
  worse than none.

## 6. STRIDE

Scope: **two** deployments, because the default changed. Rows are marked
*default* (coordinator bound to `127.0.0.1`, plain HTTP, discovery on,
self-worker on) or *exposed* (the operator passed `--host 0.0.0.0` /
`GPUMESH_HOST`, ran `gpumesh setup` in LAN mode, or is running
`gpumesh worker`). A threat that needs a remote socket to the coordinator
does not exist in the default deployment at all.

| # | Threat | STRIDE | Vector | Current control | Residual |
| --- | --- | --- | --- | --- | --- |
| 0 | Coordinator reachable from the LAN at all | Elevation (precondition for 1, 2, 4, 5, 10) | Any remote TCP connection to port 8000 | **Loopback bind by default** (`cli.py:347`, `cli.py:359`, `api.py:203`); non-loopback prints an exposure banner (`cli.py:435`) | **None in the default deployment.** In the exposed deployment: accepted, opt-in, and warned |
| 1 | Unauthenticated peer submits a job | Spoofing / Elevation | `POST /api/jobs` without a token | *Default:* no reachable socket. *Exposed:* `_authed` before body read, `server.py:591` | Low — this is the enforced boundary |
| 2 | Token guessed by brute force | Spoofing | Repeated `X-Auth-Token` values | `hmac.compare_digest` (`security.py:67`); per-IP rate limiter, 5 failures / 300 s -> 900 s lockout (`RateLimiter`, `security.py:81`); the lockout branch returns **before** `verify_token` runs (`security.py:275-282`), so a locked-out attacker gets no correctness oracle | Low for generated tokens (72 bits); **high for a short hand-picked `--token`**. Not reachable remotely in the default deployment |
| 3 | Token read from the process table | Information disclosure | `ps` / Task Manager on any mesh host | None. `--token` is argv (on `serve` and every other subcommand's parser); `serve` also prints it to stdout (`cli.py:766`) | **Accepted.** Use `GPUMESH_TOKEN` on shared hosts |
| 4 | Token read off the wire | Information disclosure | Passive sniffing of the `X-Auth-Token` header | None. Plain HTTP | **Accepted.** Requires an exposed deployment; mitigate with `--tailscale` |
| 5 | Code and data read or modified in flight | Tampering / Info disclosure | MITM on the LAN | None | **Accepted.** Exposed deployments only. Tampering with `_func` is RCE on every worker |
| 6 | Malicious worker poisons a result | Elevation of privilege | Crafted envelope -> `cloudpickle.loads`, `serializer.py:520` | Token only. **Not covered by `--safe-mode`** | **Accepted.** Documented; see §5.4. Unaffected by the bind default — the client dials out |
| 7 | Malicious coordinator serves a hostile `_func` | Elevation | `POST /api/lease` response -> `serializer.py:217` | Token; `--safe-mode` on the worker (`worker.py:1034`) | **Accepted.** Safe mode reduces it to script tasks, which still execute |
| 8 | Token holder steals credentials from `$HOME` | Info disclosure | Any task body | None; granted capability | **Accepted by design** |
| 9 | Token holder exhausts GPU / memory / disk | Denial of service | Any task body | Per-task timeout only (`--timeout`, default 240 s). `user_error` failures are final rather than retried 3x (`db.py:1254`; set at `sandbox.py:156/165/171` and `worker.py:199`), removing an accidental amplifier — not a quota | **Accepted by design.** No quotas |
| 10 | Unauthenticated flood of the coordinator port | Denial of service | Connection or request flood | *Default:* no reachable socket. *Exposed:* 10 MB body cap (`server.py:302`); `ThreadingHTTPServer` with no connection cap and **no socket timeout on the coordinator handler** | **Accepted.** In an exposed deployment a LAN peer can still exhaust threads without a token. The claim port's `timeout = 15` fix (row 15) has no coordinator equivalent |
| 11 | Rate limiter used to lock out the operator | Denial of service | 5 bad tokens from the coordinator's own host | **Fixed.** Loopback is exempt and never counted (`RateLimiter.is_exempt`, `security.py:121`; `is_loopback` handles `127.0.0.0/8`, `::1`, IPv4-mapped, `security.py:14`). Loopback rejections say so explicitly (`security.py:287-294`) | Low. Justified in code: loopback presence already implies host access, so the token can be read rather than guessed |
| 12 | Spoofed discovery beacon | Spoofing | UDP broadcast on 48900 | Field clamping (`discovery.py:254-271`); peers are printed (`server.py:918`), never auto-claimed | Low. Cosmetic pollution of the operator's console |
| 13 | Host fingerprint leaked to the subnet | Info disclosure | Beacon payload includes `platform.platform()` (`discovery.py:154`) | `--no-discovery` | **Accepted.** Broadcast regardless of bind address. Low value to an attacker already on the LAN |
| 14 | Unauthenticated claim of an idle worker | Elevation | `POST /api/claim` with a guessed token | `hmac.compare_digest` (`claimer.py:538`, and `worker.py:1231` in the patched handler); single-claim latch (`claimer.py:549-554`); **per-IP rate limiting, same policy as the coordinator** (`claimer.py:228`, accounted in `_send`, `claimer.py:296-300`) | Low. The "no rate limiting on this port" gap is closed |
| 15 | Pre-auth JSON parsing on the claim port | Tampering / DoS | Body parsed before the token check — unavoidable, the token is *in* the body | Rate limit consulted first (`claimer.py:402`); 16 KB cap refused on the declared length before allocation (`claimer.py:238`, `claimer.py:467`); chunked and length-less refused 411 (`claimer.py:441`, `claimer.py:449`); drain bounded to 256 KB / 2 s (`claimer.py:247-248`) | Low. `json.loads` over ≤16 KB, for non-locked-out peers only. Still the only unauthenticated structured parse in the codebase |
| 15b | Slowloris on the claim port | Denial of service | Open connections declaring a body that never arrives; one thread pinned per connection forever | **Fixed.** `timeout = 15` on the handler (`claimer.py:257`), applied to the connection socket by `StreamRequestHandler.setup()` | Low. A generous 15 s for a sub-kilobyte claim |
| 16 | SSRF from a claimed worker | Elevation / Info disclosure | `coordinator_url` from the request body -> `find_reachable_coordinator` (called at `claimer.py:564`) | Worker token required first | Low. A token bypass on the claim port turns into SSRF *and* RCE |
| 17 | Token leaked into a log or traceback | Info disclosure | Handler exceptions print tracebacks (`server.py:579-583` for GET, `server.py:829-833` for POST) | Handler logging is suppressed (`server.py:533`, and `claimer.py:512`); the token is not in coordinator request bodies | Low. Would be a reportable bug — see `SECURITY.md`. Note the claim token *is* in a claim body, so a claim-handler traceback would be a real leak; none is printed today |
| 18 | Offline attack on a captured token hash | Spoofing | The coordinator's in-memory `token_hash` | Single SHA-256 with a deterministic salt (`security.py:62`) — no work factor, no precomputation resistance | **Accepted for generated tokens.** See `SECURITY.md`, "A note on token hashing" |
| 19 | Repudiation: who submitted this job? | Repudiation | One shared token, no identities | None | **Accepted by design.** Job events record worker joins/leaves (`db.record_event`, `db.py:818`), never submitter identity |
| 20 | Token file readable by other local users | Info disclosure | `~/.gpumesh/config.json` in plaintext | 0600 + `icacls`; **failures now warn** (`connection_manager.py:64`) and a group/other-readable file warns on load (`connection_manager.py:155`) | Low on POSIX. **Residual on Windows:** `st_mode` carries no ACL information, so a post-hoc ACL widening is not detected (`connection_manager.py:176`) |

## 7. Accepted residual risk

These are known, deliberate, and will not be treated as vulnerabilities. They
are listed so that "we did not think of it" is never a possible reading.

1. **A mesh is a single trust domain.** Every member can compromise every
   other member. Rows 6, 7 and 8 above are the same risk seen from three
   angles.
2. **No transport security.** Rows 4 and 5. The operator picks the network.
3. **No isolation of tasks.** Same OS user, same `$HOME`, no namespaces, no
   seccomp, no cgroups. The subprocess boundary
   (`worker.py:240`, `sandbox.py:107`) is for crash containment and cleanup.
   The trimmed environment for script tasks (`sandbox.py:78-91`) and the POSIX
   CPU-seconds preamble (`sandbox.py:100-104`) are conveniences, not controls.
4. **No quotas or fairness.** Row 9.
5. **LAN exposure is opt-in, and opting in is accepted.** `gpumesh serve` and
   `GPUMesh.start_coordinator` bind loopback by default (`cli.py:347`,
   `api.py:203`). Once the operator passes `--host 0.0.0.0` or sets
   `GPUMESH_HOST`, every LAN-facing row above becomes live; that consequence
   is accepted, because the choice is explicit and the banner
   (`cli.py:435`) names the OS user and device that submitted code will run
   as. Two listeners are wide without an explicit choice and are also
   accepted: the worker claim port, which cannot function otherwise
   (`claimer.py:645`, hardened per §5.2), and `gpumesh setup` in LAN
   auto-discovery mode, whose entire flow requires a reachable socket
   (`setup_wizard.py:455`, announced at `setup_wizard.py:478`).
6. **The token is a single shared secret with no rotation, expiry, or
   revocation.** Changing it means restarting the coordinator, which drops
   every worker (`worker.py:918`).
7. **Result deserialization is unauthenticated in practice.** Row 6. The
   client has no `--safe-mode`. **This is unchanged by the loopback default** —
   the submitting client dials out to the coordinator, so no inbound socket is
   involved and the bind address gives no protection here.
8. **No socket timeout on the coordinator handler.** Row 10. The claim port
   got `timeout = 15` (`claimer.py:257`); the coordinator's
   `CoordinatorHandler` has no equivalent, so an exposed coordinator remains
   slowloris-able without a token. This is the clearest remaining asymmetry
   between the two listeners.
9. **Windows token-file ACLs are not re-checked on load.** Row 20, §5.5.
10. **Discovery ignores the bind address.** §5.3. A loopback coordinator still
    broadcasts, advertising itself to a subnet that cannot reach it.

## 8. Out of scope

- Physical access to a mesh host.
- A malicious OS, hypervisor, or Python interpreter.
- Supply-chain compromise of `cloudpickle`, `torch`, or any other dependency.
  Report those upstream. gpumesh pins nothing and audits nothing.
- ngrok's and Tailscale's own security. `--public` and `--tailscale` hand the
  transport to a third party; their threat models are theirs.
- The operator attacking their own mesh. They are the policy.
- Anything requiring a valid token, except where it defeats a control that is
  supposed to hold *even for token holders* — and there are none, which is
  precisely the point of `SECURITY.md`.
- Denial of service by an authenticated submitter.

## Provenance

- **Written:** 2026-08-15
- **Last reconciled against the code:** 2026-08-18
- **Against commit:** `10583d5` ("docs: fix Markdown formatting in the two-node
  docker example"), plus the working tree of that date. The previous
  reconciliation was against `96eee91` at gpumesh 1.3.0.
- **Version:** gpumesh 2.0.0

> [!IMPORTANT]
> **The line numbers go stale faster than the claims do.** This has now
> happened twice. Between the first reconciliation and the second, `claimer.py`
> grew ~90 lines at the top and `cli.py` grew by roughly a thousand. Between
> the second and this one, 2.0.0 landed: `server.py` went 442 -> 1077 lines,
> `db.py` 775 -> 1598, `worker.py` 970 -> 1371, `cli.py` 1021 -> 2437. Nearly
> every citation in this document moved, and **not one claim it asserts
> changed.** They have been re-anchored again, but treat them as a *finding
> aid*, not as evidence: the symbol name in the same sentence is the durable
> half of every citation, and the claim is what was verified. If a number does
> not land, search for the symbol.

Every `file:line` citation in this document was re-read against the working
tree on the reconciliation date. Several of the cited files remain under
concurrent edit, so if a citation does not land, search for the named symbol —
`decode_result`, `_authed`, `_read_json`, `_resolve_bind_host`,
`_print_exposure_warning`, `_resolve_claim_bind_host`,
`_print_claim_exposure_warning`, `verify_request`, `RateLimiter`,
`_warn_permissions` — rather than trusting the number.

This document should be re-read whenever a new endpoint, a new
`cloudpickle.loads` call site, or a new network listener is added — **or
whenever a default bind address changes, or a new way to change one is
added.** `GPUMESH_CLAIM_HOST` is the second kind: it moved no default, but it
gave an operator a lever over a trust boundary that previously had none, and
a lever is part of the model whether or not anyone pulls it.
