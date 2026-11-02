"""Tests for Phase 0 pairing-code protocol (coordinator + quickjoin --code).

Two endpoints on the coordinator:
  POST /api/pair/request  (authed: mints a 6-digit single-use code, 5-min TTL)
  POST /api/pair/claim    (NO token: trades {code, ...} for {token})

Plus ``gpumesh quickjoin --code`` claiming a code and saving the token.
"""

import http.client
import json
import threading
import time

import pytest

from gpumesh import connection_manager
from gpumesh.security import PAIRING_CODE_TTL_SECONDS
from gpumesh.server import serve
from gpumesh.worker import MeshClient

TOKEN = "pairing-test-token"


@pytest.fixture
def coord(tmp_path):
    """A real coordinator plus its per-serve handler subclass."""
    httpd = serve("127.0.0.1", 0, str(tmp_path / "pairing.db"), TOKEN)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield {"port": port, "httpd": httpd,
           "handler": httpd.RequestHandlerClass}
    httpd.gpumesh_stop.set()
    httpd.shutdown()


@pytest.fixture
def remote(coord):
    """Treat this test's loopback connections as if they were remote.

    Same honest trick as the claim-server tests: real non-loopback traffic
    cannot be generated from a unit test, and the lockout exemption is keyed
    on the peer address, so the remote path is exercised by turning the
    exemption off.
    """
    coord["handler"].gpumesh_security.rate_limiter.exempt_loopback = False
    return coord


def _request(port, path, body=None, token=None, headers=None):
    """POST JSON with full control over headers. Returns (status, parsed)."""
    raw = json.dumps(body).encode() if body is not None else b""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30.0)
    try:
        conn.putrequest("POST", path, skip_accept_encoding=True)
        conn.putheader("Content-Type", "application/json")
        if token is not None:
            conn.putheader("X-Auth-Token", token)
        for name, value in (headers or {}).items():
            conn.putheader(name, value)
        conn.putheader("Content-Length", str(len(raw)))
        conn.endheaders()
        if raw:
            conn.send(raw)
        resp = conn.getresponse()
        payload = resp.read()
        try:
            return resp.status, json.loads(payload)
        except ValueError:
            return resp.status, None
    finally:
        conn.close()


def _request_code(port, token=TOKEN):
    """Mint a code over real HTTP. Returns the code string."""
    status, data = _request(port, "/api/pair/request", {}, token=token)
    assert status == 200
    return data["code"]


class TestPairRequest:
    """Minting stays behind the token."""

    def test_request_returns_a_6_digit_code_with_ttl(self, coord):
        status, data = _request(coord["port"], "/api/pair/request",
                                {}, token=TOKEN)
        assert status == 200
        assert (isinstance(data["code"], str) and len(data["code"]) == 6
                and data["code"].isdigit())
        assert data["expires_in"] == PAIRING_CODE_TTL_SECONDS

    def test_request_without_token_is_rejected(self, coord):
        status, _ = _request(coord["port"], "/api/pair/request", {})
        assert status == 401

    def test_request_with_wrong_token_is_rejected(self, coord):
        status, _ = _request(coord["port"], "/api/pair/request",
                             {}, token="guess")
        assert status == 401

    def test_each_request_mints_a_fresh_code(self, coord):
        assert _request_code(coord["port"]) != _request_code(coord["port"])


class TestPairClaimHappyPath:
    """The one 200 that may carry the token."""

    def test_claim_needs_no_token_header(self, coord):
        code = _request_code(coord["port"])
        status, data = _request(coord["port"], "/api/pair/claim",
                                {"code": code, "hostname": "new-gpu"})
        assert status == 200
        assert data == {"token": TOKEN}

    def test_claimed_token_authenticates(self, coord):
        code = _request_code(coord["port"])
        _, data = _request(coord["port"], "/api/pair/claim", {"code": code})
        client = MeshClient(f"http://127.0.0.1:{coord['port']}",
                            data["token"])
        assert client.call("GET", "/api/workers") == {"workers": []}

    def test_success_clears_failure_history(self, remote):
        port = remote["port"]
        sec = remote["handler"].gpumesh_security
        for _ in range(3):
            status, _ = _request(port, "/api/pair/claim",
                                 {"code": "000000"})
            assert status == 403
        assert sec.rate_limiter.get_remaining_attempts("127.0.0.1") == 2
        code = _request_code(port)
        status, _ = _request(port, "/api/pair/claim", {"code": code})
        assert status == 200
        assert sec.rate_limiter.get_remaining_attempts("127.0.0.1") == 5


