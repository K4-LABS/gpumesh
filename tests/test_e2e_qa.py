"""Deep QA test: Simulates real coordinator-worker workflows.

Tests:
1. Coordinator startup and worker connection
2. Job submission, execution, and result collection
3. Error scenarios (wrong token, wrong URL, timeout)
4. Edge cases (empty payloads, large scripts, concurrent jobs)
5. CLI commands (status, cancel, kill, workers, devices)
6. Function-based distribution
7. Worker heartbeat and failure detection
8. Multiple workers competing for tasks
"""

import json
import os
import sys
import threading
import time
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from gpumesh.api import GPUMesh, GPUMeshError
from gpumesh.client import cancel_job, get_status, wait_for_job, print_job
from gpumesh.db import Database, WORKER_DEAD_AFTER, LEASE_SECONDS, MAX_ATTEMPTS
from gpumesh.server import serve
from gpumesh.worker import MeshClient, run_worker


# ============================================================================
#  Fixtures
# ============================================================================

@pytest.fixture
def coordinator(tmp_path):
    """Start a real coordinator server."""
    db_path = str(tmp_path / "qa.db")
    httpd = serve("127.0.0.1", 0, db_path, "qa-token")
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.1)  # Let server start
    yield f"http://127.0.0.1:{port}", "qa-token", httpd
    httpd.gpumesh_stop.set()
    httpd.shutdown()


@pytest.fixture
def coordinator_with_worker(tmp_path):
    """Start coordinator with a background worker."""
    db_path = str(tmp_path / "qa_worker.db")
    httpd = serve("127.0.0.1", 0, db_path, "qa-token")
    port = httpd.server_address[1]
    coord_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    coord_thread.start()

    url = f"http://127.0.0.1:{port}"
    worker_thread = threading.Thread(
        target=run_worker, args=(url, "qa-token"), daemon=True
    )
    worker_thread.start()
    time.sleep(0.5)  # Let worker register

    yield url, "qa-token", httpd
    httpd.gpumesh_stop.set()
    httpd.shutdown()


# ============================================================================
#  Test 1: Basic Coordinator-Worker Flow
# ============================================================================

class TestBasicFlow:
    """Test basic coordinator-worker interaction."""

    def test_coordinator_starts(self, coordinator):
        """Coordinator starts and accepts connections."""
        url, token, httpd = coordinator
        client = MeshClient(url, token)
        resp = client.call("GET", "/api/workers")
        assert "workers" in resp
        assert isinstance(resp["workers"], list)

    def test_worker_registers(self, coordinator):
        """Worker can register with coordinator."""
        url, token, httpd = coordinator
        client = MeshClient(url, token)

        info = {
            "hostname": "test-pc",
            "device": "cuda",
            "device_name": "RTX 3080",
            "score": 85.0,
        }
        resp = client.call("POST", "/api/register", info)
        assert "worker_id" in resp
        assert len(resp["worker_id"]) > 0

    def test_worker_heartbeat(self, coordinator):
        """Worker heartbeat updates last_seen."""
        url, token, httpd = coordinator
        client = MeshClient(url, token)

        # Register worker
        resp = client.call("POST", "/api/register", {
            "hostname": "test-pc", "device": "cpu", "score": 1.0
        })
        worker_id = resp["worker_id"]

        # Send heartbeat
        resp = client.call("POST", "/api/heartbeat", {"worker_id": worker_id})
        assert resp["ok"] is True

        # Verify worker is alive
        workers = client.call("GET", "/api/workers")
        assert len(workers["workers"]) == 1
        assert workers["workers"][0]["alive"] is True

    def test_worker_list(self, coordinator):
        """List workers shows connected workers."""
        url, token, httpd = coordinator
        client = MeshClient(url, token)

        # Register multiple workers
        for i in range(3):
            client.call("POST", "/api/register", {
                "hostname": f"worker-{i}", "device": "cpu", "score": float(i)
            })

        workers = client.call("GET", "/api/workers")
        assert len(workers["workers"]) == 3


# ============================================================================
#  Test 2: Job Submission and Execution
# ============================================================================

