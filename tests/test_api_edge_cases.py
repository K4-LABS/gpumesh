"""Tests for the GPUMesh Python API — edge cases."""

import sys
import threading
import time

import pytest

from gpumesh.api import GPUMesh
from gpumesh.server import serve
from gpumesh.worker import MeshClient, run_worker

TOKEN = "test-token"


# ── Fixtures ──────────────────────────────────────────────────────────

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


@pytest.fixture
def mesh_with_two_workers(tmp_path):
    """Create a coordinator with two real background workers."""
    httpd = serve("127.0.0.1", 0, str(tmp_path / "api_multi.db"), TOKEN)
    port = httpd.server_address[1]
    coord_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    coord_thread.start()

    url = f"http://127.0.0.1:{port}"
    for _ in range(2):
        t = threading.Thread(target=run_worker, args=(url, TOKEN), daemon=True)
        t.start()
    time.sleep(1.0)  # Let both workers register

    yield GPUMesh(url, TOKEN)
    httpd.gpumesh_stop.set()
    httpd.shutdown()


# ── Basic Tests ───────────────────────────────────────────────────────

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


# ── Distribute Tests ──────────────────────────────────────────────────

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
            port=0,
            token="test-coord-token",
            db_path=str(tmp_path / "coord.db"),
        )
        assert token == "test-coord-token"


class TestAddWorker:
    """Tests for static worker join."""

    def test_add_worker(self, tmp_path):
        """Start coordinator and add a worker."""
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


# ══════════════════════════════════════════════════════════════════════
#  EDGE CASE TESTS
# ══════════════════════════════════════════════════════════════════════


