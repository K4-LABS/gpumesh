# Security Policy

## Supported versions

Only the latest release is supported. Fixes land on `main` and ship in the
next release; please upgrade to the newest version before reporting an issue.

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
in an envelope (`serializer.encode_result`, `gpumesh/serializer.py:473`, and
its duplicate `_encode_result` in `gpumesh/_function_subprocess.py:30`); when
the value is not JSON-encodable — a numpy array, a torch tensor, a DataFrame,
which is the normal case for real work — it is cloudpickled and base64'd. The
**submitting client** then unwraps it by calling `cloudpickle.loads` on bytes
the worker produced:

- `gpumesh/serializer.py:520` — `cloudpickle.loads(base64.b64decode(envelope["value"]))`, inside `decode_result` (`gpumesh/serializer.py:498`)
- `gpumesh/api.py:450` — `results.append(serializer.decode_result(raw))`, the Python API's result collection
- `gpumesh/accelerate.py:383` — the value an `@accelerate` / `@mesh` call returns to your code
- `gpumesh/client.py:37` — `_format_result`, used by `gpumesh status` to print results

`cloudpickle.loads` on attacker-controlled bytes is arbitrary code execution.
So **a malicious or compromised worker gets code execution on every client
that collects a result from it** — including on the operator's own laptop,
which is usually where the notebook and the SSH keys live.

### What `--safe-mode` actually does

`--safe-mode` blocks *function distribution*, and nothing else:

- `gpumesh/server.py:474` — the coordinator returns 403 for a job whose script
  contains `__gpumesh_function__`
- `gpumesh/worker.py:832` — the worker refuses such a task even if one reaches it

The result path is untouched. A safe-mode mesh still runs script-based tasks
(`sandbox.run_task`, `gpumesh/sandbox.py:59`, which writes the submitted
script to a temp dir and executes it with `sys.executable`), those scripts
still print a result envelope, and clients still call `decode_result` on it.
`--safe-mode` narrows what a *token holder* can push to workers. It does not
make a worker's output safe to deserialize, and it is not a sandbox.

### The default posture: reachable only from this machine

Because a token is a license to execute code, the coordinator's listening
socket is the surface that matters, and **it binds loopback by default**.

`gpumesh serve` resolves its bind address through `_resolve_bind_host`
(`gpumesh/cli.py:356`): an explicit `--host` beats `GPUMESH_HOST`, which beats
`DEFAULT_BIND_HOST = "127.0.0.1"` (`gpumesh/cli.py:341`). `serve` then hands
that address to `server.serve` (`gpumesh/cli.py:649`), and the socket is bound
to exactly it (`gpumesh/server.py:590`). `GPUMesh.start_coordinator` has the
same default in its signature — `host: str = "127.0.0.1"` (`gpumesh/api.py:103`).

So out of the box, a coordinator plus its self-worker is a single-machine
system: no LAN peer can open a connection at all, with or without the token.
Opening it up is a deliberate act — `--host 0.0.0.0` or `GPUMESH_HOST=0.0.0.0`
— and it prints a full-width banner naming the OS user tasks will run as and
the device they will run on (`_print_exposure_warning`, `gpumesh/cli.py:432`,
called at `gpumesh/cli.py:707`; the Python API prints its own equivalent at
`gpumesh/api.py:167-179`).

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
(`_print_tunnel_exposure_warning`, `gpumesh/cli.py:500`, called at
`gpumesh/cli.py:715`), naming the public internet as the reach, the OS user
tasks run as, and the device — and it prints *in addition to* the bind banner
on a wide bind, because a bind and a tunnel are two separate doors.

**`--tailscale` is not a tunnel and never was.** `open_tunnel` in `tailscale`
mode only shells out to `tailscale ip -4` and prints `http://100.x.y.z:PORT`
(`gpumesh/tunnel.py:13`). Tailnet packets arrive addressed to that 100.x
address, which a loopback-bound socket does not accept, so the advertised URL
answers nothing. `serve --tailscale` on a loopback bind is therefore **refused**
(`_refuse_tailscale_on_loopback`, `gpumesh/cli.py:545`, called at
`gpumesh/cli.py:630`) with the command to bind the tailnet address instead. The
bind is not widened automatically: it is the operator's decision, and the right
answer there is the tailnet address specifically, not `0.0.0.0`.

Three things still listen beyond loopback, and they are the honest caveats:

