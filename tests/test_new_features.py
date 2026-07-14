"""Comprehensive tests for all new features with 0% coverage.

Covers:
  - DB layer: list_devices, device_summary, cancel_worker_tasks, cancel_all_tasks
  - Server endpoints: GET /api/devices, POST /api/kill, POST /api/cancel
  - API layer: devices, device_count, total_score, auto_device
  - Client layer: list_devices
  - CLI: cmd_kill, cmd_devices
"""

import argparse
import json
import threading
import time
import urllib.error
from unittest.mock import patch, MagicMock

import pytest

from gpumesh.db import Database, WORKER_DEAD_AFTER
from gpumesh.server import serve
from gpumesh.worker import MeshClient

TOKEN = "test-token"


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


@pytest.fixture
def mesh(tmp_path):
    httpd = serve("127.0.0.1", 0, str(tmp_path / "mesh.db"), TOKEN)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield MeshClient(f"http://127.0.0.1:{port}", TOKEN)
    httpd.gpumesh_stop.set()
    httpd.shutdown()


# ══════════════════════════════════════════════════════════════════════════
# DB Layer Tests
# ══════════════════════════════════════════════════════════════════════════


class TestDBListDevices:
    """Tests for Database.list_devices()."""

    def test_list_devices_empty(self, db):
        """No workers returns empty list."""
        result = db.list_devices()
        assert result == []

    def test_list_devices_alive_and_dead(self, db, monkeypatch):
        """Mix of alive/dead workers returns correct status."""
        import gpumesh.db as dbmod

        alive_id = db.register_worker("host-a", "cuda", 50.0)
        dead_id = db.register_worker("host-b", "cpu", 10.0)

        # Force the dead worker's last_seen to be old
        now = time.time()
        with db._lock, db._conn:
            db._conn.execute(
                "UPDATE workers SET last_seen = ? WHERE id = ?",
                (now - WORKER_DEAD_AFTER - 10, dead_id),
            )

        devices = db.list_devices()
        assert len(devices) == 2
        by_id = {d["id"]: d for d in devices}
        assert by_id[alive_id]["status"] == "alive"
        assert by_id[dead_id]["status"] == "dead"

    def test_list_devices_index_assignment(self, db):
        """Devices get sequential indices starting from 0."""
        w1 = db.register_worker("h1", "cuda", 80.0)
        w2 = db.register_worker("h2", "cpu", 10.0)
        w3 = db.register_worker("h3", "cuda", 50.0)

        devices = db.list_devices()
        assert len(devices) == 3
        indices = [d["index"] for d in devices]
        assert indices == [0, 1, 2]


class TestDBDeviceSummary:
    """Tests for Database.device_summary()."""

    def test_device_summary_empty(self, db):
        """No workers returns zeros."""
        summary = db.device_summary()
        assert summary["total_devices"] == 0
        assert summary["alive_devices"] == 0
        assert summary["total_gpus"] == 0
        assert summary["total_cpus"] == 0
        assert summary["total_score"] == 0
        assert summary["devices"] == []

    def test_device_summary_mixed_devices(self, db):
        """GPU/CPU counting and score totaling work correctly."""
        db.register_worker("gpu1", "cuda", 85.0)
        db.register_worker("gpu2", "mps", 40.0)
        db.register_worker("cpu1", "cpu", 5.0)

        summary = db.device_summary()
        assert summary["total_devices"] == 3
        assert summary["alive_devices"] == 3
        assert summary["total_gpus"] == 2  # cuda + mps
        assert summary["total_cpus"] == 1
        assert summary["total_score"] == 130.0