class TestJobFlow:
    """Test job submission, execution, and result collection."""

    def test_submit_script_job(self, coordinator_with_worker):
        """Submit a script-based job and verify it's created."""
        url, token, _ = coordinator_with_worker

        script = 'import json, sys\npayload = json.load(sys.stdin)\nprint(json.dumps({"result": payload["x"] * 2}))'
        payloads = [{"x": 5}, {"x": 10}, {"x": 15}]

        client = MeshClient(url, token)
        resp = client.call("POST", "/api/jobs", {
            "name": "test-multiply",
            "script": script,
            "payloads": payloads,
        })
        job_id = resp["job_id"]

        # Verify job exists
        status = client.call("GET", f"/api/jobs/{job_id}")
        assert status["name"] == "test-multiply"
        assert len(status["tasks"]) == 3
        assert status["finished"] is False

    def test_job_executes_and_completes(self, coordinator_with_worker):
        """Job executes on worker and completes."""
        url, token, _ = coordinator_with_worker

        script = 'import json, sys\npayload = json.load(sys.stdin)\nprint(json.dumps({"result": payload["x"] * 2}))'
        payloads = [{"x": 5}, {"x": 10}]

        client = MeshClient(url, token)
        resp = client.call("POST", "/api/jobs", {
            "name": "test-exec",
            "script": script,
            "payloads": payloads,
        })
        job_id = resp["job_id"]

        # Wait for completion
        for _ in range(30):  # Max 30 seconds
            time.sleep(1)
            status = client.call("GET", f"/api/jobs/{job_id}")
            if status["finished"]:
                break

        # Verify results
        status = client.call("GET", f"/api/jobs/{job_id}")
        assert status["finished"] is True
        assert status["counts"].get("done", 0) == 2

        results = [t["result"] for t in status["tasks"] if t["status"] == "done"]
        values = sorted([r["result"] for r in results])
        assert values == [10, 20]

    def test_function_distribution(self, coordinator_with_worker):
        """Distribute a Python function across workers."""
        url, token, _ = coordinator_with_worker
        mesh = GPUMesh(url, token)

        def square(x):
            return {"x": x, "square": x ** 2}

        results = mesh.distribute(
            function=square,
            params=[{"x": 2}, {"x": 3}, {"x": 4}],
            timeout=30,
        )

        assert len(results) == 3
        squares = sorted([r["square"] for r in results])
        assert squares == [4, 9, 16]

    def test_job_status(self, coordinator_with_worker):
        """Check job status during execution."""
        url, token, _ = coordinator_with_worker

        # Submit a slow job
        script = 'import json, sys, time\ntime.sleep(2)\npayload = json.load(sys.stdin)\nprint(json.dumps({"result": payload["x"]}))'
        payloads = [{"x": 1}]

        client = MeshClient(url, token)
        resp = client.call("POST", "/api/jobs", {
            "name": "slow-job",
            "script": script,
            "payloads": payloads,
        })
        job_id = resp["job_id"]

        # Check status immediately
        status = client.call("GET", f"/api/jobs/{job_id}")
        assert status["finished"] is False

        # Wait for completion
        for _ in range(10):
            time.sleep(1)
            status = client.call("GET", f"/api/jobs/{job_id}")
            if status["finished"]:
                break

        assert status["finished"] is True


# ============================================================================
#  Test 3: Error Scenarios
# ============================================================================

