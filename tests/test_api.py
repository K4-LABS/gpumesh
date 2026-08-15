"""Tests for the GPUMesh Python API."""

import json
import socket
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


class TestDeviceCapacity:
    """The capacity a worker reports has to reach /api/devices."""

    def _register_a100(self, mesh):
        client = MeshClient(mesh.url, mesh.token)
        client.call("POST", "/api/register", {
            "hostname": "gpu-rig",
            "device": "cuda",
            "device_name": "NVIDIA A100-SXM4-40GB",
            "score": 90.0,
            "cpu_cores": 16,
            "cpu_count": 16,
            "gpu_memory_total_mb": 40000.0,
            "gpu_memory_free_mb": 39000.0,
        })

    def _register_without_a_vram_reading(self, mesh):
        """A worker that reports no free VRAM at all — a CPU box, or an older
        release whose registration body predates the field."""
        client = MeshClient(mesh.url, mesh.token)
        client.call("POST", "/api/register", {
            "hostname": "gpu-rig-quiet",
            "device": "cuda",
            "device_name": "NVIDIA A100-SXM4-40GB",
            "score": 90.0,
            "cpu_cores": 16,
            "gpu_memory_total_mb": 40000.0,
        })

    def test_registered_capacity_is_exposed(self, mesh):
        self._register_a100(mesh)
        device = mesh.devices()[0]
        assert device["cpu_cores"] == 16
        assert device["gpu_memory_total_mb"] == 40000.0
        # The reading the worker sent in its registration body reaches
        # /api/devices, which is this class's whole subject. It used to be
        # discarded there, so a worker looked unmeasured until its first
        # heartbeat landed a moment later; the client reads unmeasured
        # permissively, so that window quietly let work onto a card whose
        # free VRAM had in fact been measured and reported.
        assert device["gpu_memory_free_mb"] == 39000.0

    def test_a_fresh_worker_is_not_reported_as_a_full_one(self, mesh):
        """The ambiguity, over real HTTP, from the client's own view.

        A worker whose free VRAM nobody has measured must not be
        indistinguishable from a 40 GB card with nothing left, because
        @accelerate reads the first permissively and the second strictly.
        Null is how the coordinator says "no reading"; 0.0 is a claim it
        cannot support, and is byte for byte what a card running flat out
        reports.
        """
        self._register_without_a_vram_reading(mesh)
        assert mesh.devices()[0]["gpu_memory_free_mb"] is None

    def test_accelerate_requirements_check_against_reported_capacity(self, mesh):
        from gpumesh import accelerate

        self._register_a100(mesh)

        @accelerate(mesh, cores=8, memory="16GB", gpu="A100")
        def fits(x):
            return x

        @accelerate(mesh, cores=64)
        def too_many_cores(x):
            return x

        fits._validate_resources()  # must not raise
        with pytest.raises(ValueError, match="No worker can satisfy"):
            too_many_cores._validate_resources()

    def test_real_worker_reports_its_cores(self, mesh_with_worker):
        """A worker joining normally posts its probe, so cores are never 0."""
        # Registration happens after the join-time benchmark, so wait for it
        # rather than racing the fixture's fixed sleep.
        deadline = time.time() + 30
        alive = []
        while time.time() < deadline and not alive:
            alive = [d for d in mesh_with_worker.devices() if d["status"] == "alive"]
            if not alive:
                time.sleep(0.2)
        assert alive
        assert alive[0]["cpu_cores"] >= 1


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


class _RecordingClient:
    """Stands in for MeshClient so a payload can be inspected without a server."""

    def __init__(self):
        self.posts = []

    def call(self, method, path, body=None):
        if method == "POST":
            self.posts.append(body)
            return {"job_id": "job-1"}
        if path == "/api/workers":
            return {"workers": [{
                "id": "w1", "device": "cuda", "device_name": "NVIDIA A100",
                "hostname": "rig", "score": 90.0, "alive": True,
            }]}
        return {
            "finished": True,
            "counts": {"done": 1},
            "tasks": [{"status": "done", "result": None}],
        }