class TestDBCancelWorkerTasks:
    """Tests for Database.cancel_worker_tasks()."""

    def _setup_running_and_pending(self, db):
        """Helper: create a worker with one running and one pending task."""
        wid = db.register_worker("host", "cpu", 1.0)
        job_id = db.create_job("j", "print(1)", [{"cost": 1}, {"cost": 2}])
        # Lease one task so it becomes running
        task = db.lease_task(wid)
        assert task is not None
        return wid, job_id

    def test_cancel_worker_tasks_force_false(self, db):
        """force=False: running tasks for this worker cancelled, pending untouched."""
        wid, job_id = self._setup_running_and_pending(db)
        result = db.cancel_worker_tasks(wid, force=False)

        # force=False cancels running tasks for this worker but NOT pending
        assert result["running"] >= 1
        assert result["pending"] == 0

        status = db.job_status(job_id)
        statuses = [t["status"] for t in status["tasks"]]
        # The running task should have been cancelled
        assert "running" not in statuses
        # The pending task should still be pending (unaffected)
        assert "pending" in statuses

    def test_cancel_worker_tasks_force_true(self, db):
        """force=True: both running and pending tasks cancelled."""
        wid, job_id = self._setup_running_and_pending(db)
        result = db.cancel_worker_tasks(wid, force=True)

        assert result["running"] >= 1
        assert result["pending"] >= 0

        status = db.job_status(job_id)
        statuses = [t["status"] for t in status["tasks"]]
        # All tasks should be failed
        assert all(s == "failed" for s in statuses)


class TestDBCancelAllTasks:
    """Tests for Database.cancel_all_tasks()."""

    def _setup_multi_worker_tasks(self, db):
        """Helper: create two workers each with a running task and pending tasks."""
        w1 = db.register_worker("host1", "cuda", 50.0)
        w2 = db.register_worker("host2", "cpu", 10.0)
        job_id = db.create_job("j", "print(1)", [
            {"cost": 1}, {"cost": 2}, {"cost": 3}, {"cost": 4}
        ])
        # Lease two tasks (one per worker)
        t1 = db.lease_task(w1)
        t2 = db.lease_task(w2)
        assert t1 is not None
        assert t2 is not None
        return w1, w2, job_id

    def test_cancel_all_tasks_force_false(self, db):
        """force=False: only pending tasks cancelled, running untouched."""
        w1, w2, job_id = self._setup_multi_worker_tasks(db)
        result = db.cancel_all_tasks(force=False)

        assert result["running"] == 0

        status = db.job_status(job_id)
        statuses = [t["status"] for t in status["tasks"]]
        # Both running tasks should still be running
        assert statuses.count("running") == 2
        # The remaining pending tasks should be cancelled
        assert statuses.count("failed") == 2

    def test_cancel_all_tasks_force_true(self, db):
        """force=True: both pending and running tasks cancelled."""
        w1, w2, job_id = self._setup_multi_worker_tasks(db)
        result = db.cancel_all_tasks(force=True)

        assert result["running"] >= 2

        status = db.job_status(job_id)
        statuses = [t["status"] for t in status["tasks"]]
        # All tasks should be failed
        assert all(s == "failed" for s in statuses)


# ══════════════════════════════════════════════════════════════════════════
# Server Endpoint Tests
# ══════════════════════════════════════════════════════════════════════════


class TestServerGetDevices:
    """Tests for GET /api/devices."""

    def test_get_devices_endpoint(self, mesh):
        """Returns device_summary with correct structure."""
        resp = mesh.call("GET", "/api/devices")
        assert "total_devices" in resp
        assert "alive_devices" in resp
        assert "total_gpus" in resp
        assert "total_cpus" in resp
        assert "total_score" in resp
        assert "devices" in resp
        assert resp["total_devices"] == 0

    def test_get_devices_unauthorized(self, mesh):
        """Wrong token returns 401."""
        bad_client = MeshClient(mesh.base_url, "wrong-token")
        with pytest.raises(urllib.error.HTTPError) as exc:
            bad_client.call("GET", "/api/devices")
        assert exc.value.code == 401

    def test_get_devices_with_worker(self, mesh):
        """Returns correct counts after a worker joins."""
        mesh.call("POST", "/api/register", {
            "hostname": "test-gpu",
            "device": "cuda",
            "score": 85.0,
        })
        resp = mesh.call("GET", "/api/devices")
        assert resp["total_devices"] == 1
        assert resp["alive_devices"] == 1
        assert resp["total_gpus"] == 1
        assert resp["total_score"] == 85.0


