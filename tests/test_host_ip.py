"""Advertised-address selection and connection-failure classification.

Both cover the same production failure: a coordinator advertised
``http://172.16.0.2:8000`` — an address only it could route to — so every
worker's registration timed out with ``WinError 10060``, and the worker then
printed advice for ``10061 / ConnectionRefused``, sending users to look for a
coordinator that was in fact running fine.
"""

import urllib.error
from unittest.mock import patch

import pytest

from gpumesh import utils, worker


# ── address ranking ────────────────────────────────────────────────────────

class TestVirtualAdapterDetection:
    @pytest.mark.parametrize("ip", [
        "192.168.56.101",   # VirtualBox host-only
        "192.168.99.100",   # docker-machine / minikube
        "192.168.137.1",    # Windows connection sharing
        "10.0.75.1",        # Docker Desktop for Windows
        "172.17.0.1",       # Docker default bridge
        "172.31.255.254",   # top of the Docker Compose pool
    ])
    def test_virtual_ranges_flagged(self, ip):
        assert utils._is_virtual_adapter_ip(ip)

    @pytest.mark.parametrize("ip", [
        "192.168.1.10",
        "10.20.30.40",
        "172.16.0.2",       # private but NOT a known virtual range
    ])
    def test_real_lan_addresses_not_flagged(self, ip):
        assert not utils._is_virtual_adapter_ip(ip)

    def test_malformed_172_address_does_not_raise(self):
        """_is_private_ip used to call int() on an unparsed octet."""
        assert utils._is_private_ip("172.abc.0.1") is False
        assert utils._is_private_ip("172.") is False


class TestRanking:
    def test_real_lan_beats_virtual_adapter(self):
        assert utils._rank_ip("192.168.1.10") < utils._rank_ip("172.17.0.1")

    def test_virtual_adapter_beats_loopback(self):
        assert utils._rank_ip("172.17.0.1") < utils._rank_ip("127.0.0.1")

    def test_private_beats_public(self):
        assert utils._rank_ip("192.168.1.10") < utils._rank_ip("8.8.8.8")

    def test_candidates_demote_virtual_even_when_default_route(self, monkeypatch):
        """A Docker bridge reported by the default route must not win."""
        monkeypatch.delenv("GPUMESH_HOST_IP", raising=False)
        monkeypatch.setattr(
            utils, "_gather_candidate_ips",
            lambda: ["172.17.0.1", "192.168.1.10"],
        )
        assert utils.lan_ip_candidates() == ["192.168.1.10", "172.17.0.1"]
        assert utils.get_lan_ip() == "192.168.1.10"

    def test_default_route_wins_ties_within_a_rank(self, monkeypatch):
        monkeypatch.delenv("GPUMESH_HOST_IP", raising=False)
        monkeypatch.setattr(
            utils, "_gather_candidate_ips",
            lambda: ["10.20.30.40", "192.168.1.10"],
        )
        assert utils.get_lan_ip() == "10.20.30.40"

    def test_loopback_and_link_local_excluded(self, monkeypatch):
        monkeypatch.delenv("GPUMESH_HOST_IP", raising=False)
        monkeypatch.setattr(
            utils, "_gather_candidate_ips",
            lambda: ["127.0.0.1", "169.254.1.1", "192.168.1.10"],
        )
        assert utils.lan_ip_candidates() == ["192.168.1.10"]


class TestHostIpOverride:
    def test_env_var_wins_over_detection(self, monkeypatch):
        monkeypatch.setenv("GPUMESH_HOST_IP", "203.0.113.9")
        monkeypatch.setattr(
            utils, "_gather_candidate_ips", lambda: ["192.168.1.10"],
        )
        assert utils.get_lan_ip() == "203.0.113.9"

    def test_blank_env_var_ignored(self, monkeypatch):
        monkeypatch.setenv("GPUMESH_HOST_IP", "   ")
        monkeypatch.setattr(
            utils, "_gather_candidate_ips", lambda: ["192.168.1.10"],
        )
        assert utils.get_lan_ip() == "192.168.1.10"

    def test_falls_back_to_loopback_when_nothing_found(self, monkeypatch):
        monkeypatch.delenv("GPUMESH_HOST_IP", raising=False)
        monkeypatch.setattr(utils, "_gather_candidate_ips", lambda: [])
        assert utils.get_lan_ip() == "127.0.0.1"

    def test_cgnat_only_machine_keeps_its_address(self, monkeypatch):
        """Tailscale-only host: better to advertise 100.x than loopback."""
        monkeypatch.delenv("GPUMESH_HOST_IP", raising=False)
        monkeypatch.setattr(
            utils, "_gather_candidate_ips", lambda: ["100.67.72.79"],
        )
        assert utils.get_lan_ip() == "100.67.72.79"


