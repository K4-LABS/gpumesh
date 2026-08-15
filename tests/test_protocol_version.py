"""Protocol version negotiation between a worker and a coordinator.

The coordinator and the workers are installed separately, on different
machines, by different people, and they drift. Three of this project's recent
fixes are cross-version failures that were only discovered when someone's task
crashed. These tests pin the handshake that turns that class of failure into a
refusal at registration with a message naming both versions.

Everything here runs against a real loopback coordinator (the pattern from
test_e2e.py / test_worker_resilience.py) or against a deliberately dumb stub
server standing in for a gpumesh released before the handshake existed. Every
wait is bounded; nothing in this file can block the suite.
"""

import json
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from gpumesh import (
    MIN_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    UNVERSIONED_PROTOCOL,
    ProtocolVersionMismatch,
)
from gpumesh import worker as worker_mod
from gpumesh.server import serve
from gpumesh.worker import MeshClient, _try_register, run_worker

TOKEN = "protocol-test-token"

BASE_INFO = {
    "hostname": "proto-test",
    "device": "cpu",
    "device_name": "stub device",
    "score": 1.0,
    "cpu_cores": 2,
}


@pytest.fixture
def mesh(tmp_path):
    """A real coordinator on an ephemeral loopback port."""
    httpd = serve("127.0.0.1", 0, str(tmp_path / "protocol.db"), TOKEN)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield MeshClient(f"http://127.0.0.1:{port}", TOKEN)
    httpd.gpumesh_stop.set()
    httpd.shutdown()


@pytest.fixture
def fast_probe(monkeypatch):
    """Skip the real hardware benchmark — these tests never run a task."""
    monkeypatch.setattr(worker_mod.capability, "full_probe",
                        lambda: dict(BASE_INFO))


def _register(client, **extra):
    body = dict(BASE_INFO)
    body.update(extra)
    return client.call("POST", "/api/register", body)


def _refusal_body(exc: urllib.error.HTTPError) -> dict:
    return json.loads(exc.read().decode("utf-8"))


# ══════════════════════════════════════════════════════════════════════
#  THE COORDINATOR'S GATE
# ══════════════════════════════════════════════════════════════════════


