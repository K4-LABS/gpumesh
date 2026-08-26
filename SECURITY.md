# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| 3.1.0 (latest release) | Yes |
| < 3.1.0 | No |

Only the latest release is supported. There are no backports and no long-term
support branches: gpumesh is maintained by one person, and a second supported
line would mean a second line that does not actually get patched. Fixes land on
`master` — the default branch — and ship in the next release, which is 3.2.0.
Please upgrade to the newest version and reproduce there before reporting an
issue.

## gpumesh executes arbitrary code by design

That is the product. `@mesh` and `@accelerate` take a Python function, pickle
it, ship it to another machine, and run it there. A valid token is therefore
not a password that guards data — **it is a license to execute arbitrary code
as the worker's OS user**, on every machine in the mesh, with that user's
files, GPUs, network access and credentials.

Everything below follows from that sentence. If you read only one section,
read this one.

### It runs in both directions

The documentation used to say "workers execute code sent by the coordinator",
which is true and is only half the story.

Results come back the same way they went out. A worker wraps its return value
in an envelope (`serializer.encode_result`, `gpumesh/serializer.py:475`, and
its duplicate `_encode_result` in `gpumesh/_function_subprocess.py:30`); when
the value is not JSON-encodable — a numpy array, a torch tensor, a DataFrame,
which is the normal case for real work — it is cloudpickled and base64'd. The
**submitting client** then unwraps it by calling `cloudpickle.loads` on bytes
the worker produced:

- `gpumesh/serializer.py:615` — `cloudpickle.loads(base64.b64decode(envelope["value"]))`, inside `decode_result` (`gpumesh/serializer.py:571`)
- `gpumesh/api.py:632` — `results.append(serializer.decode_result(raw))`, the Python API's result collection
- `gpumesh/accelerate.py:522` — the value an `@accelerate` / `@mesh` call returns to your code
- `gpumesh/client.py:37` — `_format_result`, used by `gpumesh status` to print results

`cloudpickle.loads` on attacker-controlled bytes is arbitrary code execution.
So **a malicious or compromised worker gets code execution on every client
that collects a result from it** — including on the operator's own laptop,
which is usually where the notebook and the SSH keys live.

As of 3.2.0 the client can refuse. `--strict`, or `GPUMESH_STRICT_RESULTS=1`,
makes `decode_result` raise instead of unpickling. It is off by default and it
costs you every non-JSON return value — see "What `--strict` actually does",
below. This document used to say the result path had no mitigation. It now has
one, and the one it has is a restriction.

### What `--safe-mode` actually does

`--safe-mode` blocks *function distribution*, and nothing else:

- `gpumesh/server.py:766` — the coordinator returns 403 for a job whose script
  contains `__gpumesh_function__`
- `gpumesh/worker.py:1063` — the worker refuses such a task even if one reaches it

The result path is untouched *by it*. A safe-mode mesh still runs script-based
tasks (`sandbox.run_task`, `gpumesh/sandbox.py:59`, which writes the submitted
script to a temp dir and executes it with `sys.executable`), those scripts
still print a result envelope, and clients still call `decode_result` on it.
`--safe-mode` narrows what a *token holder* can push to workers. It does not
make a worker's output safe to deserialize, and it is not a sandbox.

### What `--strict` actually does, and what it costs

`--strict` is the control on the other direction, and it is new in 3.2.0.

- `gpumesh/serializer.py:571` — `decode_result(payload, *, strict=None)`
- `gpumesh/serializer.py:594-605` — a `cloudpickle` envelope raises
  `UntrustedResultError` (`gpumesh/serializer.py:500`) instead of reaching
  `cloudpickle.loads` at `gpumesh/serializer.py:615`
- `gpumesh/serializer.py:538` — `strict_results_enabled`: the explicit
  argument, then `GPUMESH_STRICT_RESULTS`, then off
- `gpumesh/cli.py:2221` — the top-level `gpumesh --strict <subcommand>` flag,
  exported into the environment at `gpumesh/cli.py:2477` so that all three
  decode sites and any subprocess the command spawns see it
- `gpumesh/client.py:38` — `_format_result` catches the refusal and prints a
  `_gpumesh_strict` marker rather than dumping the raw base64 envelope

**The two flags are set on different machines and stop different directions of
travel.** Confusing them is easy, so:

| Flag | Set on | Stops |
| --- | --- | --- |
| `--safe-mode` | the coordinator (and the workers it serves) | pickled *functions* going out to workers |
| `--strict` | the submitting client | pickled *results* coming back from workers |

Neither implies the other. A safe-mode coordinator still hands its clients
pickles to open; a strict client still lets its coordinator distribute
functions to workers.

**`--strict` is a restriction, not transparent hardening.** JSON-encodable
results keep working unchanged. A task that returns a torch tensor, a numpy
array or a DataFrame **stops working under it** — the decode raises rather
than executing. That is the whole trade, and it is offered rather than
defaulted into, because returning those objects is gpumesh's normal workload.
Under strict mode a result has to be JSON-encodable, or be written to shared
storage and referenced by path.

With strict mode off, the first pickled result decoded in a process raises a
one-time `RuntimeWarning` (`_warn_once_about_pickle`,
`gpumesh/serializer.py:546`). Once per process, and on the first actual decode
rather than at import: a 500-task job printing 500 identical lines teaches the
reader to filter them out, and a mesh whose results are all JSON never runs
the risk and should not be told that it does.

