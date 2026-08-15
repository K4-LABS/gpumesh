"""Coordinator: threaded HTTP server exposing a JSON API.

Endpoints (all require the X-Auth-Token header):
  POST /api/register            {hostname, device, score, protocol_version?, ...}
                                -> {worker_id, protocol_version,
                                    min_protocol_version}
                                -> 426 when the worker's protocol version is
                                   outside this coordinator's window
  POST /api/heartbeat           {worker_id, score?}
  POST /api/lease               {worker_id} -> task | 204
  POST /api/result              {task_id, worker_id, ok, result?, error?}
  POST /api/jobs                {name, script, payloads} -> {job_id}
  GET  /api/jobs/<id>           -> job status + task results
  POST /api/cancel              {job_id} -> {pending, running} cancelled
  POST /api/retry               {job_id} -> {requeued, counts}
  GET  /api/workers             -> live worker list
  GET  /api/events              -> recent join/leave events (last 100)
"""

import json
import math
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import (
    __version__, status,
    MIN_PROTOCOL_VERSION, PROTOCOL_VERSION, UNVERSIONED_PROTOCOL,
    is_supported_protocol,
)
from .ansi import green, yellow, red, cyan, bold, dim
from .db import Database
from .security import SecurityManager

REAP_INTERVAL = 5.0

# How long shutdown waits for requests that were already accepted before it
# closes the database under them. This used to be a flat ``time.sleep(0.5)``,
# which is a guess dressed as a guarantee: a handler slower than the guess ran
# into "Cannot operate on a closed database" and answered 500, and if that
# handler was an /api/result the work it carried was destroyed. Waiting on the
# actual count of in-flight requests makes the common case *faster* (an idle
# coordinator stops immediately instead of sleeping half a second) and the bad
# case correct. It is still bounded, because one peer holding a socket open
# must not be able to hold shutdown open with it.
SHUTDOWN_DRAIN_SECONDS = 5.0

# 426 Upgrade Required is the one HTTP status that means exactly this: the
# request was well-formed and authenticated, and the server is refusing it
# solely because the peer speaks the wrong protocol version. It is chosen over
# 400 on purpose — a worker that sees 400 has no way to tell a version skew
# from a malformed body, and the whole point here is that the failure is
# self-diagnosing.
PROTOCOL_MISMATCH_STATUS = 426


def _protocol_refusal(worker_proto: int) -> str:
    """The message a refused worker is sent, and prints verbatim.

    Written in the register of ``serializer.deserialize_function``'s
    cross-Python refusal: name both versions, say which side is behind, and
    give the command that fixes it. An operator reading this in a terminal on
    a different machine from the coordinator has no other source of the
    coordinator's numbers, so they all have to be in the string.
    """
    if worker_proto < MIN_PROTOCOL_VERSION:
        direction = (
            f"This worker speaks gpumesh wire protocol {worker_proto}, which is "
            f"older than the oldest version this coordinator supports "
            f"({MIN_PROTOCOL_VERSION})."
        )
        fix = (
            "Upgrade the WORKER: run 'pip install -U gpumesh' on the worker "
            "machine and rejoin. If you must keep this worker as it is, run a "
            "coordinator from a gpumesh release old enough to still support "
            f"protocol {worker_proto}."
        )
    else:
        direction = (
            f"This worker speaks gpumesh wire protocol {worker_proto}, which is "
            f"newer than this coordinator's ({PROTOCOL_VERSION})."
        )
        fix = (
            "Upgrade the COORDINATOR: run 'pip install -U gpumesh' on the "
            "coordinator machine and restart it. Downgrading the worker to a "
            f"release that speaks protocol {PROTOCOL_VERSION} also works."
        )
    return (
        f"Refusing to register this worker: incompatible gpumesh protocol "
        f"version. {direction} This coordinator is gpumesh {__version__}, "
        f"speaking protocol {PROTOCOL_VERSION} and accepting "
        f"{MIN_PROTOCOL_VERSION}-{PROTOCOL_VERSION}. {fix} The two sides are "
        f"refused here, at registration, rather than allowed to join and then "
        f"fail in whatever way the version difference happens to produce."
    )


