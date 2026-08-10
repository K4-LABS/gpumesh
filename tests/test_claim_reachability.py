"""The claim handshake must not assert a reachability it never tested.

A coordinator cannot know which of its addresses a given worker can route
to — that is a property of the network between the two machines. Previously
it sent one guessed address, and the worker acked on a token match alone, so
a wrong guess was reported as "claimed successfully!" while the worker timed
out privately seconds later.

These tests pin the corrected contract: the coordinator offers candidates,
the worker probes them, and the ack means "I reached you".
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from gpumesh import utils
from gpumesh.claimer import ClaimHandler, claim_candidates, start_claim_server
from gpumesh.server import serve
from gpumesh.worker import find_reachable_coordinator

TOKEN = "claim-test-token-1234"
COORD_TOKEN = "coordinator-test-token"

# An address in TEST-NET-1 (RFC 5737). Guaranteed not routable, which is
# exactly the production failure: a coordinator advertising an address the
# worker cannot reach.
UNREACHABLE = "http://192.0.2.1:8000"


@pytest.fixture
def coordinator(tmp_path):
    """A real loopback coordinator; yields its URL."""
    httpd = serve("127.0.0.1", 0, str(tmp_path / "claim.db"), COORD_TOKEN)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.gpumesh_stop.set()
    httpd.shutdown()
    thread.join(timeout=10)


@pytest.fixture
def claim_server():
    """A real claim server; yields its URL and guarantees a clean claim flag."""
    httpd, port = start_claim_server(TOKEN, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/api/claim"
    try:
        httpd.shutdown()
    except OSError:
        pass
    with ClaimHandler._claim_lock:
        ClaimHandler._claimed = False


def _claim(url: str, body: dict, timeout: float = 25.0):
    """POST a claim; return (status, parsed body)."""
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


# ── candidate list parsing ─────────────────────────────────────────────────

class TestClaimCandidates:
    def test_prefers_the_ranked_list(self):
        assert claim_candidates({
            "coordinator_urls": ["http://a:1", "http://b:2"],
        }) == ["http://a:1", "http://b:2"]

    def test_legacy_single_url_still_accepted(self):
        """A new worker must keep working against an older coordinator."""
        assert claim_candidates({
            "coordinator_url": "http://legacy:8000",
        }) == ["http://legacy:8000"]

    def test_single_url_appended_last_when_not_in_list(self):
        assert claim_candidates({
            "coordinator_urls": ["http://a:1"],
            "coordinator_url": "http://b:2",
        }) == ["http://a:1", "http://b:2"]

    def test_no_duplicates(self):
        assert claim_candidates({
            "coordinator_urls": ["http://a:1", "http://a:1"],
            "coordinator_url": "http://a:1",
        }) == ["http://a:1"]

    def test_junk_entries_dropped(self):
        assert claim_candidates({
            "coordinator_urls": ["http://a:1", None, 42, ""],
        }) == ["http://a:1"]

    def test_empty_body_yields_nothing(self):
        assert claim_candidates({}) == []


# ── probing ────────────────────────────────────────────────────────────────

class TestFindReachableCoordinator:
    def test_returns_the_reachable_one(self, coordinator):
        url, reason = find_reachable_coordinator(
            [UNREACHABLE, coordinator], COORD_TOKEN, timeout=1.0,
        )
        assert url == coordinator
        assert reason is None

    def test_first_working_candidate_wins(self, coordinator):
        url, _ = find_reachable_coordinator(
            [coordinator, UNREACHABLE], COORD_TOKEN, timeout=1.0,
        )
        assert url == coordinator

    def test_reports_every_failure(self):
        url, reason = find_reachable_coordinator(
            ["http://127.0.0.1:1", "http://127.0.0.1:2"], COORD_TOKEN, timeout=1.0,
        )
        assert url is None
        assert "127.0.0.1:1" in reason
        assert "127.0.0.1:2" in reason

    def test_wrong_token_stops_the_search(self, coordinator):
        """401 proves the network is fine, so further candidates cannot help."""
        url, reason = find_reachable_coordinator(
            [coordinator, UNREACHABLE], "wrong-token", timeout=1.0,
        )
        assert url is None
        assert "authentication failed" in reason

    def test_empty_candidate_list(self):
        url, reason = find_reachable_coordinator([], COORD_TOKEN)
        assert url is None
        assert "no candidate URLs" in reason


# ── the handshake ──────────────────────────────────────────────────────────

class TestClaimHandshake:
    def test_ack_names_the_url_that_worked(self, claim_server, coordinator):
        status, body = _claim(claim_server, {
            "token": TOKEN,
            "coordinator_urls": [UNREACHABLE, coordinator],
            "coordinator_token": COORD_TOKEN,
        })
        assert status == 200
        assert body["ok"] is True
        assert body["coordinator_url"] == coordinator

    def test_unreachable_coordinator_is_rejected_not_acked(self, claim_server):
        """The regression: this used to return 200 and then fail silently."""
        status, body = _claim(claim_server, {
            "token": TOKEN,
            "coordinator_urls": [UNREACHABLE],
            "coordinator_token": COORD_TOKEN,
        })
        assert status == 502
        assert body["ok"] is False
        assert "cannot reach coordinator" in body["error"]
        assert body["tried"] == [UNREACHABLE]

    def test_rejection_leaves_the_worker_claimable(self, claim_server, coordinator):
        """A failed claim must not wedge the worker; a corrected retry works."""
        _claim(claim_server, {
            "token": TOKEN,
            "coordinator_urls": [UNREACHABLE],
            "coordinator_token": COORD_TOKEN,
        })
        status, body = _claim(claim_server, {
            "token": TOKEN,
            "coordinator_urls": [coordinator],
            "coordinator_token": COORD_TOKEN,
        })
        assert status == 200
        assert body["ok"] is True

    def test_wrong_worker_token_still_401(self, claim_server, coordinator):
        status, body = _claim(claim_server, {
            "token": "not-the-right-token",
            "coordinator_urls": [coordinator],
            "coordinator_token": COORD_TOKEN,
        })
        assert status == 401
        assert body["ok"] is False

    def test_missing_urls_rejected(self, claim_server):
        status, body = _claim(claim_server, {
            "token": TOKEN,
            "coordinator_token": COORD_TOKEN,
        })
        assert status == 400
        assert "coordinator_url" in body["error"]

    def test_legacy_payload_still_claims(self, claim_server, coordinator):
        """An older coordinator sending only coordinator_url must still work."""
        status, body = _claim(claim_server, {
            "token": TOKEN,
            "coordinator_url": coordinator,
            "coordinator_token": COORD_TOKEN,
        })
        assert status == 200
        assert body["coordinator_url"] == coordinator


# ── route-derived addressing ───────────────────────────────────────────────

class TestRouteDerivedAddress:
    def test_loopback_peer_yields_loopback_source(self):
        """The kernel's answer for 127.0.0.1 is 127.0.0.1 — no guessing."""
        assert utils.local_ip_for_peer("127.0.0.1") == "127.0.0.1"

    def test_no_peer_yields_none(self):
        assert utils.local_ip_for_peer("") is None

    def test_candidates_lead_with_the_routed_address(self, monkeypatch):
        monkeypatch.delenv("GPUMESH_HOST_IP", raising=False)
        monkeypatch.setattr(utils, "local_ip_for_peer", lambda ip: "192.168.1.10")
        monkeypatch.setattr(utils, "_gather_candidate_ips",
                            lambda: ["172.17.0.1", "10.0.0.5"])
        urls = utils.coordinator_url_candidates("192.168.1.99", 8000)
        assert urls[0] == "http://192.168.1.10:8000"
        # The rest stay as fallbacks for asymmetric routing.
        assert "http://10.0.0.5:8000" in urls
        assert "http://172.17.0.1:8000" in urls

    def test_explicit_pin_outranks_the_routing_table(self, monkeypatch):
        """A human pinning an address knows about NAT the kernel does not."""
        monkeypatch.setenv("GPUMESH_HOST_IP", "203.0.113.7")
        monkeypatch.setattr(utils, "local_ip_for_peer", lambda ip: "192.168.1.10")
        monkeypatch.setattr(utils, "_gather_candidate_ips", lambda: ["192.168.1.10"])
        urls = utils.coordinator_url_candidates("192.168.1.99", 8000)
        assert urls[0] == "http://203.0.113.7:8000"

    def test_unroutable_peer_still_yields_fallbacks(self, monkeypatch):
        monkeypatch.delenv("GPUMESH_HOST_IP", raising=False)
        monkeypatch.setattr(utils, "local_ip_for_peer", lambda ip: None)
        monkeypatch.setattr(utils, "_gather_candidate_ips", lambda: ["192.168.1.10"])
        assert utils.coordinator_url_candidates("192.0.2.1", 8000) == [
            "http://192.168.1.10:8000"
        ]

    def test_candidate_count_is_capped(self, monkeypatch):
        """Probing is serial, so the list must stay inside the claim timeout."""
        monkeypatch.delenv("GPUMESH_HOST_IP", raising=False)
        monkeypatch.setattr(utils, "local_ip_for_peer", lambda ip: None)
        monkeypatch.setattr(utils, "_gather_candidate_ips",
                            lambda: [f"192.168.1.{i}" for i in range(10)])
        assert len(utils.coordinator_url_candidates("192.168.1.99", 8000)) == 4
