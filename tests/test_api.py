"""Tests for the GPUMesh Python API."""

import threading
import time

import pytest

from gpumesh.api import GPUMesh
from gpumesh.server import serve
from gpumesh.worker import MeshClient, run_worker

TOKEN = "test-token"


@pytest.fixture
def mesh(tmp_path):
    """Create a real coordinator and return a GPUMesh client."""
    httpd = serve("127.0.0.1", 0, str(tmp_path / "api.db"), TOKEN)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield GPUMesh(f"http://127.0.0.1:{port}", TOKEN)
    httpd.gpumesh_stop.set()
    httpd.shutdown()


@pytest.fixture
def mesh_with_worker(tmp_path):
    """Create a coordinator with a real background worker."""
    httpd = serve("127.0.0.1", 0, str(tmp_path / "api_worker.db"), TOKEN)
    port = httpd.server_address[1]
    coord_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    coord_thread.start()
    
    url = f"http://127.0.0.1:{port}"
    worker_thread = threading.Thread(
        target=run_worker, args=(url, TOKEN), daemon=True
    )
    worker_thread.start()
    time.sleep(0.5)  # Let worker register
    
    yield GPUMesh(url, TOKEN)
    httpd.gpumesh_stop.set()
    httpd.shutdown()


class TestGPUMeshInit:
    """Tests for GPUMesh initialization."""

    def test_creates_client(self, mesh):
        """GPUMesh creates a valid client."""
        assert mesh.url.startswith("http://127.0.0.1:")
        assert mesh.token == TOKEN

    def test_strips_trailing_slash(self):
        """URL trailing slash is stripped."""
        m = GPUMesh("http://example.com:8000/", "tok")
        assert m.url == "http://example.com:8000"


class TestWorkers:
    """Tests for worker listing."""

    def test_empty_workers(self, mesh):
        """No workers initially."""
        result = mesh.workers()
        assert result == []

    def test_with_worker(self, mesh):
        """Lists connected workers."""
        # Register a worker
        client = MeshClient(mesh.url, mesh.token)
        client.call("POST", "/api/register", {
            "hostname": "test-gpu",
            "device": "cuda",
            "score": 85.0,
        })

        workers = mesh.workers()
        assert len(workers) == 1
        assert workers[0]["device"] == "cuda"
        assert workers[0]["score"] == 85.0


class TestSubmitJob:
    """Tests for script-based job submission."""

    def test_submit_and_status(self, mesh):
        """Submit a script job and check status."""
        script = """
import json, sys
payload = json.load(sys.stdin)
print(json.dumps({"result": payload["x"] * 2}))
"""
        job_id = mesh.submit_job(script, [{"x": 5}, {"x": 10}])
        assert job_id

        status = mesh.job_status(job_id)
        assert status["finished"] is False
        assert status["counts"]["pending"] == 2


class TestDistribute:
    """Tests for function distribution."""

    def test_distribute_simple(self, mesh_with_worker):
        """Distribute a simple function across workers."""
        def square(x):
            return {"x": x, "square": x ** 2}

        results = mesh_with_worker.distribute(
            function=square,
            params=[{"x": 2}, {"x": 3}, {"x": 4}],
            timeout=30,
        )

        assert len(results) == 3
        squares = sorted(r["square"] for r in results)
        assert squares == [4, 9, 16]

    def test_distribute_with_cost(self, mesh_with_worker):
        """Distribute with custom costs."""
        def identity(x):
            return {"x": x}

        results = mesh_with_worker.distribute(
            function=identity,
            params=[
                {"x": 1, "cost": 1},
                {"x": 2, "cost": 5},
            ],
            timeout=30,
        )

        assert len(results) == 2


class TestResultsToDataframe:
    """Tests for DataFrame conversion."""

    def test_results_to_dataframe(self):
        """Convert results to pandas DataFrame."""
        pytest.importorskip("pandas")
        
        results = [
            {"lr": 0.01, "accuracy": 0.82},
            {"lr": 0.05, "accuracy": 0.89},
        ]
        
        df = GPUMesh.results_to_dataframe(results)
        assert len(df) == 2
        assert list(df.columns) == ["lr", "accuracy"]


class TestStartCoordinator:
    """Tests for static coordinator start."""

    def test_start_coordinator(self, tmp_path):
        """Start coordinator and verify it works."""
        token = GPUMesh.start_coordinator(
            port=0,  # Let OS pick port
            token="test-coord-token",
            db_path=str(tmp_path / "coord.db"),
        )
        assert token == "test-coord-token"


class TestAddWorker:
    """Tests for static worker join."""

    def test_add_worker(self, tmp_path):
        """Start coordinator and add a worker."""
        # Start coordinator
        httpd = serve("127.0.0.1", 0, str(tmp_path / "worker.db"), "wk-token")
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()

        try:
            info = GPUMesh.add_worker(
                f"http://127.0.0.1:{port}",
                "wk-token",
            )
            assert "device" in info
            assert "score" in info
        finally:
            httpd.gpumesh_stop.set()
            httpd.shutdown()