### The default posture: reachable only from this machine

Because a token is a license to execute code, the coordinator's listening
socket is the surface that matters, and **it binds loopback by default**.

`gpumesh serve` resolves its bind address through `_resolve_bind_host`
(`gpumesh/cli.py:359`): an explicit `--host` beats `GPUMESH_HOST`, which beats
`DEFAULT_BIND_HOST = "127.0.0.1"` (`gpumesh/cli.py:344`). `serve` then hands
that address to `server.serve` (`gpumesh/cli.py:718`), and the socket is bound
to exactly it (`gpumesh/server.py:947`). `GPUMesh.start_coordinator` has the
same default in its signature — `host: str = "127.0.0.1"` (`gpumesh/api.py:203`).

So out of the box, a coordinator plus its self-worker is a single-machine
system: no LAN peer can open a connection at all, with or without the token.
Opening it up is a deliberate act — `--host 0.0.0.0` or `GPUMESH_HOST=0.0.0.0`
— and it prints a full-width banner naming the OS user tasks will run as and
the device they will run on (`_print_exposure_warning`, `gpumesh/cli.py:435`,
called at `gpumesh/cli.py:812`; the Python API prints its own equivalent at
`gpumesh/api.py:285-297`).

### `--public` is a second door, and it does not go through the bind

The bind address is not the only way in, and reading it as though it were is
a mistake this document itself used to make.

`gpumesh serve --public` starts an ngrok tunnel (`tunnel.open_tunnel`,
`gpumesh/tunnel.py:40`, which calls `ngrok.connect(port, "http")`). The ngrok
agent is a **local** process: it dials `127.0.0.1:PORT` from this machine and
forwards ngrok's edge into it. A loopback bind therefore does not constrain it
in the slightest. `--public` on the default bind exposes the coordinator to the
entire public internet, and it always did.

That is by design and is a legitimate configuration — loopback is in fact the
*tightest* bind to pair with `--public`, because then the local ngrok agent is
the only thing that can reach the port and the LAN cannot. What was wrong was
the reporting: the tunnel was opened unconditionally while the exposure banner
lived in the non-loopback branch, so `--public` on the default bind printed
"Other machines CANNOT reach this coordinator" and then printed a public URL.
The larger exposure printed the smaller warning.

It now prints its own full-width banner before the tunnel opens
(`_print_tunnel_exposure_warning`, `gpumesh/cli.py:503`, called at
`gpumesh/cli.py:820`), naming the public internet as the reach, the OS user
tasks run as, and the device — and it prints *in addition to* the bind banner
on a wide bind, because a bind and a tunnel are two separate doors.

**`--tailscale` is not a tunnel and never was.** `open_tunnel` in `tailscale`
mode only shells out to `tailscale ip -4` and prints `http://100.x.y.z:PORT`
(`gpumesh/tunnel.py:13`). Tailnet packets arrive addressed to that 100.x
address, which a loopback-bound socket does not accept, so the advertised URL
answers nothing. `serve --tailscale` on a loopback bind is therefore **refused**
(`_refuse_tailscale_on_loopback`, `gpumesh/cli.py:548`, called at
`gpumesh/cli.py:688`) with the command to bind the tailnet address instead. The
bind is not widened automatically: it is the operator's decision, and the right
answer there is the tailnet address specifically, not `0.0.0.0`.

Three things still listen beyond loopback, and they are the honest caveats:

- **The worker claim server** (`gpumesh worker`) binds every interface by
  default (`DEFAULT_CLAIM_BIND_HOST = "0.0.0.0"`, `gpumesh/claimer.py:37`,
  resolved by `_resolve_claim_bind_host`, `gpumesh/claimer.py:40`, and bound at
  `gpumesh/claimer.py:645`). That default is deliberate and is not the
  coordinator's: a claim server exists solely to be reached by a coordinator on
  *another* machine, so binding loopback would not make it safer, it would make
  it a broken feature that fails as an unreachable-worker mystery. What it now
  has is a *knob* — pass `bind_host`, or set `GPUMESH_CLAIM_HOST`, to pin it to
  one address (a tailnet or VPN address is the usual reason). A separate
  variable from `GPUMESH_HOST` on purpose: `GPUMESH_HOST=127.0.0.1` is a
  sensible coordinator setting, and sharing it would silently make every worker
  on that box unclaimable. A non-loopback bind prints a banner naming the OS
  user that claims will run as (`_print_claim_exposure_warning`,
  `gpumesh/claimer.py:73`, called at `gpumesh/claimer.py:657`). It is
  token-gated and rate-limited — see `THREAT_MODEL.md` §5.2, which also states
  plainly what narrowing the bind does and does not buy.
- **`gpumesh setup`**, in its "Same WiFi / LAN (auto-discover nearby devices)"
  mode, binds `0.0.0.0` on purpose (`WIZARD_LAN_BIND_HOST`,
  `gpumesh/setup_wizard.py:455`, resolved at `gpumesh/setup_wizard.py:478`).
  A loopback bind would make the wizard unable to complete its own flow. It
  honours `GPUMESH_HOST` if set, and it prints the same exposure banner
  `serve` does (`_announce_bind_host`, `gpumesh/setup_wizard.py:478`, called
  at `gpumesh/setup_wizard.py:615`).
- **The UDP discovery beacon**, which is broadcast and unauthenticated by
  design and carries no token.

