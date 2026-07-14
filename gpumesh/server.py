"""Coordinator: threaded HTTP server exposing a JSON API.

Endpoints (all require the X-Auth-Token header):
  POST /api/register            {hostname, device, score, ...} -> {worker_id}
  POST /api/heartbeat           {worker_id}
  POST /api/lease               {worker_id} -> task | 204
  POST /api/result              {task_id, worker_id, ok, result?, error?}
  POST /api/jobs                {name, script, payloads} -> {job_id}
  GET  /api/jobs/<id>           -> job status + task results
  GET  /api/workers             -> live worker list
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .db import Database
from .security import SecurityManager

REAP_INTERVAL = 5.0


class CoordinatorHandler(BaseHTTPRequestHandler):
    server_version = "gpumesh"
    db: Database = None
    token: str = ""

    # -- plumbing -------------------------------------------------------

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

    MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10 MB

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            self._send(400, {"error": "invalid Content-Length"})
            return None
        if length < 0:
            self._send(400, {"error": "invalid Content-Length"})
            return None
        if length == 0:
            return {}
        if length > self.MAX_CONTENT_LENGTH:
            # Drain the oversized body so the TCP connection stays clean
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

    def _authed(self) -> bool:
        ip = self.client_address[0]
        allowed, msg = self.gpumesh_security.verify_request(
            self.headers.get("X-Auth-Token", ""), ip
        )
        if allowed:
            return True
        self._send(401, {"error": msg})
        return False

    def log_message(self, fmt, *args):
        pass  # quiet; the CLI prints its own status lines

    # -- routes ---------------------------------------------------------

    def do_GET(self):
        if not self._authed():
            return
        try:
            if self.path == "/api/workers":
                self._send(200, {"workers": self.db.list_workers()})
            elif self.path == "/api/devices":
                self._send(200, self.db.device_summary())
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
            print(f"[mesh] ERROR GET {self.path}: {exc}")
            traceback.print_exc()
            self._send(500, {"error": "internal server error"})

    def do_POST(self):
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
                try:
                    score = float(body.get("score", 0.0))
                except (ValueError, TypeError):
                    self._send(400, {"error": "score must be a number"})
                    return
                worker_id = self.db.register_worker(
                    body.get("hostname", "unknown"),
                    body.get("device", "cpu"),
                    score,
                    body.get("device_name", ""),
                )
                print(f"[mesh] worker joined: {body.get('hostname')} "
                      f"({body.get('device_name') or body.get('device')}, "
                      f"score={body.get('score')})")
                self._send(200, {"worker_id": worker_id})

            elif self.path == "/api/heartbeat":
                ok = self.db.heartbeat(
                    body.get("worker_id", ""), body.get("task_id")
                )
                self._send(200 if ok else 404, {"ok": ok})

            elif self.path == "/api/lease":
                task = self.db.lease_task(body.get("worker_id", ""))
                if task is None:
                    self._send(204)
                else:
                    self._send(200, task)

            elif self.path == "/api/result":
                result_ok = body.get("ok")
                if type(result_ok) is not bool:
                    self._send(400, {"error": "'ok' must be true or false"})
                    return
                ok = self.db.complete_task(
                    body.get("task_id", ""),
                    body.get("worker_id", ""),
                    result_ok,
                    result=body.get("result"),
                    error=body.get("error", ""),
                )
                self._send(200 if ok else 409, {"ok": ok})

            elif self.path == "/api/jobs":
                payloads = body.get("payloads") or []
                script = body.get("script", "")
                if not script or not script.strip() or not isinstance(payloads, list) or not payloads:
                    self._send(400, {"error": "need script and non-empty payloads list"})
                    return
                job_id = self.db.create_job(
                    body.get("name", "job"), script, payloads
                )
                job_type = "function" if script == "__gpumesh_function__" else "script"
                print(f"[mesh] {job_type} job submitted: {body.get('name', 'job')} "
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
            print(f"[mesh] ERROR POST {self.path}: {exc}")
            traceback.print_exc()
            self._send(500, {"error": "internal server error"})


def _reaper(db: Database, stop: threading.Event):
    while not stop.is_set():
        requeued = db.reap_expired_leases()
        if requeued:
            print(f"[mesh] re-queued {requeued} task(s) from dead workers")
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
                    print(f"[mesh] nearby worker: {peer.hostname} "
                          f"({peer.display_name}, score={peer.score:.1f}) "
                          f"at {peer.ip}")
        except Exception:
            pass  # discovery printer is best-effort
        stop.wait(2.0)


def serve(host: str, port: int, db_path: str, token: str,
          discovery: bool = False) -> ThreadingHTTPServer:
    handler = type("Handler", (CoordinatorHandler,),
                   {"db": Database(db_path), "token": token,
                    "gpumesh_security": SecurityManager(token)})
    httpd = ThreadingHTTPServer((host, port), handler)
    stop = threading.Event()
    t = threading.Thread(target=_reaper, args=(handler.db, stop), daemon=True)
    t.start()
    httpd.gpumesh_stop = stop

    # Optional: start UDP discovery listener
    listener = None
    if discovery:
        try:
            from .discovery import Listener
            listener = Listener()
            listener.start()
            dt = threading.Thread(target=_discovery_printer,
                                  args=(listener, stop), daemon=True)
            dt.start()
            httpd.gpumesh_discovery_listener = listener
        except Exception as exc:
            print(f"[mesh] discovery listener failed to start: {exc}")

    # Ensure listener is stopped on shutdown.
    # Idempotent shutdown guard (python-zeroconf pattern):
    # prevents double-shutdown if called from multiple threads or
    # if serve_forever() already triggered cleanup.
    _orig_shutdown = httpd.shutdown
    _shutdown_done = threading.Event()

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
        # Only call shutdown if serve_forever was started; otherwise
        # BaseServer.shutdown() hangs on an unset event
        if hasattr(httpd, "_BaseServer__serving") and httpd._BaseServer__serving:
            _orig_shutdown()
        else:
            httpd.server_close()
        # Brief pause to let in-flight handler threads finish their DB ops
        import time as _time
        _time.sleep(0.5)
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

    def _safe_serve_forever():
        try:
            _orig_serve_forever()
        except OSError as exc:
            if getattr(exc, "winerror", None) == 10038:
                pass
            else:
                raise

    httpd.serve_forever = _safe_serve_forever

    return httpd
