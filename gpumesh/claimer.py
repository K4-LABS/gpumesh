"""Worker-side claim server: lightweight HTTP endpoint that lets a
coordinator connect to this worker by presenting the worker's token.

Endpoint:
    POST /api/claim   {"token": "...", "coordinator_url": "...", "coordinator_token": "..."}

If the token matches, the worker starts a background thread that joins
the coordinator mesh via ``gpumesh.worker.run_worker``.
"""

# Required for the ``X | None`` annotations below: on Python 3.9 they would
# otherwise be evaluated at definition time and raise TypeError on import.
from __future__ import annotations

import hmac
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gpumesh.ansi import safe_print, green, yellow, red, cyan, bold, dim
from gpumesh.security import RateLimiter


# The claim server's default bind, and deliberately NOT the coordinator's
# ``cli.DEFAULT_BIND_HOST = "127.0.0.1"``. The two listeners look alike and are
# not: ``gpumesh serve`` can sensibly default to loopback because running a
# coordinator and a worker on one machine is a real, common, useful thing. The
# claim server has no such case. It exists for exactly one purpose — to let
# *another* machine reach this one — so a loopback claim server is not a safer
# claim server, it is a broken one that fails as an unreachable-worker
# mystery rather than a refusal. Discovery mode is already an explicit opt-in
# the operator chose; narrowing the default here would silently undo that
# choice. What was missing was never a smaller default, it was the ability to
# say something more precise than "everything", which is what
# _resolve_claim_bind_host adds below.
DEFAULT_CLAIM_BIND_HOST = "0.0.0.0"


def _resolve_claim_bind_host(bind_host=None) -> str:
    """Resolve the address the claim server's listening socket binds to.

    Same precedence, and deliberately the same shape, as
    ``cli._resolve_bind_host``: an explicit argument beats the
    ``GPUMESH_CLAIM_HOST`` environment variable, which beats the default. An
    operator who has learned how ``--host``/``GPUMESH_HOST`` behave on the
    coordinator should not have to learn a second set of rules for the worker,
    so the only thing that differs is the default and the variable name.

    A separate variable rather than reusing ``GPUMESH_HOST`` on purpose: the
    two answer different questions, they are frequently set on the same
    machine, and ``GPUMESH_HOST=127.0.0.1`` — an entirely reasonable thing to
    set for a coordinator — would otherwise quietly render every worker on
    that box unclaimable.

    The value is passed to ``bind()`` as-is; a name that does not resolve or
    an address this machine does not hold surfaces as the OSError from bind,
    which ``run_worker_broadcast`` already reports.
    """
    explicit = bind_host or ""
    try:
        explicit = explicit.strip()
    except AttributeError:
        explicit = str(explicit)
    if explicit:
        return explicit
    env = os.environ.get("GPUMESH_CLAIM_HOST", "").strip()
    if env:
        return env
    return DEFAULT_CLAIM_BIND_HOST


def _print_claim_exposure_warning(bind_host: str, port: int):
    """Announce a claim port that is reachable from off this machine.

    Not ``cli._print_exposure_warning``, though it is modelled on it. That one
    is written for the coordinator: it is titled NETWORK-EXPOSED COORDINATOR,
    and its closing line tells the reader that loopback is the default and how
    to get back to it — advice that is actively wrong here, where loopback is
    not the default and would break the feature. It also probes the GPU to
    name the device, which is right for a banner printed once at ``serve``
    startup and wrong for a function the worker path and the test suite call
    repeatedly; ``run_worker_broadcast`` has already printed the device name
    two lines above this anyway.

    What it keeps is the part that matters: name the consequence concretely.
    "Exposure" is skimmable, "anyone who reaches this port with the token runs
    code as <you>" is not — and on this port the consequence is strictly worse
    than on the coordinator's, because the claim body also names the URL this
    worker will then fetch and obey.
    """
    try:
        import getpass
        user = getpass.getuser()
    except Exception:
        user = (os.environ.get("USERNAME") or os.environ.get("USER")
                or "the user running this process")

    wildcard = str(bind_host).strip().strip("[]") in (
        "", "0.0.0.0", "*", "::", "0:0:0:0:0:0:0:0",
    )
    scope = ("every network interface on this machine" if wildcard
             else f"the {bind_host} interface only")

    safe_print("")
    safe_print(yellow(bold("  " + "!" * 62)))
    safe_print(yellow(bold("  !! CLAIM PORT OPEN TO THE NETWORK")))
    safe_print(yellow(bold("  " + "!" * 62)))
    safe_print(f"   Listening on: {bold(f'{bind_host}:{port}')} {dim('(' + scope + ')')}")
    safe_print(f"   Claims run as: {bold(user)}")
    safe_print("")
    safe_print(yellow("   Anyone who can reach this port AND has this worker's token"))
    safe_print(yellow(f"   can make it join a mesh of their choosing and run code as {user}."))
    if wildcard:
        safe_print(dim("   Narrow it to one interface with GPUMESH_CLAIM_HOST=<addr>"))
        safe_print(dim("   (for example your tailnet address) if you do not need all of them."))
    safe_print(yellow(bold("  " + "!" * 62)))
    safe_print("")