Everything else in this document assumes the operator has opted into
exposure — by widening the bind, or by opening a tunnel with `--public`, which
is a larger opt-in than any bind. That opt-in is where the threat model begins.

### Transport: plain HTTP by default, `--tls` when asked for

The mesh speaks plain HTTP unless told otherwise, and that default has not
changed. `gpumesh serve --tls` (new in 3.2.0) wraps the coordinator's listener
in TLS: a self-signed certificate is generated under `~/.gpumesh/tls/` on
first use — via `cryptography`, falling back to the `openssl` binary
(`ensure_self_signed_cert`, `gpumesh/tls.py:222`) — with the key at 0600 and
the directory at 0700 (`gpumesh/tls.py:184`, `gpumesh/tls.py:231`), a SAN
covering `localhost`, this host's name and every local address
(`_san_names`, `gpumesh/tls.py:66`), a TLS 1.2 floor (`gpumesh/tls.py:276`),
and a SHA-256 fingerprint printed at startup (`gpumesh/tls.py:256`, printed at
`gpumesh/server.py:974`). The pair is reused across restarts, so a pinned
fingerprint stays valid (`gpumesh/tls.py:239`). `--tls-cert`/`--tls-key`
supply a real certificate instead; both must be given together or startup
fails (`gpumesh/server.py:955-960`).

**What `--tls` buys.** Confidentiality and integrity on the wire. The shared
bearer token no longer travels in cleartext in `X-Auth-Token` where anyone on
the same Wi-Fi can lift it out of a capture, and a submitted function's
pickled bytes can no longer be rewritten in flight. That is the
passive-eavesdropper branch on a LAN, closed.

**What `--tls` does not buy.** It does not authenticate the coordinator unless
the operator moved the certificate by hand. Client trust has three tiers
(`client_context`, `gpumesh/tls.py:295`):

1. `GPUMESH_TLS_CA` pointing at a copy of the coordinator's certificate —
   verified, and the printed fingerprint is what it verifies against.
2. `--tls-cert`/`--tls-key` with a certificate from a real CA (an internal one,
   or `tailscale cert`) — verified, nothing else to do.
3. `GPUMESH_TLS_INSECURE=1` — encrypted, **unauthenticated**. An active
   on-path attacker can still substitute their own certificate and read
   everything. Strictly better than plain HTTP; strictly worse than either
   option above, and it leaves the active-attacker branch open on purpose.

It also changes nothing about what a valid token grants. An encrypted request
bearing a valid token is still arbitrary code execution on every worker.

Every client request funnels through `MeshClient` (`gpumesh/worker.py:104`),
which builds the context once (`gpumesh/worker.py:122`) and rewrites a bare
"certificate verify failed" into an instruction naming both variables
(`explain_verify_failure`, `gpumesh/tls.py:338`, called at
`gpumesh/worker.py:147`).

**`--tls` is LAN-grade, not internet-grade.** For anything crossing a network
you do not control, tunnel it — `--tailscale` or `--public` — and let the
tunnel be the boundary. Plain HTTP should be read as LAN-only, and LAN-only
should be read as "a network whose other occupants you trust".

### The honest framing

A gpumesh mesh is **a single trust domain**. This is the same position the
vLLM project takes about Ray: every member of the cluster is as trusted as the
least trustworthy member. Adding a machine to your mesh is closer to giving
its owner a shell on your laptop than to giving them an API key.

Run a mesh with people you would lend a laptop to.

## Personas and granted capabilities

You cannot say "X is not a vulnerability" until you have said who X is and
what they were deliberately given. Apache Airflow's security model is the
reference here. gpumesh has four personas.

### Mesh operator — fully trusted

Runs `gpumesh serve`. Chooses the bind address — `--host` / `GPUMESH_HOST`,
defaulting to loopback (`gpumesh/cli.py:359`) — generates or supplies the
token (`gpumesh/cli.py:631`, `secrets.token_urlsafe(12)`), decides who gets
it, and decides whether to expose the coordinator beyond the LAN with
`--tailscale` or `--public`.

The bind address is the operator's single most consequential choice, which is
why it is a flag and not an inference, why the default is the closed one, and
why choosing the open one prints a banner rather than a line of dim text.

`--public` is the same kind of choice, one size larger, and reached by a
different route: it does not widen the bind, it tunnels past it. It gets a
banner of its own for exactly that reason.

The operator is the security policy. gpumesh does not defend against them and
has no mechanism that could.

### Token holder / task submitter — granted arbitrary code execution

Anyone in possession of the coordinator URL and the token, *and* able to reach
the bind address. Presenting the token in the `X-Auth-Token` header
(read at `gpumesh/server.py:526`, checked by `SecurityManager.verify_request`,
`gpumesh/security.py:446`) grants:

- arbitrary code execution on every worker in the mesh, as the OS user that
  ran `gpumesh join` (`gpumesh/worker.py:213` deserializes the function,
  `gpumesh/_function_subprocess.py:72` calls it)
- that user's filesystem, including `$HOME`, SSH keys and cloud credentials
- that machine's GPUs, CPU and memory, for as long as they like
- that machine's network position — outbound requests originate from inside
  the worker's network
- every result of every job on the coordinator, and the ability to cancel,
  retry or kill any job (`/api/cancel`, `/api/retry`, `/api/kill`)

There is one token and there are no roles. A token holder is an operator in
all but name.

### Worker host owner — trusts everyone the operator trusted

