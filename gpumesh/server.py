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

REAP_INTERVAL = 5.0


class CoordinatorHandler(BaseHTTPRequestHandler):
    server_version = "gpumesh"
    db: Database = None
    token: str = ""

    # -- plumbing -------------------------------------------------------

    def _send(self, code: int, body=None):
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def _authed(self) -> bool:
        if self.headers.get("X-Auth-Token") == self.token:
            return True
        self._send(401, {"error": "bad or missing token"})
        return False

    def log_message(self, fmt, *args):
        pass  # quiet; the CLI prints its own status lines

    # -- routes ---------------------------------------------------------

    def do_GET(self):
        if not self._authed():
            return
        if self.path == "/api/workers":
            self._send(200, {"workers": self.db.list_workers()})
        elif self.path.startswith("/api/jobs/"):
            job = self.db.job_status(self.path.rsplit("/", 1)[-1])
            if job is None:
                self._send(404, {"error": "no such job"})
            else:
                self._send(200, job)
        else:
            self._send(404, {"error": "unknown endpoint"})

    def do_POST(self):
        if not self._authed():
            return
        try:
            body = self._read_json()
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid JSON"})
            return

        if self.path == "/api/register":
            worker_id = self.db.register_worker(
                body.get("hostname", "unknown"),
                body.get("device", "cpu"),
                float(body.get("score", 0.0)),
                body.get("device_name", ""),
            )
            print(f"[mesh] worker joined: {body.get('hostname')} "
                  f"({body.get('device_name') or body.get('device')}, "
                  f"score={body.get('score')})")
            self._send(200, {"worker_id": worker_id})

        elif self.path == "/api/heartbeat":
            ok = self.db.heartbeat(body.get("worker_id", ""))
            self._send(200 if ok else 404, {"ok": ok})

        elif self.path == "/api/lease":
            task = self.db.lease_task(body.get("worker_id", ""))
            if task is None:
                self._send(204)
            else:
                self._send(200, task)

        elif self.path == "/api/result":
            ok = self.db.complete_task(
                body.get("task_id", ""),
                body.get("worker_id", ""),
                bool(body.get("ok")),
                result=body.get("result"),
                error=body.get("error", ""),
            )
            self._send(200 if ok else 409, {"ok": ok})

        elif self.path == "/api/jobs":
            payloads = body.get("payloads") or []
            script = body.get("script", "")
            if not script or not isinstance(payloads, list) or not payloads:
                self._send(400, {"error": "need script and non-empty payloads list"})
                return
            job_id = self.db.create_job(
                body.get("name", "job"), script, payloads
            )
            job_type = "function" if script == "__gpumesh_function__" else "script"
            print(f"[mesh] {job_type} job submitted: {body.get('name', 'job')} "
                  f"({len(payloads)} tasks) -> {job_id}")
            self._send(200, {"job_id": job_id})

        else:
            self._send(404, {"error": "unknown endpoint"})


def _reaper(db: Database, stop: threading.Event):
    while not stop.is_set():
        requeued = db.reap_expired_leases()
        if requeued:
            print(f"[mesh] re-queued {requeued} task(s) from dead workers")
        stop.wait(REAP_INTERVAL)


def serve(host: str, port: int, db_path: str, token: str) -> ThreadingHTTPServer:
    handler = type("Handler", (CoordinatorHandler,),
                   {"db": Database(db_path), "token": token})
    httpd = ThreadingHTTPServer((host, port), handler)
    stop = threading.Event()
    t = threading.Thread(target=_reaper, args=(handler.db, stop), daemon=True)
    t.start()
    httpd.gpumesh_stop = stop
    return httpd