class TestPairClaimFailures:
    """Wrong, used, and expired codes are one indistinguishable 403."""

    def test_wrong_code_is_403(self, coord):
        status, data = _request(coord["port"], "/api/pair/claim",
                                {"code": "000000"})
        assert status == 403
        assert "invalid or expired" in data["error"]

    def test_second_claim_of_a_code_fails(self, coord):
        code = _request_code(coord["port"])
        status, _ = _request(coord["port"], "/api/pair/claim",
                             {"code": code})
        assert status == 200
        status, data = _request(coord["port"], "/api/pair/claim",
                                {"code": code})
        assert status == 403
        assert "invalid or expired" in data["error"]

    def test_expired_code_fails(self, coord):
        code = _request_code(coord["port"])
        sec = coord["handler"].gpumesh_security
        sec.pairing._codes[code] = time.time() - 1  # force expiry
        status, data = _request(coord["port"], "/api/pair/claim",
                                {"code": code})
        assert status == 403
        assert "invalid or expired" in data["error"]

    def test_no_oracle_between_wrong_used_and_expired(self, coord):
        fresh_wrong = _request(coord["port"], "/api/pair/claim",
                               {"code": "000001"})
        code = _request_code(coord["port"])
        _request(coord["port"], "/api/pair/claim", {"code": code})
        used = _request(coord["port"], "/api/pair/claim", {"code": code})
        code2 = _request_code(coord["port"])
        coord["handler"].gpumesh_security.pairing._codes[code2] = (
            time.time() - 1)
        expired = _request(coord["port"], "/api/pair/claim",
                           {"code": code2})
        assert fresh_wrong[0] == used[0] == expired[0] == 403
        assert fresh_wrong[1] == used[1] == expired[1]

    def test_missing_code_is_a_400_shape_error(self, coord):
        status, data = _request(coord["port"], "/api/pair/claim",
                                {"hostname": "no-code-here"})
        assert status == 400
        assert "code" in data["error"]

    def test_shape_errors_do_not_burn_the_lockout_budget(self, remote):
        port = remote["port"]
        sec = remote["handler"].gpumesh_security
        for _ in range(5):
            status, _ = _request(port, "/api/pair/claim",
                                 {"hostname": "typo-shape"})
            assert status == 400
        assert sec.rate_limiter.is_allowed("127.0.0.1") is True


class TestPairClaimLockout:
    """Failed claims feed the SAME IP lockout that guards the token."""

    def test_six_rapid_wrong_codes_trigger_lockout(self, remote):
        port = remote["port"]
        for _ in range(5):
            status, _ = _request(port, "/api/pair/claim",
                                 {"code": "000000"})
            assert status == 403
        status, data = _request(port, "/api/pair/claim",
                                {"code": "000000"})
        assert status == 429
        assert "not checked" in data["error"]

    def test_locked_out_caller_cannot_claim_even_with_a_live_code(
            self, remote):
        port = remote["port"]
        code = _request_code(port)
        for _ in range(5):
            _request(port, "/api/pair/claim", {"code": "000000"})
        status, _ = _request(port, "/api/pair/claim", {"code": code})
        assert status == 429
        # And the code is still live: the lockout gates the body, so nothing
        # was consumed.
        sec = remote["handler"].gpumesh_security
        assert sec.pairing.redeem(code) is True

    def test_loopback_keeps_its_exemption_by_default(self, coord):
        for _ in range(8):
            status, _ = _request(coord["port"], "/api/pair/claim",
                                 {"code": "000000"})
            assert status == 403
        code = _request_code(coord["port"])
        status, data = _request(coord["port"], "/api/pair/claim",
                                {"code": code})
        assert status == 200
        assert data["token"] == TOKEN

    def test_proxied_loopback_is_rate_limited(self, coord):
        # What ngrok (--public) looks like: loopback peer + X-Forwarded-For.
        # Before the fix all 8 of these were 403 and none 429.
        port, xff = coord["port"], {"X-Forwarded-For": "203.0.113.9"}
        for _ in range(5):
            status, _ = _request(port, "/api/pair/claim",
                                 {"code": "000000"}, headers=xff)
            assert status == 403
        status, _ = _request(port, "/api/pair/claim",
                             {"code": "000000"}, headers=xff)
        assert status == 429
        # Same bucket for the token path, whatever the far client claims.
        status, _ = _request(port, "/api/pair/request", {}, token=TOKEN,
                             headers={"X-Forwarded-For": "198.51.100.1"})
        assert status == 401
        # The operator's own unproxied loopback is untouched.
        code = _request_code(port)
        status, _ = _request(port, "/api/pair/claim", {"code": code})
        assert status == 200