Runs `gpumesh join`, `gpumesh quickjoin` or `gpumesh worker`. By joining, they
extend the capabilities above to every person the operator has given the token
to — not just to the operator. They are trusting a set they cannot see and
were never shown.

This is the persona most likely to be surprised, so it is stated plainly in
the README and again here.

### Unauthenticated LAN peer — untrusted, and out of scope by default

Anyone who can reach the coordinator's port, a worker's claim port, or the UDP
discovery broadcast without holding the token.

**Since the loopback bind default, this persona is not present in a default
`gpumesh serve`.** There is no coordinator socket for them to reach. They
appear when the operator opts into exposure with `--host` / `GPUMESH_HOST`,
runs `gpumesh setup` in LAN mode, or runs `gpumesh worker` (whose claim port
binds `0.0.0.0` by necessity). Discovery beacons are visible to them either
way, and carry no token.

**When they do exist, this is the only trust boundary gpumesh actually
enforces.** Everything inside the mesh is one domain; the boundary is the
token check at `gpumesh/server.py:523` (`_authed`) and, on the claim port,
`gpumesh/claimer.py:538` (`hmac.compare_digest`, mirrored in the worker's
patched handler at `gpumesh/worker.py:1260`).

## Not a vulnerability

Each of these is a granted capability being used, not a control being broken.
Reports that restate them will be closed with a link to this section.

- **A token holder running arbitrary code on a worker.** That is the feature.
  See "Token holder", above.
- **A token holder reading files, environment variables or credentials on a
  worker.** Granted with the code execution; there is no filesystem boundary.
- **A token holder monopolising the GPU, filling memory, or spawning
  processes.** Resource exhaustion by an *authenticated* submitter is granted.
  There are no quotas. A task timeout exists (`--timeout`, default 240s) to
  recover from hangs, not to enforce fairness.
- **Tasks not isolated from each other or from `$HOME`.** Function tasks run
  in a subprocess (`gpumesh/worker.py:269`) and script tasks in a temp
  directory (`gpumesh/sandbox.py:73`), both for crash containment and cleanup
  — not for confinement. Same user, same filesystem, no namespaces, no seccomp.
  Script tasks do get a trimmed environment (`gpumesh/sandbox.py:78-91`), which
  is a convenience for reproducibility, not a boundary: the script can read
  anything the OS user can.
- **Cleartext HTTP on a network the operator chose.** gpumesh speaks plain
  HTTP *by default*. `gpumesh serve --tls` exists as of 3.2.0 and closes the
  passive-eavesdropper hole on a LAN (`gpumesh/tls.py`, wired in at
  `gpumesh/server.py:953-978`); it is opt-in, so a mesh still speaking
  cleartext is doing so by the operator's choice. The operator picks the
  network and is told to use `--tailscale` or `--public` when it is not one
  they control.
- **An active on-path attacker defeating a self-signed `--tls` listener.**
  A self-signed certificate nobody copied to the worker authenticates nothing,
  and `GPUMESH_TLS_INSECURE=1` disables verification outright
  (`gpumesh/tls.py:315-320`). That is the documented limit of the feature, not
  a bypass of it. Move the certificate and set `GPUMESH_TLS_CA`, or use a real
  CA, if the active attacker is in your model.
- **A worker returning a malicious result to a client that trusts it.** The
  reverse-deserialization path described above is a documented property of a
  single trust domain, not a bypass: the malicious worker had to be admitted
  to the mesh with a valid token first. It is listed here so nobody discovers
  it by surprise — but a report that a worker you deliberately joined can
  attack your client is a report that the trust model works as written.
  `--strict` refuses the decode outright, at the cost of every non-JSON return
  value; running without it is a choice, and the first pickled result decoded
  in a process says so (`gpumesh/serializer.py:546`).
- **Anything reachable only because the operator opened the port.**
  `gpumesh serve` binds `127.0.0.1` unless told otherwise
  (`gpumesh/cli.py:344`, `gpumesh/cli.py:359`). Passing `--host 0.0.0.0`, or
  setting `GPUMESH_HOST`, is an opt-in that prints a full-width banner naming
  the OS user and device that submitted code will run as
  (`gpumesh/cli.py:435`). A consequence of an exposure the operator chose and
  was warned about is not a vulnerability in gpumesh.
- **Anything reachable only because the operator opened a tunnel.**
  `--public` is a *separate* opt-in from the bind, not an additional one on
  top of it. This document used to say exposure to the internet required
  `--public` on top of a widened bind; that was never true. The ngrok agent
  dials `127.0.0.1:PORT` locally, so `--public` alone reaches the internet
  from the loopback default — see "`--public` is a second door", above. Until
  that was corrected, the combination also printed no exposure banner at all,
  which *was* a real defect and is fixed: the tunnel banner
  (`gpumesh/cli.py:503`) now prints on every bind, before the tunnel opens.
  Running `--public` and being reachable from the internet is the feature
  working. Running it and *not being told* would be a bug — report that.
- **The worker claim port listening on `0.0.0.0`.** `gpumesh worker` exists to
  be claimed by a coordinator on another machine, so its claim server binds
  every interface by default (`gpumesh/claimer.py:37`, bound at
  `gpumesh/claimer.py:645`). It is token-gated (`gpumesh/claimer.py:538`),
  rate-limited (`gpumesh/claimer.py:228`, consulted at
  `gpumesh/claimer.py:402`), capped at 16 KB (`gpumesh/claimer.py:238`), and
  socket-timed-out (`gpumesh/claimer.py:257`). An operator who wants a narrower
  listener has `GPUMESH_CLAIM_HOST`, and a non-loopback bind announces itself.
  A *bypass* of any of those is a vulnerability; the listener's existence,
  and the fact that its default is wide, is not.
