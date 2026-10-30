"""Regression tests for the security-audit fixes.

Each class pins one finding: the setup-wizard claim challenge, the empty
token, the --public loopback exemption, the TLS accept-loop stall, beacon
control characters, the unbounded ticker log, the rate-limiter race, the
pre-auth header deadline, the %mesh_status token prefix (#83) and the
world-readable job database (#56).
"""

import json
import math
import os
import socket
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.request

import pytest

from gpumesh import claimer, discovery, server, status
from gpumesh.security import RateLimiter, SecurityManager


def _serve(token="audit-token", **kw):
    db_path = os.path.join(tempfile.mkdtemp(), "audit.db")
    httpd = server.serve("127.0.0.1", 0, db_path, token, discovery=False, **kw)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.2)
    return httpd, httpd.socket.getsockname()[1]


def _stop(httpd):
    httpd.shutdown()
    httpd.server_close()


class TestClaimChallenge:
    """A spoofed beacon must never receive the coordinator token."""

    def test_worker_answers_challenge_with_hmac(self):
        httpd, port = claimer.start_claim_server("worker-token-123", port=0,
                                                 bind_host="127.0.0.1")
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            nonce = "ab" * 32
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}{claimer.CLAIM_CHALLENGE_PATH}",
                data=json.dumps({"nonce": nonce}).encode(), method="POST",
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read())
            assert body["proof"] == claimer.claim_proof("worker-token-123", nonce)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_verify_accepts_real_worker_and_rejects_wrong_token(self):
        from gpumesh.setup_wizard import _verify_claim_peer

        httpd, port = claimer.start_claim_server("worker-token-123", port=0,
                                                 bind_host="127.0.0.1")
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            assert _verify_claim_peer("127.0.0.1", port, "worker-token-123")[0]
            ok, why = _verify_claim_peer("127.0.0.1", port, "some-other-token")
            assert not ok and "proof" in why
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_spoofed_claim_server_never_receives_coordinator_token(self):
        """A fake worker that does not know the token must be refused before
        anything secret is sent. Here the fake answers every POST with ok."""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        from gpumesh.setup_wizard import _verify_claim_peer

        received = []

        class Fake(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                received.append(self.rfile.read(n))
                data = json.dumps({"ok": True, "proof": "0" * 64}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        fake = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        try:
            ok, _ = _verify_claim_peer("127.0.0.1", fake.server_address[1],
                                       "worker-token-123")
            assert not ok
            assert all(b"token" not in body for body in received)
        finally:
            fake.shutdown()
            fake.server_close()

    def test_challenge_does_not_reset_claim_failure_count(self):
        limiter = RateLimiter(exempt_loopback=False)
        httpd, port = claimer.start_claim_server("worker-token-123", port=0,
                                                 bind_host="127.0.0.1")
        claimer.ClaimHandler._rate_limiter = limiter
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        def post(path, body):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
                method="POST", headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status
            except urllib.error.HTTPError as exc:
                return exc.code

        try:
            for _ in range(4):
                assert post("/api/claim", {"token": "wrong"}) == 401
                assert post(claimer.CLAIM_CHALLENGE_PATH, {"nonce": "ab" * 32}) == 200
            assert post("/api/claim", {"token": "wrong"}) == 401
            assert post("/api/claim", {"token": "wrong"}) == 429
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestEmptyToken:

    def test_security_manager_rejects_empty_token(self):
        for bad in ("", "   "):
            with pytest.raises(ValueError):
                SecurityManager(bad)

    def test_start_coordinator_replaces_empty_token(self, tmp_path):
        from gpumesh.api import GPUMesh

        handle = GPUMesh.start_coordinator(port=0, token="",
                                           db_path=str(tmp_path / "e.db"),
                                           self_worker=False)
        try:
            assert str(handle).strip()
            port = handle.gpumesh_httpd.server_address[1]
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/workers")
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(req, timeout=5)
            assert exc.value.code == 401
        finally:
            handle.gpumesh_shutdown()


class TestTunnelledLoopback:

    def test_tunnelled_coordinator_locks_out_loopback(self):
        httpd, port = _serve(tunnelled=True)
        try:
            limiter = httpd.RequestHandlerClass.gpumesh_security.rate_limiter
            assert limiter.exempt_loopback is False
            codes = []
            for i in range(6):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/workers",
                    headers={"X-Auth-Token": f"wrong{i}"})
                try:
                    urllib.request.urlopen(req, timeout=10)
                except urllib.error.HTTPError as exc:
                    codes.append(json.loads(exc.read()).get("error", ""))
            assert "Too many attempts" in codes[-1]
        finally:
            _stop(httpd)

    def test_default_coordinator_keeps_loopback_exemption(self):
        httpd, _ = _serve()
        try:
            assert httpd.RequestHandlerClass.gpumesh_security.rate_limiter.exempt_loopback
        finally:
            _stop(httpd)


class TestTlsAcceptLoop:

    def test_idle_connection_does_not_stall_tls_coordinator(self, monkeypatch):
        monkeypatch.setenv("GPUMESH_TLS_INSECURE", "1")
        from gpumesh.worker import MeshClient

        httpd, port = _serve(tls=True)
        idle = socket.create_connection(("127.0.0.1", port))
        try:
            time.sleep(0.3)
            start = time.monotonic()
            result = MeshClient(f"https://127.0.0.1:{port}", "audit-token").call(
                "GET", "/api/workers", timeout=4)
            assert result == {"workers": []}
            assert time.monotonic() - start < 3
        finally:
            idle.close()
            _stop(httpd)


class TestBeaconSanitizing:

    def test_control_characters_are_stripped(self):
        peer = discovery.Peer({
            "type": "gpumesh_worker",
            "hostname": "\x1b[2J\x1b]52;c;ZHVtbXk=\x07trusted-gpu\r",
            "device": "cuda\x1b[8m",
            "device_name": "RTX\x9b4090",
            "platform": "lin\x07ux",
        }, ("127.0.0.1", 48900))
        for value in (peer.hostname, peer.device, peer.device_name, peer.platform):
            assert all(c.isprintable() for c in value), repr(value)
        assert peer.hostname.endswith("trusted-gpu")

    def test_all_control_hostname_falls_back(self):
        peer = discovery.Peer({"type": "gpumesh_worker", "hostname": "\x1b\x07"},
                              ("127.0.0.1", 48900))
        assert peer.hostname == "unknown"

    @pytest.mark.parametrize("score", [float("nan"), float("inf"), 1e308, -5])
    def test_score_is_clamped(self, score):
        peer = discovery.Peer({"type": "gpumesh_worker", "score": score},
                              ("127.0.0.1", 48900))
        assert math.isfinite(peer.score) and 0 <= peer.score <= discovery._MAX_SCORE


class TestTickerLogBounded:

    def test_buffer_is_bounded(self):
        status.set_active(True)
        try:
            for i in range(10_000):
                status.log(f"line {i}")
            assert len(status._log_lines) <= status._LOG_KEEP
            assert status.snapshot() == [f"line {i}" for i in range(9_994, 10_000)]
        finally:
            status.set_active(False)


class TestRateLimiterRace:

    def test_try_admit_reserves_attempts(self):
        limiter = RateLimiter(max_attempts=5, exempt_loopback=False)
        admitted = [limiter.try_admit("203.0.113.7") for _ in range(10)]
        assert admitted == [True] * 5 + [False] * 5

    def test_concurrent_wrong_tokens_checked_at_most_max_attempts(self):
        mgr = SecurityManager("right-token", kdf_iterations=1000)
        mgr.rate_limiter.exempt_loopback = False
        results = []
        barrier = threading.Barrier(20)

        def guess(i):
            barrier.wait()
            results.append(mgr.verify_request(f"wrong{i}", "203.0.113.7")[1])

        threads = [threading.Thread(target=guess, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        checked = [r for r in results if r.startswith("Invalid token")]
        assert len(checked) <= 5


class TestHeaderDeadline:

    def test_trickled_headers_are_cut_off(self, monkeypatch):
        httpd, port = _serve()
        monkeypatch.setattr(httpd.RequestHandlerClass, "HEADER_DEADLINE", 1.0)
        sock = socket.create_connection(("127.0.0.1", port))
        sock.sendall(b"GET /api/health HTTP/1.0\r\nX-Pad: ")
        try:
            closed = False
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                try:
                    sock.sendall(b"a")
                    sock.settimeout(0.3)
                    if sock.recv(1) == b"":
                        closed = True
                        break
                except socket.timeout:
                    continue
                except OSError:
                    closed = True
                    break
            assert closed
        finally:
            sock.close()
            _stop(httpd)


class TestMeshStatusHidesToken:

    def test_token_not_printed(self, monkeypatch, capsys):
        from gpumesh import connection_manager, jupyter_magic

        token = "dummypass10"
        monkeypatch.setattr(connection_manager, "load_connection",
                            lambda: {"url": "http://127.0.0.1:9", "token": token})
        monkeypatch.setattr(jupyter_magic, "_mesh_functions",
                            lambda: (lambda: None, None, lambda: 1, None, None))
        jupyter_magic._mesh_status("")
        out = capsys.readouterr().out
        assert "11 chars" in out
        assert all(token[i:i + 4] not in out for i in range(len(token) - 3))


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
class TestDatabasePermissions:

    def test_db_and_sidecars_are_owner_only(self, tmp_path):
        from gpumesh.db import Database

        old = os.umask(0o022)
        try:
            path = str(tmp_path / "jobs.db")
            db = Database(path)
            db.create_job("dummy", "print(1)", [{"x": 1}])
        finally:
            os.umask(old)
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                assert stat.S_IMODE(os.stat(path + suffix).st_mode) == 0o600, suffix

    def test_existing_world_readable_db_is_tightened(self, tmp_path):
        from gpumesh.db import Database

        path = str(tmp_path / "old.db")
        Database(path)
        os.chmod(path, 0o644)
        Database(path)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_config_dir_is_owner_only(self, tmp_path, monkeypatch):
        from gpumesh import connection_manager

        target = tmp_path / "gm"
        target.mkdir(mode=0o755)
        monkeypatch.setattr(connection_manager, "_CONFIG_DIR", str(target))
        connection_manager._ensure_dir()
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o700

    def test_generated_tls_key_is_owner_only(self, tmp_path):
        from gpumesh import tls

        _, key = tls.ensure_self_signed_cert(tmp_path)
        assert stat.S_IMODE(os.stat(key).st_mode) == 0o600