class TestShowIpAlternatives:
    def test_lists_other_addresses(self, monkeypatch, capsys):
        monkeypatch.delenv("GPUMESH_HOST_IP", raising=False)
        monkeypatch.setattr(
            utils, "_gather_candidate_ips",
            lambda: ["192.168.1.10", "172.22.96.1"],
        )
        utils.show_ip_alternatives("192.168.1.10", 8000)
        out = capsys.readouterr().out
        assert "http://172.22.96.1:8000" in out
        assert "virtual adapter" in out

    def test_warns_when_chosen_address_is_virtual(self, monkeypatch, capsys):
        monkeypatch.delenv("GPUMESH_HOST_IP", raising=False)
        monkeypatch.setattr(
            utils, "_gather_candidate_ips", lambda: ["172.17.0.1"],
        )
        utils.show_ip_alternatives("172.17.0.1", 8000)
        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "time out" in out

    def test_silent_when_pinned(self, monkeypatch, capsys):
        monkeypatch.setenv("GPUMESH_HOST_IP", "192.168.1.10")
        monkeypatch.setattr(
            utils, "_gather_candidate_ips",
            lambda: ["192.168.1.10", "172.22.96.1"],
        )
        utils.show_ip_alternatives("192.168.1.10", 8000)
        assert capsys.readouterr().out == ""

    def test_silent_when_only_one_candidate(self, monkeypatch, capsys):
        monkeypatch.delenv("GPUMESH_HOST_IP", raising=False)
        monkeypatch.setattr(
            utils, "_gather_candidate_ips", lambda: ["192.168.1.10"],
        )
        utils.show_ip_alternatives("192.168.1.10", 8000)
        assert capsys.readouterr().out == ""


# ── connection-failure classification ──────────────────────────────────────

_PROBE = {
    "hostname": "test-host", "device": "cpu", "device_name": "Test CPU",
    "score": 1.0, "cpu_count": 4, "cpu_cores": 4, "platform": "test",
}


def _run_worker_with_failure(exc, capsys):
    """Drive run_worker through a registration failure; return its output."""
    with patch("gpumesh.capability.full_probe", return_value=dict(_PROBE)), \
         patch("gpumesh.worker._try_register", side_effect=exc):
        worker.run_worker("http://172.16.0.2:8000", "tok")
    return capsys.readouterr().out


class TestRegistrationFailureGuidance:
    def test_timeout_is_not_reported_as_refused(self, capsys):
        """WinError 10060 maps to TimeoutError; it must not take the refused branch."""
        exc = urllib.error.URLError(
            TimeoutError("[WinError 10060] A connection attempt failed")
        )
        out = _run_worker_with_failure(exc, capsys)
        assert "TIMED OUT" in out
        assert "10060" in out
        assert "REFUSED" not in out
        # The distinguishing advice: the coordinator may be fine.
        assert "may well be running" in out
        assert "unroutable" in out

    def test_refused_still_reported_as_refused(self, capsys):
        exc = urllib.error.URLError(
            ConnectionRefusedError("[WinError 10061] No connection could be made")
        )
        out = _run_worker_with_failure(exc, capsys)
        assert "REFUSED" in out
        assert "10061" in out
        assert "TIMED OUT" not in out

    def test_bare_10060_string_classified_as_timeout(self, capsys):
        """Some platforms surface the code without a TimeoutError instance."""
        exc = urllib.error.URLError(OSError("[WinError 10060] timed out"))
        out = _run_worker_with_failure(exc, capsys)
        assert "TIMED OUT" in out

    def test_other_errors_get_generic_guidance(self, capsys):
        exc = urllib.error.URLError("name or service not known")
        out = _run_worker_with_failure(exc, capsys)
        assert "network or DNS error" in out
        assert "REFUSED" not in out
        assert "TIMED OUT" not in out

    def test_curl_hint_uses_the_actual_url(self, capsys):
        exc = urllib.error.URLError(TimeoutError("[WinError 10060] timed out"))
        out = _run_worker_with_failure(exc, capsys)
        assert "curl http://172.16.0.2:8000/api/workers" in out