- **`gpumesh setup` binding `0.0.0.0` in LAN mode.** The wizard's
  auto-discovery flow needs a socket other machines can reach, so it
  deliberately overrides the loopback default (`gpumesh/setup_wizard.py:455`).
  It honours `GPUMESH_HOST` and prints the same exposure banner
  (`gpumesh/setup_wizard.py:478`). Choosing that menu item is the opt-in.
- **Scanner output with no proof of concept against a default install.** A
  CVE in a transitive dependency, a "dangerous function" hit on `pickle` or
  `exec`, or a static-analysis report with no reachable path is not a
  vulnerability report. See "No bug bounty", below.

## Definitely a vulnerability

Report these. Every one of them is a control failing, not a capability being
used.

- **Any path to code execution, task submission, or result retrieval without a
  valid token.** Any endpoint reachable before `_authed` returns True.
- **Token bypass, forgery, timing leak, or feasible brute force.** The
  comparison is `hmac.compare_digest` (`verify_token`,
  `gpumesh/security.py:175`, on all three stored hash formats); a way around
  it, or a measurable timing signal, is a vulnerability. So is a way to make the rate limiter reveal whether a
  token was correct — see "How a rejected request answers", below.
- **Tokens leaking into logs, tracebacks, error responses, saved files with
  wrong permissions, or process listings.** Note the known one: passing
  `--token` on the command line puts it in the process table for every local
  user, and `gpumesh serve` prints it to stdout. Use `GPUMESH_TOKEN` where
  local users are a concern, and note that `gpumesh serve` also prints the
  token to stdout (`gpumesh/cli.py:774`). `~/.gpumesh/config.json` holds the
  token in plaintext at 0600; when gpumesh cannot restrict it, or finds it
  group/other-readable on load, it says so (`gpumesh/connection_manager.py:64`
  and `:155`). A *new* leak — into a traceback, an HTTP error body, the SQLite
  database, or a world-readable file that is written silently — is a bug we
  will fix.
- **gpumesh binding an interface beyond what the documented default and the
  user's flags call for.** The documented defaults are: `127.0.0.1` for
  `gpumesh serve` and `GPUMesh.start_coordinator`; `0.0.0.0` for the worker
  claim server and for `gpumesh setup` in LAN mode. A coordinator that bound
  wider than `--host` / `GPUMESH_HOST` asked for, or that ignored either, is a
  vulnerability — this is a security control now, not a cosmetic default. The
  claim port carries the same obligation through its own variable: a claim
  server that bound wider than `GPUMESH_CLAIM_HOST` asked for, or that ignored
  it, is a vulnerability on the same reasoning, because narrowing that bind is
  the only control on that port that reduces attack *surface* rather than cost
  per attempt. (The claim port not reading `GPUMESH_HOST` is deliberate, not a
  bug — see the caveats above. And `--host-ip` is a different flag again: it
  changes only the address *printed* for workers to dial and never affects any
  bind. Its being ignored is a bug, not a security issue.)
- **A tunnel opening without its banner, or a banner that understates it.**
  `--public` reaches further than any bind can, so `serve` must print the
  tunnel banner (`gpumesh/cli.py:503`) *before* `open_tunnel` runs, on every
  bind, and no other line printed in that run may claim the coordinator is
  unreachable. A build where `--public` is quiet, or where the tunnel banner
  is skipped on some bind addresses, is a vulnerability on the same reasoning
  as an ignored `--host`: the operator's consent is only real if they were
  told what they were consenting to.
- **Path traversal, or arbitrary file read/write, through any endpoint.**
  Job names, task IDs and payload keys must never reach a filesystem path.
- **Deserialization of an attacker payload before authentication is verified.**
  On the coordinator, `_authed` runs before `_read_json` on every route
  (`gpumesh/server.py:542` for GET, `gpumesh/server.py:591` for POST). If a
  coordinator route ever unpickles, or even parses, an unauthenticated body,
  that is critical. The claim port is the one place that *must* parse before
  it can authenticate, because the claim protocol carries the token in the
  body — the exposure there is bounded deliberately and described in
  `THREAT_MODEL.md` §5.2. Widening it is a vulnerability.
- **SSRF from a worker or coordinator to an attacker-chosen URL.** The claim
  flow takes a URL from the network and fetches it
  (`find_reachable_coordinator`, called at `gpumesh/claimer.py:564`) — it is gated on
  the worker's own token, and a way to trigger it without that token is a
  vulnerability.
- **A `--tls` coordinator that serves anything in cleartext.** The socket is
  wrapped between `bind()` and `serve_forever()` (`gpumesh/server.py:953-978`)
  and any failure there closes the server and re-raises
  (`gpumesh/server.py:967-969`), so a certificate problem is a startup failure
  rather than a silent downgrade. A build where `--tls` is accepted and the
  listener answers plain HTTP anyway, or where a wrap failure is swallowed, is
  a vulnerability: the operator's `--tls` is a claim about the wire.