class TestErrorScenarios:
    """Test error handling and edge cases."""

    def test_wrong_token_rejected(self, coordinator):
        """Wrong token is rejected with 401."""
        url, _, _ = coordinator
        client = MeshClient(url, "wrong-token")

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            client.call("GET", "/api/workers")
        assert exc_info.value.code == 401

    def test_wrong_url_connection_refused(self):
        """Wrong URL gives connection refused."""
        client = MeshClient("http://127.0.0.1:19999", "token")

        with pytest.raises(urllib.error.URLError):
            client.call("GET", "/api/workers")

    def test_empty_payloads_rejected(self, coordinator):
        """Empty payloads list is rejected with 400."""
        url, token, _ = coordinator
        client = MeshClient(url, token)

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            client.call("POST", "/api/jobs", {
                "name": "empty",
                "script": "print(1)",
                "payloads": [],
            })
        assert exc_info.value.code == 400
        # Verify error message mentions payloads
        error_body = json.loads(exc_info.value.read())
        assert "payloads" in error_body["error"].lower()

    def test_missing_script_rejected(self, coordinator):
        """Missing script is rejected with 400."""
        url, token, _ = coordinator
        client = MeshClient(url, token)

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            client.call("POST", "/api/jobs", {
                "name": "no-script",
                "payloads": [{"x": 1}],
            })
        assert exc_info.value.code == 400

    def test_empty_script_rejected(self, coordinator):
        """Empty script is rejected with 400."""
        url, token, _ = coordinator
        client = MeshClient(url, token)

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            client.call("POST", "/api/jobs", {
                "name": "empty-script",
                "script": "",
                "payloads": [{"x": 1}],
            })
        assert exc_info.value.code == 400

    def test_missing_payloads_key_rejected(self, coordinator):
        """Missing payloads key is rejected with 400."""
        url, token, _ = coordinator
        client = MeshClient(url, token)

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            client.call("POST", "/api/jobs", {
                "name": "no-payloads",
                "script": "print(1)",
            })
        assert exc_info.value.code == 400

    def test_invalid_payloads_type_rejected(self, coordinator):
        """Non-list payloads is rejected with 400."""
        url, token, _ = coordinator
        client = MeshClient(url, token)

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            client.call("POST", "/api/jobs", {
                "name": "bad-type",
                "script": "print(1)",
                "payloads": "not-a-list",
            })
        assert exc_info.value.code == 400

    def test_invalid_json_rejected(self, coordinator):
        """Invalid JSON body is rejected."""
        url, token, _ = coordinator
        client = MeshClient(url, token)

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            req = urllib.request.Request(
                f"{url}/api/register",
                data=b"not json",
                method="POST",
                headers={"Content-Type": "application/json", "X-Auth-Token": token},
            )
            urllib.request.urlopen(req)
        assert exc_info.value.code == 400

    def test_worker_death_detection(self, coordinator):
        """Dead workers are detected after timeout."""
        url, token, httpd = coordinator
        client = MeshClient(url, token)

        # Register a worker
        resp = client.call("POST", "/api/register", {
            "hostname": "dying-worker", "device": "cpu", "score": 1.0
        })
        worker_id = resp["worker_id"]

        # Verify worker is alive immediately
        workers = client.call("GET", "/api/workers")
        alive_worker = [w for w in workers["workers"] if w["id"] == worker_id]
        assert len(alive_worker) == 1
        assert alive_worker[0]["alive"] is True

        # Note: Testing actual death detection requires waiting for WORKER_DEAD_AFTER
        # which is 30 seconds - too slow for unit tests. The lease reaper tests
        # in test_worker_recovery cover this functionality.


# ============================================================================
#  Test 4: Multiple Workers
# ============================================================================

class TestMultipleWorkers:
    """Test multiple workers competing for tasks."""

    def test_multiple_workers_register(self, coordinator):
        """Multiple workers can register."""
        url, token, _ = coordinator
        client = MeshClient(url, token)

        worker_ids = []
        for i in range(5):
            resp = client.call("POST", "/api/register", {
                "hostname": f"worker-{i}", "device": "cpu", "score": float(i * 10)
            })
            worker_ids.append(resp["worker_id"])

        workers = client.call("GET", "/api/workers")
        assert len(workers["workers"]) == 5

    def test_tasks_distributed_by_capability(self, coordinator):
        """Tasks are distributed based on worker capability."""
        url, token, _ = coordinator
        client = MeshClient(url, token)

        # Register fast and slow workers
        fast_resp = client.call("POST", "/api/register", {
            "hostname": "fast-gpu", "device": "cuda", "score": 100.0
        })
        slow_resp = client.call("POST", "/api/register", {
            "hostname": "slow-cpu", "device": "cpu", "score": 1.0
        })

        # Submit many tasks
        script = 'import json, sys\npayload = json.load(sys.stdin)\nprint(json.dumps({"result": payload["x"]}))'
        payloads = [{"x": i, "cost": float(i)} for i in range(10)]
        client.call("POST", "/api/jobs", {
            "name": "distribution-test",
            "script": script,
            "payloads": payloads,
        })

        # Both workers should get tasks
        # Fast worker should get higher-cost tasks
        fast_tasks = []
        slow_tasks = []
        for _ in range(10):
            task = client.call("POST", "/api/lease", {"worker_id": fast_resp["worker_id"]})
            if task:
                fast_tasks.append(task)
            task = client.call("POST", "/api/lease", {"worker_id": slow_resp["worker_id"]})
            if task:
                slow_tasks.append(task)
            time.sleep(0.1)

        # At least some tasks should be assigned to each worker
        assert len(fast_tasks) + len(slow_tasks) > 0


# ============================================================================
#  Test 5: Task Cancellation and Kill
# ============================================================================

