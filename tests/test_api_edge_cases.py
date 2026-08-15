"""Tests for the GPUMesh Python API — edge cases."""

import http.client
import json
import socket
import sys
import threading
import time

import pytest

from gpumesh import server
from gpumesh.api import GPUMesh
from gpumesh.server import CoordinatorHandler, serve
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
        """A non-dict return arrives unchanged — same as calling it locally."""
        def simple(x):
            return x * 2

        results = mesh_with_worker.distribute(
            function=simple,
            params=[{"x": 5}],
            timeout=30,
        )

        assert len(results) == 1
        assert results[0] == 10

    def test_function_returns_none(self, mesh_with_worker):
        """A function returning None returns None, not a wrapper dict."""
        def noop(x):
            return None

        results = mesh_with_worker.distribute(
            function=noop,
            params=[{"x": 1}],
            timeout=30,
        )

        assert len(results) == 1
        assert results[0] is None

    def test_function_returns_list(self, mesh_with_worker):
        """Container returns keep their type across the mesh."""
        def make_list(n):
            return list(range(n))

        results = mesh_with_worker.distribute(
            function=make_list,
            params=[{"n": 4}],
            timeout=30,
        )

        assert results == [[0, 1, 2, 3]]

    def test_function_returns_non_json_object(self, mesh_with_worker):
        """Values JSON cannot express still survive the round trip.

        Real workloads return numpy scalars, arrays and tensors; those used to
        fail the task outright because results crossed the wire as plain JSON.
        """
        pytest.importorskip("numpy")

        def compute(x):
            import numpy as np
            return {"loss": np.float32(x) * 2, "arr": np.arange(3)}

        results = mesh_with_worker.distribute(
            function=compute,
            params=[{"x": 1.5}],
            timeout=30,
        )

        import numpy as np
        assert len(results) == 1
        assert results[0]["loss"] == np.float32(3.0)
        assert list(results[0]["arr"]) == [0, 1, 2]

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

    def test_cost_hint_not_passed_to_function_kwargs(self, mesh_with_worker):
        """The scheduler's 'cost' hint never reaches the function as a kwarg.

        ``cost`` controls task weight (see examples/payloads.json) and is
        kept at the payload top level; it is stripped before the function
        runs so fixed-signature functions don't see it.
        """
        def no_cost(**kwargs):
            return {"received_keys": sorted(kwargs.keys())}

        results = mesh_with_worker.distribute(
            function=no_cost,
            params=[{"x": 1, "cost": 99}],
            timeout=30,
        )

        assert len(results) == 1
        keys = results[0]["received_keys"]
        assert "x" in keys
        assert "cost" not in keys

    def test_cost_hint_not_passed_to_function(self, mesh_with_worker):
        """The scheduler's 'cost' hint is stripped before calling the function.

        Regression: distribute() used to forward the payload's ``cost`` key
        into the function's kwargs, so a fixed-signature function like
        ``add(x)`` failed with "unexpected keyword argument 'cost'" whenever
        a payload carried the documented scheduler hint (examples/payloads.json).
        """
        def add(x):
            return {"x": x, "double": x * 2}

        results = mesh_with_worker.distribute(
            function=add,
            params=[{"x": 5, "cost": 3}],
            timeout=30,
        )

        assert len(results) == 1
        assert results[0] == {"x": 5, "double": 10}

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

    def test_content_length_limit(self, tmp_path):
        """Requests exceeding Content-Length limit get 413."""
        import json
        import urllib.request
        httpd = serve("127.0.0.1", 0, str(tmp_path / "cl.db"), TOKEN)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()

        try:
            # Send a request with Content-Length > 10MB
            big_body = "x" * (11 * 1024 * 1024)
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/register",
                data=big_body.encode(),
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-Auth-Token": TOKEN,
                    "Content-Length": str(len(big_body)),
                },
            )
            try:
                urllib.request.urlopen(req, timeout=10)
                assert False, "Expected 413 or error"
            except urllib.error.HTTPError as e:
                assert e.code == 413
        finally:
            httpd.gpumesh_stop.set()
            httpd.shutdown()