def _square(x):
    return {"x": x, "square": x * x}


class TestDistributePlacementHints:
    """Placement hints ride at the payload top level for the scheduler."""

    def _mesh_with_recorder(self):
        m = GPUMesh("http://127.0.0.1:1", "tok")
        client = _RecordingClient()
        m._client = client
        return m, client

    def test_cores_becomes_cpu_cores_in_the_payload(self):
        m, client = self._mesh_with_recorder()
        m.distribute(function=_square, params=[{"x": 2}], cores=8)
        payload = client.posts[0]["payloads"][0]
        assert payload["cpu_cores"] == 8

    def test_all_three_hints_travel_together(self):
        m, client = self._mesh_with_recorder()
        m.distribute(
            function=_square,
            params=[{"x": 2}, {"x": 3}],
            gpu="A100",
            gpu_memory_mb=16384.0,
            cores=8,
        )
        for payload in client.posts[0]["payloads"]:
            assert payload["gpu"] == "A100"
            assert payload["gpu_memory_mb"] == 16384.0
            assert payload["cpu_cores"] == 8

    def test_unset_hints_are_omitted_entirely(self):
        """An absent hint is what 'no constraint' looks like to the scheduler."""
        m, client = self._mesh_with_recorder()
        m.distribute(function=_square, params=[{"x": 2}])
        payload = client.posts[0]["payloads"][0]
        assert "cpu_cores" not in payload
        assert "gpu" not in payload
        assert "gpu_memory_mb" not in payload

    def test_cores_never_reaches_the_function_params(self):
        m, client = self._mesh_with_recorder()
        m.distribute(function=_square, params=[{"x": 2}], cores=8)
        assert client.posts[0]["payloads"][0]["_params"] == {"x": 2}

    def test_map_cores_reaches_distribute_payload_end_to_end(self):
        """@accelerate(cores=N).map() -> distribute(cores=N) -> cpu_cores."""
        from gpumesh import accelerate

        m, client = self._mesh_with_recorder()

        @accelerate(m, cores=8)
        def work(x):
            return {"x": x}

        m.devices = lambda: [
            {"device": "cpu", "device_name": "Intel i7", "status": "alive",
             "cpu_cores": 16}
        ]
        work.map([{"x": 1}])
        assert client.posts[0]["payloads"][0]["cpu_cores"] == 8


class _ExplodingClient:
    """A client that fails the test if distribute() reaches the network."""

    def call(self, method, path, body=None):
        raise AssertionError(
            "distribute() contacted the coordinator with a malformed hint; "
            "the point of the client-side check is that it never gets here"
        )