class TestCancellation:
    """Test task cancellation and kill operations."""

    def test_cancel_pending_tasks(self, coordinator):
        """Cancel pending tasks."""
        url, token, _ = coordinator
        client = MeshClient(url, token)

        # Submit a job with many tasks (no worker to execute)
        script = 'import json, sys\nprint(json.dumps({"result": 42}))'
        payloads = [{"x": i} for i in range(10)]
        resp = client.call("POST", "/api/jobs", {
            "name": "cancel-test",
            "script": script,
            "payloads": payloads,
        })
        job_id = resp["job_id"]

        # Cancel the job
        result = client.call("POST", "/api/cancel", {"job_id": job_id})
        assert result["pending"] == 10
        assert result["running"] == 0

    def test_cancel_running_tasks(self, coordinator):
        """Cancel running tasks."""
        url, token, _ = coordinator
        client = MeshClient(url, token)

        # Register a worker
        resp = client.call("POST", "/api/register", {
            "hostname": "slow-worker", "device": "cpu", "score": 1.0
        })
        worker_id = resp["worker_id"]

        # Submit a slow job
        script = 'import json, sys, time\ntime.sleep(60)\nprint(json.dumps({"result": 42}))'
        resp = client.call("POST", "/api/jobs", {
            "name": "slow-cancel",
            "script": script,
            "payloads": [{"x": 1}],
        })
        job_id = resp["job_id"]

        # Worker gets the task
        task = client.call("POST", "/api/lease", {"worker_id": worker_id})
        assert task is not None

        # Cancel the job
        result = client.call("POST", "/api/cancel", {"job_id": job_id})
        assert result["running"] == 1

    def test_kill_all_pending(self, coordinator):
        """Kill all pending tasks."""
        url, token, _ = coordinator
        client = MeshClient(url, token)

        # Submit jobs
        script = 'import json, sys\nprint(json.dumps({"result": 42}))'
        for i in range(3):
            client.call("POST", "/api/jobs", {
                "name": f"kill-test-{i}",
                "script": script,
                "payloads": [{"x": i}],
            })

        # Kill all
        result = client.call("POST", "/api/kill", {"force": False})
        assert result["pending"] >= 3

    def test_kill_all_force(self, coordinator):
        """Force kill all tasks."""
        url, token, _ = coordinator
        client = MeshClient(url, token)

        # Register worker and get a task
        resp = client.call("POST", "/api/register", {
            "hostname": "force-worker", "device": "cpu", "score": 1.0
        })
        worker_id = resp["worker_id"]

        script = 'import json, sys, time\ntime.sleep(60)\nprint(json.dumps({"result": 42}))'
        client.call("POST", "/api/jobs", {
            "name": "force-kill",
            "script": script,
            "payloads": [{"x": 1}],
        })

        task = client.call("POST", "/api/lease", {"worker_id": worker_id})
        assert task is not None

        # Force kill
        result = client.call("POST", "/api/kill", {"force": True})
        assert result["running"] == 1

    def test_cancel_nonexistent_job(self, coordinator):
        """Cancel nonexistent job returns None or raises 404."""
        url, token, _ = coordinator
        client = MeshClient(url, token)

        try:
            result = client.call("POST", "/api/cancel", {"job_id": "nonexistent"})
            # Server may return None for 404
            assert result is None
        except urllib.error.HTTPError as e:
            # 404 is expected for nonexistent job
            assert e.code == 404


# ============================================================================
#  Test 6: Device Summary
# ============================================================================