- **The worker claim server** (`gpumesh worker`) binds every interface by
  default (`DEFAULT_CLAIM_BIND_HOST = "0.0.0.0"`, `gpumesh/claimer.py:37`,
  resolved by `_resolve_claim_bind_host`, `gpumesh/claimer.py:40`, and bound at
  `gpumesh/claimer.py:585`). That default is deliberate and is not the
  coordinator's: a claim server exists solely to be reached by a coordinator on
  *another* machine, so binding loopback would not make it safer, it would make
  it a broken feature that fails as an unreachable-worker mystery. What it now
  has is a *knob* — pass `bind_host`, or set `GPUMESH_CLAIM_HOST`, to pin it to
  one address (a tailnet or VPN address is the usual reason). A separate
  variable from `GPUMESH_HOST` on purpose: `GPUMESH_HOST=127.0.0.1` is a
  sensible coordinator setting, and sharing it would silently make every worker
  on that box unclaimable. A non-loopback bind prints a banner naming the OS
  user that claims will run as (`_print_claim_exposure_warning`,
  `gpumesh/claimer.py:73`, called at `gpumesh/claimer.py:597`). It is
  token-gated and rate-limited — see `THREAT_MODEL.md` §5.2, which also states
  plainly what narrowing the bind does and does not buy.
- **`gpumesh setup`**, in its "Same WiFi / LAN (auto-discover nearby devices)"
  mode, binds `0.0.0.0` on purpose (`WIZARD_LAN_BIND_HOST`,
  `gpumesh/setup_wizard.py:455`, resolved at `gpumesh/setup_wizard.py:478`).
  A loopback bind would make the wizard unable to complete its own flow. It
  honours `GPUMESH_HOST` if set, and it prints the same exposure banner
  `serve` does (`_announce_bind_host`, `gpumesh/setup_wizard.py:478`, called
  at `gpumesh/setup_wizard.py:611`).
- **The UDP discovery beacon**, which is broadcast and unauthenticated by
  design and carries no token.

Everything else in this document assumes the operator has opted into
exposure — by widening the bind, or by opening a tunnel with `--public`, which
is a larger opt-in than any bind. That opt-in is where the threat model begins.

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
defaulting to loopback (`gpumesh/cli.py:356`) — generates or supplies the
token (`gpumesh/cli.py:578`, `secrets.token_urlsafe(12)`), decides who gets
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
(read at `gpumesh/server.py:276`, checked by `SecurityManager.verify_request`,
`gpumesh/security.py:239`) grants:

- arbitrary code execution on every worker in the mesh, as the OS user that
  ran `gpumesh join` (`gpumesh/worker.py:145` deserializes the function,
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
token check at `gpumesh/server.py:273` (`_authed`) and, on the claim port,
`gpumesh/claimer.py:478` (`hmac.compare_digest`, mirrored in the worker's
patched handler at `gpumesh/worker.py:1029`).

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
  in a subprocess (`gpumesh/worker.py:201`) and script tasks in a temp
  directory (`gpumesh/sandbox.py:73`), both for crash containment and cleanup
  — not for confinement. Same user, same filesystem, no namespaces, no seccomp.
  Script tasks do get a trimmed environment (`gpumesh/sandbox.py:78-91`), which
  is a convenience for reproducibility, not a boundary: the script can read
  anything the OS user can.
- **Cleartext HTTP on a network the operator chose.** gpumesh speaks plain
  HTTP. The operator picks the network and is told to use `--tailscale` or
  `--public` when it is not one they control.
- **A worker returning a malicious result to a client that trusts it.** The
  reverse-deserialization path described above is a documented property of a
  single trust domain, not a bypass: the malicious worker had to be admitted
  to the mesh with a valid token first. It is listed here so nobody discovers
  it by surprise — but a report that a worker you deliberately joined can
  attack your client is a report that the trust model works as written.
- **Anything reachable only because the operator opened the port.**
  `gpumesh serve` binds `127.0.0.1` unless told otherwise
  (`gpumesh/cli.py:341`, `gpumesh/cli.py:356`). Passing `--host 0.0.0.0`, or
  setting `GPUMESH_HOST`, is an opt-in that prints a full-width banner naming
  the OS user and device that submitted code will run as
  (`gpumesh/cli.py:432`). A consequence of an exposure the operator chose and
  was warned about is not a vulnerability in gpumesh.
- **Anything reachable only because the operator opened a tunnel.**
  `--public` is a *separate* opt-in from the bind, not an additional one on
  top of it. This document used to say exposure to the internet required
  `--public` on top of a widened bind; that was never true. The ngrok agent
  dials `127.0.0.1:PORT` locally, so `--public` alone reaches the internet
  from the loopback default — see "`--public` is a second door", above. Until
  that was corrected, the combination also printed no exposure banner at all,
  which *was* a real defect and is fixed: the tunnel banner
  (`gpumesh/cli.py:500`) now prints on every bind, before the tunnel opens.
  Running `--public` and being reachable from the internet is the feature
  working. Running it and *not being told* would be a bug — report that.
- **The worker claim port listening on `0.0.0.0`.** `gpumesh worker` exists to
  be claimed by a coordinator on another machine, so its claim server binds
  every interface by default (`gpumesh/claimer.py:37`, bound at
  `gpumesh/claimer.py:585`). It is token-gated (`gpumesh/claimer.py:478`),
  rate-limited (`gpumesh/claimer.py:217`, consulted at
  `gpumesh/claimer.py:356`), capped at 16 KB (`gpumesh/claimer.py:227`), and
  socket-timed-out (`gpumesh/claimer.py:246`). An operator who wants a narrower
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
  `gpumesh/security.py:67`); a way around it, or a measurable timing signal,
  is a vulnerability. So is a way to make the rate limiter reveal whether a
  token was correct — see "How a rejected request answers", below.
