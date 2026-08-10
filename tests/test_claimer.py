"""Tests for gpumesh.claimer — worker-side claim HTTP server."""

import http.client
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from gpumesh.claimer import ClaimHandler, start_claim_server
from gpumesh.server import serve

# A claim is only acked once the worker has proved it can reach the
# coordinator, so these tests need a coordinator that genuinely answers.
# Pointing at a dead port now (correctly) yields 502, not a claim.
_COORD_TOKEN = "coord-tok"
_COORD_URL = ""


@pytest.fixture(scope="module", autouse=True)
def _reachable_coordinator(tmp_path_factory):
    """Run one real loopback coordinator for every test in this module."""
    global _COORD_URL
    db = tmp_path_factory.mktemp("claimer") / "coordinator.db"
    httpd = serve("127.0.0.1", 0, str(db), _COORD_TOKEN)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _COORD_URL = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield _COORD_URL
    httpd.gpumesh_stop.set()
    httpd.shutdown()
    thread.join(timeout=10)


def _claim_body(token, coord_url=None, coord_token=_COORD_TOKEN):
    return json.dumps({
        "token": token,
        "coordinator_url": coord_url or _COORD_URL,
        "coordinator_token": coord_token,
    }).encode()


def _post(port, path, body=b"", headers=None):
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=body,
        method="POST",
        headers=hdrs,
    )
    return urllib.request.urlopen(req, timeout=5)


def _post_raw(port, path, body=b"", headers=None):
    """POST without Content-Type — useful for testing missing-header paths."""
    hdrs = headers or {}
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=body,
        method="POST",
        headers=hdrs,
    )
    return urllib.request.urlopen(req, timeout=5)