# ══════════════════════════════════════════════════════════════════════
#  REQUEST FRAMING / RESOURCE LIMITS
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def coord(tmp_path):
    """A bare coordinator, plus the per-serve handler subclass.

    ``serve()`` builds a fresh subclass of CoordinatorHandler for every
    coordinator, so a test can lower ``timeout`` on it to make a socket
    timeout observable in seconds instead of a minute, without touching the
    class the rest of the suite uses.
    """
    httpd = serve("127.0.0.1", 0, str(tmp_path / "framing.db"), TOKEN)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield {"port": port, "httpd": httpd, "handler": httpd.RequestHandlerClass}
    httpd.gpumesh_stop.set()
    httpd.shutdown()


def _headers(**extra):
    hdrs = {"Content-Type": "application/json", "X-Auth-Token": TOKEN}
    hdrs.update(extra)
    return hdrs


def _raw_request(port, headers=None, body=b"", path="/api/register",
                 raw_after_headers=None, timeout=30.0):
    """Send a hand-built POST and return (status, parsed body or None).

    http.client rather than urllib because these tests need to control the
    framing headers that urllib insists on setting correctly. The client
    timeout is what turns a regression into a failure instead of a hung suite.
    """
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.putrequest("POST", path, skip_accept_encoding=True)
        for key, value in (headers or {}).items():
            conn.putheader(key, value)
        conn.endheaders()
        if raw_after_headers is not None:
            conn.send(raw_after_headers)
        elif body:
            conn.send(body)
        resp = conn.getresponse()
        payload = resp.read()
        try:
            return resp.status, json.loads(payload)
        except ValueError:
            return resp.status, None
    finally:
        conn.close()


def _send_and_read(port, request_bytes, half_close=False, timeout=30.0):
    """Write raw bytes, then read until the server closes. Returns the bytes.

    Every socket here carries a timeout: the failure mode this whole section
    guards against is blocking, so a test that could block would be useless.
    """
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        sock.sendall(request_bytes)
        if half_close:
            sock.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            try:
                chunk = sock.recv(65536)
            except OSError:
                break  # includes socket.timeout and a Windows RST
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        sock.close()


def _request_bytes(path="/api/register", body=b"", headers=None):
    hdrs = {"Host": "127.0.0.1", "Content-Type": "application/json",
            "X-Auth-Token": TOKEN}
    if headers is not None:
        hdrs.update(headers)
    hdrs.setdefault("Content-Length", str(len(body)))
    head = f"POST {path} HTTP/1.1\r\n" + "".join(
        f"{k}: {v}\r\n" for k, v in hdrs.items() if v is not None
    ) + "\r\n"
    return head.encode() + body


def _status_lines(raw):
    return [line for line in raw.split(b"\r\n") if line.startswith(b"HTTP/")]


