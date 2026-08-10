"""Worker-side claim server: lightweight HTTP endpoint that lets a
coordinator connect to this worker by presenting the worker's token.

Endpoint:
    POST /api/claim   {"token": "...", "coordinator_url": "...", "coordinator_token": "..."}

If the token matches, the worker starts a background thread that joins
the coordinator mesh via ``gpumesh.worker.run_worker``.
"""

import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gpumesh.ansi import safe_print, green, yellow, red, cyan, bold


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
    server_version = "gpumesh-claim"
    _claimed = False          # class-level flag — prevents double-claim
    _claim_lock = threading.Lock()
    _token: str = ""          # set by start_claim_server
    _worker_thread: threading.Thread | None = None

    # -- plumbing -------------------------------------------------------

    MAX_CONTENT_LENGTH = 1 * 1024 * 1024  # 1 MB

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
            pass

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            self._send(400, {"error": "invalid Content-Length"})
            return None
        if length <= 0:
            self._send(400, {"error": "empty request body"})
            return None
        if length > self.MAX_CONTENT_LENGTH:
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
            self._send(413, {"error": "request too large"})
            return None
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

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
            return
        if not isinstance(body, dict):
            self._send(400, {"error": "JSON body must be an object"})
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


def start_claim_server(token: str, port: int = 0) -> tuple[ThreadingHTTPServer, int]:
    """Start the claim server and return ``(httpd, actual_port)``.

    Binds to ``0.0.0.0`` so it is reachable from the network.
    If *port* is 0 the OS picks an ephemeral port.
    """
    ClaimHandler._token = token
    with ClaimHandler._claim_lock:
        ClaimHandler._claimed = False

    httpd = ThreadingHTTPServer(("0.0.0.0", port), ClaimHandler)
    actual_port = httpd.server_address[1]

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