class TestServerKillEndpoint:
    """Tests for POST /api/kill."""

    def test_kill_endpoint_force_true(self, mesh):
        """force=True cancels all tasks."""
        # Register worker and submit a job
        reg = mesh.call("POST", "/api/register", {
            "hostname": "test", "device": "cpu", "score": 1.0,
        })
        worker_id = reg["worker_id"]

        mesh.call("POST", "/api/jobs", {
            "name": "test",
            "script": "print(1)",
            "payloads": [{"cost": 1}, {"cost": 2}],
        })

        # Lease a task so it becomes running
        task = mesh.call("POST", "/api/lease", {"worker_id": worker_id})
        assert task is not None

        # Kill all tasks with force
        result = mesh.call("POST", "/api/kill", {"force": True})
        assert result["running"] >= 1

    def test_kill_endpoint_force_false(self, mesh):
        """force=False cancels only pending tasks."""
        reg = mesh.call("POST", "/api/register", {
            "hostname": "test", "device": "cpu", "score": 1.0,
        })
        worker_id = reg["worker_id"]

        mesh.call("POST", "/api/jobs", {
            "name": "test",
            "script": "print(1)",
            "payloads": [{"cost": 1}, {"cost": 2}],
        })

        # Lease one task
        task = mesh.call("POST", "/api/lease", {"worker_id": worker_id})
        assert task is not None

        # Graceful kill
        result = mesh.call("POST", "/api/kill", {"force": False})
        assert result["running"] == 0

    def test_kill_endpoint_missing_force(self, mesh):
        """Missing force field returns 400."""
        mesh.call("POST", "/api/jobs", {
            "name": "test",
            "script": "print(1)",
            "payloads": [{"cost": 1}],
        })
        with pytest.raises(urllib.error.HTTPError) as exc:
            mesh.call("POST", "/api/kill", {})
        assert exc.value.code == 400


class TestServerCancelEndpoint:
    """Tests for POST /api/cancel."""

    def test_cancel_endpoint(self, mesh):
        """Cancels a job and returns counts."""
        resp = mesh.call("POST", "/api/jobs", {
            "name": "cancel_test",
            "script": "print(1)",
            "payloads": [{"cost": 1}, {"cost": 2}],
        })
        job_id = resp["job_id"]

        result = mesh.call("POST", "/api/cancel", {"job_id": job_id})
        assert result["pending"] == 2
        assert result["running"] == 0

    def test_cancel_endpoint_not_found(self, mesh):
        """Returns 404 for nonexistent job."""
        with pytest.raises(urllib.error.HTTPError) as exc:
            mesh.call("POST", "/api/cancel", {"job_id": "nonexistent"})
        assert exc.value.code == 404

    def test_cancel_endpoint_missing_job_id(self, mesh):
        """Returns 400 when job_id is missing."""
        with pytest.raises(urllib.error.HTTPError) as exc:
            mesh.call("POST", "/api/cancel", {})
        assert exc.value.code == 400


# ══════════════════════════════════════════════════════════════════════════
# API Layer Tests
# ══════════════════════════════════════════════════════════════════════════