class TestCoordinatorRequestLimits:
    """The coordinator answers an oversized or stalled request without
    letting the caller decide how much work that costs.

    Both defects here were the same shape as the ones just fixed on the
    claim server: an attacker-declared Content-Length was executed rather
    than merely inspected, and a socket that went quiet mid-body held a
    thread for as long as the peer felt like holding it.
    """

    def test_coordinator_sets_a_socket_timeout(self):
        """Without this, ThreadingHTTPServer leaks a thread per dead socket."""
        assert isinstance(CoordinatorHandler.timeout, (int, float))
        # Generous enough for a multi-megabyte payload over a slow link (it
        # bounds silence between reads, not the whole transfer), tight enough
        # that a leaked thread is reclaimed in about a minute.
        assert 15 <= CoordinatorHandler.timeout <= 120

    def test_drain_is_bounded_but_still_covers_a_modest_overshoot(self):
        """The cap has to sit between 'useless' and 'free 10 GB read'."""
        assert (CoordinatorHandler.MAX_DRAIN_BYTES
                > CoordinatorHandler.MAX_CONTENT_LENGTH)
        assert (CoordinatorHandler.MAX_DRAIN_BYTES
                <= 4 * CoordinatorHandler.MAX_CONTENT_LENGTH)
        assert CoordinatorHandler.DRAIN_BUDGET <= 30

    def test_huge_declared_content_length_is_refused_promptly(self, coord):
        """"Content-Length: 10 GB" must cost us nothing.

        Only a handful of bytes are actually sent. The old code drained the
        *declared* length in 64 KB chunks before answering, so this request
        made the coordinator sit reading until the peer or the suite gave up.
        Wall-clock is the assertion because promptness is the entire point —
        the status code was already 413 before the fix.
        """
        started = time.monotonic()
        status, data = _raw_request(
            coord["port"],
            headers=_headers(**{"Content-Length": str(10 * 1000 * 1000 * 1000)}),
            raw_after_headers=b'{"hostn',
            timeout=30.0,
        )
        elapsed = time.monotonic() - started
        assert status == 413
        assert "too large" in data["error"]
        assert elapsed < 10, (
            f"413 took {elapsed:.1f}s — the server is reading the declared "
            f"length instead of checking it"
        )

    def test_oversize_body_is_drained_so_the_413_is_readable(self, coord):
        """A batch that came out slightly too big deserves a real answer.

        The bytes are sent for real, not merely declared. If the coordinator
        closed on us mid-write we would eat a connection reset and never get
        to read the 413 — which is why draining a *bounded* prefix is worth
        keeping rather than dropping the connection outright.
        """
        oversized = b"x" * (CoordinatorHandler.MAX_CONTENT_LENGTH + 4096)
        status, data = _raw_request(
            coord["port"],
            headers=_headers(**{"Content-Length": str(len(oversized))}),
            body=oversized,
            timeout=60.0,
        )
        assert status == 413
        assert "too large" in data["error"]

    def test_coordinator_still_serves_after_a_refused_request(self, coord):
        """An oversized request must not wedge the listener.

        A fresh connection rather than a reused one: the coordinator speaks
        HTTP/1.0, so it closes after every response by design. What is being
        checked is that the *server* survived, not that keep-alive works.
        """
        oversized = b"x" * (CoordinatorHandler.MAX_CONTENT_LENGTH + 1024)
        _raw_request(
            coord["port"],
            headers=_headers(**{"Content-Length": str(len(oversized))}),
            body=oversized,
            timeout=60.0,
        )
        body = json.dumps({"hostname": "after-413", "device": "cpu",
                           "score": 1.0}).encode()
        status, data = _raw_request(
            coord["port"],
            headers=_headers(**{"Content-Length": str(len(body))}),
            body=body,
        )
        assert status == 200
        assert data["worker_id"]

    def test_huge_declared_length_leaves_the_coordinator_usable(self, coord):
        """The absurd case must also be survivable, not just fast."""
        _raw_request(
            coord["port"],
            headers=_headers(**{"Content-Length": str(8 * 1024 * 1024 * 1024)}),
            raw_after_headers=b"{",
        )
        body = json.dumps({"hostname": "after-huge", "device": "cpu",
                           "score": 2.0}).encode()
        status, data = _raw_request(
            coord["port"],
            headers=_headers(**{"Content-Length": str(len(body))}),
            body=body,
        )
        assert status == 200
        assert data["worker_id"]

    def test_stalled_body_does_not_pin_a_handler_thread(self, coord):
        """Declare a body, send none, and see the handler let go.

        Before the fix the socket was left blocking, so this connection held
        one coordinator thread until the process died — and ThreadingHTTPServer
        means an attacker gets one leaked thread per socket they open. The
        handler's timeout is lowered here only so the test costs two seconds
        instead of a minute; the production value is asserted separately.
        """
        coord["handler"].timeout = 2
        started = time.monotonic()
        raw = _send_and_read(
            coord["port"],
            _request_bytes(body=b"", headers={"Content-Length": "4096"}),
            timeout=30.0,
        )
        elapsed = time.monotonic() - started
        assert elapsed < 20, (
            f"connection held for {elapsed:.1f}s — the socket has no timeout"
        )
        assert elapsed >= 1.0, (
            f"closed after {elapsed:.1f}s — something other than the timeout "
            f"ended this request"
        )
        assert not _status_lines(raw), (
            "a peer that never sent its body should get no response, just a "
            f"closed connection; got {raw[:80]!r}"
        )
        # And the coordinator is still answering afterwards.
        body = json.dumps({"hostname": "after-stall", "device": "cpu",
                           "score": 3.0}).encode()
        status, _ = _raw_request(
            coord["port"],
            headers=_headers(**{"Content-Length": str(len(body))}),
            body=body,
        )
        assert status == 200

    def test_large_under_cap_job_payload_still_succeeds(self, coord):
        """The cap must not have been quietly tightened into breaking work.

        Job bodies carry a serialized function plus every parameter set, so
        multi-megabyte submissions are ordinary. A 6 MB one has to go through.
        """
        blob = "y" * (6 * 1024 * 1024)
        body = json.dumps({
            "name": "big-batch",
            "script": "import sys, json; json.load(sys.stdin); print('{}')",
            "payloads": [{"x": 1, "blob": blob}],
        }).encode()
        assert 5 * 1024 * 1024 < len(body) < CoordinatorHandler.MAX_CONTENT_LENGTH
        status, data = _raw_request(
            coord["port"], path="/api/jobs",
            headers=_headers(**{"Content-Length": str(len(body))}),
            body=body,
            timeout=60.0,
        )
        assert status == 200
        assert data["job_id"]

    def test_chunked_body_is_refused_with_411(self, coord):
        """BaseHTTPRequestHandler cannot decode chunked framing.

        Same answer as the claim server, for a sharper reason: a chunked
        request carries no Content-Length, so the old code read it as an empty
        body and let it through to the route with every field defaulted.
        """
        status, data = _raw_request(
            coord["port"],
            headers=_headers(**{"Transfer-Encoding": "chunked"}),
            raw_after_headers=b"5\r\nhello\r\n0\r\n\r\n",
        )
        assert status == 411
        assert "chunked" in data["error"]

    def test_missing_content_length_is_read_as_an_empty_body(self, coord):
        """Deliberately NOT the claim server's 411.

        With chunked already refused, "no Content-Length" is unambiguous HTTP
        for "no body", and this endpoint authenticates from a header before
        the body is touched — so body-less POSTs from curl stay legal. A 400
        about the missing 'force' field proves the request reached the route
        with {}, rather than being turned away at the framing check.
        """
        status, data = _raw_request(
            coord["port"], path="/api/kill", headers=_headers(),
        )
        assert status == 400
        assert "force" in data["error"]

    def test_truncated_body_returns_400(self, coord):
        """A peer that closes mid-body gets told that, not a JSON syntax error."""
        raw = _send_and_read(
            coord["port"],
            _request_bytes(body=b'{"hostname"',
                           headers={"Content-Length": "4096"}),
            half_close=True,
        )
        lines = _status_lines(raw)
        assert len(lines) == 1
        assert b"400" in lines[0]
        assert b"truncated" in raw

    def test_negative_content_length_returns_400(self, coord):
        status, data = _raw_request(
            coord["port"], headers=_headers(**{"Content-Length": "-1"}),
        )
        assert status == 400
        assert "Content-Length" in data["error"]

    def test_non_numeric_content_length_returns_400(self, coord):
        status, data = _raw_request(
            coord["port"], headers=_headers(**{"Content-Length": "lots"}),
        )
        assert status == 400
        assert "Content-Length" in data["error"]