- **Tokens leaking into logs, tracebacks, error responses, saved files with
  wrong permissions, or process listings.** Note the known one: passing
  `--token` on the command line puts it in the process table for every local
  user, and `gpumesh serve` prints it to stdout. Use `GPUMESH_TOKEN` where
  local users are a concern, and note that `gpumesh serve` also prints the
  token to stdout (`gpumesh/cli.py:669`). `~/.gpumesh/config.json` holds the
  token in plaintext at 0600; when gpumesh cannot restrict it, or finds it
  group/other-readable on load, it says so (`gpumesh/connection_manager.py:44`
  and `:135`). A *new* leak — into a traceback, an HTTP error body, the SQLite
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
  tunnel banner (`gpumesh/cli.py:500`) *before* `open_tunnel` runs, on every
  bind, and no other line printed in that run may claim the coordinator is
  unreachable. A build where `--public` is quiet, or where the tunnel banner
  is skipped on some bind addresses, is a vulnerability on the same reasoning
  as an ignored `--host`: the operator's consent is only real if they were
  told what they were consenting to.
- **Path traversal, or arbitrary file read/write, through any endpoint.**
  Job names, task IDs and payload keys must never reach a filesystem path.
- **Deserialization of an attacker payload before authentication is verified.**
  On the coordinator, `_authed` runs before `_read_json` on every route
  (`gpumesh/server.py:292` for GET, `gpumesh/server.py:339` for POST). If a
  coordinator route ever unpickles, or even parses, an unauthenticated body,
  that is critical. The claim port is the one place that *must* parse before
  it can authenticate, because the claim protocol carries the token in the
  body — the exposure there is bounded deliberately and described in
  `THREAT_MODEL.md` §5.2. Widening it is a vulnerability.
- **SSRF from a worker or coordinator to an attacker-chosen URL.** The claim
  flow takes a URL from the network and fetches it
  (`find_reachable_coordinator`, called at `gpumesh/claimer.py:504`) — it is gated on
  the worker's own token, and a way to trigger it without that token is a
  vulnerability.

## How a rejected request answers

`SecurityManager.verify_request` (`gpumesh/security.py:239`) returns one of
three distinguishable messages, and the distinction is intentional in both
directions — usability and disclosure.

1. **Not on the allowlist** — the address will never be accepted.
2. **Rate limited** — this IP has burned its attempts. Five failures in a
   300 s window buy a 900 s lockout (`RateLimiter`, `gpumesh/security.py:81`).
3. **Bad token** — the token really is wrong, with the number of attempts
   remaining before lockout.

The rate-limited message says explicitly that *the token in this request was
not checked at all* (`gpumesh/security.py:275-282`), and that is literally
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
`gpumesh/security.py:121`; `is_loopback` covers `127.0.0.0/8`, `::1`, and
IPv4-mapped forms, `gpumesh/security.py:14`). This is not a weakened control.
Anyone who can open a socket from `127.0.0.1` is already executing code on the
coordinator host, where the token sits in argv, in the environment, and in
`~/.gpumesh/config.json` — they read it, they do not guess it. Meanwhile a
loopback lockout takes out the coordinator's own self-worker and the
operator's own CLI, which are the two things that cannot be told to come from
a different address. An exempt IP is never even *counted*
(`gpumesh/security.py:142-148`), so no history accumulates. Loopback failures
therefore get a fourth message, saying plainly that retrying is fine.

The same `RateLimiter` class, with the same defaults and the same loopback
exemption, guards the worker claim port (`gpumesh/claimer.py:217`).

## A note on token hashing

The README describes "token hashing, SHA-256, in memory only". That is
accurate about where the hash lives and misleading about what it protects
against, so here is the whole truth.

`hash_token` (`gpumesh/security.py:38`) derives its salt deterministically
from the token itself — `sha256(token)[:16]`, `gpumesh/security.py:62` — and
then does a single SHA-256 round. The determinism is deliberate and correct: workers persist the plain
token, and the coordinator must produce the same hash after a restart or every
worker would be locked out.

But a salt that is a pure function of its input cannot make two hashes of the
same token differ, so it provides **no protection against precomputation**.
One SHA-256 round provides no meaningful work factor either. What this gives
you is: the plain token is not held in the coordinator's stored state, and it
is never written to the database. What it does **not** give you is resistance
to an offline attack on a weak token.

That is fine for the tokens gpumesh generates — `secrets.token_urlsafe(12)` is
about 72 bits of entropy, beyond reach of any wordlist or GPU cracker. It is
**not** fine for a short `--token` you chose by hand. If you set your own
token, make it long and random. The defence is the token's entropy, not the
hash.

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

Use GitHub's private vulnerability reporting:

1. Open the **Security** tab on this repository.
2. Click **Report a vulnerability**.
3. Describe the problem and follow up privately from there.

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