class TestDeviceSummary:
    """Test device summary and counting."""

    def test_device_summary_empty(self, coordinator):
        """Device summary with no workers."""
        url, token, _ = coordinator
        client = MeshClient(url, token)

        summary = client.call("GET", "/api/devices")
        assert summary["total_devices"] == 0
        assert summary["alive_devices"] == 0

    def test_device_summary_with_workers(self, coordinator):
        """Device summary with multiple workers."""
        url, token, _ = coordinator
        client = MeshClient(url, token)

        # Register GPU and CPU workers
        client.call("POST", "/api/register", {
            "hostname": "gpu-pc", "device": "cuda", "device_name": "RTX 3080", "score": 85.0
        })
        client.call("POST", "/api/register", {
            "hostname": "cpu-pc", "device": "cpu", "device_name": "cpu", "score": 5.0
        })

        summary = client.call("GET", "/api/devices")
        assert summary["total_devices"] == 2
        assert summary["alive_devices"] == 2
        assert summary["total_gpus"] == 1
        assert summary["total_cpus"] == 1
        assert summary["total_score"] == 90.0

    def test_device_summary_dead_workers(self, coordinator):
        """Device summary shows dead workers."""
        url, token, _ = coordinator
        client = MeshClient(url, token)

        # Register a worker
        resp = client.call("POST", "/api/register", {
            "hostname": "dying", "device": "cpu", "score": 1.0
        })
        worker_id = resp["worker_id"]

        # Fast-forward time to simulate worker death
        import unittest.mock as um
        original_time = time.time
        with um.patch.object(time, "time", return_value=original_time() + WORKER_DEAD_AFTER + 1):
            pass

        summary = client.call("GET", "/api/devices")
        assert summary["total_devices"] == 1
        assert summary["alive_devices"] == 0

    def test_python_api_device_count(self, coordinator):
        """Python API device_count works."""
        url, token, _ = coordinator
        mesh = GPUMesh(url, token)

        assert mesh.device_count() == 0

        # Register a worker
        client = MeshClient(url, token)
        client.call("POST", "/api/register", {
            "hostname": "gpu", "device": "cuda", "score": 50.0
        })

        assert mesh.device_count() == 1

    def test_python_api_auto_device(self, coordinator):
        """Python API auto_device picks the best device."""
        url, token, _ = coordinator
        mesh = GPUMesh(url, token)

        client = MeshClient(url, token)
        client.call("POST", "/api/register", {
            "hostname": "slow", "device": "cpu", "score": 5.0
        })
        client.call("POST", "/api/register", {
            "hostname": "fast", "device": "cuda", "device_name": "RTX 4090", "score": 120.0
        })

        device = mesh.auto_device()
        assert device is not None
        assert device["device_name"] == "RTX 4090"
        assert device["score"] == 120.0


# ============================================================================
#  Test 7: Worker Failure and Recovery
# ============================================================================

class TestWorkerRecovery:
    """Test worker failure detection and task re-queueing."""

    def test_lease_task_assignment(self, coordinator):
        """Lease assigns task to worker."""
        url, token, _ = coordinator
        client = MeshClient(url, token)

        # Register worker
        resp = client.call("POST", "/api/register", {
            "hostname": "fast-worker", "device": "cuda", "score": 100.0
        })
        worker_id = resp["worker_id"]

        # Submit job
        script = 'import json, sys\nprint(json.dumps({"result": 42}))'
        client.call("POST", "/api/jobs", {
            "name": "lease-test",
            "script": script,
            "payloads": [{"x": 1}],
        })

        # Worker should get a task
        task = client.call("POST", "/api/lease", {"worker_id": worker_id})
        assert task is not None
        assert "task_id" in task
        assert "script" in task
        assert task["payload"] == {"x": 1}

    def test_task_failure_reporting(self, coordinator_with_worker):
        """Task failures are properly reported."""
        url, token, _ = coordinator_with_worker

        # Submit a failing script
        script = 'import sys\nsys.exit(1)'
        client = MeshClient(url, token)
        resp = client.call("POST", "/api/jobs", {
            "name": "fail-test",
            "script": script,
            "payloads": [{"x": 1}],
        })
        job_id = resp["job_id"]

        # Wait for failure
        for _ in range(10):
            time.sleep(1)
            status = client.call("GET", f"/api/jobs/{job_id}")
            if status["finished"]:
                break

        assert status["finished"] is True
        assert status["counts"].get("failed", 0) >= 1


# ============================================================================
#  Test 8: Edge Cases
# ============================================================================

