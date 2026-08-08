"""Tests for worker resilience: surviving coordinator outage and auto-reconnect.

These tests verify the Phase 1 reliability rework:
  1. Workers no longer have permanent-exit thresholds (the old health-check
     3-strike exit and the 120s coordinator-timeout exit are gone).
  2. Workers survive a coordinator outage (laptop sleep / WiFi drop /
     coordinator restart) and automatically re-register when it returns.
  3. Workers can run tasks again after reconnecting.

The suite is kept fast: since the permanent-exit thresholds no longer
exist, a ~15s outage is enough to prove the worker does not die.
"""

import threading
import time

import pytest

from gpumesh.server import serve
from gpumesh.worker import MeshClient, run_worker

TOKEN = "resilience-test-token"

SCRIPT = """
import json, sys
payload = json.load(sys.stdin)
print(json.dumps({"result": payload["x"] * 2}))
"""


@pytest.fixture
def coordinator(tmp_path):
    """Start a real loopback coordinator; yields a holder with .start()."""
    db_path = str(tmp_path / "resilience.db")
    holder = {"db_path": db_path, "port": None, "httpd": None}

    def start():
        httpd = serve("127.0.0.1", 0, db_path, TOKEN)
        holder["port"] = httpd.server_address[1]
        holder["httpd"] = httpd
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd

    holder["httpd"] = start()
    yield holder
    try:
        holder["httpd"].gpumesh_stop.set()
        holder["httpd"].shutdown()
    except Exception:
        pass


def _wait_for_worker(client, timeout=20.0):
    """Poll /api/workers until at least one alive worker appears."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = client.call("GET", "/api/workers")
            alive = [w for w in resp.get("workers", []) if w.get("alive")]
            if alive:
                return alive
        except Exception:
            pass
        time.sleep(0.2)
    return []


def _start_worker(url, state):
    def worker_main():
        try:
            run_worker(url, TOKEN, persist_connection=False)
            state["exited"] = "returned"
        except Exception as exc:
            state["exited"] = f"raised: {exc}"

    thread = threading.Thread(target=worker_main, daemon=True)
    thread.start()
    return thread


class TestWorkerSurvivesOutage:
    """Workers must never exit on their own due to a coordinator outage."""

    def test_worker_survives_outage_and_runs_tasks_after_reconnect(self, tmp_path, coordinator):
        """Full story: register, survive an outage, reconnect, run a task."""
        holder = coordinator
        url = f"http://127.0.0.1:{holder['port']}"
        client = MeshClient(url, TOKEN)

        state = {}
        worker_thread = _start_worker(url, state)
        assert _wait_for_worker(client), "worker never registered"

        # ── Coordinator goes away (network drop / laptop sleep) ──
        holder["httpd"].gpumesh_stop.set()
        holder["httpd"].shutdown()
        time.sleep(15)  # longer than the old 3x15s health-check exit window

        # The worker must STILL be alive and waiting.
        assert worker_thread.is_alive(), (
            f"worker permanently died during the outage: {state.get('exited')}"
        )

        # ── Coordinator comes back on the same port ──
        httpd2 = serve("127.0.0.1", holder["port"], holder["db_path"], TOKEN)
        holder["httpd"] = httpd2
        threading.Thread(target=httpd2.serve_forever, daemon=True).start()
        client2 = MeshClient(url, TOKEN)

        # Worker re-registers and can run a real task.
        assert _wait_for_worker(client2), "worker did not re-register after reconnect"
        job = client2.call("POST", "/api/jobs", {
            "name": "reconnect-task",
            "script": SCRIPT,
            "payloads": [{"x": 21, "cost": 1}],
        })
        job_id = job["job_id"]

        deadline = time.time() + 30
        status = None
        while time.time() < deadline:
            status = client2.call("GET", f"/api/jobs/{job_id}")
            if status.get("finished"):
                break
            time.sleep(1)
        assert status and status["finished"], f"job did not finish: {status}"
        tasks = status.get("tasks", [])
        assert tasks and tasks[0]["status"] == "done", f"task failed: {tasks}"
        assert tasks[0]["result"]["result"] == 42

    def test_worker_gets_new_id_after_reconnect(self, coordinator):
        """A fresh coordinator DB means the worker re-registers with a new id."""
        holder = coordinator
        url = f"http://127.0.0.1:{holder['port']}"
        client = MeshClient(url, TOKEN)

        state = {}
        worker_thread = _start_worker(url, state)
        workers = _wait_for_worker(client)
        assert workers, "worker never registered"
        initial_id = workers[0]["id"]

        # Wipe the coordinator (fresh DB) on the same port.
        holder["httpd"].gpumesh_stop.set()
        holder["httpd"].shutdown()

        db_path2 = str(holder["db_path"]) + ".2"
        httpd2 = serve("127.0.0.1", holder["port"], db_path2, TOKEN)
        holder["httpd"] = httpd2
        threading.Thread(target=httpd2.serve_forever, daemon=True).start()
        client2 = MeshClient(url, TOKEN)

        workers = _wait_for_worker(client2, timeout=30)
        assert workers, "worker did not re-register after coordinator restart"
        assert workers[0]["id"] != initial_id, (
            "worker should re-register with a new id after the DB was wiped"
        )
        assert worker_thread.is_alive()

    def test_worker_survives_repeated_outages(self, tmp_path, coordinator):
        """Two consecutive outages — the worker keeps coming back."""
        holder = coordinator
        url = f"http://127.0.0.1:{holder['port']}"
        client = MeshClient(url, TOKEN)

        state = {}
        worker_thread = _start_worker(url, state)
        assert _wait_for_worker(client), "worker never registered"

        for round_no in range(2):
            # outage
            holder["httpd"].gpumesh_stop.set()
            holder["httpd"].shutdown()
            time.sleep(8)
            assert worker_thread.is_alive(), (
                f"worker died in outage round {round_no}: {state.get('exited')}"
            )
            # recovery on the same port
            httpd2 = serve("127.0.0.1", holder["port"], holder["db_path"], TOKEN)
            holder["httpd"] = httpd2
            threading.Thread(target=httpd2.serve_forever, daemon=True).start()
            assert _wait_for_worker(MeshClient(url, TOKEN)), (
                f"worker did not reconnect in round {round_no}"
            )
            time.sleep(1)
