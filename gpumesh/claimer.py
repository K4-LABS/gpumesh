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

        coordinator_url = body.get("coordinator_url", "")
        coordinator_token = body.get("coordinator_token", "")
        if not coordinator_url or not coordinator_token:
            self._send(400, {"ok": False, "error": "need coordinator_url and coordinator_token"})
            return

        # Atomically check and set _claimed to prevent double-claim race
        with ClaimHandler._claim_lock:
            if ClaimHandler._claimed:
                self._send(409, {"ok": False, "error": "already claimed"})
                return
            ClaimHandler._claimed = True

        # Spawn background thread to join the coordinator mesh
        def _join():
            try:
                from .worker import run_worker
                run_worker(coordinator_url, coordinator_token)
            except Exception as exc:
                print(f"[claim] failed to join coordinator: {exc}")
                with ClaimHandler._claim_lock:
                    ClaimHandler._claimed = False  # allow retry on failure

        thread = threading.Thread(target=_join, daemon=True, name="gpumesh-claim-worker")
        ClaimHandler._worker_thread = thread
        thread.start()
        self._send(200, {"ok": True})


def start_claim_server(token: str, port: int = 0) -> tuple[ThreadingHTTPServer, int]:
    """Start the claim server and return ``(httpd, actual_port)``.

    Binds to ``0.0.0.0`` so it is reachable from the network.
    If *port* is 0 the OS picks an ephemeral port.
    """
    ClaimHandler._token = token
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