class TestDistributeEdgeCases:
    """Edge case tests for function distribution."""

    # ── Function Timeout ──────────────────────────────────────────────

    def test_function_timeout_raises_timeout_error(self, mesh_with_worker):
        """When the entire job exceeds timeout, TimeoutError is raised."""
        def very_slow(x):
            time.sleep(120)
            return {"x": x}

        with pytest.raises(TimeoutError, match="timed out"):
            mesh_with_worker.distribute(
                function=very_slow,
                params=[{"x": 1}],
                timeout=3,
                poll_interval=0.5,
            )

    # ── Function Errors ───────────────────────────────────────────────

    def test_function_raises_exception(self, mesh_with_worker):
        """Function that raises an exception is reported as failed."""
        def bad_function(x):
            raise ValueError(f"Bad value: {x}")

        results = mesh_with_worker.distribute(
            function=bad_function,
            params=[{"x": 1}, {"x": 2}],
            timeout=30,
        )

        assert len(results) == 2
        for r in results:
            assert "_error" in r
            assert "Bad value" in r["_error"]

    def test_function_division_by_zero(self, mesh_with_worker):
        """Function with division by zero is caught."""
        def divide(x):
            return {"result": 1 / 0}

        results = mesh_with_worker.distribute(
            function=divide,
            params=[{"x": 1}],
            timeout=30,
        )

        assert len(results) == 1
        assert "_error" in results[0]

    def test_function_returns_non_dict(self, mesh_with_worker):
        """Function returning non-dict is wrapped in {"result": ...}."""
        def simple(x):
            return x * 2

        results = mesh_with_worker.distribute(
            function=simple,
            params=[{"x": 5}],
            timeout=30,
        )

        assert len(results) == 1
        assert results[0]["result"] == 10

    def test_function_returns_none(self, mesh_with_worker):
        """Function returning None is wrapped in {"result": None}."""
        def noop(x):
            return None

        results = mesh_with_worker.distribute(
            function=noop,
            params=[{"x": 1}],
            timeout=30,
        )

        assert len(results) == 1
        assert results[0]["result"] is None

    def test_function_key_error(self, mesh_with_worker):
        """Function accessing missing key raises KeyError."""
        def bad_access(x):
            data = {}
            return {"value": data["missing_key"]}

        results = mesh_with_worker.distribute(
            function=bad_access,
            params=[{"x": 1}],
            timeout=30,
        )

        assert len(results) == 1
        assert "_error" in results[0]
        assert "missing_key" in results[0]["_error"]

    # ── Empty / Minimal Params ────────────────────────────────────────

    def test_single_param(self, mesh_with_worker):
        """Distribute with a single parameter set."""
        def echo(x):
            return {"x": x}

        results = mesh_with_worker.distribute(
            function=echo,
            params=[{"x": 42}],
            timeout=30,
        )

        assert len(results) == 1
        assert results[0]["x"] == 42

    def test_params_with_many_keys(self, mesh_with_worker):
        """Params with many keys."""
        def multi(a, b, c, d, e):
            return {"sum": a + b + c + d + e}

        results = mesh_with_worker.distribute(
            function=multi,
            params=[{"a": 1, "b": 2, "c": 3, "d": 4, "e": 5}],
            timeout=30,
        )

        assert len(results) == 1
        assert results[0]["sum"] == 15

    def test_params_with_underscore_keys_stripped(self, mesh_with_worker):
        """Internal underscore-prefixed keys are stripped."""
        def check_keys(**kwargs):
            # Should NOT receive _func, _params, _task_index
            return {"received_keys": sorted(kwargs.keys())}

        results = mesh_with_worker.distribute(
            function=check_keys,
            params=[{"x": 1, "_secret": "should_be_stripped"}],
            timeout=30,
        )

        assert len(results) == 1
        keys = results[0]["received_keys"]
        # _secret starts with _ so it gets stripped by the worker
        assert "_secret" not in keys
        assert "x" in keys

    def test_empty_params_list(self, mesh_with_worker):
        """Empty params list returns empty results."""
        def noop(x):
            return {"x": x}

        results = mesh_with_worker.distribute(
            function=noop,
            params=[],
            timeout=10,
        )

        assert results == []

    def test_cost_stripped_from_params(self, mesh_with_worker):
        """Cost key is stripped before passing to function."""
        def no_cost(**kwargs):
            return {"received_keys": sorted(kwargs.keys())}

        results = mesh_with_worker.distribute(
            function=no_cost,
            params=[{"x": 1, "cost": 99}],
            timeout=30,
        )

        assert len(results) == 1
        keys = results[0]["received_keys"]
        assert "cost" not in keys
        assert "x" in keys

    # ── Multi-Worker Distribute ───────────────────────────────────────

    def test_multi_worker_distribute(self, mesh_with_two_workers):
        """Distribute across two workers — all tasks complete."""
        def double(x):
            return {"x": x, "doubled": x * 2}

        results = mesh_with_two_workers.distribute(
            function=double,
            params=[{"x": 1}, {"x": 2}, {"x": 3}, {"x": 4}],
            timeout=30,
        )

        assert len(results) == 4
        doubled = sorted(r["doubled"] for r in results)
        assert doubled == [2, 4, 6, 8]

    def test_multi_worker_all_complete(self, mesh_with_two_workers):
        """With two workers, all tasks eventually complete."""
        def identity(x):
            return {"x": x}

        params = [{"x": i} for i in range(6)]
        results = mesh_with_two_workers.distribute(
            function=identity,
            params=params,
            timeout=30,
        )

        assert len(results) == 6
        values = sorted(r["x"] for r in results)
        assert values == [0, 1, 2, 3, 4, 5]

    @pytest.mark.xfail(sys.platform == "win32", reason="Windows socket cleanup issue", strict=False)
    def test_multi_worker_mixed_success_and_failure(self, mesh_with_two_workers):
        """Some tasks succeed, some fail — results collected correctly."""
        def maybe_fail(x):
            if x == 3:
                raise ValueError("x=3 is unlucky")
            return {"x": x}

        results = mesh_with_two_workers.distribute(
            function=maybe_fail,
            params=[{"x": i} for i in range(5)],
            timeout=30,
        )

        assert len(results) == 5

        successes = [r for r in results if "_error" not in r]
        failures = [r for r in results if "_error" in r]

        assert len(successes) == 4  # x=0,1,2,4
        assert len(failures) == 1   # x=3
        assert "x=3 is unlucky" in failures[0]["_error"]

    def test_multi_worker_large_batch(self, mesh_with_two_workers):
        """Large batch of tasks distributed across workers."""
        def square(x):
            return {"x": x, "square": x ** 2}

        params = [{"x": i} for i in range(20)]
        results = mesh_with_two_workers.distribute(
            function=square,
            params=params,
            timeout=60,
        )

        assert len(results) == 20
        squares = {r["x"]: r["square"] for r in results}
        for i in range(20):
            assert squares[i] == i ** 2

    # ── Result Ordering ───────────────────────────────────────────────

    def test_results_maintain_order(self, mesh_with_worker):
        """Results come back in the same order as input params."""
        def label(x):
            return {"x": x, "label": f"task_{x}"}

        params = [{"x": i} for i in range(10)]
        results = mesh_with_worker.distribute(
            function=label,
            params=params,
            timeout=30,
        )

        assert len(results) == 10
        for i, r in enumerate(results):
            assert r["x"] == i
            assert r["label"] == f"task_{i}"

    # ── Closure / Lambda ──────────────────────────────────────────────

    def test_distribute_with_closure(self, mesh_with_worker):
        """Function using a closure variable."""
        multiplier = 10

        def multiply(x):
            return {"x": x, "result": x * multiplier}

        results = mesh_with_worker.distribute(
            function=multiply,
            params=[{"x": 1}, {"x": 2}, {"x": 3}],
            timeout=30,
        )

        assert len(results) == 3
        results_sorted = sorted(results, key=lambda r: r["x"])
        assert results_sorted[0]["result"] == 10
        assert results_sorted[1]["result"] == 20
        assert results_sorted[2]["result"] == 30

    def test_distribute_with_lambda(self, mesh_with_worker):
        """Lambda function works."""
        results = mesh_with_worker.distribute(
            function=lambda x: {"square": x ** 2},
            params=[{"x": 3}, {"x": 4}],
            timeout=30,
        )

        assert len(results) == 2
        squares = sorted(r["square"] for r in results)
        assert squares == [9, 16]

    # ── Auth / Connection ─────────────────────────────────────────────

    def test_wrong_token_fails(self, tmp_path):
        """Wrong token causes auth failure."""
        httpd = serve("127.0.0.1", 0, str(tmp_path / "auth.db"), "correct-token")
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()

        try:
            bad_mesh = GPUMesh(f"http://127.0.0.1:{port}", "wrong-token")
            with pytest.raises(Exception):
                bad_mesh.workers()
        finally:
            httpd.gpumesh_stop.set()
            httpd.shutdown()