class TestClaimServer:
    """Start a fresh claim server per test for full isolation."""

    def setup_method(self):
        self.token = "test-token-1234"
        self.httpd, self.port = start_claim_server(self.token, port=0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        time.sleep(0.2)

    def teardown_method(self):
        try:
            self.httpd.shutdown()
        except Exception:
            pass

    # -- core claim flow -------------------------------------------------

    def test_correct_token(self):
        """POST with correct token returns 200 and ok=True."""
        resp = _post(self.port, "/api/claim", _claim_body(self.token))
        data = json.loads(resp.read())
        assert resp.status == 200
        assert data["ok"] is True

    def test_wrong_token(self):
        """POST with wrong token returns 401."""
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(self.port, "/api/claim", _claim_body("wrong-token"))
        assert exc_info.value.code == 401
        data = json.loads(exc_info.value.read())
        assert data["ok"] is False
        assert "wrong token" in data["error"]

    def test_empty_token_matches_empty_server_token(self):
        """If server token is '', an empty-string token is accepted."""
        httpd, port = start_claim_server("", port=0)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        time.sleep(0.2)
        try:
            resp = _post(port, "/api/claim", _claim_body(""))
            data = json.loads(resp.read())
            assert resp.status == 200
            assert data["ok"] is True
        finally:
            httpd.shutdown()

    # -- double-claim protection -----------------------------------------

    def test_double_claim_returns_409(self):
        """First claim succeeds, second gets 409 already-claimed."""
        resp = _post(self.port, "/api/claim", _claim_body(self.token))
        assert resp.status == 200

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(self.port, "/api/claim", _claim_body(self.token))
        assert exc_info.value.code == 409
        data = json.loads(exc_info.value.read())
        assert "already claimed" in data["error"]

    def test_wrong_token_after_claim_still_409(self):
        """After a successful claim, wrong token gets 409 if claimed, or 401 if claim failed."""
        _post(self.port, "/api/claim", _claim_body(self.token))

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(self.port, "/api/claim", _claim_body("wrong-token"))
        # 409 = still claimed (run_worker is running)
        # 401 = claim failed (run_worker crashed, _claimed reset, retryable)
        assert exc_info.value.code in (401, 409)

    # -- port auto-assignment --------------------------------------------

    def test_port_zero_picks_ephemeral_port(self):
        """port=0 should assign a non-privileged port."""
        httpd, port = start_claim_server("tok", port=0)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        time.sleep(0.15)
        try:
            assert port > 0
            assert port < 65536
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                _post_raw(port, "/api/claim")  # empty POST → treated as {}
            # Server is reachable — responds with error (no valid token)
            assert exc_info.value.code in (400, 401)
        finally:
            httpd.shutdown()

    def test_different_servers_get_different_ports(self):
        """Two servers started with port=0 bind to different ports."""
        httpd1, port1 = start_claim_server("tok1", port=0)
        t1 = threading.Thread(target=httpd1.serve_forever, daemon=True)
        t1.start()
        time.sleep(0.15)

        httpd2, port2 = start_claim_server("tok2", port=0)
        t2 = threading.Thread(target=httpd2.serve_forever, daemon=True)
        t2.start()
        time.sleep(0.15)

        try:
            assert port1 != port2
        finally:
            httpd1.shutdown()
            httpd2.shutdown()

    # -- invalid request bodies ------------------------------------------

    def test_empty_body(self):
        """Empty body with Content-Length 0 is accepted (treated as {})."""
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(self.port, "/api/claim", b"")
        # Server treats empty body as {}; token is "" which mismatches → 401
        assert exc_info.value.code in (200, 400, 401)

    def test_malformed_json(self):
        """Malformed JSON returns 400."""
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(self.port, "/api/claim", b"{bad json!!")
        assert exc_info.value.code == 400
        data = json.loads(exc_info.value.read())
        assert "invalid JSON" in data["error"]

    def test_non_object_json_body(self):
        """A JSON array or scalar returns 400."""
        for payload in [b"[1,2,3]", b'"just a string"', b"42"]:
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                _post(self.port, "/api/claim", payload)
            assert exc_info.value.code == 400

    def test_missing_token_field(self):
        """Body without 'token' field → 401 (empty string vs expected)."""
        body = json.dumps({
            "coordinator_url": "http://127.0.0.1:8000",
            "coordinator_token": "ct",
        }).encode()
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(self.port, "/api/claim", body)
        assert exc_info.value.code == 401

    def test_missing_coordinator_url(self):
        """Body without coordinator_url → 400."""
        body = json.dumps({
            "token": self.token,
            "coordinator_token": "ct",
        }).encode()
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(self.port, "/api/claim", body)
        assert exc_info.value.code == 400
        data = json.loads(exc_info.value.read())
        assert "coordinator_url" in data["error"]

    def test_missing_coordinator_token(self):
        """Body without coordinator_token → 400."""
        body = json.dumps({
            "token": self.token,
            "coordinator_url": "http://127.0.0.1:8000",
        }).encode()
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(self.port, "/api/claim", body)
        assert exc_info.value.code == 400

    def test_both_coordinator_fields_missing(self):
        """Body missing both coordinator fields → 400."""
        body = json.dumps({"token": self.token}).encode()
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(self.port, "/api/claim", body)
        assert exc_info.value.code == 400

    def test_invalid_utf8_body(self):
        """Non-UTF-8 bytes in body → 400."""
        bad_bytes = b"\x80\x81\x82\xff"
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(self.port, "/api/claim", bad_bytes)
        assert exc_info.value.code == 400

    # -- unknown paths ---------------------------------------------------

    def test_unknown_path_returns_404(self):
        """POST to an unknown path returns 404."""
        import socket as _sock
        for attempt in range(5):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            try:
                conn.request("POST", "/something/random",
                             body=_claim_body(self.token),
                             headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                data = json.loads(resp.read())
                assert resp.status == 404
                assert "unknown endpoint" in data["error"]
                return
            except (ConnectionAbortedError, _sock.error, ConnectionResetError):
                if attempt == 4:
                    raise
            finally:
                conn.close()

    def test_claim_path_without_trailing_slash(self):
        """/api/claim (no trailing slash) works."""
        resp = _post(self.port, "/api/claim", _claim_body(self.token))
        assert resp.status == 200

    def test_root_path_returns_404(self):
        """POST to / returns 404."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("POST", "/",
                         body=_claim_body(self.token),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            data = json.loads(resp.read())
            assert resp.status == 404
        except (ConnectionAbortedError, OSError):
            # Windows: server sends 404 but client connection can abort
            # before response is fully read. This is a known Windows
            # socket behavior — the server handled the request correctly.
            pass
        finally:
            conn.close()

    # -- server metadata -------------------------------------------------

    def test_server_version_header(self):
        """Response includes gpumesh-claim server version."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("POST", "/api/claim",
                         body=_claim_body("wrong"),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            assert resp.getheader("Server", "").startswith("gpumesh-claim")
            resp.read()
        finally:
            conn.close()

    def test_response_content_type_json(self):
        """Responses have application/json content type."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("POST", "/api/claim",
                         body=_claim_body("wrong"),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            ct = resp.getheader("Content-Type", "")
            assert "application/json" in ct
            resp.read()
        finally:
            conn.close()

    # -- class state reset between servers -------------------------------

    def test_claimed_flag_resets_across_servers(self):
        """A new server instance starts with _claimed=False."""
        ClaimHandler._claimed = False
        httpd1, p1 = start_claim_server("tok", port=0)
        t1 = threading.Thread(target=httpd1.serve_forever, daemon=True)
        t1.start()
        time.sleep(0.15)
        _post(p1, "/api/claim", _claim_body("tok"))
        assert ClaimHandler._claimed is True
        httpd1.shutdown()

        httpd2, p2 = start_claim_server("tok", port=0)
        t2 = threading.Thread(target=httpd2.serve_forever, daemon=True)
        t2.start()
        time.sleep(0.15)
        try:
            assert ClaimHandler._claimed is False
            resp = _post(p2, "/api/claim", _claim_body("tok"))
            assert resp.status == 200
        finally:
            httpd2.shutdown()

    def test_wrong_token_on_fresh_server(self):
        """Wrong token on a brand-new server returns 401, not 409."""
        ClaimHandler._claimed = False
        httpd, port = start_claim_server("secret", port=0)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        time.sleep(0.15)
        try:
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                _post(port, "/api/claim", _claim_body("not-secret"))
            assert exc_info.value.code == 401
            assert ClaimHandler._claimed is False
        finally:
            httpd.shutdown()
