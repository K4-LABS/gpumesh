"""Tests for gpumesh.claimer — worker-side claim HTTP server."""

import http.client
import json
import pathlib
import re
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from gpumesh.claimer import ClaimHandler, start_claim_server
from gpumesh.security import RateLimiter
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


@pytest.fixture(autouse=True)
def _restore_claim_handler_class_state():
    """ClaimHandler keeps its state on the class, so every test here leaks it.

    ``start_claim_server`` assigns ``_token``, ``_claimed`` and a fresh
    ``_rate_limiter`` onto ClaimHandler itself — there is no per-serve
    subclass of the kind ``server.serve`` builds — and several tests below
    deliberately swap in a stricter limiter or a patched ``do_POST``. Any of
    that surviving a test follows the session into every later file that
    drives the same class, tests/test_worker_resilience.py included. Most
    tests happen to overwrite it again on their way in, which is exactly what
    makes the leak easy to miss.
    """
    names = ("_token", "_claimed", "_rate_limiter", "do_POST", "_worker_thread")
    saved = {name: getattr(ClaimHandler, name) for name in names}
    yield
    for name, value in saved.items():
        setattr(ClaimHandler, name, value)


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


def _wait_until_accepting(port, timeout=10.0):
    """Block until the claim server on *port* actually accepts a connection.

    The sleep this replaces was a guess at how long ``serve_forever`` needs to
    reach its accept loop — too long on every machine where it was already
    listening, and silently too short on a loaded CI runner. Connecting is the
    condition the tests below actually depend on, so wait for that instead.
    """
    deadline = time.monotonic() + timeout
    last_exc = None
    while time.monotonic() < deadline:
        try:
            import socket as _socket

            _socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError as exc:  # not listening yet
            last_exc = exc
            time.sleep(0.02)
    raise AssertionError(f"claim server on port {port} never accepted: {last_exc}")


# A body deliberately over the *intended* 16 KB cap, written as a number
# rather than as ``MAX_CONTENT_LENGTH + n``.
#
# Both forms are needed and neither replaces the other, so please do not
# "simplify" one into the other:
#
#   * relative ("cap + 4 KB", used by
#     ``test_oversized_body_is_never_parsed_or_claimed``) pins the *behaviour*
#     — an over-cap body is refused before it is parsed — and stays correct if
#     the cap is ever deliberately retuned;
#   * absolute (this constant) pins the *value*. A payload sized from the
#     constant grows with it, so restoring the inherited 1 MB cap would leave
#     every relative test green while a 64x larger unauthenticated allocation
#     quietly became legal again. That is exactly the regression this port's
#     hardening exists to prevent.
_OVER_16KB = 20 * 1024