class TestTokenNeverLeaks:
    """The token leaves in exactly one 200 body and no log line."""

    def test_failure_bodies_never_contain_the_token(self, remote):
        port = remote["port"]
        # Mint first: after the lockout trips below, even authed minting is
        # (correctly) refused, so there would be no code left to test with.
        code = _request_code(port)
        bodies = []
        for _ in range(5):
            _, data = _request(port, "/api/pair/claim", {"code": "000000"})
            bodies.append(json.dumps(data))
        # Under lockout now: even the live code and the shape error get 429
        # without being examined. Every one of these bodies must be clean.
        for body in ({"code": code}, {"code": "000000"},
                     {"hostname": "x"}):
            _, data = _request(port, "/api/pair/claim", body)
            bodies.append(json.dumps(data))
        for text in bodies:
            assert TOKEN not in text

    def test_claim_attempts_never_reach_the_server_log(self, coord,
                                                       monkeypatch):
        from gpumesh import server as server_module

        lines = []
        monkeypatch.setattr(server_module.status, "log", lines.append)
        code = _request_code(coord["port"])
        _request(coord["port"], "/api/pair/claim", {"code": code})
        _request(coord["port"], "/api/pair/claim", {"code": code})
        _request(coord["port"], "/api/pair/claim", {"code": "000000"})
        for line in lines:
            assert TOKEN not in line
            assert code not in line


class TestQuickjoinCode:
    """``gpumesh quickjoin --code`` claims and saves, then joins."""

    def _run(self, argv, **kw):
        from unittest.mock import patch

        from gpumesh.cli import main

        with patch("sys.argv", ["gpumesh", "quickjoin", *argv]), \
             patch("gpumesh.cli._run_worker_with_report") as mock_join:
            try:
                main()
            except SystemExit as exc:
                return mock_join, exc
            return mock_join, None

    def test_code_with_url_claims_saves_and_joins(self, coord):
        port = coord["port"]
        url = f"http://127.0.0.1:{port}"
        code = _request_code(port)
        mock_join, exited = self._run(["--code", code, url])
        assert exited is None
        mock_join.assert_called_once()
        assert mock_join.call_args[0][0] == url
        assert mock_join.call_args[0][1] == TOKEN
        saved = connection_manager.load_connection()
        assert saved["url"] == url
        assert saved["token"] == TOKEN

    def test_code_uses_saved_url_when_none_given(self, coord):
        port = coord["port"]
        url = f"http://127.0.0.1:{port}"
        connection_manager.save_connection(url, "stale-token")
        code = _request_code(port)
        mock_join, exited = self._run(["--code", code])
        assert exited is None
        mock_join.assert_called_once()
        assert mock_join.call_args[0][0] == url
        assert mock_join.call_args[0][1] == TOKEN
        assert connection_manager.load_connection()["token"] == TOKEN

    def test_code_without_url_or_saved_connection_fails(self, coord):
        _, exited = self._run(["--code", "123456"])
        assert exited is not None and exited.code == 1

    def test_wrong_code_fails_without_joining_or_saving(self, coord):
        from unittest.mock import patch

        port = coord["port"]
        url = f"http://127.0.0.1:{port}"
        with patch("sys.argv", ["gpumesh", "quickjoin", "--code",
                                "000000", url]), \
             patch("gpumesh.cli._run_worker_with_report") as mock_join:
            with pytest.raises(SystemExit) as exc_info:
                from gpumesh.cli import main
                main()
            assert exc_info.value.code == 1
        mock_join.assert_not_called()
        assert connection_manager.load_connection() is None

    def test_code_and_token_together_is_rejected(self, coord):
        port = coord["port"]
        _, exited = self._run(["--code", "123456", "--token", "abc",
                               f"http://127.0.0.1:{port}"])
        assert exited is not None and exited.code == 1

    def test_neither_code_nor_token_is_rejected(self, coord):
        port = coord["port"]
        _, exited = self._run([f"http://127.0.0.1:{port}"])
        assert exited is not None and exited.code == 1

    def test_existing_token_path_is_unchanged(self, coord):
        port = coord["port"]
        url = f"http://127.0.0.1:{port}"
        mock_join, exited = self._run(["--token", "test123", url])
        assert exited is None
        mock_join.assert_called_once()
        assert mock_join.call_args[0] == (url, "test123", 240.0)
        assert mock_join.call_args[1] == {"safe_mode": False}