class _FastBindHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer whose bind skips the reverse-DNS lookup.

    ``socketserver.HTTPServer.server_bind`` calls ``socket.getfqdn(host)``
    to set ``server_name``, and ``getfqdn`` does a reverse lookup
    (``gethostbyaddr``). On a machine with a slow or broken resolver —
    macOS CI runners are the canonical example, where a single lookup can
    block for tens of seconds — every claim server start would hang at
    bind. ``server_name`` is cosmetic here, so set it to the plain host
    string instead of paying for DNS.
    """

    # Same reasoning as the coordinator's server of this name: on Windows
    # SO_REUSEADDR lets a second process bind a port that is already being
    # listened on, with no error, so another process — including one running
    # as a different user — could quietly bind over a live claim port and
    # receive the claims meant for it. A claim carries a coordinator URL and
    # token that this worker will then go and execute code for, so silently
    # handing that to whoever bound second is the worst possible outcome on
    # this particular socket. POSIX keeps it: there it only means "reuse a
    # port stuck in TIME_WAIT", which is what lets a claim server restart.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        from socketserver import TCPServer

        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def claim_candidates(body: dict) -> list:
    """Extract the coordinator URLs to try, best first.

    ``coordinator_urls`` is the current field: a ranked list, because the
    coordinator cannot know which of its addresses this worker can route to
    and so should offer every plausible one. ``coordinator_url`` is the
    original single-address field, still accepted so a new worker keeps
    working with an older coordinator, and appended last if it is not
    already in the list.
    """
    candidates = []
    raw = body.get("coordinator_urls")
    if isinstance(raw, list):
        for url in raw:
            if isinstance(url, str) and url and url not in candidates:
                candidates.append(url)
    single = body.get("coordinator_url", "")
    if isinstance(single, str) and single and single not in candidates:
        candidates.append(single)
    return candidates


class ClaimHandler(BaseHTTPRequestHandler):
    """Handler for the claim port.

    WHAT AN UNAUTHENTICATED CALLER CAN REACH, exactly. This listener binds
    every interface by default (``GPUMESH_CLAIM_HOST`` narrows it; see
    ``start_claim_server``) on a machine whose entire purpose is executing
    Python that arrived over the network, and the token that guards it lives
    in the request *body* — the claim protocol puts it there, so unlike the
    coordinator (which reads ``X-Auth-Token`` from the headers and can
    therefore authenticate before touching the body at all) this handler
    physically cannot check the token without parsing something first. That
    is not a thing to fix by moving the check; it is a thing to fix by
    making the parse small and bounded. So:

    Reachable before the token is checked:
      * the request line and headers, parsed by ``BaseHTTPRequestHandler``;
      * ``Content-Length`` handling, but only as far as an ``int()`` and
        three comparisons — a declared length above ``MAX_CONTENT_LENGTH``
        is refused before a single body byte is allocated;
      * ``json.loads`` over at most ``MAX_CONTENT_LENGTH`` bytes, and only
        for a caller who is not currently rate limited.

    NOT reachable before the token is checked:
      * any allocation proportional to an attacker-declared length;
      * a body at all, once the caller's IP has burned its attempts —
        ``_rate_limiter`` is consulted before ``Content-Length`` is even
        read, so a locked-out peer cannot drive the JSON parser;
      * ``claim_candidates``, ``find_reachable_coordinator``, the
        ``_claimed`` flag, the outbound fetch to a caller-supplied URL, and
        ``run_worker``. Those all sit behind ``hmac.compare_digest``, which
        matters more here than anywhere else in the codebase: a token
        bypass on this port is remote code execution *and* SSRF, because
        the coordinator URL that the worker then talks to comes out of this
        same request body.
      * an unbounded hold on a server thread — see ``timeout`` below.
    """

    server_version = "gpumesh-claim"
    _claimed = False          # class-level flag — prevents double-claim
    _claim_lock = threading.Lock()
    _token: str = ""          # set by start_claim_server
    _worker_thread: threading.Thread | None = None

    # Brute-force protection for the claim token. This is deliberately the
    # *same* RateLimiter class the coordinator uses rather than a second
    # implementation: one lockout policy, one set of numbers for an operator
    # to learn, one place for a bug to be fixed. Defaults (5 attempts in a
    # 300 s window -> 900 s lockout) are inherited unchanged for the same
    # reason. Loopback stays exempt, exactly as on the coordinator: anyone
    # who can open a socket from 127.0.0.1 to this worker is already running
    # code on this machine and can simply read the token out of argv or
    # ~/.gpumesh/config.json, so a loopback lockout protects nothing and
    # would wedge the operator's own retry after a couple of typos.
    _rate_limiter = RateLimiter()

    # -- plumbing -------------------------------------------------------

    # A claim payload is a token, a coordinator token, and at most a handful
    # of candidate URLs — under a kilobyte in practice. 16 KB is roughly
    # sixteen times the largest legitimate claim and still small enough that
    # an unauthenticated peer cannot make us allocate anything meaningful.
    # The old 1 MB cap was inherited from the coordinator, which carries
    # serialized task payloads; nothing on this port ever does.
    MAX_CONTENT_LENGTH = 16 * 1024

    # How much of an oversized body we are willing to swallow purely to be
    # polite. Draining lets the peer actually read our 413 instead of eating
    # a connection reset (Windows is especially unforgiving here — closing a
    # socket with unread bytes in the receive buffer sends an RST and the
    # response is lost). But draining the *full* declared length would turn
    # "Content-Length: 10000000000" into a free way to make us read 10 GB,
    # so past this point we stop being polite and close the connection.
    MAX_DRAIN_BYTES = 256 * 1024
    DRAIN_TIMEOUT = 2.0

    # StreamRequestHandler.setup() applies this to the connection socket.
    # Without it, rfile reads block forever, and since this is a threading
    # server, an unauthenticated peer that opens connections and declares a
    # body it never sends pins one worker thread per connection until the
    # process dies. A real coordinator delivers a sub-kilobyte claim in
    # milliseconds; fifteen seconds is generous for anything that is not an
    # attack or a dead link.
    timeout = 15

    # Per-request state; see handle_one_request for why these are reset
    # there rather than in __init__.
    _responded = False
    _body_consumed = False

    def handle_one_request(self):
        # BaseHTTPRequestHandler reuses one handler instance for every
        # request on a connection, so these flags have to be cleared per
        # request rather than per instance.
        self._responded = False
        self._body_consumed = False
        super().handle_one_request()

    def _client_ip(self) -> str:
        try:
            return self.client_address[0]
        except (AttributeError, IndexError, TypeError):
            return ""

    def _send(self, code: int, body=None):
        # First response wins. This is not defensive padding: worker.py
        # monkey-patches do_POST with _intercepted_do_POST, which on any
        # falsy/None body from _read_json falls through to the original
        # do_POST — and the original then tries to answer a request that has
        # already been answered. Without this guard that path writes two HTTP
        # responses onto one connection, which a client reads as a garbled
        # second reply. Swallowing the duplicate here keeps the two handlers
        # consistent without reaching into worker.py.
        if self._responded:
            return
        self._responded = True

        # Rate-limit accounting lives here, in the one method both do_POST
        # implementations funnel through, so worker.py's patched handler is
        # protected by exactly the same policy without needing to know it
        # exists. 401 is only ever emitted for a token mismatch and 200 only
        # for a completed claim, so the mapping is unambiguous.
        ip = self._client_ip()
        if code == 401:
            self._rate_limiter.record_failure(ip)
        elif code == 200:
            self._rate_limiter.record_success(ip)

        data = (
            json.dumps(body, ensure_ascii=False).encode("utf-8")
            if body is not None else b""
        )
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if data:
                self.wfile.write(data)
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            pass

    # Enough to hold any plausible chunked prologue, and small enough that a
    # peer cannot use this path to make us read indefinitely. Mirrors the
    # coordinator's helper of the same name — the two handlers face the same
    # Windows RST behaviour and should read as siblings.
    SWEEP_BYTES = 16 * 1024
    SWEEP_TIMEOUT = 0.25

    def _sweep_unread(self):
        """Discard already-arrived body bytes so a close does not RST.

        Best effort by design: this exists only so the response we are about
        to send survives the close, never to receive anything. The timeout is
        short because the peer has already sent what it is going to send and
        is now waiting on us — waiting longer would delay the error without
        making it any more likely to arrive.
        """
        try:
            self.connection.settimeout(self.SWEEP_TIMEOUT)
        except (AttributeError, OSError):
            return
        try:
            remaining = self.SWEEP_BYTES
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 8192))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass  # timeout or reset: nothing left worth taking
        finally:
            try:
                self.connection.settimeout(self.timeout)
            except (AttributeError, OSError):
                pass

    def _drain(self, length: int):
        """Swallow a body we refused to read, so the peer can read our 413.

        Only for bodies that are merely too big, not absurd. Past
        ``MAX_DRAIN_BYTES`` the polite thing stops being the safe thing:
        reading it would be free bandwidth-burning for an attacker, and a
        peer that declared gigabytes is not going to be surprised by a
        closed connection.
        """
        if length > self.MAX_DRAIN_BYTES:
            self.close_connection = True
            return
        # A peer that declares bytes and then stalls should not get the full
        # request timeout out of us — it already told us the request is
        # dead, and this read exists only for its benefit.
        try:
            self.connection.settimeout(self.DRAIN_TIMEOUT)
        except (AttributeError, OSError):
            pass
        remaining = length
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            # Includes socket.timeout (an OSError subclass since 3.3).
            self.close_connection = True
        finally:
            try:
                self.connection.settimeout(self.timeout)
            except (AttributeError, OSError):
                pass

    def _read_json(self):
        # Re-entry guard. worker.py's _intercepted_do_POST delegates to the
        # original do_POST when this returns None, and the original calls
        # _read_json again. Reading the body a second time would block on a
        # socket whose bytes we already consumed — a hang, not an error — so
        # every path below sets _body_consumed before it returns None.
        if self._body_consumed:
            return None

        # Rate limit FIRST, ahead of every byte of body handling, because
        # this is the only check that costs nothing and the whole point is
        # that a locked-out peer must not be able to drive the parser. The
        # token cannot be checked here (it is in the body we have not read
        # yet), which is precisely why the gate that does not need the body
        # goes first.
        ip = self._client_ip()
        if not self._rate_limiter.is_allowed(ip):
            self._body_consumed = True
            self.close_connection = True
            remaining = int(self._rate_limiter.get_lockout_remaining(ip))
            safe_print(yellow(
                f"[claim] rate limited {ip}: too many bad tokens "
                f"({remaining}s remaining)"
            ))
            self._send(429, {
                "ok": False,
                "error": (
                    f"Too many failed claim attempts from this address. "
                    f"Locked out for another {remaining}s. The token in this "
                    f"request was not checked at all."
                ),
            })
            return None

        # No chunked bodies. BaseHTTPRequestHandler does not decode chunked
        # transfer encoding, so accepting one would mean handing the raw
        # framing bytes to json.loads and leaving the rest in the pipe.
        # Say 411 and close rather than half-understand the request.
        if self.headers.get("Transfer-Encoding"):
            self._body_consumed = True
            self.close_connection = True
            # Sweep the framing bytes that already arrived before answering.
            # This file's own MAX_DRAIN_BYTES comment states the rule: closing
            # a socket with unread bytes in the receive buffer sends an RST on
            # Windows and the response is lost. The oversize branch below obeys
            # it via _drain(); this branch did not, and the 411 was silently
            # dropped whenever the client sent its headers and body as two
            # writes with any gap between them. A 1 ms gap was enough to
            # reproduce it every time.
            #
            # _drain() is not usable here: it takes a declared length, and a
            # chunked request has none by definition. Reading toward a guessed
            # length would block for DRAIN_TIMEOUT waiting for bytes the peer
            # already finished sending. Only what is buffered is worth taking.
            self._sweep_unread()
            self._send(411, {"error": "chunked bodies are not accepted; "
                                      "send Content-Length"})
            return None

        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._body_consumed = True
            self.close_connection = True
            self._send(411, {"error": "Content-Length required"})
            return None
        try:
            length = int(raw_length)
        except (ValueError, TypeError):
            self._body_consumed = True
            self.close_connection = True
            self._send(400, {"error": "invalid Content-Length"})
            return None
        if length < 0:
            self._body_consumed = True
            self.close_connection = True
            self._send(400, {"error": "invalid Content-Length"})
            return None
        if length == 0:
            self._body_consumed = True
            self._send(400, {"error": "empty request body"})
            return None
        if length > self.MAX_CONTENT_LENGTH:
            # Refused on the declared length alone — nothing is allocated,
            # and _drain reads in bounded chunks it immediately discards.
            self._body_consumed = True
            self._drain(length)
            self._send(413, {"error": "request too large"})
            return None

        self._body_consumed = True
        try:
            raw = self.rfile.read(length)
        except OSError:
            # Peer promised `length` bytes and stalled. Nothing to answer.
            self.close_connection = True
            return None
        if len(raw) != length:
            self.close_connection = True
            self._send(400, {"error": "truncated request body"})
            return None

        # Answer here *and* re-raise. do_POST below has its own handlers for
        # these two exceptions and would send the identical 400, which the
        # first-response-wins guard in _send then swallows — so nothing
        # changes for this handler. What changes is worker.py's patched
        # handler: it catches these exceptions and delegates to the original
        # do_POST, which used to re-read a body that was already gone. That
        # was an indefinite block on a socket, not an error. Now the caller
        # gets its 400 from right here regardless of which do_POST is
        # installed, and the delegation is a no-op.
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError:
            self._send(400, {"error": "request body must be UTF-8"})
            raise
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid JSON"})
            raise

        # Shape check belongs here for the same reason: a JSON array reaching
        # the patched handler would otherwise fall through to a second read.
        if not isinstance(parsed, dict):
            self._send(400, {"error": "JSON body must be an object"})
            return None
        return parsed

    def log_message(self, fmt, *args):
        pass  # quiet

    # -- routes ---------------------------------------------------------

    def do_POST(self):
        if self.path != "/api/claim":
            self._send(404, {"error": "unknown endpoint"})
            return

        try:
            body = self._read_json()
        except UnicodeDecodeError:
            self._send(400, {"error": "request body must be UTF-8"})
            return
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid JSON"})
            return
        if body is None:
            # _read_json has already answered — rate limit, framing, size,
            # or shape. Everything below this line is post-authentication
            # or leads directly to it.
            return

        # Validate inputs BEFORE acquiring lock (no lock needed for read-only checks)
        token = body.get("token", "")
        if not hmac.compare_digest(str(token), ClaimHandler._token):
            self._send(401, {"ok": False, "error": "wrong token"})
            return

        candidates = claim_candidates(body)
        coordinator_token = body.get("coordinator_token", "")
        if not candidates or not coordinator_token:
            self._send(400, {"ok": False,
                             "error": "need coordinator_url(s) and coordinator_token"})
            return

        # Atomically check and set _claimed to prevent double-claim race
        with ClaimHandler._claim_lock:
            if ClaimHandler._claimed:
                self._send(409, {"ok": False, "error": "already claimed"})
                return
            ClaimHandler._claimed = True

        # Prove reachability BEFORE acking. Acking on a token match alone
        # made the ack mean "your token is right" while the coordinator read
        # it as "the worker joined" — so a claim naming an address this
        # machine cannot route to was reported as a success, and the failure
        # only surfaced seconds later in the worker's own log where the
        # coordinator never saw it.
        from .worker import find_reachable_coordinator

        coordinator_url, reason = find_reachable_coordinator(
            candidates, coordinator_token
        )
        if coordinator_url is None:
            with ClaimHandler._claim_lock:
                ClaimHandler._claimed = False  # allow a corrected retry
            safe_print(yellow(f"[claim] rejected: cannot reach coordinator ({reason})"))
            self._send(502, {
                "ok": False,
                "error": f"cannot reach coordinator from this machine: {reason}",
                "tried": candidates,
            })
            return

        # Spawn background thread to join the coordinator mesh
        def _join():
            try:
                from .worker import run_worker
                run_worker(coordinator_url, coordinator_token)
            except Exception as exc:
                safe_print(red(f"[claim] failed to join coordinator: {exc}"))
                with ClaimHandler._claim_lock:
                    ClaimHandler._claimed = False  # allow retry on failure

        thread = threading.Thread(target=_join, daemon=True, name="gpumesh-claim-worker")
        ClaimHandler._worker_thread = thread
        thread.start()
        safe_print(green(f"[claim] claimed via {coordinator_url}"))
        self._send(200, {"ok": True, "coordinator_url": coordinator_url})


def start_claim_server(token: str, port: int = 0,
                       bind_host: str | None = None
                       ) -> tuple[ThreadingHTTPServer, int]:
    """Start the claim server and return ``(httpd, actual_port)``.

    Binds every interface (``0.0.0.0``) unless narrowed, because a claim
    server exists solely to be reached from another machine — see
    ``DEFAULT_CLAIM_BIND_HOST``. Pass *bind_host*, or set
    ``GPUMESH_CLAIM_HOST``, to pin it to one interface instead (a tailnet or
    VPN address is the usual reason). If *port* is 0 the OS picks an ephemeral
    port.

    RESIDUAL RISK, stated plainly because narrowing the bind does not remove
    it. Everything an unauthenticated caller can reach on this port is
    enumerated in ``ClaimHandler``'s docstring; the hardening around it is
    real — a 16 KB cap refused on the declared ``Content-Length`` before a
    byte is allocated, 411 on chunked or absent framing, a 15 s socket timeout
    so a stalled peer cannot pin a thread, and ``security.RateLimiter``
    consulted ahead of all of it — but every one of those is a limit on the
    *cost* of an unauthenticated request, not a gate on making one. The gate
    is ``hmac.compare_digest`` against a token that travels in the request
    body, which the claim protocol requires and which therefore cannot be
    checked until some parsing has happened. So:

      * the port is unauthenticated up to and including ``json.loads`` over at
        most 16 KB, for callers that are not locked out;
      * past that point a correct token is remote code execution as this user,
        by design — that is what claiming *is* — and also SSRF, since the
        coordinator URL this worker then fetches comes out of the same body;
      * a lockout is per source IP and in-memory, so it slows a single
        attacker rather than a distributed or spoofing one, and loopback is
        exempt (anyone on 127.0.0.1 can read the token out of argv anyway);
      * narrowing the bind shrinks who can open the socket at all, which is
        the only control here that reduces the attack *surface* rather than
        the cost per attempt. That is why the knob exists.

    The token is the security boundary. Treat it like one.
    """
    ClaimHandler._token = token
    with ClaimHandler._claim_lock:
        ClaimHandler._claimed = False

    # Fresh listener, fresh lockout state — same reasoning as _claimed above.
    # A restarted worker is a deliberate operator action on the worker's own
    # console; carrying a stale lockout across it would only punish the
    # operator, and it gives a remote attacker nothing, since they cannot
    # cause a restart in the first place.
    ClaimHandler._rate_limiter = RateLimiter()

    resolved_host = _resolve_claim_bind_host(bind_host)
    httpd = _FastBindHTTPServer((resolved_host, port), ClaimHandler)
    actual_port = httpd.server_address[1]

    # Announce after the bind, not before: the port is only real once bind()
    # has succeeded, and printing "open to the network" for a listener that
    # then failed to start would be a lie in the one direction that matters.
    # Loopback is not the default and not useful, but it is reachable through
    # the knob and through tests, and a warning that fires when nothing is
    # actually exposed is how operators learn to skim these.
    from .cli import _is_loopback_bind  # local import: cli imports worker

    if not _is_loopback_bind(resolved_host):
        _print_claim_exposure_warning(resolved_host, actual_port)

    # On Windows, suppress the WinError 10038 noise when shutting down
    _orig_serve_forever = httpd.serve_forever

    def _safe_serve_forever():
        try:
            _orig_serve_forever()
        except OSError as exc:
            if getattr(exc, "winerror", None) == 10038:
                pass
            else:
                raise

    httpd.serve_forever = _safe_serve_forever

    return httpd, actual_port