- **`--strict` unpickling anyway.** Strict mode is checked before
  `cloudpickle.loads` is reached (`gpumesh/serializer.py:594-605`, versus
  `gpumesh/serializer.py:615`). A path that decodes a pickled result while
  `strict_results_enabled` is True defeats the only control the client has.

## How a rejected request answers

`SecurityManager.verify_request` (`gpumesh/security.py:446`) returns one of
three distinguishable messages, and the distinction is intentional in both
directions — usability and disclosure.

1. **Not on the allowlist** — the address will never be accepted.
2. **Rate limited** — this IP has burned its attempts. Five failures in a
   300 s window buy a 900 s lockout (`RateLimiter`, `gpumesh/security.py:271`).
3. **Bad token** — the token really is wrong, with the number of attempts
   remaining before lockout.

The rate-limited message says explicitly that *the token in this request was
not checked at all* (`gpumesh/security.py:482-489`), and that is literally
true: the lockout branch returns before `verify_token` is ever called.

That ordering is a security property, not an implementation accident.
Verifying the token during a lockout and reporting "your token is correct, but
wait" would hand a brute-forcer a free correctness oracle — they could keep
guessing at full speed straight through the lockout and learn the instant they
hit, which is exactly what the lockout exists to prevent. So the message says
"not checked" rather than "checked and correct", and the code makes that
honest by not checking. **A change that makes a locked-out response depend on
whether the token was right is a vulnerability**, however helpful the error
text becomes.

**Loopback is exempt from rate limiting** (`RateLimiter.is_exempt`,
`gpumesh/security.py:311`; `is_loopback` covers `127.0.0.0/8`, `::1`, and
IPv4-mapped forms, `gpumesh/security.py:92`). This is not a weakened control.
Anyone who can open a socket from `127.0.0.1` is already executing code on the
coordinator host, where the token sits in argv, in the environment, and in
`~/.gpumesh/config.json` — they read it, they do not guess it. Meanwhile a
loopback lockout takes out the coordinator's own self-worker and the
operator's own CLI, which are the two things that cannot be told to come from
a different address. An exempt IP is never even *counted*
(`gpumesh/security.py:334-338`), so no history accumulates. Loopback failures
therefore get a fourth message, saying plainly that retrying is fine.

The same `RateLimiter` class, with the same defaults and the same loopback
exemption, guards the worker claim port (`gpumesh/claimer.py:228`).

## A note on token hashing

The coordinator never holds the plain token in its stored state. It derives a
hash once at startup (`SecurityManager.__init__`, `gpumesh/security.py:420`,
hashing at `gpumesh/security.py:437`; constructed once per coordinator at
`gpumesh/server.py:942`) and compares every request against that. The hash is
never written to the database — `gpumesh/db.py` contains no token column and
no reference to a token at all — and it is re-derived from the token at every
coordinator start.

**That re-derivation is exactly why a random salt is safe here.** The old
scheme derived the salt deterministically from the token itself —
`sha256(token)[:16]` — on the reasoning that workers persist the plain token
and the coordinator must produce the same hash after a restart or every worker
would be locked out. The premise was right and the conclusion did not follow:
nothing anywhere compares two hashes, so nothing requires the *hash* to be
stable. Only the token has to be, and it is.

### What changed in 3.2.0

`hash_token` (`gpumesh/security.py:155`) now defaults to PBKDF2-HMAC-SHA256
(`_hash_pbkdf2`, `gpumesh/security.py:135`): a random 16-byte hex salt per
token (`gpumesh/security.py:148`) and 200000 iterations by default
(`DEFAULT_KDF_ITERATIONS`, `gpumesh/security.py:43`), stored as
`pbkdf2_sha256$<iterations>$<salt>$<hash>`.

- The cost factor is `GPUMESH_AUTH_KDF_ITERATIONS`, or the `kdf_iterations=`
  keyword on `SecurityManager` (`gpumesh/security.py:423-425`). It is
  **floored at `MIN_KDF_ITERATIONS` = 1000** (`gpumesh/security.py:44`,
  enforced at `gpumesh/security.py:84-88`), so a typo'd variable cannot
  silently turn the KDF back into one round. Below the floor is an error, not
  a warning.
- The legacy derivation is still selectable by name: `scheme="sha256"`, or
  `GPUMESH_AUTH_KDF=sha256` (`_resolve_kdf`, `gpumesh/security.py:49`).
- Legacy hashes stay **verifiable forever**. `verify_token`
  (`gpumesh/security.py:175`) detects the format from the string rather than
  from configuration — so a coordinator configured for one scheme still
  verifies a hash written under the other — and handles
  `pbkdf2_sha256$<iterations>$<salt>$<hash>`, `<salt>:<hash>`, and a bare
  unsalted hash. All three comparisons end in `hmac.compare_digest`
  (`gpumesh/security.py:208`, `:213`, `:217`).
- A malformed stored hash returns False rather than raising
  (`gpumesh/security.py:191-204`). This runs on the request path, and an
  exception there is a 500 that tells an unauthenticated caller something
  about the coordinator's state.

### The old scheme's honest weakness, now closed

A salt that is a pure function of its input cannot make two hashes of the same
token differ, so it provided **no protection against precomputation**, and one
SHA-256 round provided no meaningful work factor either. For a short `--token`
chosen by hand, that was a real weakness and this document said so. PBKDF2
closes it: an offline guess now costs 200000 SHA-256 rounds instead of one,
against a salt the attacker cannot know in advance.

