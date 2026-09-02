# Architecture

```
                              COORDINATOR
        ┌──────────────────────────────────────────────────────┐
        │                                                      │
        │  ┌───────────┐  ┌───────────┐  ┌──────────────────┐  │
        │  │ Job Queue │  │  Task DB  │  │ Worker Registry  │  │
        │  │ (memory)  │  │ (SQLite)  │  │ (in memory)      │  │
        │  └─────┬─────┘  └───────────┘  └─────────┬────────┘  │
        │        │                                 │           │
        │        └────────────────┬────────────────┘           │
        │                         │                            │
        │               ┌─────────┴──────────┐                 │
        │               │ Token check        │                 │
        │               │ cache ─► PBKDF2    │                 │
        │               └─────────┬──────────┘                 │
        │                         │                            │
        │   HTTP API :8000   (HTTPS when started with --tls)   │
        └─────────────────────────┼────────────────────────────┘
                                  │
                   ┌──────────────┼──────────────┐
                   │              │              │
             ┌─────▼────┐   ┌─────▼────┐   ┌─────▼────┐
             │ Worker 1 │   │ Worker 2 │   │ Worker 3 │
             │ RTX 4090 │   │ RTX 3080 │   │    T4    │
             │Score: 120│   │Score:  85│   │Score:  12│
             └──────────┘   └──────────┘   └──────────┘
                   │              │              │
                   └──────────────┴──────────────┘
                                  │
                           ┌──────▼──────┐
                           │   Results   │
                           │  Collected  │
                           └─────────────┘

  JOB FLOW:  Submit ─► Queue ─► Claim ─► Execute ─► Report ─► Collect ─► Decode
```

**How it works:** jobs are stored in SQLite, workers pull tasks over HTTP (or
HTTPS, see below) with a lease (so a crashed worker's task is automatically
re-queued), run each task in an isolated subprocess, and post results back.
Workers are scored by a benchmark and the scheduler assigns heavier tasks to
stronger workers.

## Transport: HTTP by default, HTTPS on request

The mesh speaks plain HTTP unless the coordinator was started with
`gpumesh serve --tls`. TLS is a per-coordinator decision, not a mesh-wide
setting and not a negotiation: the listener speaks exactly one scheme, and
every URL `serve` prints, saves to the connection file, or probes itself with
switches to `https://` automatically. Nothing guesses. A worker dialling
`http://` at an HTTPS listener gets a connection reset with no useful error,
which is the failure that rewrite exists to prevent.

The socket is wrapped after bind and before `serve_forever`, so a certificate
problem is a startup failure the operator sees on screen rather than a
per-connection error nobody is watching for. With no `--tls-cert`/`--tls-key`,
a self-signed pair is generated under `~/.gpumesh/tls/` on first use, via the
`cryptography` library, falling back to the `openssl` binary, and refusing to
start with an error naming all three fixes if neither is available. The key
file is chmod 0600 and its directory 0700. The certificate is valid for 825
days, reused across restarts (so a fingerprint pinned on a worker stays valid),
and regenerated once it is within 30 days of expiry. Its SAN covers
`localhost`, the machine's hostname, and every local address the machine can
see. TLS 1.2 is the floor on both ends. The coordinator prints the certificate
path and its SHA-256 fingerprint at startup.

A worker trusts that certificate one of three ways:

| Tier | How | What it gets you |
|------|-----|------------------|
| `GPUMESH_TLS_CA=/path/to/coordinator-cert.pem` | Copy the certificate to the worker by hand | Encrypted **and** verified against the fingerprint the coordinator printed |
| `--tls-cert` / `--tls-key` with a real CA certificate | An internal CA, or `tailscale cert` | Encrypted and verified; nothing to do on the worker |
| `GPUMESH_TLS_INSECURE=1` | Skip verification | Encrypted, unauthenticated. An active on-path attacker can still substitute a certificate |

All client HTTP goes through `MeshClient` in `gpumesh/worker.py`, which builds
the SSL context once and rewrites a bare "certificate verify failed" into an
instruction naming both variables.

**What `--tls` buys, and what it does not.** It closes passive eavesdropping on
a LAN: the shared token stops travelling in cleartext, and pickled payloads
cannot be rewritten in flight. It does *not* authenticate the coordinator
unless the certificate was moved to the worker by hand, and it does not make
gpumesh safe to face the internet. Anything crossing a network you do not
control should still be tunnelled over Tailscale or ngrok, with the tunnel as
the boundary.

`gpumesh/tls.py` holds all of it: certificate generation and renewal, the
server-side socket wrap, the client SSL context, and the verify-failure
message.

## Authenticating a request

Every arrow in the diagram crosses the network carrying the shared token, and
the coordinator checks it before the request reaches the queue, the database or
the worker registry. The check, in order:

1. **Rate limit by IP.** A limited IP is rejected without the token being
   examined at all, and the message says so.
2. **Verification cache.** A hit short-circuits the KDF.
3. **Key derivation.** A miss falls through to the full PBKDF2-HMAC-SHA256
   derivation and a constant-time compare.

The stored form is `pbkdf2_sha256$<iterations>$<salt>$<hash>`, with a random 16-byte
hex salt per token, 200000 iterations by default. That hash is derived from the
token at every coordinator start and lives in coordinator memory only. It is
never written to the database, so a stolen `gpumesh.db` yields nothing to crack.
Legacy single-round SHA-256 (`salt:hash`) still verifies forever and is produced
only when asked for, via `GPUMESH_AUTH_KDF=sha256`; `GPUMESH_AUTH_KDF_ITERATIONS`
moves the cost factor.

The cache exists because 200000 rounds per request is the wrong price for a
worker that polls for tasks every second or two. It is keyed by an HMAC of the
token under a random per-process key, held in memory, so a polling worker pays
the derivation once rather than on every request. A failure clears nothing and
falls straight into the lockout accounting.

`gpumesh/security.py` owns the derivation, the cache and the rate limiter.

## Getting results back

A worker posts its result to the coordinator; the submitting client fetches it
and decodes it. Two envelope shapes come back: JSON for anything
JSON-encodable, and cloudpickle for everything else: a tensor, a numpy array, a
DataFrame.

Decoding the second shape means unpickling bytes produced on another machine,
which runs that machine's code on the submitting host. `gpumesh --strict`
(or `GPUMESH_STRICT_RESULTS=1`) refuses to do it: a cloudpickled result raises
`UntrustedResultError` instead of being unpickled, and `gpumesh/client.py`
renders a `_gpumesh_strict` marker in place of the raw envelope. JSON results
are unaffected.

State the cost plainly: under `--strict`, a task that returns a tensor, a numpy
array or a DataFrame stops working. That is the trade, not a bug to route
around. With strict mode off, the first pickled decode in a process fires a
one-time `RuntimeWarning` so the risk is at least visible.

The two hardening flags point in opposite directions and are not substitutes:

| Flag | Side | Stops |
|------|------|-------|
| `--safe-mode` | Coordinator | Functions going **out**, leaving script jobs only |
| `--strict` | Submitting client | Pickled results coming **back** |

`gpumesh/serializer.py` implements the envelope and the strict branch.