class TestClaimServer:
    """Start a fresh claim server per test for full isolation."""

    def setup_method(self):
        self.token = "test-token-1234"
        self.httpd, self.port = start_claim_server(self.token, port=0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        _wait_until_accepting(self.port)

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
        _wait_until_accepting(port)
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
        _wait_until_accepting(port)
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
        _wait_until_accepting(port1)

        httpd2, port2 = start_claim_server("tok2", port=0)
        t2 = threading.Thread(target=httpd2.serve_forever, daemon=True)
        t2.start()
        _wait_until_accepting(port2)

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
        _wait_until_accepting(p1)
        _post(p1, "/api/claim", _claim_body("tok"))
        assert ClaimHandler._claimed is True
        httpd1.shutdown()

        httpd2, p2 = start_claim_server("tok", port=0)
        t2 = threading.Thread(target=httpd2.serve_forever, daemon=True)
        t2.start()
        _wait_until_accepting(p2)
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
        _wait_until_accepting(port)
        try:
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                _post(port, "/api/claim", _claim_body("not-secret"))
            assert exc_info.value.code == 401
            assert ClaimHandler._claimed is False
        finally:
            httpd.shutdown()


def _raw_request(port, headers=None, body=b"", path="/api/claim",
                 raw_after_headers=None, timeout=10):
    """Send a hand-built POST and return (status, parsed body or None).

    http.client rather than urllib because these tests need to control the
    framing headers that urllib insists on setting correctly.
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


class TestClaimBodyLimits:
    """The claim body is parsed before the token can be checked, because the
    token is *in* the body. So the parse itself has to be the thing that is
    safe: bounded, framed explicitly, and refused on the declared length
    before anything is allocated.
    """

    def setup_method(self):
        self.token = "limits-token-1234"
        self.httpd, self.port = start_claim_server(self.token, port=0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        _wait_until_accepting(self.port)

    def teardown_method(self):
        try:
            self.httpd.shutdown()
        except Exception:
            pass

    def test_cap_is_small_enough_to_be_meaningful(self):
        """A claim is under a kilobyte; the cap must not be a formality."""
        assert ClaimHandler.MAX_CONTENT_LENGTH <= 64 * 1024
        assert ClaimHandler.MAX_CONTENT_LENGTH > len(_claim_body(self.token)) * 4

    def test_drain_budget_still_covers_a_modest_overshoot(self):
        """The two limits are a pair, and raising the cap breaks the pair.

        ``_drain`` refuses outright above ``MAX_DRAIN_BYTES``, and a refused
        drain means the connection closes with the peer's bytes unread — which
        on Windows is an RST, so the 413 the server just wrote is lost. A cap
        that grew past the drain budget would therefore turn every over-cap
        request into a connection reset instead of a readable error, silently.
        The coordinator asserts the same pair in test_api_edge_cases.py.
        """
        assert ClaimHandler.MAX_DRAIN_BYTES > ClaimHandler.MAX_CONTENT_LENGTH
        assert (ClaimHandler.MAX_DRAIN_BYTES
                <= 16 * ClaimHandler.MAX_CONTENT_LENGTH)
        assert 0 < ClaimHandler.DRAIN_TIMEOUT <= 10

    def test_oversized_body_returns_413(self):
        """A body over the cap is refused, and the refusal is readable.

        Sending the bytes for real (rather than only declaring them) is what
        proves the server drains instead of resetting the connection: if it
        closed on us mid-write we would never get to read the 413.

        The size is the absolute ``_OVER_16KB``, not ``MAX_CONTENT_LENGTH +
        n`` — see that constant for why. Sized from the cap, this test would
        keep passing if the cap were raised back to 1 MB.
        """
        oversized = b"x" * _OVER_16KB
        status, data = _raw_request(
            self.port,
            headers={"Content-Type": "application/json",
                     "Content-Length": str(len(oversized))},
            body=oversized,
        )
        assert status == 413
        assert "too large" in data["error"]

    def test_oversized_body_is_never_parsed_or_claimed(self):
        """Rejection happens on the declared length, before any allocation.

        The body here is *valid JSON carrying the correct token*. If the
        server had read and parsed it the claim would have succeeded, so a
        413 plus an unclaimed worker is direct evidence that neither the
        read nor the parse ran.

        This is the one oversize test deliberately kept *relative* to
        ``MAX_CONTENT_LENGTH``: the property it pins — "over the cap is
        refused before it is parsed" — is true at any cap, so expressing it in
        absolute bytes would make a deliberate retune look like a regression.
        The absolute counterparts around it are what notice the cap changing
        value. Keep both.
        """
        assert ClaimHandler._claimed is False
        padding = "p" * (ClaimHandler.MAX_CONTENT_LENGTH + 4096)
        body = json.dumps({
            "token": self.token,
            "coordinator_url": _COORD_URL,
            "coordinator_token": _COORD_TOKEN,
            "padding": padding,
        }).encode()
        assert len(body) > ClaimHandler.MAX_CONTENT_LENGTH
        status, _ = _raw_request(
            self.port,
            headers={"Content-Type": "application/json",
                     "Content-Length": str(len(body))},
            body=body,
        )
        assert status == 413
        assert ClaimHandler._claimed is False

    def test_server_still_claimable_after_an_oversized_body(self):
        """An oversized request must not wedge the listener.

        Absolute size on purpose — see ``_OVER_16KB``. The refusal is asserted
        rather than merely provoked: without it this test would keep passing
        against a cap large enough to swallow the body whole, and would then
        be testing nothing but that the listener survives an ordinary request.
        """
        oversized = b"y" * _OVER_16KB
        status, _ = _raw_request(
            self.port,
            headers={"Content-Type": "application/json",
                     "Content-Length": str(len(oversized))},
            body=oversized,
        )
        assert status == 413
        resp = _post(self.port, "/api/claim", _claim_body(self.token))
        assert resp.status == 200
        assert json.loads(resp.read())["ok"] is True

    def test_huge_declared_length_is_refused_immediately(self):
        """"Content-Length: 4 GB" must cost us nothing.

        Only a few bytes are actually sent. A server that allocated or read
        the declared length would sit here until its socket timeout; a
        server that checks the number first answers at once. The guard is
        aligned to the server's own socket timeout — the thing a regression
        would make us wait out — rather than to a fixed 10 s, because the
        elapsed time is wall clock and a heavily loaded CI can add seconds
        of pure scheduling to a request that takes 0.03 s of real work. The
        client's socket timeout is set to the same value so a regression
        still surfaces as a failed read rather than a hung suite.
        """
        started = time.time()
        status, _ = _raw_request(
            self.port,
            headers={"Content-Type": "application/json",
                     "Content-Length": str(4 * 1024 * 1024 * 1024)},
            raw_after_headers=b'{"token"',
            timeout=ClaimHandler.timeout,
        )
        assert status == 413
        assert time.time() - started < ClaimHandler.timeout

    def test_missing_content_length_returns_411(self):
        """No length, no chunked framing — we refuse to guess."""
        status, data = _raw_request(self.port,
                                    headers={"Content-Type": "application/json"})
        assert status == 411
        assert "Content-Length" in data["error"]

    def test_chunked_body_returns_411(self):
        """BaseHTTPRequestHandler cannot decode chunked, so we say so."""
        status, data = _raw_request(
            self.port,
            headers={"Content-Type": "application/json",
                     "Transfer-Encoding": "chunked"},
            raw_after_headers=b"5\r\nhello\r\n0\r\n\r\n",
        )
        assert status == 411
        assert "chunked" in data["error"]

    def test_negative_content_length_returns_400(self):
        status, data = _raw_request(
            self.port,
            headers={"Content-Type": "application/json",
                     "Content-Length": "-1"},
        )
        assert status == 400
        assert "Content-Length" in data["error"]

    def test_non_numeric_content_length_returns_400(self):
        status, data = _raw_request(
            self.port,
            headers={"Content-Type": "application/json",
                     "Content-Length": "not-a-number"},
        )
        assert status == 400

    def test_malformed_body_is_rejected_cleanly(self):
        """Garbage JSON gets one well-formed 400, not a traceback."""
        status, data = _raw_request(
            self.port,
            headers={"Content-Type": "application/json",
                     "Content-Length": "11"},
            body=b"{bad json!!",
        )
        assert status == 400
        assert "invalid JSON" in data["error"]
        assert ClaimHandler._claimed is False

    def test_successful_claim_still_works_end_to_end(self):
        """The hardening must not have changed the happy path at all."""
        resp = _post(self.port, "/api/claim", _claim_body(self.token))
        data = json.loads(resp.read())
        assert resp.status == 200
        assert data["ok"] is True
        assert data["coordinator_url"] == _COORD_URL
        assert ClaimHandler._claimed is True


# The framing of a chunked claim, split at the point a real client splits it:
# the header block goes out in one write, the body in another. urllib and
# http.client both do this, and so does every HTTP library that streams a body
# it is still generating — which is the only reason anyone sends chunked here
# in the first place.
_CHUNKED_HEAD = (
    b"POST /api/claim HTTP/1.1\r\n"
    b"Host: 127.0.0.1\r\n"
    b"Content-Type: application/json\r\n"
    b"Transfer-Encoding: chunked\r\n"
    b"\r\n"
)
_CHUNKED_BODY = b"5\r\nhello\r\n0\r\n\r\n"


def _split_write_exchange(port, head, body, gap, timeout=10.0):
    """Send *head*, pause for *gap* seconds, send *body*, read everything back.

    The pause is the entire point, and it is why this cannot be expressed with
    ``_raw_request`` or ``_raw_exchange``. Both of those hand the server bytes
    that are already in its receive buffer by the time it looks, so the
    server's decision about draining is never exercised. TCP_NODELAY is set for
    the same reason: without it Nagle can coalesce the two writes back into one
    segment and quietly restore the case that always worked.

    Returns the raw reply bytes. A connection reset — which is exactly how this
    fails on Windows — surfaces as an empty or truncated reply rather than an
    exception, so the assertion that reads it can say something useful.
    """
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.sendall(head)
        if gap:
            time.sleep(gap)
        try:
            sock.sendall(body)
        except OSError:
            # The server already reset us mid-exchange; whatever it wrote
            # before that is gone, which the caller asserts on.
            pass
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


class TestChunkedRefusalSurvivesASplitWrite:
    """The 411 for a chunked body has to actually reach the client.

    ``_read_json`` refuses chunked framing because BaseHTTPRequestHandler
    cannot decode it, sets ``close_connection``, and answers 411. Writing the
    411 is not the same as delivering it: closing a socket with unread bytes
    still sitting in the receive buffer sends an RST on Windows, and an RST
    discards whatever the peer had not yet read — so the client sees a reset
    connection instead of the explanation. This file's own ``MAX_DRAIN_BYTES``
    comment states that rule, and the oversize branch obeys it via ``_drain``;
    the chunked branch did not, until it started sweeping first.

    Whether it bites depends entirely on *when* the body arrives. Send headers
    and body in one write and the bytes are already buffered when the handler
    looks, so there is nothing left unread and the 411 sails through — which is
    why ``test_chunked_body_returns_411`` above passed throughout the bug, and
    why a single-write test here would prove nothing. Send them as two writes
    with even a millisecond between and the handler has answered and closed
    before the body lands. That reproduced every single time.

    The gaps are parametrised rather than picked because the failing one was
    1 ms: a test that only tried 100 ms would look like a test of a slow
    client, and a test that only tried 0 would look like a passing test.
    """

    def setup_method(self):
        self.token = "chunked-token-1234"
        self.httpd, self.port = start_claim_server(self.token, port=0)
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()
        _wait_until_accepting(self.port)

    def teardown_method(self):
        try:
            self.httpd.shutdown()
        except Exception:
            pass

    @pytest.mark.parametrize("gap", [0, 0.001, 0.005, 0.02, 0.1])
    def test_the_411_arrives_when_the_body_is_a_second_write(self, gap):
        raw = _split_write_exchange(self.port, _CHUNKED_HEAD, _CHUNKED_BODY,
                                    gap)
        lines = _status_lines(raw)
        assert lines, (
            f"the connection was reset with a {gap * 1000:.0f}ms gap between "
            f"the headers and the body — the 411 was written but never "
            f"delivered, because the arrived body bytes were left unread"
        )
        assert b"411" in lines[0], f"expected 411, got {lines[0]!r}"
        assert b"chunked" in raw
        assert ClaimHandler._claimed is False

    @pytest.mark.parametrize("gap", [0, 0.001, 0.02])
    def test_the_listener_survives_it_and_is_still_claimable(self, gap):
        """A refused request must cost the worker nothing beyond that request.

        The sweep reads from the socket, so getting it wrong could plausibly
        park a server thread on bytes that never come — which would be a worse
        bug than the one it fixes, and invisible in a test that only checked
        the status line. A perfectly ordinary claim, right afterwards, is what
        rules that out.
        """
        _split_write_exchange(self.port, _CHUNKED_HEAD, _CHUNKED_BODY, gap)
        resp = _post(self.port, "/api/claim", _claim_body(self.token))
        assert resp.status == 200
        assert json.loads(resp.read())["ok"] is True


def _raw_exchange(port, request_bytes, timeout=10.0):
    """Send raw bytes and read the whole reply until the server closes.

    Returns the complete byte stream, so a test can count how many HTTP
    status lines came back. The socket timeout is what keeps a regression
    from turning into a hung suite.
    """
    import socket as _socket

    sock = _socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        sock.sendall(request_bytes)
        chunks = []
        while True:
            try:
                chunk = sock.recv(65536)
            except (OSError, _socket.timeout):
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        sock.close()


def _request_bytes(body=b"", headers=None, path="/api/claim"):
    hdrs = {"Host": "127.0.0.1", "Content-Type": "application/json"}
    if headers is not None:
        hdrs.update(headers)
    hdrs.setdefault("Content-Length", str(len(body)))
    head = f"POST {path} HTTP/1.1\r\n" + "".join(
        f"{k}: {v}\r\n" for k, v in hdrs.items() if v is not None
    ) + "\r\n"
    return head.encode() + body


def _status_lines(raw):
    return [line for line in raw.split(b"\r\n") if line.startswith(b"HTTP/")]


def _impatient_handler(seconds):
    """A per-server handler subclass whose socket timeout is shortened.

    ``start_claim_server`` hands ``ClaimHandler`` itself to the server rather
    than building a per-serve subclass the way ``server.serve`` does, so the
    only way to make a stall observable in seconds instead of fifteen is to
    swap a subclass onto the server *instance*. Lowering
    ``ClaimHandler.timeout`` in place would leak into every other test in the
    session.

    The subclass derives its value from the production attribute instead of
    naming one outright, and that is the whole point of the helper: if
    ``ClaimHandler.timeout`` is ever deleted, ``StreamRequestHandler``'s
    ``None`` propagates through here, the connection goes back to blocking
    forever, and the stall test below fails. A subclass that simply hardcoded
    ``2`` would be supplying the very defence it is meant to be testing.
    """
    inherited = ClaimHandler.timeout
    return type(
        "_ImpatientClaimHandler", (ClaimHandler,),
        {"timeout": None if inherited is None else min(inherited, seconds)},
    )


class TestClaimSocketStalls:
    """A peer that declares a body and never sends it must not keep a thread.

    ``start_claim_server`` runs a ThreadingHTTPServer, so every accepted
    connection costs one thread. Without a socket timeout,
    ``rfile.read(length)`` in ``_read_json`` blocks forever and an
    unauthenticated peer gets one pinned thread per socket it opens — on a
    port that is bound to every interface by default and that grants code
    execution to whoever guesses the token. This is the slowloris defence
    described above ``ClaimHandler.timeout`` and in THREAT_MODEL.md; the
    coordinator's equivalent is asserted in test_api_edge_cases.py.
    """

    # Lowered on a per-server subclass so the test costs two seconds rather
    # than fifteen. The production value is pinned separately, below.
    STALL_TIMEOUT = 2.0

    def setup_method(self):
        self.token = "stall-token-1234"
        self.httpd, self.port = start_claim_server(self.token, port=0)
        self.httpd.RequestHandlerClass = _impatient_handler(self.STALL_TIMEOUT)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        _wait_until_accepting(self.port)

    def teardown_method(self):
        try:
            self.httpd.shutdown()
        except Exception:
            pass

    def test_production_socket_timeout_is_set_and_sane(self):
        """The value the shipped server actually runs with.

        The behavioural test below runs against a lowered copy, so this is
        what stops the real one from being removed or set to something
        useless. Generous enough for a claim delivered over a slow link (it
        bounds silence between reads, not the whole transfer), tight enough
        that a pinned thread is reclaimed in well under a minute.
        """
        assert isinstance(ClaimHandler.timeout, (int, float))
        assert 5 <= ClaimHandler.timeout <= 60

    def test_stalled_body_does_not_pin_a_handler_thread(self):
        """Declare a body, send none, and watch the handler let go.

        The request is under the size cap, so nothing else in ``_read_json``
        can refuse it — only the socket timeout ends this connection. With no
        timeout the read blocks forever and the only thing that eventually
        returns is our own client deadline, which is what turns the
        regression into a failure instead of a hung suite.
        """
        started = time.monotonic()
        raw = _raw_exchange(
            self.port,
            _request_bytes(b"", headers={"Content-Length": "4096"}),
            timeout=30.0,
        )
        elapsed = time.monotonic() - started
        assert elapsed < 20, (
            f"connection held for {elapsed:.1f}s — the handler socket has no "
            f"timeout, so this peer owns a thread for as long as it likes"
        )
        assert elapsed >= 1.0, (
            f"closed after {elapsed:.1f}s — something other than the socket "
            f"timeout ended this request"
        )
        assert not _status_lines(raw), (
            "a peer that never sent its body should get no response, just a "
            f"closed connection; got {raw[:80]!r}"
        )

    def test_the_thread_is_reclaimed_not_merely_the_socket(self):
        """Closing the connection is only half of it — the thread must go too.

        ThreadingHTTPServer starts one thread per connection, so a defence
        that dropped the socket while leaving the handler parked would fix
        nothing. Ten stalled connections are opened at once and the live
        thread count is compared before and after; the numbers, not the
        response, are the assertion.
        """
        before = threading.active_count()
        socks = []
        try:
            for _ in range(10):
                sock = socket.create_connection(("127.0.0.1", self.port),
                                                timeout=30.0)
                sock.sendall(_request_bytes(
                    b"", headers={"Content-Length": "4096"}))
                socks.append(sock)
            # All ten are now parked in _read_json waiting for bytes that
            # will never arrive.
            assert threading.active_count() > before

            # Slack of two: this counts every thread in the process, and the
            # module-scoped coordinator next door is entitled to a couple.
            # Ten leaked handler threads are still unmissable against it.
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if threading.active_count() <= before + 2:
                    break
                time.sleep(0.1)
            assert threading.active_count() <= before + 2, (
                f"{threading.active_count() - before} handler threads still "
                f"alive after {self.STALL_TIMEOUT}s of silence — stalled "
                f"connections are leaking threads"
            )
        finally:
            for sock in socks:
                try:
                    sock.close()
                except OSError:
                    pass

    def test_the_listener_still_answers_after_a_stall(self):
        """A stalled peer must not cost the operator their own claim."""
        _raw_exchange(
            self.port,
            _request_bytes(b"", headers={"Content-Length": "4096"}),
            timeout=30.0,
        )
        resp = _post(self.port, "/api/claim", _claim_body(self.token))
        assert resp.status == 200
        assert json.loads(resp.read())["ok"] is True


class TestInterceptedHandlerStaysInSync:
    """worker.py monkey-patches ClaimHandler.do_POST at runtime, and its
    replacement falls through to the original whenever _read_json does not
    hand back a dict. That fall-through re-enters _read_json on a body that
    has already been consumed, so the handler has to survive being called
    twice: exactly one response on the wire, and no second socket read that
    would block forever on bytes nobody is going to send.

    These tests replicate that patch shape rather than importing
    run_worker_broadcast, which would also start a UDP beacon and block
    waiting to be claimed.
    """

    def setup_method(self):
        self.token = "intercept-token-1234"
        self.httpd, self.port = start_claim_server(self.token, port=0)
        self._orig_do_POST = ClaimHandler.do_POST
        orig = self._orig_do_POST

        def _intercepted(handler):
            # Mirrors gpumesh.worker.run_worker_broadcast._intercepted_do_POST.
            if handler.path != "/api/claim":
                return orig(handler)
            try:
                body = handler._read_json()
            except (UnicodeDecodeError, json.JSONDecodeError):
                return orig(handler)
            if body is None or not isinstance(body, dict):
                return orig(handler)
            if body.get("token") != ClaimHandler._token:
                handler._send(401, {"ok": False, "error": "wrong token"})
                return
            handler._send(200, {"ok": True, "intercepted": True})

        ClaimHandler.do_POST = _intercepted
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        _wait_until_accepting(self.port)

    def teardown_method(self):
        ClaimHandler.do_POST = self._orig_do_POST
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        ClaimHandler._rate_limiter = RateLimiter()

    @staticmethod
    def _status_lines(raw):
        return [line for line in raw.split(b"\r\n") if line.startswith(b"HTTP/")]

    def test_malformed_json_answers_once_and_does_not_hang(self):
        """The old fall-through re-read a consumed body and blocked there."""
        raw = _raw_exchange(self.port, _request_bytes(b"{bad json!!"))
        lines = self._status_lines(raw)
        assert len(lines) == 1
        assert b"400" in lines[0]
        assert b"invalid JSON" in raw

    def test_empty_body_answers_once(self):
        raw = _raw_exchange(self.port, _request_bytes(b""))
        lines = self._status_lines(raw)
        assert len(lines) == 1
        assert b"400" in lines[0]

    def test_non_object_json_answers_once(self):
        raw = _raw_exchange(self.port, _request_bytes(b"[1,2,3]"))
        lines = self._status_lines(raw)
        assert len(lines) == 1
        assert b"400" in lines[0]

    def test_oversized_body_answers_once(self):
        # Absolute size on purpose — see ``_OVER_16KB``.
        body = b"z" * _OVER_16KB
        raw = _raw_exchange(self.port, _request_bytes(body))
        lines = self._status_lines(raw)
        assert len(lines) == 1
        assert b"413" in lines[0]

    def test_patched_handler_is_rate_limited_too(self):
        """The limiter lives in _read_json/_send, which both do_POSTs share,
        so worker.py inherits the policy without knowing it exists."""
        ClaimHandler._rate_limiter = RateLimiter(
            max_attempts=2, exempt_loopback=False,
        )
        good = json.dumps({"token": self.token}).encode()
        bad = json.dumps({"token": "nope"}).encode()
        for _ in range(2):
            raw = _raw_exchange(self.port, _request_bytes(bad))
            assert b"401" in self._status_lines(raw)[0]

        raw = _raw_exchange(self.port, _request_bytes(good))
        lines = self._status_lines(raw)
        assert len(lines) == 1
        assert b"429" in lines[0]

    def test_valid_claim_reaches_the_patched_handler(self):
        body = json.dumps({"token": self.token}).encode()
        raw = _raw_exchange(self.port, _request_bytes(body))
        lines = self._status_lines(raw)
        assert len(lines) == 1
        assert b"200" in lines[0]
        assert b"intercepted" in raw


class TestClaimRateLimiting:
    """Without a limiter the claim token can be guessed as fast as the
    network allows, on a port that grants code execution when guessed.
    """

    def setup_method(self):
        self.token = "rl-token-1234"
        self.httpd, self.port = start_claim_server(self.token, port=0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        _wait_until_accepting(self.port)

    def teardown_method(self):
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        # start_claim_server installs a fresh limiter, but restore an
        # exempting one anyway so a swapped-in strict limiter cannot leak
        # into another test through the shared class attribute.
        ClaimHandler._rate_limiter = RateLimiter()

    def _make_remote(self, max_attempts=3):
        """Treat this test's loopback connections as if they were remote.

        Real non-loopback traffic cannot be generated from a unit test, and
        the exemption is keyed on the peer address, so the honest way to
        exercise the remote path is to turn the exemption off.
        """
        ClaimHandler._rate_limiter = RateLimiter(
            max_attempts=max_attempts, window_seconds=300,
            lockout_seconds=900, exempt_loopback=False,
        )

    def test_claim_server_has_a_rate_limiter(self):
        assert isinstance(ClaimHandler._rate_limiter, RateLimiter)

    def test_repeated_wrong_tokens_get_rate_limited(self):
        self._make_remote(max_attempts=3)
        for _ in range(3):
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                _post(self.port, "/api/claim", _claim_body("guess"))
            assert exc_info.value.code == 401

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(self.port, "/api/claim", _claim_body("guess"))
        assert exc_info.value.code == 429
        assert "not checked" in json.loads(exc_info.value.read())["error"]

    def test_rate_limited_caller_cannot_claim_even_with_the_right_token(self):
        """The lockout must gate the body, not merely the verdict.

        If a locked-out peer could still get their token examined, the
        lockout would be a free correctness oracle and the brute force would
        continue at full speed. A correct token arriving under lockout gets
        429 and the worker stays unclaimed.
        """
        self._make_remote(max_attempts=2)
        for _ in range(2):
            with pytest.raises(urllib.error.HTTPError):
                _post(self.port, "/api/claim", _claim_body("guess"))

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(self.port, "/api/claim", _claim_body(self.token))
        assert exc_info.value.code == 429
        assert ClaimHandler._claimed is False

    def test_malformed_bodies_do_not_count_as_token_guesses(self):
        """400s are not failed authentications; only 401 is."""
        self._make_remote(max_attempts=3)
        for _ in range(5):
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                _post(self.port, "/api/claim", b"{bad json!!")
            assert exc_info.value.code == 400
        assert ClaimHandler._rate_limiter.is_allowed("127.0.0.1") is True

    def test_loopback_is_never_locked_out(self):
        """The operator's own machine keeps working after a run of typos.

        Anyone who can connect from 127.0.0.1 already runs code here and can
        read the token out of argv, so a loopback lockout costs an attacker
        nothing and costs the operator their worker.
        """
        for _ in range(8):
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                _post(self.port, "/api/claim", _claim_body("wrong"))
            assert exc_info.value.code == 401

        resp = _post(self.port, "/api/claim", _claim_body(self.token))
        assert resp.status == 200
        assert json.loads(resp.read())["ok"] is True

    def test_successful_claim_clears_failure_history(self):
        self._make_remote(max_attempts=5)
        for _ in range(3):
            with pytest.raises(urllib.error.HTTPError):
                _post(self.port, "/api/claim", _claim_body("guess"))
        assert ClaimHandler._rate_limiter.get_remaining_attempts("127.0.0.1") == 2

        resp = _post(self.port, "/api/claim", _claim_body(self.token))
        assert resp.status == 200
        assert ClaimHandler._rate_limiter.get_remaining_attempts("127.0.0.1") == 5

    def test_a_fresh_claim_server_starts_unlocked(self):
        """Restarting the worker is an operator action, not an attacker one."""
        self._make_remote(max_attempts=1)
        with pytest.raises(urllib.error.HTTPError):
            _post(self.port, "/api/claim", _claim_body("guess"))
        assert ClaimHandler._rate_limiter.is_allowed("127.0.0.1") is False

        httpd, port = start_claim_server("fresh-token", port=0)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        _wait_until_accepting(port)
        try:
            assert ClaimHandler._rate_limiter.is_allowed("127.0.0.1") is True
        finally:
            httpd.shutdown()


# ── bind address ─────────────────────────────────────────────────────────

class _RecordingServer:
    """Stands in for ``_FastBindHTTPServer`` so a test can assert on the bind.

    These tests are about *which* address the claim server asks the OS for,
    and the honest way to check that is to look at the argument rather than to
    actually bind it. Binding 0.0.0.0 for real would open a port on every
    interface of whatever machine runs the suite — including CI runners and
    laptops on untrusted networks — to prove a fact that is visible without
    doing so. The narrower binds below are exercised against a real socket,
    because loopback costs nothing.
    """

    instances = []

    def __init__(self, server_address, handler):
        self.requested_address = server_address
        self.handler = handler
        # Mimic a real bind on an ephemeral port: the OS picks the number,
        # the host stays exactly as asked for.
        host, port = server_address
        self.server_address = (host, port or 54321)
        _RecordingServer.instances.append(self)

    def serve_forever(self):  # pragma: no cover - never started
        raise AssertionError("these tests must not start a listener")

    def server_close(self):
        pass


@pytest.fixture
def recorded_bind(monkeypatch):
    """Capture the address start_claim_server hands to the server class."""
    from gpumesh import claimer

    _RecordingServer.instances = []
    monkeypatch.delenv("GPUMESH_CLAIM_HOST", raising=False)
    monkeypatch.setattr(claimer, "_FastBindHTTPServer", _RecordingServer)
    return _RecordingServer


class TestClaimBindAddress:
    """The claim port binds every interface by default, and can be narrowed.

    The default is deliberately NOT the coordinator's loopback default. A
    claim server exists solely so another machine can reach this one, so a
    loopback claim server is not a hardened claim server — it is a worker that
    can never be claimed, failing as a silent unreachability mystery. What was
    missing was the ability to say something more precise than "everything".
    """

    def test_default_is_still_every_interface(self, recorded_bind):
        """Regression guard in the unusual direction: do not tighten this.

        If someone "fixes" this to 127.0.0.1 by analogy with `gpumesh serve`,
        discovery mode breaks for every user and the tests should say so
        before the bug reports do.
        """
        from gpumesh.claimer import DEFAULT_CLAIM_BIND_HOST, start_claim_server

        assert DEFAULT_CLAIM_BIND_HOST == "0.0.0.0"
        start_claim_server("tok", port=0)
        assert recorded_bind.instances[-1].requested_address[0] == "0.0.0.0"

    def test_explicit_bind_host_is_honoured(self):
        """A narrower bind is real: the socket answers only on that address."""
        from gpumesh.claimer import start_claim_server

        httpd, port = start_claim_server("tok", port=0, bind_host="127.0.0.1")
        try:
            assert httpd.server_address[0] == "127.0.0.1"
            assert port > 0
        finally:
            httpd.server_close()

    def test_env_var_narrows_the_bind(self, recorded_bind, monkeypatch):
        """``GPUMESH_CLAIM_HOST`` is the no-code-change knob.

        Mirrors ``GPUMESH_HOST`` on the coordinator so an operator who learned
        one already knows the other.
        """
        from gpumesh.claimer import start_claim_server

        monkeypatch.setenv("GPUMESH_CLAIM_HOST", "100.64.0.7")
        start_claim_server("tok", port=0)
        assert recorded_bind.instances[-1].requested_address[0] == "100.64.0.7"

    def test_explicit_argument_beats_the_env_var(self, recorded_bind, monkeypatch):
        """Same precedence as cli._resolve_bind_host: explicit wins."""
        from gpumesh.claimer import start_claim_server

        monkeypatch.setenv("GPUMESH_CLAIM_HOST", "100.64.0.7")
        start_claim_server("tok", port=0, bind_host="192.0.2.5")
        assert recorded_bind.instances[-1].requested_address[0] == "192.0.2.5"

    @pytest.mark.parametrize("blank", ["", "   ", None])
    def test_blank_values_fall_through_to_the_default(self, blank, recorded_bind):
        """An empty string is not a bind address — and emphatically not INADDR_ANY
        by accident. Treat it as "unset" so it falls through explicitly."""
        from gpumesh.claimer import start_claim_server

        start_claim_server("tok", port=0, bind_host=blank)
        assert recorded_bind.instances[-1].requested_address[0] == "0.0.0.0"

    def test_env_var_is_stripped(self, recorded_bind, monkeypatch):
        from gpumesh.claimer import _resolve_claim_bind_host

        monkeypatch.setenv("GPUMESH_CLAIM_HOST", "  10.1.2.3  ")
        assert _resolve_claim_bind_host() == "10.1.2.3"

    def test_coordinator_host_var_does_not_leak_into_the_claim_bind(
        self, recorded_bind, monkeypatch
    ):
        """``GPUMESH_HOST=127.0.0.1`` is a reasonable coordinator setting and
        must not silently make every worker on that box unclaimable."""
        from gpumesh.claimer import start_claim_server

        monkeypatch.setenv("GPUMESH_HOST", "127.0.0.1")
        start_claim_server("tok", port=0)
        assert recorded_bind.instances[-1].requested_address[0] == "0.0.0.0"


# ── environment isolation ────────────────────────────────────────────────

# Only the forms that actually *read* the environment. Matching bare
# GPUMESH_* identifiers instead would drag in things that are not environment
# variables at all — serializer.py has a module constant named
# _GPUMESH_DECORATOR_ROOTS — and would make the check below fail for a name
# nobody could ever export.
_ENV_READ = re.compile(
    r"""os\.environ\.get\(\s*["'](GPUMESH_[A-Z0-9_]+)["']"""
    r"""|os\.getenv\(\s*["'](GPUMESH_[A-Z0-9_]+)["']"""
    r"""|os\.environ\[\s*["'](GPUMESH_[A-Z0-9_]+)["']\s*\]"""
)


def _env_vars_the_package_reads():
    """Every GPUMESH_* variable read anywhere in the installed package."""
    import gpumesh

    package_dir = pathlib.Path(gpumesh.__file__).parent
    found = {}
    for path in sorted(package_dir.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _ENV_READ.finditer(text):
            name = next(g for g in match.groups() if g)
            found.setdefault(name, set()).add(path.name)
    return found


class TestGpumeshEnvIsolation:
    """conftest clears GPUMESH_* before every test; this keeps that list honest.

    It lives beside the claim-bind tests because ``GPUMESH_CLAIM_HOST`` is the
    variable that proved how expensive the gap is. It is one line in
    ``_isolate_gpumesh_env``, and without it a contributor who had exported it
    — as the README and CONTRIBUTING both suggest doing — watched 46 tests
    error out, because the claim server dutifully bound somewhere the tests
    could not dial. ``GPUMESH_HOST`` cost another five the same way.

    The failure has no signature: nothing points at the environment, the tests
    that break have no obvious relationship to each other, and they pass again
    the moment the contributor opens a different shell. So the list is checked
    against the package rather than maintained by memory, and the check runs
    from the direction that matters — start from what the code reads, not from
    what the fixture happens to name.
    """

    def test_claim_host_is_cleared(self):
        """The specific one, named, because it is the expensive one."""
        import conftest

        assert "GPUMESH_CLAIM_HOST" in conftest.ISOLATED_GPUMESH_ENV_VARS

    def test_the_scan_finds_the_variables_we_know_about(self):
        """Guard the guard: a regex that matched nothing would pass silently."""
        found = _env_vars_the_package_reads()
        assert "GPUMESH_CLAIM_HOST" in found
        assert "GPUMESH_TOKEN" in found
        assert "GPUMESH_HOST" in found
        # A module constant is not an environment variable, and matching bare
        # identifiers would have swept this one up.
        assert "GPUMESH_DECORATOR_ROOTS" not in found

    def test_every_variable_the_package_reads_is_cleared(self):
        """The forward-looking half: a new GPUMESH_* read must be added here.

        This is the test that fires for a variable that does not exist yet.
        Whoever adds ``os.environ.get("GPUMESH_SOMETHING")`` to the package
        gets told, once, in the same change, instead of a contributor
        discovering it months later as a wall of unrelated failures.
        """
        import conftest

        cleared = set(conftest.ISOLATED_GPUMESH_ENV_VARS)
        found = _env_vars_the_package_reads()
        missing = {name: sorted(files) for name, files in found.items()
                   if name not in cleared}
        assert not missing, (
            f"the package reads these environment variables but "
            f"_isolate_gpumesh_env does not clear them: {missing}. A "
            f"contributor with any of them exported will see unrelated tests "
            f"fail for reasons that point nowhere near the environment. Add "
            f"them to ISOLATED_GPUMESH_ENV_VARS in tests/conftest.py."
        )


class TestClaimExposureWarning:
    """A listener this dangerous announces itself.

    A correct token on this port is remote code execution as this user *and*
    SSRF — the coordinator URL the worker then obeys arrives in the same
    request body. The operator opted into discovery mode; they did not
    necessarily picture that.
    """

    def test_wildcard_bind_warns(self, recorded_bind, capsys):
        from gpumesh.claimer import start_claim_server

        start_claim_server("tok", port=0)
        out = capsys.readouterr().out
        assert "CLAIM PORT OPEN TO THE NETWORK" in out
        assert "0.0.0.0" in out
        assert "GPUMESH_CLAIM_HOST" in out, "must say how to narrow it"

    def test_warning_names_the_reachable_address_and_port(self, recorded_bind):
        from gpumesh.claimer import start_claim_server

        _, port = start_claim_server("tok", port=0)
        # Re-run the printer directly so the assertion is about content, not
        # about however the caller happened to format the line.
        from gpumesh.claimer import _print_claim_exposure_warning
        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _print_claim_exposure_warning("100.64.0.7", 9999)
        text = buf.getvalue()
        assert "100.64.0.7:9999" in text
        assert "the 100.64.0.7 interface only" in text
        assert port > 0

    def test_narrowed_bind_still_warns_because_it_is_still_remote(
        self, recorded_bind, capsys
    ):
        """Pinning one LAN interface is narrower, not safe."""
        from gpumesh.claimer import start_claim_server

        start_claim_server("tok", port=0, bind_host="192.0.2.5")
        out = capsys.readouterr().out
        assert "CLAIM PORT OPEN TO THE NETWORK" in out
        assert "192.0.2.5" in out

    def test_loopback_bind_is_silent(self, capsys):
        """Nothing became reachable, so there is nothing to warn about.

        A banner that fires when nothing is exposed is how operators learn to
        skim banners.
        """
        from gpumesh.claimer import start_claim_server

        httpd, _ = start_claim_server("tok", port=0, bind_host="127.0.0.1")
        try:
            assert "CLAIM PORT OPEN" not in capsys.readouterr().out
        finally:
            httpd.server_close()