**Token entropy is still the primary defence.** `secrets.token_urlsafe(12)`
(`gpumesh/cli.py:631`) is about 72 bits, beyond reach of any wordlist or GPU
cracker, and it never needed a work factor. PBKDF2 is the *second* defence —
the one that used to be missing, and the one that matters for a token you
chose yourself. If you set your own token, still make it long and random.

### The cost, and what pays it

A cost factor is a denial-of-service trade, and pretending otherwise would be
dishonest. Without mitigation, 200000 rounds per verification hands anyone who
can reach the port a CPU amplifier: every unauthenticated request would burn
them, and a worker polling for tasks would pay them several times a second for
a token that has not changed.

`_VerifyCache` (`gpumesh/security.py:220`, held at `gpumesh/security.py:444`,
consulted at `gpumesh/security.py:494`) resolves that:

- **Only successes are cached** (`gpumesh/security.py:519`). Caching failures
  would let a brute-forcer replay a wrong guess for free, and the cache sits
  *in front of* the rate limiter — which is the thing that is supposed to be
  counting those guesses. So a wrong token pays the full PBKDF2 cost every
  single time and is counted every single time; a right one pays it once.
- **The token itself is never stored.** The cache key is
  `HMAC-SHA256(random per-process key, token)` (`gpumesh/security.py:245`),
  where the key is 32 random bytes generated at construction
  (`gpumesh/security.py:240`) and never leaves the process. A heap dump of the
  cache therefore yields nothing replayable against another coordinator, or
  against this one after a restart.

Steady state on a busy mesh is one HMAC per request, not one PBKDF2.

## What is hardened today

Nine controls ship in 3.2.0. Each gets one line on what it buys and one on
what it does not, because a control described only by what it buys is how a
reader ends up trusting it for something it never did.

- **Subprocess isolation per task** — functions at `gpumesh/worker.py:269`,
  scripts at `gpumesh/sandbox.py:107`.
  *Buys:* a task that segfaults, hangs or OOMs kills a child process rather
  than the worker, and its temp directory is cleaned up afterwards.
  *Does not buy:* confinement of any kind. Same OS user, same `$HOME`, same
  credentials, same network position; no namespaces, no seccomp, no cgroups.
  **It is a crash boundary, not a security boundary.**

- **Loopback-by-default bind** — `DEFAULT_BIND_HOST` (`gpumesh/cli.py:344`),
  `_resolve_bind_host` (`gpumesh/cli.py:359`), `gpumesh/api.py:203`.
  *Buys:* on a default install there is no coordinator socket for an
  unauthenticated LAN peer to reach at all, so most of the threat model has no
  attacker standing in it.
  *Does not buy:* anything once the operator widens the bind, and nothing
  whatsoever against `--public`, which tunnels past the bind rather than
  through it.

- **PBKDF2 token derivation** — `hash_token` (`gpumesh/security.py:155`),
  200000 iterations (`gpumesh/security.py:43`).
  *Buys:* an offline attack on a captured hash costs 200000 SHA-256 rounds per
  guess, against a random per-token salt, with no precomputation possible.
  *Does not buy:* protection for a token weak enough to be guessed online, and
  nothing at all about what a *valid* token grants.

- **Timing-safe comparison** — `verify_token` (`gpumesh/security.py:175`),
  claim port at `gpumesh/claimer.py:538`.
  *Buys:* no byte-by-byte early exit for an attacker to measure.
  *Does not buy:* anything against a token that leaked. Constant time is not
  secrecy.

- **Rate limiting, with a documented loopback exemption** — `RateLimiter`
  (`gpumesh/security.py:271`), `is_exempt` (`gpumesh/security.py:311`), the
  same class on the claim port (`gpumesh/claimer.py:228`).
  *Buys:* five failures in 300 s cost one source IP a 900 s lockout, and the
  lockout branch returns before the token is examined, so a locked-out
  attacker gets no correctness oracle.
  *Does not buy:* anything against a distributed or address-spoofing attacker,
  and nothing at all from loopback, which is exempt and never even counted
  (`gpumesh/security.py:334-338`) — deliberately, for the reasons in "How a
  rejected request answers", above.

- **0600 config permissions** — `gpumesh/connection_manager.py:121`,
  re-checked on load at `gpumesh/connection_manager.py:155`.
  *Buys:* the plaintext token in `~/.gpumesh/config.json` is unreadable by
  other local users on POSIX, and a failure to restrict it is printed rather
  than swallowed (`gpumesh/connection_manager.py:64`).
  *Does not buy:* protection from root or from the same user, and no detection
  on Windows of an ACL widened after the file was written
  (`gpumesh/connection_manager.py:176`).

- **Opt-in TLS** — `gpumesh serve --tls` (`gpumesh/tls.py`, wired at
  `gpumesh/server.py:953-978`).
  *Buys:* the token and the pickled payloads stop travelling in cleartext on a
  LAN, so a passive eavesdropper on the same Wi-Fi gets nothing usable.
  *Does not buy:* authentication of the coordinator, unless the certificate
  was moved to the worker by hand or came from a real CA — and nothing at all
  under `GPUMESH_TLS_INSECURE=1`. Off by default.

- **`--safe-mode`** — coordinator at `gpumesh/server.py:766`, worker at
  `gpumesh/worker.py:1063`.
  *Buys:* a token holder cannot push a pickled *function* to a worker.
  *Does not buy:* a sandbox. Script tasks still execute arbitrary Python as
  the worker's OS user, and results still come back as pickles.