class TestDistributeRejectsMalformedHints:
    """A bad hint type is a typo, and a typo deserves an answer immediately.

    The coordinator already refuses these with a 400 quoting the value, so the
    hole is closed either way — this is about the error arriving from the line
    the caller wrote instead of from a machine across the room, one round trip
    later.
    """

    def _mesh(self, client=None):
        m = GPUMesh("http://127.0.0.1:1", "tok")
        m._client = client or _ExplodingClient()
        return m

    @pytest.mark.parametrize("kwargs,fragment", [
        ({"gpu": 123}, "gpu=123"),
        ({"gpu": ["A100"]}, "not a device name string"),
        ({"gpu_memory_mb": "8GB"}, "gpu_memory_mb='8GB'"),
        ({"cores": "eight"}, "cores='eight'"),
        ({"cores": True}, "cores=True"),
        ({"gpu_memory_mb": float("nan")}, "gpu_memory_mb"),
    ])
    def test_raises_before_any_http_call(self, kwargs, fragment):
        m = self._mesh()
        with pytest.raises(ValueError) as exc:
            m.distribute(function=_square, params=[{"x": 1}], **kwargs)
        assert fragment in str(exc.value)

    def test_the_message_names_the_keyword_the_caller_typed(self):
        """``cores=`` is the parameter; ``cpu_cores`` is only the wire name."""
        m = self._mesh()
        with pytest.raises(ValueError) as exc:
            m.distribute(function=_square, params=[{"x": 1}], cores="eight")
        assert "cpu_cores" not in str(exc.value)

    @pytest.mark.parametrize("kwargs", [
        {},
        {"gpu": "A100"},
        {"gpu": "cuda:1"},
        {"gpu_memory_mb": 16384},
        {"gpu_memory_mb": 16384.5},
        {"gpu_memory_mb": 0},
        {"cores": 8},
        {"cores": 0},
        {"gpu": None, "gpu_memory_mb": None, "cores": None},
        {"gpu": "", "gpu_memory_mb": 1024, "cores": 4},
    ])
    def test_everything_the_coordinator_accepts_still_goes_through(self, kwargs):
        """Client and coordinator share one predicate, so this cannot drift."""
        m = self._mesh(_RecordingClient())
        m.distribute(function=_square, params=[{"x": 1}], **kwargs)
        assert m._client.posts  # the job was submitted, not refused

    def test_the_client_check_matches_the_coordinator_exactly(self):
        """Same values, same verdict, on both sides of the wire."""
        from gpumesh.db import Database

        cases = [
            {"gpu": 123}, {"gpu_memory_mb": "8GB"}, {"cores": "eight"},
            {"cores": True}, {"gpu": "A100"}, {"gpu_memory_mb": 16384},
            {"cores": 8}, {"gpu_memory_mb": 0}, {},
        ]
        for kwargs in cases:
            client_rejected = False
            m = self._mesh(_RecordingClient())
            try:
                m.distribute(function=_square, params=[{"x": 1}], **kwargs)
            except ValueError:
                client_rejected = True

            payload = dict(kwargs)
            if "cores" in payload:
                payload["cpu_cores"] = payload.pop("cores")
            db = Database(":memory:")
            coordinator_rejected = False
            try:
                db.create_job("j", "s", [payload])
            except ValueError:
                coordinator_rejected = True
            finally:
                db.close()

            assert client_rejected is coordinator_rejected, kwargs


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


def _port_of(handle):
    """The port the coordinator behind *handle* is listening on."""
    return handle.gpumesh_httpd.server_address[1]


def _accepts(port, timeout=1.0):
    """Can a TCP connection be opened to *port* on loopback right now?

    Used instead of an HTTP round trip because the question here is whether
    the listening socket still exists, not whether anything behind it answers.
    """
    try:
        socket.create_connection(("127.0.0.1", port), timeout=timeout).close()
        return True
    except OSError:
        return False