# ══════════════════════════════════════════════════════════════════════
#  MALFORMED PLACEMENT HINTS
# ══════════════════════════════════════════════════════════════════════
#
# The trigger is a typo — ``gpu=123`` where ``gpu="cuda"`` was meant.
# GPUMesh.distribute(gpu=..., gpu_memory_mb=..., cores=...) forwards those
# values into the task payload untouched, and payloads also arrive over HTTP
# from any token holder, so the scheduler ends up comparing whatever was typed
# against worker capacity. Two things used to follow from that, both far out
# of proportion to their cause: every worker's /api/lease answered 500 for
# every task in the queue, and the coordinator's reaper thread died for good.


def _post_json(port, path, body, timeout=30.0):
    raw = json.dumps(body).encode("utf-8")
    return _raw_request(
        port, headers=_headers(**{"Content-Length": str(len(raw))}),
        body=raw, path=path, timeout=timeout,
    )


def _poison_task_payload(db, cost, payload_json):
    """Rewrite a queued task's payload behind the API's back.

    Now that /api/jobs refuses a malformed hint, a row like this can only come
    from a coordinator that predates the check — which is precisely the fleet
    these guards exist for, since the database outlives the upgrade.
    """
    with db._lock, db._conn:
        db._conn.execute(
            "UPDATE tasks SET payload = ? WHERE cost = ?", (payload_json, cost)
        )