- **`--strict`** — client-side, `gpumesh/serializer.py:594-605`.
  *Buys:* a hostile worker cannot get code execution on the submitting client
  through a result envelope.
  *Does not buy:* compatibility. Tensors, numpy arrays and DataFrames stop
  coming back at all. Off by default.

What is *not* on this list matters as much as what is. There are no quotas, no
roles, no per-worker keys, no signing of results, no token rotation or
revocation, and no isolation of a task from the worker's filesystem.

## Isolation roadmap

**Today's subprocess isolation is a crash boundary, not a security boundary.**
A task runs as the OS user who started the worker, with that user's `$HOME`,
credentials, GPUs and network position. `gpumesh/worker.py:269` and
`gpumesh/sandbox.py:107` spawn a child process, and a child process is not a
sandbox. The trimmed environment for script tasks (`gpumesh/sandbox.py:78-91`)
and the POSIX CPU-seconds preamble (`gpumesh/sandbox.py:100-104`) are
conveniences for reproducibility, not controls.

Below is the intended direction of travel, in order. **This is a plan, not a
promise.** gpumesh is unfunded and maintained by one person; treat every item
as "not shipped" until it appears in a release, and do not deploy on the
strength of anything in this section. Written 2026-08-26.

1. **User namespaces / `unshare`, plus seccomp, plus a read-only filesystem
   view.** *Not started.* The first real boundary and the cheapest one: run
   the task child in its own user and mount namespace, put a seccomp filter
   over the syscall surface, and make `$HOME` either absent or read-only.
   Linux-first by necessity; Windows and macOS would keep today's behaviour,
   which means the documentation would have to say plainly which platform is
   protected rather than implying all of them are.

2. **A Firecracker microVM per task.** *Not started.* Linux-only, and a hard
   kernel boundary rather than a filtered one — the failure mode of a seccomp
   escape is a compromised host, the failure mode of a microVM escape is a
   hypervisor CVE. It is second rather than first because the costs are real:
   boot latency on every task, GPU passthrough that is far from free, and a
   device story that does not exist on a laptop.

3. **A WASM runtime for the pure-compute subset.** *Not started, and the
   narrowest of the three.* Portable across every platform gpumesh runs on,
   with a memory-safe boundary by construction — at the cost of **not running
   arbitrary PyTorch**. A WASM tier would be a second, narrower execution mode
   for tasks that fit inside it, not a replacement for the current one.

None of these changes the trust model by itself. A mesh would still be a
single trust domain: isolation raises the cost of a hostile *task*, it does
not make a hostile mesh *member* safe to admit.

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

Use GitHub's private vulnerability reporting:

1. Open the **Security** tab on this repository.
2. Click **Report a vulnerability**.
3. Describe the problem and follow up privately from there.

Direct link: <https://github.com/K4-LABS/gpumesh/security/advisories/new>

**If that page does not offer you a report form**, private reporting is not
enabled on the repository and you should not fall back to a public issue.
Email the maintainer instead: **arijitkonar16@gmail.com**, subject line
starting `gpumesh security:`. Plain email is not encrypted, so send the
"there is a problem in X, may I have a private channel" version rather than
the full proof of concept, and wait for a reply.

Please include:

- The gpumesh version (`gpumesh --version`) and Python version
- What you did, what you expected, and what happened instead
- A minimal reproduction
- Whether the issue is exploitable across a network, and by which persona
  above

### What you can expect

| Stage | Target |
| --- | --- |
| Acknowledgement that we received the report | 3 business days |
| Initial assessment (valid / not / need more) | 14 days |
| Fix or documented mitigation plan, medium severity and above | 60 days |

gpumesh is maintained by one person. These are honest targets, not an SLA
backed by a rota. If a deadline slips you will hear why rather than nothing.

Coordinated disclosure: we will credit you in the advisory unless you prefer
otherwise, and we ask that you hold public details until a fix ships or the
60 days elapse, whichever comes first.

### Safe harbour

We will not pursue or support legal action against researchers acting in good
faith under this policy. Good faith means: testing against your own installs,
not accessing or exfiltrating other people's data, not degrading other
people's service, and reporting privately rather than publicly.

### Scope

In scope:

- This repository (`github.com/K4-LABS/gpumesh`)
- The `gpumesh` package on PyPI
- The `samurai007ak/gpumesh` container image

Out of scope:

- Third-party dependencies (`cloudpickle`, `torch`, `pyngrok`, …). Report
  those upstream. If a dependency issue is exploitable *specifically because
  of how gpumesh uses it*, that is in scope — say so and show the path.
- Anything in the "Not a vulnerability" section.
- The maintainer's other projects, accounts, or infrastructure.

### No bug bounty

There is no bug bounty, no payment, and no swag. gpumesh is an unfunded
project maintained by one person.

**Bring a proof of concept against a default install.** A report that shows a
concrete attack — the commands, the payload, the observed result — will be
read carefully and fixed. A report that is scanner output, a severity score, a
list of "dangerous functions", or an AI-generated description of a
vulnerability class will be closed without discussion. This is not hostility;
it is the only way one maintainer can keep the queue readable.

## Further reading

`THREAT_MODEL.md` has the full data flow, trust boundaries, assets, actors,
a STRIDE table, and the residual risks we have accepted, with file and line
citations throughout.
