"""Tests for the opt-in TLS transport (issue #28).

The point of these is not that OpenSSL works. It is that the three ways a
worker can be told to trust the coordinator all behave as documented, that the
untrusted case fails with the *instruction* rather than the stock verify
error, and that turning on encryption does not accidentally turn off
authentication.
"""

import os
import ssl
import tempfile
import threading
import time

import pytest

from gpumesh import server, tls
from gpumesh.worker import MeshClient


@pytest.fixture
def coordinator():
    """A live TLS coordinator on an ephemeral port, torn down afterwards."""
    db_path = os.path.join(tempfile.mkdtemp(), "tls-test.db")
    httpd = server.serve("127.0.0.1", 0, db_path, "tls-test-token",
                         discovery=False, tls=True)
    port = httpd.socket.getsockname()[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    # serve_forever needs to reach its accept loop before the first connect,
    # or the client races it and sees a refusal that means nothing.
    time.sleep(0.3)
    try:
        yield httpd, port
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture(autouse=True)
def clean_tls_env(monkeypatch):
    monkeypatch.delenv("GPUMESH_TLS_CA", raising=False)
    monkeypatch.delenv("GPUMESH_TLS_INSECURE", raising=False)


class TestCertificateGeneration:

    def test_generates_a_reusable_pair(self, tmp_path):
        cert, key = tls.ensure_self_signed_cert(tmp_path)
        assert cert.exists() and key.exists()
        assert cert.read_bytes().startswith(b"-----BEGIN CERTIFICATE-----")
        # Idempotent: a restart must not invalidate a fingerprint a worker
        # was told to pin.
        again_cert, _ = tls.ensure_self_signed_cert(tmp_path)
        assert tls.fingerprint(again_cert) == tls.fingerprint(cert)

    def test_force_regenerates(self, tmp_path):
        cert, _ = tls.ensure_self_signed_cert(tmp_path)
        first = tls.fingerprint(cert)
        cert, _ = tls.ensure_self_signed_cert(tmp_path, force=True)
        assert tls.fingerprint(cert) != first

    def test_fingerprint_is_colon_separated_sha256(self, tmp_path):
        cert, _ = tls.ensure_self_signed_cert(tmp_path)
        fp = tls.fingerprint(cert)
        parts = fp.split(":")
        assert len(parts) == 32
        assert all(len(p) == 2 for p in parts)


class TestServerSide:

    def test_listener_is_tls_and_still_requires_the_token(self, coordinator,
                                                          monkeypatch):
        httpd, port = coordinator
        assert httpd.gpumesh_tls is True
        assert isinstance(httpd.socket, ssl.SSLSocket)

        monkeypatch.setenv("GPUMESH_TLS_INSECURE", "1")
        url = f"https://127.0.0.1:{port}"
        assert MeshClient(url, "tls-test-token").call("GET", "/api/workers") \
            == {"workers": []}

        import urllib.error
        with pytest.raises(urllib.error.HTTPError) as exc:
            MeshClient(url, "wrong-token").call("GET", "/api/workers")
        assert exc.value.code == 401

    def test_cert_without_key_is_refused_before_binding(self, tmp_path):
        cert, _ = tls.ensure_self_signed_cert(tmp_path)
        db_path = os.path.join(tempfile.mkdtemp(), "half.db")
        with pytest.raises(tls.TLSError):
            server.serve("127.0.0.1", 0, db_path, "t", discovery=False,
                         tls_cert=str(cert))


class TestClientTrust:

    def test_plain_http_gets_no_context(self):
        assert tls.client_context("http://127.0.0.1:8000") is None

    def test_ca_file_verifies(self, coordinator, monkeypatch):
        _, port = coordinator
        monkeypatch.setenv("GPUMESH_TLS_CA", str(_coordinator_cert(coordinator)))
        # 'localhost' rather than the IP: verification checks the SAN, and
        # this asserts the generated cert actually carries usable names.
        client = MeshClient(f"https://localhost:{port}", "tls-test-token")
        assert client.call("GET", "/api/workers") == {"workers": []}

    def test_missing_ca_file_says_so(self, monkeypatch):
        monkeypatch.setenv("GPUMESH_TLS_CA", "/nope/does-not-exist.pem")
        with pytest.raises(tls.TLSError, match="does not exist"):
            tls.client_context("https://127.0.0.1:8000")

    def test_untrusted_failure_carries_the_fix(self, coordinator):
        _, port = coordinator
        import urllib.error
        with pytest.raises(urllib.error.URLError) as exc:
            MeshClient(f"https://127.0.0.1:{port}", "tls-test-token").call(
                "GET", "/api/workers")
        message = str(exc.value)
        assert "GPUMESH_TLS_CA" in message
        assert "GPUMESH_TLS_INSECURE" in message

    def test_insecure_is_encrypted_but_unverified(self):
        context = tls.client_context("https://127.0.0.1:8000")
        assert context.verify_mode == ssl.CERT_REQUIRED
        os.environ["GPUMESH_TLS_INSECURE"] = "1"
        try:
            context = tls.client_context("https://127.0.0.1:8000")
            assert context.verify_mode == ssl.CERT_NONE
            assert context.check_hostname is False
        finally:
            del os.environ["GPUMESH_TLS_INSECURE"]

    def test_tls_floor_is_1_2(self, tmp_path):
        cert, key = tls.ensure_self_signed_cert(tmp_path)
        assert tls.server_context(cert, key).minimum_version \
            == ssl.TLSVersion.TLSv1_2
        assert tls.client_context("https://x").minimum_version \
            == ssl.TLSVersion.TLSv1_2


def _coordinator_cert(coordinator):
    httpd, _ = coordinator
    return httpd.gpumesh_tls_certfile