class TestMalformedPlacementHintsOverHTTP:

    def test_jobs_route_rejects_a_bad_hint_with_400(self, coord):
        """The submitter is told in the same call, with their value quoted."""
        status, data = _post_json(coord["port"], "/api/jobs", {
            "name": "typo", "script": "print(1)",
            "payloads": [{"n": 1, "gpu": 123}],
        })
        assert status == 400
        assert "gpu=123" in data["error"]

    def test_jobs_route_rejects_a_string_memory_hint(self, coord):
        status, data = _post_json(coord["port"], "/api/jobs", {
            "name": "typo", "script": "print(1)",
            "payloads": [{"n": 1, "gpu_memory_mb": "8GB"}],
        })
        assert status == 400
        assert "gpu_memory_mb" in data["error"]

    def test_jobs_route_still_accepts_well_formed_hints(self, coord):
        status, data = _post_json(coord["port"], "/api/jobs", {
            "name": "ok", "script": "print(1)",
            "payloads": [{"n": 1, "gpu": "A100", "gpu_memory_mb": 16384,
                          "cpu_cores": 8}],
        })
        assert status == 200
        assert data["job_id"]

    def test_one_bad_task_does_not_500_every_lease_in_the_queue(self, coord):
        """The mesh-stopping defect: one poisoned row, every lease a 500."""
        port = coord["port"]
        status, reg = _post_json(port, "/api/register", {
            "hostname": "laptop", "device": "cpu", "score": 1.0,
        })
        assert status == 200
        worker_id = reg["worker_id"]

        status, _ = _post_json(port, "/api/jobs", {
            "name": "batch", "script": "print(1)",
            "payloads": [{"n": 1, "cost": 1}, {"n": 2, "cost": 2},
                         {"n": 3, "cost": 3}],
        })
        assert status == 200
        _poison_task_payload(coord["httpd"].RequestHandlerClass.db, 2.0,
                             '{"gpu_memory_mb": "8GB"}')

        costs = []
        for _ in range(4):
            status, task = _post_json(port, "/api/lease",
                                      {"worker_id": worker_id})
            assert status != 500, "one malformed payload blocked the whole queue"
            if status == 204 or task is None:
                break
            costs.append(task["cost"])
        assert sorted(costs) == [1.0, 3.0]

    def test_a_malformed_gpu_hint_does_not_kill_the_reaper(self, coord, monkeypatch):
        """``123``.strip() raised AttributeError straight through the loop.

        Asserts both halves of the damage: the thread is still alive, and it
        is still doing its job (the poisoned task gets a verdict).
        """
        monkeypatch.setattr(server, "REAP_INTERVAL", 0.05)
        db = coord["httpd"].RequestHandlerClass.db
        reaper = coord["httpd"].gpumesh_reaper
        assert reaper.is_alive()

        status, _ = _post_json(coord["port"], "/api/jobs", {
            "name": "typo", "script": "print(1)",
            "payloads": [{"n": 1, "cost": 1}],
        })
        assert status == 200
        _poison_task_payload(db, 1.0, '{"gpu": 123}')

        deadline = time.time() + 10.0
        while time.time() < deadline:
            counts = db.job_status_counts()
            if counts.get("failed"):
                break
            time.sleep(0.05)
        assert db.job_status_counts().get("failed") == 1, \
            "the reaper never reached the poisoned task"
        assert reaper.is_alive(), \
            "the reaper thread died on a malformed placement hint"

    def test_the_reaper_survives_any_exception_and_keeps_reaping(
            self, coord, monkeypatch):
        """A daemon loop whose death is silent must not have a fatal case.

        Faked rather than provoked on purpose: the point is that the loop
        holds for whatever a callee throws next, not only for the one payload
        that exposed it.
        """
        monkeypatch.setattr(server, "REAP_INTERVAL", 0.05)
        db = coord["httpd"].RequestHandlerClass.db
        reaper = coord["httpd"].gpumesh_reaper
        assert reaper.is_alive()

        passes = []
        real_reap = db.reap_expired_leases

        def counting_reap():
            passes.append(1)
            return real_reap()

        def explode():
            raise AttributeError("'int' object has no attribute 'strip'")

        monkeypatch.setattr(db, "reap_expired_leases", counting_reap)
        monkeypatch.setattr(db, "fail_unsatisfiable_tasks", explode)

        deadline = time.time() + 10.0
        while len(passes) < 3 and time.time() < deadline:
            time.sleep(0.05)
        assert len(passes) >= 3, \
            "the reaper stopped running after an exception in a callee"
        assert reaper.is_alive()