def _wait_until(predicate, timeout, interval=0.05):
    """Poll *predicate* until it holds, or the timeout expires. Never blocks
    longer than *timeout* — a regression here has to fail the test, not hang
    the suite."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class TestStartCoordinator:
    """Tests for static coordinator start.

    ``start_coordinator`` used to return the token and nothing else, which
    meant the listener thread, the reaper thread and the self-worker it started
    had no owner at all: from a notebook the only way to stop a coordinator was
    to restart the kernel, and from a test there was no way at all. This class
    is the one that leaked them — a live coordinator and a reaper waking every
    five seconds for the remainder of the session, which is how an unrelated
    claim-server test in another file came to flake on scheduler jitter. So
    these tests both use and pin the shutdown handle.
    """

    def test_start_coordinator(self, tmp_path):
        """Start coordinator and verify it works."""
        token = GPUMesh.start_coordinator(
            port=0,  # Let OS pick port
            token="test-coord-token",
            db_path=str(tmp_path / "coord.db"),
        )
        try:
            assert token == "test-coord-token"
            assert _accepts(_port_of(token)), "the coordinator is not listening"
        finally:
            token.gpumesh_shutdown()

    def test_the_return_value_is_still_the_token(self, tmp_path):
        """Backward compatibility, spelled out.

        Every caller in the wild writes ``token = start_coordinator(...)`` and
        then treats the result as a string — compares it, formats it into a
        join snippet, JSON-encodes it into a config file. Carrying the shutdown
        handles must not change any of that, which is the whole reason the
        return value is a ``str`` subclass rather than a new object or a tuple.
        """
        token = GPUMesh.start_coordinator(
            port=0, token="still-a-string", db_path=str(tmp_path / "s.db"),
            self_worker=False,
        )
        try:
            assert isinstance(token, str)
            assert token == "still-a-string"
            assert f"{token}" == "still-a-string"
            assert json.dumps({"token": token}) == '{"token": "still-a-string"}'
            assert {token: 1}["still-a-string"] == 1
            assert token.upper() == "STILL-A-STRING"
        finally:
            token.gpumesh_shutdown()

    def test_shutdown_stops_the_listener_and_the_reaper(self, tmp_path):
        """The leak itself: both background threads must actually end.

        The reaper is checked by joining the thread ``server.serve`` publishes
        as ``gpumesh_reaper``, not by watching for side effects — a reaper that
        had merely gone quiet would satisfy the latter and still be there for
        the rest of the session. Its own wait is REAP_INTERVAL long, so the
        join budget is derived from that constant instead of guessed.
        """
        from gpumesh.server import REAP_INTERVAL

        token = GPUMesh.start_coordinator(
            port=0, token="stoppable", db_path=str(tmp_path / "stop.db"),
            self_worker=False,
        )
        port = _port_of(token)
        reaper = token.gpumesh_httpd.gpumesh_reaper
        assert _accepts(port)
        assert reaper.is_alive()

        token.gpumesh_shutdown()

        assert not token.gpumesh_serve_thread.is_alive(), (
            "serve_forever is still running after gpumesh_shutdown()"
        )
        reaper.join(timeout=REAP_INTERVAL + 10)
        assert not reaper.is_alive(), (
            "the reaper outlived the coordinator that started it"
        )
        assert _wait_until(lambda: not _accepts(port), timeout=10), (
            f"port {port} is still accepting connections after shutdown"
        )

    def test_shutdown_stops_the_self_worker(self, tmp_path):
        """The self-worker is the third thread, and the one that keeps polling.

        ``spawn_local_worker`` has always returned a thread with a
        ``gpumesh_stop`` event attached; ``start_coordinator`` dropped it on
        the floor, so nothing could ever set it. This is the only test here
        that pays for a real capability probe, because a stub worker would not
        be the thing that was leaking.
        """
        token = GPUMesh.start_coordinator(
            port=0, token="selfworker", db_path=str(tmp_path / "sw.db"),
            self_worker=True,
        )
        worker = token.gpumesh_worker
        assert worker is not None, "no self-worker was started to test"

        token.gpumesh_shutdown()

        assert not worker.is_alive(), (
            "the self-worker kept polling after the coordinator it belongs to "
            "was shut down"
        )

    def test_shutdown_is_safe_to_call_twice(self, tmp_path):
        """Notebooks re-run cells, and a teardown often runs after an explicit
        stop. A second call must be a no-op, not an exception."""
        token = GPUMesh.start_coordinator(
            port=0, token="twice", db_path=str(tmp_path / "twice.db"),
            self_worker=False,
        )
        token.gpumesh_shutdown()
        token.gpumesh_shutdown()

    def test_the_handles_are_the_ones_serve_already_publishes(self, tmp_path):
        """No new vocabulary: the same two objects, under the same names.

        ``server.serve`` publishes ``httpd.gpumesh_stop`` and every fixture in
        tests/ drives ``httpd.gpumesh_stop.set(); httpd.shutdown()`` directly.
        Exposing the identical objects means that code keeps working against a
        coordinator started through the API, rather than needing to learn a
        second way to stop the same server.
        """
        token = GPUMesh.start_coordinator(
            port=0, token="handles", db_path=str(tmp_path / "h.db"),
            self_worker=False,
        )
        try:
            assert token.gpumesh_stop is token.gpumesh_httpd.gpumesh_stop
            assert isinstance(token.gpumesh_stop, threading.Event)
            assert token.gpumesh_serve_thread.is_alive()
        finally:
            token.gpumesh_shutdown()


class _FakeHTTPD:
    """Enough of a ThreadingHTTPServer for start_coordinator's bookkeeping."""

    def __init__(self, host, port):
        self.server_address = (host, port or 45678)

    def serve_forever(self):
        pass