class _InFlight:
    """Counts requests currently being served, so shutdown can wait them out.

    One instance per coordinator (``serve()`` puts a fresh one on the handler
    subclass it builds), because two coordinators in the same process — which
    is the normal shape of this project's test suite — must not be able to
    hold each other's shutdown open.

    Deliberately counts whole *connections*, from the moment the handler
    thread starts to the moment it returns, rather than only the window inside
    a route. The point is to answer "can the database still be pulled out from
    under somebody?", and a request that has read its headers but not yet
    reached ``do_POST`` is just as capable of touching the database a
    microsecond later as one already inside it.
    """

    def __init__(self):
        self._cond = threading.Condition()
        self._count = 0

    def enter(self):
        with self._cond:
            self._count += 1

    def leave(self):
        with self._cond:
            self._count -= 1
            if self._count <= 0:
                self._cond.notify_all()

    def wait_until_idle(self, timeout: float) -> bool:
        """Block until nothing is in flight. False if the budget ran out."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._count > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True


def _coerce_elapsed(raw):
    """Turn whatever a worker sent as ``elapsed`` into a usable float or None.

    ``elapsed`` feeds one thing: the straggler statistics in
    ``Database.complete_task``, which does ``elapsed > 0``. On Python 3 that
    raises TypeError for a string — *inside* complete_task's ``with
    self._conn`` block, so the UPDATE that marked the task done is rolled
    back with it. A junk timing figure therefore used to un-record a finished
    task: the row stayed 'running', the worker got a 500, and the work was
    re-run 300 seconds later when the lease expired.

    Which is exactly backwards. The result is the only thing here that cannot
    be recomputed cheaply; the timing figure is an input to an average. So
    anything unusable degrades to None — "this task contributed no timing
    information" — and the result is still recorded. Nothing about a bad
    ``elapsed`` is worth a task's output.

    Rejected, and why:
      * bool — ``True`` is an int to Python and would score as 1.0 seconds.
      * NaN / inf — they survive the comparison and then poison an average
        permanently, which is worse than being dropped.
      * <= 0 — complete_task ignores these anyway; normalising them here
        keeps "no timing" a single value instead of two.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        # A numeric string is a client that stringified a float somewhere in
        # its own stack, not a client sending garbage. Accepting it costs
        # nothing and keeps its statistics.
        try:
            value = float(raw.strip())
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def _coerce_text(raw) -> str:
    """Force a free-text field to something sqlite will accept.

    Same failure shape as ``elapsed``, one layer along: ``error`` lands in a
    TEXT column, and binding a dict or a list to it raises
    sqlite3.InterfaceError inside the same transaction that records the
    outcome — so a malformed error message would discard the failure it was
    describing, and with it the task's retry accounting.
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return str(raw)


class _FastBindHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer whose bind skips the reverse-DNS lookup.

    ``socketserver.HTTPServer.server_bind`` calls ``socket.getfqdn(host)``
    to set ``server_name``, and ``getfqdn`` does a reverse lookup
    (``gethostbyaddr``). On a machine with a slow or broken resolver —
    macOS CI runners are the canonical example, where a single lookup can
    block for tens of seconds — every coordinator start would hang at
    bind. ``server_name`` is cosmetic here (nothing reads it), so set it
    to the plain host string instead of paying for DNS.
    """

    # SO_REUSEADDR means two very different things depending on the platform,
    # and inheriting HTTPServer's default is wrong on one of them.
    #
    # On POSIX it only lets a new socket take over a port still sitting in
    # TIME_WAIT, which is what makes restarting a coordinator on the same port
    # work — keep it.
    #
    # On Windows it lets a second process bind a port another process is
    # *actively listening on*, with no error. That is not a theoretical
    # concern here: it silently produced two coordinators on port 8000 with
    # different tokens, `serve()` raised no OSError so the "port already in
    # use" message never printed, connections went to whichever socket the
    # kernel picked, and the second coordinator's own self-worker was told
    # "authentication failed" by the first one. The user is then debugging a
    # token that was never wrong.
    #
    # It is also a hijack primitive: any local process, including one running
    # as a different user, can bind over a live coordinator and receive its
    # traffic — on a service whose whole purpose is executing code that
    # arrives over that socket. Windows' answer is to not ask for
    # SO_REUSEADDR at all, so a real "port in use" surfaces as EADDRINUSE the
    # way the calling code already expects.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        from socketserver import TCPServer

        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class CoordinatorHandler(BaseHTTPRequestHandler):
    server_version = "gpumesh"
    db: Database = None
    token: str = ""
    start_time: float = 0.0
    safe_mode: bool = False
    _shutdown_event = None
    # Replaced per-coordinator by serve(); the class-level default exists only
    # so a handler constructed outside serve() still works.
    _inflight = _InFlight()

    # POST routes that keep working after the shutdown flag is set.
    #
    # The rule is not "which endpoints are harmless" — it is which direction
    # the obligation runs. /api/register, /api/lease and /api/jobs all ask the
    # coordinator to take on NEW work, and a coordinator that is going away
    # must refuse those: accepting a job it will never schedule, or handing out
    # a lease it will never collect, is worse than a 503.
    #
    # /api/result is the opposite. The coordinator asked for that work, a
    # worker went and did it, and this request is the only copy coming back.
    # Refusing it does not decline work — it destroys work already done. The
    # worker sees the 503, and because HTTPError subclasses URLError it landed
    # in the "failed to submit result" warning arm, printed a line, counted the
    # task as done and dropped the payload on the floor; coordinator-side the
    # row stayed 'running' until LEASE_SECONDS expired and the whole task was
    # recomputed. So this one is routed BEFORE the gate and allowed to write.
    #
    # /api/heartbeat deliberately stays refused. It looks like it belongs here
    # — it is also a message about work already handed out — but all it does is
    # push a lease expiry and a last_seen timestamp forward, and extending a
    # lease on a coordinator whose database is about to close buys nothing.
    # The worker reads a failed heartbeat as "reconnect later", which is the
    # correct conclusion when the coordinator is shutting down. Nothing is lost
    # by refusing it, and that is the whole test.
    _SHUTDOWN_EXEMPT_POST_PATHS = ("/api/result",)

    # -- plumbing -------------------------------------------------------

    def handle(self):
        """Count this connection for the duration of its handler thread.

        Wrapping ``handle`` rather than the two route methods is what makes
        the shutdown drain honest: a thread that has been spawned but has not
        yet parsed its request line is still a thread that is about to touch
        the database.
        """
        self._inflight.enter()
        try:
            BaseHTTPRequestHandler.handle(self)
        finally:
            self._inflight.leave()

    # A job payload carries a cloudpickled function plus every parameter set
    # for the whole batch, so unlike the claim port's 16 KB this is a real
    # working limit rather than a nominal one — people submit multi-megabyte
    # batches on purpose. Anything past it is a mistake or an attack.
    MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10 MB

    # How much of an oversized body we will swallow purely so the peer can
    # read our 413 instead of eating a connection reset (Windows is
    # unforgiving here — closing a socket with unread bytes still in the
    # receive buffer sends an RST and the response is lost).
    #
    # Draining the *full declared* length, which is what this used to do,
    # turns "Content-Length: 10000000000" into a free way to make the
    # coordinator read 10 GB. Draining nothing at all would be worse for the
    # case that actually happens: a user whose batch came out slightly over
    # the cap, who deserves "request too large" rather than a socket error.
    # Twice the cap covers that overshoot and nothing else; past it we stop
    # being polite and close. A peer that declared more than 20 MB is not
    # going to be surprised by a closed connection.
    MAX_DRAIN_BYTES = 2 * MAX_CONTENT_LENGTH

    # Per-read timeout while draining, and a wall-clock ceiling on the whole
    # drain. The byte cap alone is not enough here the way it is on the claim
    # port: 20 MB delivered one slow chunk at a time would otherwise occupy a
    # thread for minutes without ever exceeding it.
    DRAIN_TIMEOUT = 5.0
    DRAIN_BUDGET = 15.0

    # StreamRequestHandler.setup() applies this to the connection socket.
    # Without it, rfile reads block forever, and because this is a threading
    # server a peer that declares a body it never sends pins one coordinator
    # thread per socket until the process dies — one leaked thread per open
    # connection is a trivially cheap way to exhaust the mesh.
    #
    # This is a *per read* timeout, not a budget for the whole request: it
    # bounds how long we wait for the next bytes to show up, so a legitimate
    # 10 MB job payload trickling in over a slow LAN link never trips it as
    # long as the sender keeps sending. That is why it can be this generous
    # and why it must not be as tight as the claim port's 15 s — a claim is
    # under a kilobyte and arrives in one packet, a job payload is megabytes
    # and may cross a congested WiFi link, and cutting one of those off would
    # break real work. Sixty seconds of complete silence mid-body means the
    # peer is gone or lying either way.
    timeout = 60

    def _send(self, code: int, body=None):
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
            # Client disconnected (e.g. Windows WinError 10053 / 10054) before
            # we finished writing. Nothing we can do — just drop the response.
            pass

    def _drain(self, length: int):
        """Swallow a body we refused to read, so the peer can read our 413.

        Only for bodies that are merely too big, not absurd. Past
        ``MAX_DRAIN_BYTES`` — or past ``DRAIN_BUDGET`` seconds of trickling —
        the polite thing stops being the safe thing, and reading further is
        just free bandwidth burning on the attacker's terms.
        """
        if length > self.MAX_DRAIN_BYTES:
            self.close_connection = True
            return
        # A peer that declares bytes and then stalls should not get the full
        # request timeout out of us — it already told us the request is dead,
        # and this read exists only for its benefit.
        try:
            self.connection.settimeout(self.DRAIN_TIMEOUT)
        except (AttributeError, OSError):
            pass
        deadline = time.monotonic() + self.DRAIN_BUDGET
        remaining = length
        try:
            while remaining > 0:
                if time.monotonic() > deadline:
                    self.close_connection = True
                    break
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

    # Enough to hold any plausible chunked prologue, and small enough that a
    # peer cannot use this path to make us read indefinitely.
    SWEEP_BYTES = 64 * 1024
    SWEEP_TIMEOUT = 0.25

    def _sweep_unread(self):
        """Discard already-arrived body bytes so a close does not RST.

        Best effort by design: this exists only so the response we are about
        to send survives the close, never to receive anything. The timeout is
        short because the peer has already sent what it is going to send and
        is now waiting on us — waiting longer would delay the error without
        making it more likely to arrive.
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

    def _read_json(self):
        # No chunked bodies. BaseHTTPRequestHandler does not decode chunked
        # transfer encoding, so accepting one would mean either handing the
        # raw framing bytes to json.loads or — worse, since a chunked request
        # carries no Content-Length — silently reading it as an empty body and
        # letting it through to a route with every field defaulted. The claim
        # server refuses these for the same reason; so does this one.
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            # Sweep whatever framing bytes already arrived before answering,
            # for the same reason the 413 path drains: closing a socket that
            # still has unread bytes in its receive buffer sends an RST on
            # Windows, and the RST can discard a response we already queued.
            # Without this the 411 is lost intermittently and the client sees
            # a bare connection reset instead of being told what is wrong —
            # the worst possible diagnostic for a fixable request.
            #
            # This cannot use _drain(): that takes a declared length, and a
            # chunked request has none by definition. Reading toward a guessed
            # length would block for DRAIN_TIMEOUT waiting for bytes the peer
            # already finished sending, putting a multi-second stall on every
            # rejection. Only what is already buffered is worth taking.
            self._sweep_unread()
            self._send(411, {"error": "chunked bodies are not accepted; "
                                      "send Content-Length"})
            return None

        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            # Deliberately NOT the claim server's 411. With Transfer-Encoding
            # already ruled out above, "no Content-Length" is unambiguous HTTP
            # for "no body", and this endpoint has clients the claim port does
            # not: the token is checked from the X-Auth-Token header before we
            # ever get here, so an unauthenticated peer cannot reach this line
            # at all, and body-less POSTs from curl or a shell script are a
            # normal way to poke /api/kill or /api/cancel. Refusing them would
            # be an API break that buys no safety.
            return {}
        try:
            length = int(raw_length)
        except (ValueError, TypeError):
            self._send(400, {"error": "invalid Content-Length"})
            return None
        if length < 0:
            self._send(400, {"error": "invalid Content-Length"})
            return None
        if length == 0:
            return {}
        if length > self.MAX_CONTENT_LENGTH:
            # Refused on the declared length alone — nothing is allocated, and
            # _drain reads in bounded chunks it immediately discards.
            self._drain(length)
            self._send(413, {"error": "request too large"})
            return None
        try:
            raw = self.rfile.read(length)
        except OSError:
            # Peer promised `length` bytes and stalled past `timeout`. There
            # is nobody left to answer; drop the connection and the thread.
            self.close_connection = True
            return None
        if len(raw) != length:
            # A short read means the peer closed mid-body. Saying so beats
            # handing the fragment to json.loads, which would report a syntax
            # error for what is really a truncated transfer.
            self.close_connection = True
            self._send(400, {"error": "truncated request body"})
            return None
        return json.loads(raw.decode("utf-8"))

    def _refuse_early(self, code: int, body):
        """Answer a request whose body we never read, without losing the answer.

        Both early refusals in do_POST — the shutdown 503 and the 401 — are
        decided from headers alone, so the body is still sitting unread in the
        receive buffer. Closing a socket in that state sends an RST on
        Windows, and the RST discards the response we just queued: the caller
        gets a bare connection reset in place of the one message that would
        have told it what to do. A worker cannot distinguish "wrong token"
        from "network died" through a reset, so it retries a token that will
        never work; and a 503 that arrives as a reset is indistinguishable
        from the coordinator having crashed.

        Same reasoning and the same bounds as the 411 path, and conditional so
        an unauthenticated GET — which has no body to sweep — does not pay
        SWEEP_TIMEOUT waiting for bytes nobody is sending.
        """
        if (self.headers.get("Content-Length")
                or self.headers.get("Transfer-Encoding")):
            self._sweep_unread()
        self._send(code, body)

    def _authed(self) -> bool:
        ip = self.client_address[0]
        allowed, msg = self.gpumesh_security.verify_request(
            self.headers.get("X-Auth-Token", ""), ip
        )
        if allowed:
            return True
        self._refuse_early(401, {"error": msg})
        return False

    def log_message(self, fmt, *args):
        pass  # quiet; the CLI prints its own status lines

    # -- routes ---------------------------------------------------------

    def do_GET(self):
        if self._shutdown_event is not None and self._shutdown_event.is_set():
            self._send(503, {"error": "server shutting down"})
            return
        if not self._authed():
            return
        try:
            if self.path == "/api/workers":
                self._send(200, {"workers": self.db.list_workers()})
            elif self.path == "/api/events":
                self._send(200, {"events": self.db.list_events()})
            elif self.path == "/api/devices":
                self._send(200, self.db.device_summary())
            elif self.path == "/api/health":
                summary = self.db.device_summary()
                counts = self.db.job_status_counts()
                self._send(200, {
                    "status": "ok",
                    "version": __version__,
                    # An operator debugging "my worker will not join" needs the
                    # window without first having a worker that can join, so it
                    # is reported here rather than only in a refusal.
                    "protocol_version": PROTOCOL_VERSION,
                    "min_protocol_version": MIN_PROTOCOL_VERSION,
                    "uptime_seconds": round(time.monotonic() - self.start_time, 2),
                    "workers_alive": summary["alive_devices"],
                    "workers_dead": summary["total_devices"] - summary["alive_devices"],
                    "jobs_pending": counts.get("pending", 0),
                    "jobs_running": counts.get("running", 0),
                    "jobs_done": counts.get("done", 0),
                    "jobs_failed": counts.get("failed", 0),
                    "total_score": summary["total_score"],
                })
            elif self.path.startswith("/api/jobs/"):
                job = self.db.job_status(self.path.rsplit("/", 1)[-1])
                if job is None:
                    self._send(404, {"error": "no such job"})
                else:
                    self._send(200, job)
            else:
                self._send(404, {"error": "unknown endpoint"})
        except Exception as exc:
            import traceback
            status.log(f"{bold(cyan('[mesh]'))} {red('ERROR GET')} {self.path}: {exc}")
            traceback.print_exc()
            self._send(500, {"error": "internal server error"})

    def do_POST(self):
        if (self._shutdown_event is not None
                and self._shutdown_event.is_set()
                and self.path not in self._SHUTDOWN_EXEMPT_POST_PATHS):
            self._refuse_early(503, {"error": "server shutting down"})
            return
        if not self._authed():
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
            return
        if not isinstance(body, dict):
            self._send(400, {"error": "JSON body must be an object"})
            return

        try:
            if self.path == "/api/register":
                # Version negotiation happens FIRST, before anything about this
                # worker is written down. A worker we are going to refuse must
                # not leave a row behind, must not fire a "worker joined" event,
                # and must not appear in /api/workers — an operator debugging a
                # skew should see the refusal, not a ghost worker that never
                # leases anything.
                raw_proto = body.get("protocol_version")
                if raw_proto is None:
                    # Absent means "sent by a gpumesh from before the handshake
                    # existed", which is a specific, known version — not a
                    # reason to refuse. See UNVERSIONED_PROTOCOL in __init__.py.
                    worker_proto = UNVERSIONED_PROTOCOL
                else:
                    try:
                        worker_proto = int(raw_proto)
                    except (ValueError, TypeError):
                        self._send(400, {
                            "error": "protocol_version must be an integer",
                        })
                        return
                if not is_supported_protocol(worker_proto):
                    message = _protocol_refusal(worker_proto)
                    status.log(f"{bold(cyan('[mesh]'))} worker {red('refused')}: "
                               f"{body.get('hostname')} speaks protocol "
                               f"{yellow(str(worker_proto))}, this coordinator "
                               f"accepts {MIN_PROTOCOL_VERSION}-{PROTOCOL_VERSION}")
                    self._send(PROTOCOL_MISMATCH_STATUS, {
                        "error": message,
                        # The same numbers as structured fields, so a client
                        # that wants to branch on them does not have to parse
                        # the prose. The prose is what a human reads; these are
                        # what a program reads. Both are part of the contract.
                        "protocol_version": PROTOCOL_VERSION,
                        "min_protocol_version": MIN_PROTOCOL_VERSION,
                        "worker_protocol_version": worker_proto,
                    })
                    return
                try:
                    score = float(body.get("score", 0.0))
                except (ValueError, TypeError):
                    self._send(400, {"error": "score must be a number"})
                    return
                # Workers post their whole capability probe, which already
                # carries the capacity numbers @accelerate(cores=, memory=)
                # is checked against. Anything missing or unparseable stays 0,
                # which simply means "cannot satisfy a requirement".
                try:
                    cpu_cores = int(body.get("cpu_cores") or body.get("cpu_count") or 0)
                except (ValueError, TypeError):
                    cpu_cores = 0
                try:
                    gpu_total_mb = float(body.get("gpu_memory_total_mb") or 0.0)
                except (ValueError, TypeError):
                    gpu_total_mb = 0.0
                # Free VRAM is forwarded raw rather than coerced to 0.0 like
                # the two above, because for this field zero and unknown are
                # different answers: the coordinator reports null when nobody
                # has measured a worker yet, and both @accelerate's validation
                # and the scheduler rely on telling those apart — a measured
                # zero disqualifies a card, an unmeasured one falls back to its
                # total. register_worker drops anything unusable itself.
                #
                # Forwarding it at all is what shrinks the unknown window to
                # nothing. The worker's capability probe already measures free
                # VRAM and sends it in this very body; without this line it was
                # discarded and the worker looked unmeasured until its first
                # heartbeat landed a moment later.
                worker_id = self.db.register_worker(
                    body.get("hostname", "unknown"),
                    body.get("device", "cpu"),
                    score,
                    body.get("device_name", ""),
                    cpu_cores=cpu_cores,
                    gpu_memory_total_mb=gpu_total_mb,
                    gpu_memory_free_mb=body.get("gpu_memory_free_mb"),
                )
                self.db.record_event("worker_joined", worker_id)
                status.log(f"{bold(cyan('[mesh]'))} worker {green('joined')}: {body.get('hostname')} "
                      f"({body.get('device_name') or body.get('device')}, "
                      f"score={body.get('score')})")
                # The coordinator reports its own version on every successful
                # registration. This is the other half of the negotiation: a
                # NEWER worker joining an OLDER coordinator is refused by
                # nobody here (the old coordinator has no gate at all), so the
                # worker has to be able to check for itself — and an old
                # coordinator answering without this key is exactly the absent
                # case, which the worker maps to UNVERSIONED_PROTOCOL.
                self._send(200, {
                    "worker_id": worker_id,
                    "protocol_version": PROTOCOL_VERSION,
                    "min_protocol_version": MIN_PROTOCOL_VERSION,
                })

            elif self.path == "/api/heartbeat":
                score = body.get("score")
                ok = self.db.heartbeat(
                    body.get("worker_id", ""), body.get("task_id"),
                    score=score if score is not None else None,
                    gpu_memory_free_mb=body.get("gpu_memory_free_mb"),
                )
                self._send(200 if ok else 404, {"ok": ok})

            elif self.path == "/api/lease":
                task = self.db.lease_task(body.get("worker_id", ""))
                if task is None:
                    self._send(204)
                else:
                    self._send(200, task)

            elif self.path == "/api/result":
                # Every value forwarded from here is validated or coerced
                # BEFORE it reaches complete_task, because complete_task does
                # its work in one transaction: anything that raises in there —
                # a TypeError comparing a string to 0, an InterfaceError
                # binding a dict to a TEXT column — takes the UPDATE that
                # records the outcome down with it. The task goes back to
                # looking like it is still running, and a finished computation
                # is thrown away and re-run. A bad field must cost the field,
                # never the result.
                result_ok = body.get("ok")
                if type(result_ok) is not bool:
                    self._send(400, {"error": "'ok' must be true or false"})
                    return
                task_id = body.get("task_id", "")
                worker_id = body.get("worker_id", "")
                if not isinstance(task_id, str) or not isinstance(worker_id, str):
                    # These are bound into WHERE clauses. A non-string is not
                    # something we can degrade gracefully — it identifies no
                    # task — so it is refused here rather than turned into a
                    # 500 by sqlite three frames down.
                    self._send(400, {
                        "error": "'task_id' and 'worker_id' must be strings",
                    })
                    return
                # 'result' needs no guard: it came out of json.loads, so it is
                # by construction something json.dumps can put back.
                # 'diagnostics' is accepted and dropped — complete_task has
                # nowhere to store it. Left as-is on purpose: silently
                # ignoring a field is a documentation problem, while inventing
                # a column for it is somebody else's schema change.
                ok = self.db.complete_task(
                    task_id,
                    worker_id,
                    result_ok,
                    result=body.get("result"),
                    error=_coerce_text(body.get("error", "")),
                    elapsed=_coerce_elapsed(body.get("elapsed")),
                    user_error=bool(body.get("user_error", False)),
                )
                self._send(200 if ok else 409, {"ok": ok})

            elif self.path == "/api/jobs":
                payloads = body.get("payloads") or []
                script = body.get("script", "")
                if not script or not script.strip() or not isinstance(payloads, list) or not payloads:
                    self._send(400, {"error": "need script and non-empty payloads list"})
                    return
                if self.safe_mode and "__gpumesh_function__" in script:
                    self._send(403, {"error": "Safe mode: function distribution disabled. Submit scripts only."})
                    return
                # A placement hint of an impossible type is rejected here, at
                # submission, rather than left for the scheduler to survive.
                # The scheduler does survive it — but the submitter is the
                # only party who can fix it, and this is the last moment they
                # are still on the phone. Accepted, the same typo costs them a
                # minute of UNSATISFIABLE_AFTER and then an error attached to
                # a task they have to go look up; refused, it is a 400 in the
                # same call, with the bad value quoted back.
                #
                # create_job() owns the actual rule (it is the one path every
                # caller, HTTP or in-process, goes through) and signals with
                # ValueError. Translating it to 400 rather than letting the
                # handler's blanket except turn it into a 500 is the whole
                # difference between "you sent something wrong" and "the
                # coordinator is broken".
                try:
                    job_id = self.db.create_job(
                        body.get("name", "job"), script, payloads
                    )
                except ValueError as exc:
                    self._send(400, {"error": str(exc)})
                    return
                job_type = "function" if script == "__gpumesh_function__" else "script"
                status.log(f"{bold(cyan('[mesh]'))} {job_type} job submitted: {body.get('name', 'job')} "
                      f"({len(payloads)} tasks) -> {job_id}")
                self._send(200, {"job_id": job_id})

            elif self.path == "/api/cancel":
                job_id = body.get("job_id", "")
                if not job_id:
                    self._send(400, {"error": "need job_id"})
                    return
                result = self.db.cancel_job(job_id)
                if result is None:
                    self._send(404, {"error": "job not found"})
                else:
                    self._send(200, result)

            elif self.path == "/api/retry":
                job_id = body.get("job_id", "")
                if not job_id:
                    self._send(400, {"error": "need job_id"})
                    return
                result = self.db.retry_job(job_id)
                if result is None:
                    self._send(404, {"error": "job not found"})
                else:
                    self._send(200, result)

            elif self.path == "/api/kill":
                force = body.get("force")
                if type(force) is not bool:
                    self._send(400, {"error": "'force' must be true or false"})
                    return
                result = self.db.cancel_all_tasks(force=force)
                self._send(200, result)

            else:
                self._send(404, {"error": "unknown endpoint"})
        except Exception as exc:
            import traceback
            status.log(f"{bold(cyan('[mesh]'))} {red('ERROR POST')} {self.path}: {exc}")
            traceback.print_exc()
            self._send(500, {"error": "internal server error"})


def _reap_once(db: Database):
    """One pass of the maintenance loop. May raise; _reaper contains that."""
    requeued = db.reap_expired_leases()
    if requeued:
        status.log(f"{bold(cyan('[mesh]'))} re-queued {yellow(str(requeued))} task(s) from {red('dead')} workers")

    # Tasks whose placement hints no live worker can ever meet would
    # otherwise sit pending until the client's own timeout fired, which
    # names neither the task nor the reason. Fail them here, where both
    # the queue and the live roster are visible.
    for task in db.fail_unsatisfiable_tasks():
        status.log(f"{bold(cyan('[mesh]'))} task {bold(task['task_id'])} "
                   f"{red('unsatisfiable')} (job {task['job_id']}): {task['error']}")

    # Hivemind TTL pattern: DELETE workers stale for >5 minutes
    deleted = db.cleanup_dead_workers()
    for wid in deleted:
        db.record_event("worker_left", wid)
        status.log(f"{bold(cyan('[mesh]'))} worker {yellow('expired')} and removed: {wid}")

    # Exo-style topology change notification
    alive = db.list_workers()
    alive_count = sum(1 for w in alive if w["alive"])
    if deleted:
        status.log(f"{bold(cyan('[mesh]'))} topology changed: {bold(str(alive_count))} workers alive")


def _reaper(db: Database, stop: threading.Event):
    """Run the maintenance pass forever, surviving anything it raises.

    The broad except is the point of this function. This is a daemon thread
    with no supervisor and no exit report: an exception escaping the loop
    kills it silently, the coordinator keeps answering HTTP as if nothing
    happened, and from then on nothing re-queues a dead worker's lease,
    nothing evicts a stale worker row, and nothing ever diagnoses an
    unplaceable task — for the entire remaining life of the process, with
    a restart as the only cure and no message saying so.

    It has already happened once: a task payload of ``{"gpu": 123}`` raised
    AttributeError out of fail_unsatisfiable_tasks(), through this loop, and
    the thread was gone. Every callee here reads rows written by somebody
    else, so "the callees are careful now" is a statement about today's code,
    while a loop that cannot die is a property of the loop. Both are worth
    having; only this one keeps holding after the next edit.

    Swallowing is right for the same reason: none of this work is
    transactional across passes, so the next pass in five seconds starts
    clean and will very likely succeed. The one thing that must not be
    swallowed is the wait — it stays outside the try so a permanently failing
    pass logs at REAP_INTERVAL rather than spinning a core at full speed.
    """
    while not stop.is_set():
        try:
            _reap_once(db)
        except Exception as exc:
            import traceback
            status.log(f"{bold(cyan('[mesh]'))} {red('reaper pass failed')}: "
                       f"{exc} (retrying in {REAP_INTERVAL:.0f}s)")
            traceback.print_exc()
        stop.wait(REAP_INTERVAL)


def _discovery_printer(listener, stop: threading.Event):
    """Print newly discovered workers in the background.

    Uses (hostname, ip) as dedup key so that DHCP IP rotations for the
    same machine are still reported.  Periodically cleans stale peers.
    """
    seen: set[tuple[str, str]] = set()
    cleanup_counter = 0
    while not stop.is_set():
        try:
            listener.cleanup_stale()
            # Periodically clean the seen set to allow re-printing of peers
            cleanup_counter += 1
            if cleanup_counter >= 30:  # Every ~60 seconds
                cleanup_counter = 0
                alive_peers = {(p.hostname, p.ip) for p in listener.peers()}
                seen = seen & alive_peers  # Keep only still-alive entries
            for peer in listener.peers():
                key = (peer.hostname, peer.ip)
                if key not in seen:
                    seen.add(key)
                    status.log(f"{bold(cyan('[mesh]'))} nearby worker: {green(peer.hostname)} "
                          f"({peer.display_name}, score={peer.score:.1f}) "
                          f"at {peer.ip}")
        except Exception:
            pass  # discovery printer is best-effort
        stop.wait(2.0)


def serve(host: str, port: int, db_path: str, token: str,
          discovery: bool = False, safe_mode: bool = False) -> ThreadingHTTPServer:
    handler = type("Handler", (CoordinatorHandler,),
                   {"db": Database(db_path), "token": token,
                    "safe_mode": safe_mode,
                    "gpumesh_security": SecurityManager(token)})
    handler.start_time = time.monotonic()
    handler._shutdown_event = threading.Event()
    # Per-coordinator, not shared with the class default — see _InFlight.
    handler._inflight = _InFlight()
    httpd = _FastBindHTTPServer((host, port), handler)
    stop = threading.Event()
    # Named so that a test — or an operator with a stack dump — can tell
    # whether the reaper is still alive without reaching into Thread._target.
    # Its death used to be undetectable from outside, which is half of why it
    # went unnoticed.
    t = threading.Thread(target=_reaper, args=(handler.db, stop), daemon=True,
                         name="gpumesh-reaper")
    t.start()
    httpd.gpumesh_stop = stop
    # Published for the same reason it is named: this thread dying is the
    # coordinator quietly losing lease reaping, dead-worker cleanup and
    # unplaceable-task diagnosis, with nothing in the API to show for it.
    # A handle makes "is the reaper still alive?" an answerable question.
    httpd.gpumesh_reaper = t

    # Optional: start UDP discovery listener
    listener = None
    if discovery:
        try:
            from .discovery import Listener
            listener = Listener()
            listener.start()
            dt = threading.Thread(target=_discovery_printer,
                                  args=(listener, stop), daemon=True,
                                  name="gpumesh-discovery")
            dt.start()
            httpd.gpumesh_discovery_listener = listener
        except Exception as exc:
            status.log(f"{bold(cyan('[mesh]'))} {red('discovery listener failed to start')}: {exc}")

    # Ensure listener is stopped on shutdown.
    # Idempotent shutdown guard (python-zeroconf pattern):
    # prevents double-shutdown if called from multiple threads or
    # if serve_forever() already triggered cleanup.
    _orig_shutdown = httpd.shutdown
    _shutdown_done = threading.Event()

    # Whether the accept loop is running. BaseServer.shutdown() waits on an
    # event that only serve_forever() sets, so calling it before the loop
    # starts blocks forever — but skipping it when the loop IS running leaves
    # the coordinator serving after shutdown() returned. Both cases need the
    # real answer, and the server object does not expose one, so track it.
    _serve_lock = threading.Lock()
    _serving = False
    _stop_requested = False

    def _shutdown_with_listener():
        if _shutdown_done.is_set():
            return  # idempotent — already shut down
        _shutdown_done.set()

        stop.set()
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass
        # Signal shutdown — new requests will get 503
        handler._shutdown_event.set()
        nonlocal _stop_requested
        with _serve_lock:
            _stop_requested = True   # stops a loop that has not started yet
            serving = _serving
        if serving:
            _orig_shutdown()
        # shutdown() only stops the accept loop — it leaves the listening
        # socket bound. A coordinator that has "shut down" but still owns the
        # port is not shut down: restarting on the same port fails with
        # EADDRINUSE on Linux, and even where SO_REUSEADDR papers over it
        # (Windows) the old socket lingers as a leaked descriptor.
        # server_close() is idempotent, so this is safe on either path.
        httpd.server_close()
        # ONLY NOW is the database safe to close.
        #
        # It used to be closed before the accept loop was stopped, with a
        # 1s sleep standing in for "the handlers have drained". Two things
        # went wrong. A handler that had already passed the 503 gate and took
        # longer than that second — a 10 MB /api/jobs body, a slow lease_task —
        # ran straight into "sqlite3.ProgrammingError: Cannot operate on a
        # closed database", which the broad `except Exception` above turned
        # into a 500. And every request arriving during that second was
        # answered 503, including /api/result: work a worker had already
        # finished was thrown away at the door.
        #
        # Stopping the accept loop first collapses that window to the few
        # microseconds between setting the 503 flag and shutdown() returning,
        # and after it no new request can be admitted at all — so the close
        # races only with a shrinking set of handlers that were already
        # in flight. server_close() does not block on those: ThreadingHTTPServer
        # sets daemon_threads = True, which makes its _threads.join() a no-op,
        # so the drain here is what gives them a chance to finish.
        #
        # It used to be a flat half-second sleep, which is not a drain — it is
        # a hope. Any handler slower than it (a 10 MB /api/jobs body still
        # arriving, a lease_task behind a busy lock) met a closed database and
        # answered 500. That matters most for the one route now allowed
        # through the 503 gate: an /api/result admitted at the door and then
        # 500'd at the database has lost the same work the gate change exists
        # to save. So wait for the actual count instead, bounded, and say so
        # if the bound is what ended the wait.
        if not handler._inflight.wait_until_idle(SHUTDOWN_DRAIN_SECONDS):
            # Built outside the f-string: a multi-line expression inside
            # braces is a SyntaxError before Python 3.12, and this package
            # still supports 3.9.
            warning = yellow("requests still in flight")
            status.log(f"{bold(cyan('[mesh]'))} shutting down with {warning} "
                       f"after {SHUTDOWN_DRAIN_SECONDS:.0f}s — closing the "
                       f"database anyway")
        try:
            handler.db.close()
        except Exception:
            pass

    httpd.shutdown = _shutdown_with_listener

    # On Windows, calling shutdown() while serve_forever() is blocked in
    # select() can close the listening socket mid-select and raise
    # OSError [WinError 10038]. serve_forever's finally-block still sets the
    # shutdown flag, so swallowing the error here keeps Ctrl+C clean.
    _orig_serve_forever = httpd.serve_forever

    def _safe_serve_forever(*args, **kwargs):
        nonlocal _serving
        with _serve_lock:
            if _stop_requested:
                return  # shutdown() ran before this thread got going
            _serving = True
        try:
            _orig_serve_forever(*args, **kwargs)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 10038:
                pass
            else:
                raise
        finally:
            with _serve_lock:
                _serving = False

    httpd.serve_forever = _safe_serve_forever

    return httpd