# ══════════════════════════════════════════════════════════════════════
#  /api/result — the route that carries the only copy
# ══════════════════════════════════════════════════════════════════════
#
# Everything else this API accepts can be repeated for free: a registration, a
# lease request, a job submission. A result cannot — the compute behind it is
# already spent. That asymmetry is the reason this one route is treated
# differently from every other POST, both at the shutdown gate and in how
# carefully its fields are handled before they reach a transaction that can
# roll back the write recording them.
#
# The end-to-end story lives in tests/test_result_delivery.py; what follows is
# the route boundary on its own.


def _lease_a_task(port):
    """Register, queue one task, lease it. Returns (worker_id, task_id)."""
    status, reg = _post_json(port, "/api/register", {
        "hostname": "route-test", "device": "cpu", "score": 1.0,
    })
    assert status == 200
    status, _ = _post_json(port, "/api/jobs", {
        "name": "route", "script": "print('{}')", "payloads": [{"n": 1}],
    })
    assert status == 200
    status, task = _post_json(port, "/api/lease",
                              {"worker_id": reg["worker_id"]})
    assert status == 200 and task is not None
    return reg["worker_id"], task["task_id"]


class TestResultRouteShutdownGate:
    """The 503 gate distinguishes new work from the return of old work."""

    def test_result_is_routed_before_the_gate(self, coord):
        worker_id, task_id = _lease_a_task(coord["port"])
        coord["handler"]._shutdown_event.set()

        status, data = _post_json(coord["port"], "/api/result", {
            "task_id": task_id, "worker_id": worker_id,
            "ok": True, "result": {"v": 1}, "elapsed": 0.5,
        })

        assert status == 200
        assert data == {"ok": True}

    @pytest.mark.parametrize("path,body", [
        ("/api/register", {"hostname": "x", "device": "cpu", "score": 1.0}),
        ("/api/lease", {"worker_id": "nobody"}),
        ("/api/jobs", {"name": "x", "script": "print(1)",
                       "payloads": [{"n": 1}]}),
        ("/api/heartbeat", {"worker_id": "nobody"}),
        ("/api/cancel", {"job_id": "nope"}),
        ("/api/retry", {"job_id": "nope"}),
        ("/api/kill", {"force": False}),
    ])
    def test_every_other_post_still_gets_503(self, coord, path, body):
        coord["handler"]._shutdown_event.set()
        status, data = _post_json(coord["port"], path, body)
        assert status == 503
        assert "shutting down" in data["error"]

    def test_gets_are_untouched_by_the_exemption(self, coord):
        """Reads create no obligation either way, and losing one costs nothing."""
        coord["handler"]._shutdown_event.set()
        conn = http.client.HTTPConnection("127.0.0.1", coord["port"],
                                          timeout=30.0)
        try:
            conn.putrequest("GET", "/api/workers", skip_accept_encoding=True)
            conn.putheader("X-Auth-Token", TOKEN)
            conn.endheaders()
            resp = conn.getresponse()
            resp.read()
            assert resp.status == 503
        finally:
            conn.close()