class TestCoordinatorBindAddress:
    """A worker runs arbitrary code as this user, so exposure must be opt-in."""

    def _patched_serve(self, monkeypatch):
        from gpumesh import server as server_module

        recorded = {}

        def fake_serve(host, port, db_path, token, discovery=False, safe_mode=False):
            recorded["host"] = host
            recorded["port"] = port
            recorded["token"] = token
            return _FakeHTTPD(host, port)

        monkeypatch.setattr(server_module, "serve", fake_serve)
        return recorded

    def test_defaults_to_loopback(self, monkeypatch, tmp_path):
        recorded = self._patched_serve(monkeypatch)
        GPUMesh.start_coordinator(
            port=0, token="tok", db_path=str(tmp_path / "c.db"),
            self_worker=False,
        )
        assert recorded["host"] == "127.0.0.1"

    def test_loopback_says_the_lan_url_will_not_work(self, monkeypatch, tmp_path, capsys):
        self._patched_serve(monkeypatch)
        GPUMesh.start_coordinator(
            port=0, token="tok", db_path=str(tmp_path / "c.db"),
            self_worker=False,
        )
        out = capsys.readouterr().out
        assert "only this machine can join" in out
        assert "will NOT work from" in out
        # The join snippet must point at an address that actually resolves here.
        assert 'add_worker("http://127.0.0.1:45678"' in out

    def test_explicit_host_is_passed_through(self, monkeypatch, tmp_path):
        recorded = self._patched_serve(monkeypatch)
        GPUMesh.start_coordinator(
            port=0, token="tok", db_path=str(tmp_path / "c.db"),
            self_worker=False, host="0.0.0.0",
        )
        assert recorded["host"] == "0.0.0.0"

    def test_non_loopback_host_warns_loudly(self, monkeypatch, tmp_path, capsys):
        self._patched_serve(monkeypatch)
        GPUMesh.start_coordinator(
            port=0, token="tok", db_path=str(tmp_path / "c.db"),
            self_worker=False, host="0.0.0.0",
        )
        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "0.0.0.0" in out          # the address
        assert "45678" in out            # the port actually bound
        assert "run code as you" in out  # what it means

    def test_loopback_does_not_warn(self, monkeypatch, tmp_path, capsys):
        self._patched_serve(monkeypatch)
        GPUMesh.start_coordinator(
            port=0, token="tok", db_path=str(tmp_path / "c.db"),
            self_worker=False,
        )
        assert "WARNING" not in capsys.readouterr().out

    @pytest.mark.parametrize("host,expected", [
        ("127.0.0.1", True),
        ("127.1.2.3", True),
        ("localhost", True),
        ("LOCALHOST", True),
        ("::1", True),
        ("[::1]", True),
        ("0.0.0.0", False),   # the wildcard includes loopback but is not it
        ("::", False),
        ("192.168.1.10", False),
        ("gpu-rig.local", False),  # unresolvable names are treated as exposed
        ("", False),
    ])
    def test_is_loopback_host(self, host, expected):
        from gpumesh.api import _is_loopback_host

        assert _is_loopback_host(host) is expected


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