class TestAPIDevices:
    """Tests for GPUMesh.devices()."""

    def test_devices_method(self, mesh):
        """Returns device list from server."""
        from gpumesh.api import GPUMesh

        api = GPUMesh(mesh.base_url, TOKEN)

        # Register a worker
        mesh.call("POST", "/api/register", {
            "hostname": "test-gpu", "device": "cuda", "score": 85.0,
        })

        devices = api.devices()
        assert isinstance(devices, list)
        assert len(devices) == 1
        assert devices[0]["device"] == "cuda"
        assert devices[0]["score"] == 85.0

    def test_device_count_method(self, mesh):
        """Returns GPU count."""
        from gpumesh.api import GPUMesh

        api = GPUMesh(mesh.base_url, TOKEN)

        mesh.call("POST", "/api/register", {
            "hostname": "g1", "device": "cuda", "score": 85.0,
        })
        mesh.call("POST", "/api/register", {
            "hostname": "g2", "device": "cuda", "score": 50.0,
        })
        mesh.call("POST", "/api/register", {
            "hostname": "c1", "device": "cpu", "score": 5.0,
        })

        count = api.device_count()
        assert count == 2  # Only cuda devices

    def test_total_score_method(self, mesh):
        """Returns score total."""
        from gpumesh.api import GPUMesh

        api = GPUMesh(mesh.base_url, TOKEN)

        mesh.call("POST", "/api/register", {
            "hostname": "g1", "device": "cuda", "score": 85.0,
        })
        mesh.call("POST", "/api/register", {
            "hostname": "c1", "device": "cpu", "score": 5.0,
        })

        score = api.total_score()
        assert score == 90.0


class TestAPIAutoDevice:
    """Tests for GPUMesh.auto_device()."""

    def test_auto_device_picks_highest_score(self, mesh):
        """Picks the most powerful device by score."""
        from gpumesh.api import GPUMesh

        api = GPUMesh(mesh.base_url, TOKEN)

        mesh.call("POST", "/api/register", {
            "hostname": "weak", "device": "cpu", "score": 5.0,
        })
        mesh.call("POST", "/api/register", {
            "hostname": "strong", "device": "cuda", "score": 85.0,
        })

        device = api.auto_device()
        assert device is not None
        assert device["hostname"] == "strong"
        assert device["score"] == 85.0

    def test_auto_device_returns_none_when_empty(self, mesh):
        """No devices returns None."""
        from gpumesh.api import GPUMesh

        api = GPUMesh(mesh.base_url, TOKEN)
        device = api.auto_device()
        assert device is None


# ══════════════════════════════════════════════════════════════════════════
# Client Layer Tests
# ══════════════════════════════════════════════════════════════════════════


class TestClientListDevices:
    """Tests for client.list_devices()."""

    def test_list_devices_success(self, mesh):
        """Returns device summary on success."""
        from gpumesh.client import list_devices

        mesh.call("POST", "/api/register", {
            "hostname": "test", "device": "cuda", "score": 85.0,
        })

        summary = list_devices(mesh.base_url, TOKEN)
        assert summary["total_devices"] == 1
        assert summary["total_gpus"] == 1
        assert summary["total_score"] == 85.0

    def test_list_devices_connection_error(self):
        """Returns fallback dict on connection error."""
        from gpumesh.client import list_devices

        result = list_devices("http://127.0.0.1:1", TOKEN)
        assert result["total_devices"] == 0
        assert result["alive_devices"] == 0
        assert result["total_gpus"] == 0
        assert result["total_cpus"] == 0
        assert result["total_score"] == 0
        assert result["devices"] == []

    def test_list_devices_auth_error(self, mesh):
        """Propagates HTTPError for auth failures."""
        from gpumesh.client import list_devices

        with pytest.raises(urllib.error.HTTPError) as exc:
            list_devices(mesh.base_url, "bad-token")
        assert exc.value.code == 401


# ══════════════════════════════════════════════════════════════════════════
# CLI Tests
# ══════════════════════════════════════════════════════════════════════════


class TestCLIKill:
    """Tests for the kill CLI subcommand."""

    def test_cmd_kill_help(self, capsys):
        """--help works and prints usage."""
        from gpumesh.cli import main

        with pytest.raises(SystemExit) as exc_info:
            with patch("sys.argv", ["gpumesh", "kill", "--help"]):
                main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "kill" in captured.out.lower()


class TestCLIDevices:
    """Tests for the devices CLI subcommand."""

    def test_cmd_devices_help(self, capsys):
        """--help works and prints usage."""
        from gpumesh.cli import main

        with pytest.raises(SystemExit) as exc_info:
            with patch("sys.argv", ["gpumesh", "devices", "--help"]):
                main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "devices" in captured.out.lower()