class TestResultRouteValidation:
    """A bad field must cost the field, never the result.

    ``complete_task`` does its work in one transaction, so anything that
    raises inside it — a TypeError from ``elapsed > 0`` on a string, an
    InterfaceError binding a dict to a TEXT column — rolls back the UPDATE
    that recorded the outcome. The task goes back to looking like it is still
    running and gets recomputed 300 seconds later.
    """

    @pytest.mark.parametrize("elapsed", [
        "fast", "", {"s": 1}, [2], True, -1, 0, float("nan"), float("inf"),
    ])
    def test_an_unusable_elapsed_never_costs_the_result(self, coord, elapsed):
        worker_id, task_id = _lease_a_task(coord["port"])
        status, data = _post_json(coord["port"], "/api/result", {
            "task_id": task_id, "worker_id": worker_id,
            "ok": True, "result": {"v": 1}, "elapsed": elapsed,
        })
        assert status == 200, f"elapsed={elapsed!r} produced {status}"
        assert data == {"ok": True}

    def test_a_dict_error_message_never_costs_the_failure(self, coord):
        worker_id, task_id = _lease_a_task(coord["port"])
        status, data = _post_json(coord["port"], "/api/result", {
            "task_id": task_id, "worker_id": worker_id,
            "ok": False, "error": {"type": "ValueError"},
        })
        assert status == 200
        assert data == {"ok": True}

    def test_a_non_string_id_is_refused_at_the_boundary(self, coord):
        """Nothing to degrade to: a dict identifies no task."""
        status, data = _post_json(coord["port"], "/api/result", {
            "task_id": ["t"], "worker_id": "w", "ok": True,
        })
        assert status == 400
        assert "task_id" in data["error"]

    def test_a_non_bool_ok_is_still_refused(self, coord):
        status, data = _post_json(coord["port"], "/api/result", {
            "task_id": "t", "worker_id": "w", "ok": 1,
        })
        assert status == 400
        assert "ok" in data["error"]

    def test_an_unknown_task_is_a_409_not_a_500(self, coord):
        """409 is what tells a worker its result is genuinely obsolete.

        The worker discards on 409 and retries on 5xx, so confusing the two
        turns "somebody else already did this" into a retry loop, or a
        transient coordinator hiccup into a discarded result.
        """
        status, data = _post_json(coord["port"], "/api/result", {
            "task_id": "no-such-task", "worker_id": "no-such-worker",
            "ok": True, "result": {},
        })
        assert status == 409
        assert data == {"ok": False}


class TestElapsedCoercion:
    """The coercion itself, away from HTTP — it is the load-bearing part."""

    @pytest.mark.parametrize("raw", [
        None, "", "fast", "1.5s", {"s": 1}, [1], True, False, 0, -0.5,
        float("nan"), float("inf"), float("-inf"),
    ])
    def test_unusable_values_become_no_timing_information(self, raw):
        assert server._coerce_elapsed(raw) is None

    @pytest.mark.parametrize("raw,expected", [
        (1, 1.0), (2.5, 2.5), ("3", 3.0), (" 4.5 ", 4.5),
    ])
    def test_usable_values_survive(self, raw, expected):
        assert server._coerce_elapsed(raw) == pytest.approx(expected)

    def test_free_text_is_forced_to_something_sqlite_accepts(self):
        assert server._coerce_text(None) == ""
        assert server._coerce_text("boom") == "boom"
        assert "boom" in server._coerce_text({"msg": "boom"})