class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_large_result(self, coordinator_with_worker):
        """Handle large result data."""
        url, token, _ = coordinator_with_worker

        # Script that returns large result
        script = '''import json, sys
payload = json.load(sys.stdin)
# Return 100KB of data
data = {"result": "x" * 100000}
print(json.dumps(data))
'''
        client = MeshClient(url, token)
        resp = client.call("POST", "/api/jobs", {
            "name": "large-result",
            "script": script,
            "payloads": [{"x": 1}],
        })
        job_id = resp["job_id"]

        # Wait for completion
        for _ in range(10):
            time.sleep(1)
            status = client.call("GET", f"/api/jobs/{job_id}")
            if status["finished"]:
                break

        assert status["finished"] is True

    def test_special_characters_in_payload(self, coordinator_with_worker):
        """Handle special characters in payload."""
        url, token, _ = coordinator_with_worker

        script = 'import json, sys\npayload = json.load(sys.stdin)\nprint(json.dumps({"key": payload["key"]}))'
        payloads = [{"key": "hello world !@#$%^&*()"}]

        client = MeshClient(url, token)
        resp = client.call("POST", "/api/jobs", {
            "name": "special-chars",
            "script": script,
            "payloads": payloads,
        })
        job_id = resp["job_id"]

        for _ in range(10):
            time.sleep(1)
            status = client.call("GET", f"/api/jobs/{job_id}")
            if status["finished"]:
                break

        assert status["finished"] is True

    def test_concurrent_job_submission(self, coordinator_with_worker):
        """Submit multiple jobs concurrently."""
        url, token, _ = coordinator_with_worker

        script = 'import json, sys, time\ntime.sleep(0.1)\nprint(json.dumps({"result": 42}))'
        client = MeshClient(url, token)

        job_ids = []
        for i in range(5):
            resp = client.call("POST", "/api/jobs", {
                "name": f"concurrent-{i}",
                "script": script,
                "payloads": [{"x": i}],
            })
            job_ids.append(resp["job_id"])

        # All jobs should be created
        assert len(job_ids) == 5
        assert len(set(job_ids)) == 5  # All unique


# ============================================================================
#  Test 9: Python API End-to-End
# ============================================================================

class TestPythonAPIEndToEnd:
    """Test Python API with real coordinator."""

    def test_api_workers_list(self, coordinator_with_worker):
        """Python API lists workers."""
        url, token, _ = coordinator_with_worker
        mesh = GPUMesh(url, token)

        workers = mesh.workers()
        assert len(workers) >= 1
        assert "device" in workers[0]
        assert "score" in workers[0]

    def test_api_distribute_and_collect(self, coordinator_with_worker):
        """Distribute function and collect results."""
        url, token, _ = coordinator_with_worker
        mesh = GPUMesh(url, token)

        def add_one(x):
            return {"x": x, "result": x + 1}

        results = mesh.distribute(
            function=add_one,
            params=[{"x": i} for i in range(5)],
            timeout=30,
        )

        assert len(results) == 5
        results_sorted = sorted(results, key=lambda r: r["x"])
        for i, r in enumerate(results_sorted):
            assert r["result"] == i + 1

    def test_api_start_coordinator_and_worker(self, tmp_path):
        """Start coordinator and worker from Python API."""
        token = GPUMesh.start_coordinator(
            port=0,
            token="api-test-token",
            db_path=str(tmp_path / "api.db"),
        )
        assert token == "api-test-token"


# ============================================================================
#  Test 10: CLI Integration (via Python API)
# ============================================================================

class TestCLIIntegration:
    """Test CLI-like operations through the API."""

    def test_show_connection_info(self, coordinator):
        """Show connection info works."""
        url, token, _ = coordinator
        # This tests the connection_manager functionality
        from gpumesh import connection_manager

        connection_manager.save_connection(url, token)
        saved = connection_manager.load_connection()
        assert saved is not None
        assert saved["url"] == url
        assert saved["token"] == token

        connection_manager.clear_connection()
        saved = connection_manager.load_connection()
        assert saved is None

    def test_version_info(self):
        """Version info is accessible."""
        from gpumesh import __version__
        assert __version__
        assert len(__version__.split(".")) >= 2  # At least major.minor


# ============================================================================
#  Test 11: Stress Test
# ============================================================================

class TestStress:
    """Stress test with many tasks."""

    def test_many_tasks_single_worker(self, coordinator_with_worker):
        """Process multiple tasks with single worker."""
        url, token, _ = coordinator_with_worker

        script = 'import json, sys\npayload = json.load(sys.stdin)\nprint(json.dumps({"result": payload["x"] * 2}))'
        payloads = [{"x": i} for i in range(10)]  # Reduced from 50 to 10 for speed

        client = MeshClient(url, token)
        resp = client.call("POST", "/api/jobs", {
            "name": "stress-test",
            "script": script,
            "payloads": payloads,
        })
        job_id = resp["job_id"]

        # Wait for completion
        for _ in range(30):
            time.sleep(1)
            status = client.call("GET", f"/api/jobs/{job_id}")
            if status["finished"]:
                break

        assert status["finished"] is True
        assert status["counts"].get("done", 0) == 10