class TestRegistrationGate:
    def test_matching_version_registers(self, mesh):
        resp = _register(mesh, protocol_version=PROTOCOL_VERSION)
        assert resp["worker_id"]
        assert resp["protocol_version"] == PROTOCOL_VERSION
        assert resp["min_protocol_version"] == MIN_PROTOCOL_VERSION

    def test_absent_version_registers(self, mesh):
        """A gpumesh from before the handshake existed must still join.

        This is the case the serializer got wrong once already: a missing
        field means "unknown", and treating unknown as a mismatch broke real
        users. Every gpumesh released up to 1.3.0 sends no version at all, so
        if this test fails the change is unshippable.
        """
        resp = _register(mesh)
        assert resp["worker_id"]

    def test_oldest_supported_version_registers(self, mesh):
        """The N-1 edge of the window is inside it, not on the wrong side."""
        resp = _register(mesh, protocol_version=MIN_PROTOCOL_VERSION)
        assert resp["worker_id"]

    def test_absent_and_explicit_unversioned_agree(self, mesh):
        """Absent is mapped to a concrete version, not special-cased.

        If these two ever diverge, the absent case has grown a private path
        that the explicit one does not share — which is how the mapping ends
        up outliving the window it was supposed to be checked against.
        """
        assert _register(mesh)["worker_id"]
        assert _register(mesh, protocol_version=UNVERSIONED_PROTOCOL)["worker_id"]

    def test_too_old_is_refused_naming_both_versions(self, mesh):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _register(mesh, protocol_version=MIN_PROTOCOL_VERSION - 1)
        exc = exc_info.value
        assert exc.code == 426, "a version skew must not look like a bad request"
        body = _refusal_body(exc)
        message = body["error"]
        # Both numbers, in the prose, because the operator reading this is on
        # a different machine from the coordinator and has no other source.
        assert str(MIN_PROTOCOL_VERSION - 1) in message
        assert str(PROTOCOL_VERSION) in message
        assert "WORKER" in message, "must say which side to upgrade"
        assert "pip install -U gpumesh" in message
        # ...and the same numbers as structured fields for programs.
        assert body["protocol_version"] == PROTOCOL_VERSION
        assert body["min_protocol_version"] == MIN_PROTOCOL_VERSION
        assert body["worker_protocol_version"] == MIN_PROTOCOL_VERSION - 1

    def test_too_new_is_refused_naming_both_versions(self, mesh):
        future = PROTOCOL_VERSION + 5
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _register(mesh, protocol_version=future)
        exc = exc_info.value
        assert exc.code == 426
        message = _refusal_body(exc)["error"]
        assert str(future) in message
        assert str(PROTOCOL_VERSION) in message
        assert "COORDINATOR" in message, "the newer side is the worker here"

    def test_refused_worker_leaves_no_row_behind(self, mesh):
        """A refusal must not create a ghost worker.

        An operator debugging a skew looks at /api/workers first. A registered
        worker that will never lease a task is a second mystery on top of the
        one being diagnosed.
        """
        with pytest.raises(urllib.error.HTTPError):
            _register(mesh, protocol_version=PROTOCOL_VERSION + 5)
        assert mesh.call("GET", "/api/workers")["workers"] == []

    def test_unparseable_version_is_a_bad_request(self, mesh):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _register(mesh, protocol_version="two point oh")
        assert exc_info.value.code == 400
        assert "protocol_version" in _refusal_body(exc_info.value)["error"]

    def test_health_reports_the_protocol_version(self, mesh):
        """Operators must be able to read the window without owning a worker."""
        health = mesh.call("GET", "/api/health")
        assert health["protocol_version"] == PROTOCOL_VERSION
        assert health["min_protocol_version"] == MIN_PROTOCOL_VERSION


# ══════════════════════════════════════════════════════════════════════
#  THE WORKER'S SIDE OF THE HANDSHAKE
# ══════════════════════════════════════════════════════════════════════


class _StubCoordinatorHandler(BaseHTTPRequestHandler):
    """A coordinator that answers /api/register with a canned body.

    This is how a gpumesh with no version gate at all is reproduced — which is
    every release up to 1.3.0, and therefore the coordinator most workers in
    the field are talking to today. Monkeypatching the real server's constants
    would not reproduce it: the gate would still be there.
    """

    register_response: dict = {"worker_id": "stub-worker"}
    timeout = 10

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        # _try_register probes this before registering.
        self._send(200, {"workers": []})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self._send(200, dict(self.register_response))


@pytest.fixture
def stub_coordinator():
    """Yields a factory: response body -> MeshClient pointed at a stub."""
    servers = []

    def make(register_response):
        handler = type("Handler", (_StubCoordinatorHandler,),
                       {"register_response": register_response})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return MeshClient(f"http://127.0.0.1:{httpd.server_address[1]}", TOKEN)

    yield make
    for httpd in servers:
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass


class TestWorkerSideCheck:
    def test_unversioned_coordinator_is_accepted(self, stub_coordinator):
        """The mirror of test_absent_version_registers, from the other end.

        A current worker joining a 1.3.0 coordinator gets a response with no
        protocol_version in it. That is the pre-handshake protocol, which is
        inside today's window, so the worker joins.
        """
        client = stub_coordinator({"worker_id": "old-coordinator-worker"})
        assert _try_register(client, dict(BASE_INFO), retries=1,
                             verbose=False) == "old-coordinator-worker"

    def test_coordinator_too_new_is_refused(self, stub_coordinator):
        """The half of the skew the coordinator's own gate cannot catch.

        A coordinator older than this worker has no gate — it accepts the
        registration and then behaves however the difference makes it behave.
        The worker has to check the version it was told about.
        """
        future = PROTOCOL_VERSION + 5
        client = stub_coordinator({"worker_id": "x", "protocol_version": future})
        with pytest.raises(ProtocolVersionMismatch) as exc_info:
            _try_register(client, dict(BASE_INFO), retries=1, verbose=False)
        message = str(exc_info.value)
        assert str(future) in message
        assert str(PROTOCOL_VERSION) in message
        assert "WORKER" in message, "the worker is the older side here"
        assert exc_info.value.remote == future
        assert exc_info.value.local == PROTOCOL_VERSION

    def test_unparseable_coordinator_version_is_refused(self, stub_coordinator):
        client = stub_coordinator({"worker_id": "x", "protocol_version": "?"})
        with pytest.raises(ProtocolVersionMismatch):
            _try_register(client, dict(BASE_INFO), retries=1, verbose=False)

    def test_refusal_is_not_a_value_error(self, mesh, monkeypatch):
        """The named exception, not a bare ValueError, and no retry storm."""
        monkeypatch.setattr(worker_mod, "PROTOCOL_VERSION", PROTOCOL_VERSION + 5)
        started = time.monotonic()
        with pytest.raises(ProtocolVersionMismatch):
            _try_register(mesh, dict(BASE_INFO), retries=3, verbose=False)
        # Three retries would sleep 2s each. Neither machine's version changes
        # while we sleep, so the refusal must be immediate.
        assert time.monotonic() - started < 2.0


# ══════════════════════════════════════════════════════════════════════
#  DOES IT REACH THE USER?
# ══════════════════════════════════════════════════════════════════════


def _run_worker_once(url, timeout=30.0):
    """Run run_worker in a thread and require it to return on its own."""
    state = {}

    def main():
        try:
            run_worker(url, TOKEN, persist_connection=False)
            state["outcome"] = "returned"
        except BaseException as exc:  # noqa: BLE001 - recorded, then asserted
            state["outcome"] = f"raised {type(exc).__name__}: {exc}"

    thread = threading.Thread(target=main, daemon=True, name="proto-test-worker")
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), (
        "run_worker did not return after a protocol refusal — it is retrying "
        "or blocked, which is the failure this handshake exists to remove"
    )
    return state


class TestRefusalReachesTheUser:
    def test_run_worker_prints_the_version_mismatch(self, mesh, fast_probe,
                                                    monkeypatch, capsys):
        """The regression that matters: not swallowed by the generic branch.

        ``run_worker``'s generic handlers answer a failed registration with
        "failed to register" plus a troubleshooting block about firewalls,
        10061 vs 10060 and "is the coordinator running?". Every word of that
        is wrong for a version skew and sends the operator to inspect a
        network that is working perfectly.
        """
        monkeypatch.setattr(worker_mod, "PROTOCOL_VERSION", PROTOCOL_VERSION + 5)
        state = _run_worker_once(mesh.base_url)
        assert state["outcome"] == "returned", state

        out = capsys.readouterr().out
        assert "protocol version mismatch" in out
        assert str(PROTOCOL_VERSION + 5) in out, "the worker's version"
        assert str(PROTOCOL_VERSION) in out, "the coordinator's version"
        assert "pip install -U gpumesh" in out
        # The generic branches must not have run.
        assert "failed to register" not in out
        assert "TROUBLESHOOTING" not in out
        assert "Firewall" not in out

    def test_matching_worker_still_joins(self, mesh, fast_probe):
        """The control: the same path with no skew registers normally."""
        stop = threading.Event()
        thread = threading.Thread(
            target=run_worker,
            kwargs={"url": mesh.base_url, "token": TOKEN,
                    "persist_connection": False, "stop_event": stop},
            daemon=True,
        )
        thread.start()
        try:
            deadline = time.time() + 30
            workers = []
            while time.time() < deadline and not workers:
                workers = mesh.call("GET", "/api/workers")["workers"]
                if not workers:
                    time.sleep(0.2)
            assert workers, "a matching worker never registered"
        finally:
            stop.set()
            thread.join(timeout=20)
