"""Tests for ``gpumesh.utils`` — address classification and candidate ranking.

``tests/test_host_ip.py`` already covers the headline regression (a
coordinator advertising a hypervisor address no worker can route to). This
file fills the gaps around it: the classifier predicates themselves at their
range boundaries, ``local_ip_for_peer``, ``coordinator_url_candidates``, and
the de-duplication of the candidate list.

Nothing here touches the real network. ``local_ip_for_peer`` and
``coordinator_url_candidates`` open a UDP socket, which sends no packet, but
they can still do a DNS lookup for a non-literal peer — so the socket itself
is faked rather than pointed at a hostname that might make the test block on a
slow resolver.
"""

import socket

import pytest

from gpumesh import utils


# ── _is_private_ip ─────────────────────────────────────────────────────────

class TestIsPrivateIp:
    @pytest.mark.parametrize("ip", [
        "10.0.0.1", "10.255.255.255",
        "192.168.0.1", "192.168.255.254",
        "172.16.0.1", "172.31.255.254", "172.20.10.1",
    ])
    def test_rfc1918_ranges(self, ip):
        assert utils._is_private_ip(ip) is True

    @pytest.mark.parametrize("ip", [
        "8.8.8.8", "203.0.113.9",
        "172.15.0.1",       # just below the private block
        "172.32.0.1",       # just above it
        "100.67.72.79",     # CGNAT, not RFC1918
        "127.0.0.1",
        "11.0.0.1",
        "193.168.1.1",      # one digit off 192.168
    ])
    def test_public_and_near_miss_ranges(self, ip):
        assert utils._is_private_ip(ip) is False

    @pytest.mark.parametrize("ip", ["172.abc.0.1", "172.", "172", "", "1.2.3.4"])
    def test_malformed_input_never_raises(self, ip):
        assert utils._is_private_ip(ip) in (True, False)

    def test_hundred_prefix_is_not_mistaken_for_ten(self):
        """'100.64.x' must not match the '10.' prefix test."""
        assert utils._is_private_ip("100.64.1.1") is False


# ── _is_loopback_or_special ────────────────────────────────────────────────

class TestIsLoopbackOrSpecial:
    @pytest.mark.parametrize("ip", [
        "127.0.0.1", "127.255.255.254",
        "169.254.1.1",          # link-local / APIPA
        "100.67.72.79",         # Tailscale CGNAT
    ])
    def test_special_ranges(self, ip):
        assert utils._is_loopback_or_special(ip) is True

    @pytest.mark.parametrize("ip", [
        "192.168.1.10", "10.0.0.1", "172.17.0.1", "8.8.8.8",
        "128.0.0.1",            # near-miss on '127.'
        "169.255.0.1",          # near-miss on '169.254.'
    ])
    def test_ordinary_ranges(self, ip):
        assert utils._is_loopback_or_special(ip) is False

    @pytest.mark.parametrize("ip,special", [
        ("100.63.255.255", False),   # last address below CGNAT
        ("100.64.0.0", True),        # first CGNAT address
        ("100.127.255.255", True),   # last CGNAT address
        ("100.128.0.0", False),      # first address above CGNAT
    ])
    def test_cgnat_is_only_the_slash_ten(self, ip, special):
        """CGNAT is 100.64.0.0/10, not all of 100.0.0.0/8.

        The check was a bare ``startswith("100.")``, so publicly routable
        addresses either side of the real range were demoted to rank 3 and
        dropped from lan_ip_candidates without a word — on a machine whose
        only usable address was one of them, that left nothing to advertise.
        """
        assert utils._is_loopback_or_special(ip) is special

    def test_a_public_hundred_address_stays_advertisable(self, monkeypatch):
        """The user-visible half: it must survive into the candidate list."""
        monkeypatch.setattr(
            utils, "_gather_candidate_ips", lambda: ["100.1.2.3"],
        )
        assert utils.lan_ip_candidates() == ["100.1.2.3"]

    @pytest.mark.parametrize("ip", ["", "not-an-ip", "10.0.0", "10.0.0.256"])
    def test_unparseable_input_is_not_special(self, ip):
        """Unchanged from the string-prefix version: classify only what parses."""
        assert utils._is_loopback_or_special(ip) is False


# ── _is_virtual_adapter_ip ─────────────────────────────────────────────────

class TestIsVirtualAdapterIp:
    @pytest.mark.parametrize("ip", [
        "192.168.56.1", "192.168.99.1", "192.168.137.1", "10.0.75.1",
        "172.17.0.1", "172.22.96.1", "172.31.0.1",
    ])
    def test_known_virtual_ranges(self, ip):
        assert utils._is_virtual_adapter_ip(ip) is True

    @pytest.mark.parametrize("ip", [
        "192.168.5.1",      # not 192.168.56
        "192.168.560.1",    # prefix requires the trailing dot
        "10.0.7.1",
        "172.16.0.2",       # private but a real LAN range
        "172.32.0.1",
    ])
    def test_real_ranges(self, ip):
        assert utils._is_virtual_adapter_ip(ip) is False

    def test_every_prefix_in_the_table_is_flagged(self):
        for prefix in utils._VIRTUAL_IP_PREFIXES:
            assert utils._is_virtual_adapter_ip(prefix + "5")


# ── _rank_ip ───────────────────────────────────────────────────────────────

class TestRankIp:
    def test_rank_values(self):
        assert utils._rank_ip("192.168.1.10") == 0    # real LAN
        assert utils._rank_ip("8.8.8.8") == 1         # public
        assert utils._rank_ip("172.17.0.1") == 2      # virtual adapter
        assert utils._rank_ip("127.0.0.1") == 3       # loopback/special

    def test_full_ordering(self):
        ranked = sorted(
            ["127.0.0.1", "172.17.0.1", "8.8.8.8", "192.168.1.10"],
            key=utils._rank_ip,
        )
        assert ranked == ["192.168.1.10", "8.8.8.8", "172.17.0.1", "127.0.0.1"]

    def test_special_beats_nothing(self):
        """Loopback / link-local / CGNAT is always the worst choice."""
        for other in ("192.168.1.10", "8.8.8.8", "172.17.0.1"):
            assert utils._rank_ip(other) < utils._rank_ip("169.254.1.1")

    def test_virtual_check_runs_before_the_private_check(self):
        """172.17.0.1 is RFC1918, so order matters: virtual must win."""
        assert utils._is_private_ip("172.17.0.1") is True
        assert utils._rank_ip("172.17.0.1") == 2


# ── lan_ip_candidates ──────────────────────────────────────────────────────

class TestLanIpCandidates:
    def test_sorting_is_stable_within_a_rank(self, monkeypatch):
        monkeypatch.setattr(
            utils, "_gather_candidate_ips",
            lambda: ["10.1.1.1", "192.168.1.10", "172.16.0.2"],
        )
        assert utils.lan_ip_candidates() == [
            "10.1.1.1", "192.168.1.10", "172.16.0.2",
        ]

    def test_public_address_kept_but_demoted(self, monkeypatch):
        monkeypatch.setattr(
            utils, "_gather_candidate_ips",
            lambda: ["203.0.113.9", "172.17.0.1", "192.168.1.10"],
        )
        assert utils.lan_ip_candidates() == [
            "192.168.1.10", "203.0.113.9", "172.17.0.1",
        ]

    def test_empty_when_nothing_is_routable(self, monkeypatch):
        monkeypatch.setattr(
            utils, "_gather_candidate_ips", lambda: ["127.0.0.1", "169.254.9.9"],
        )
        assert utils.lan_ip_candidates() == []

    def test_gather_failure_returns_empty_list(self, monkeypatch):
        def boom():
            raise OSError("no interfaces")
        monkeypatch.setattr(utils, "_gather_candidate_ips", boom)
        assert utils.lan_ip_candidates() == []

    def test_no_interfaces_at_all(self, monkeypatch):
        monkeypatch.setattr(utils, "_gather_candidate_ips", lambda: [])
        assert utils.lan_ip_candidates() == []

    def test_candidates_are_deduplicated(self, monkeypatch):
        """Observed live: the default-route address arriving twice.

        _gather_candidate_ips de-duplicated the getaddrinfo answers only
        against each other, never against the default-route address it had
        already appended, and lan_ip_candidates did not de-duplicate at all.
        Where the hostname resolves to the default-route address — the common
        case — the list came back as
        ``['10.0.0.7', '10.0.0.7', '172.22.96.1']``.
        """
        monkeypatch.setattr(
            utils, "_gather_candidate_ips",
            lambda: ["10.0.0.7", "10.0.0.7", "172.22.96.1"],
        )
        assert utils.lan_ip_candidates() == ["10.0.0.7", "172.22.96.1"]

    def test_deduplication_keeps_the_best_ranked_position(self, monkeypatch):
        """The survivor is the first occurrence *after* ranking, not before."""
        monkeypatch.setattr(
            utils, "_gather_candidate_ips",
            lambda: ["172.17.0.1", "192.168.1.10", "172.17.0.1", "192.168.1.10"],
        )
        assert utils.lan_ip_candidates() == ["192.168.1.10", "172.17.0.1"]

    def test_duplicates_no_longer_reach_show_ip_alternatives(self, monkeypatch, capsys):
        """The user-visible half of the same bug: one address, printed twice."""
        monkeypatch.delenv("GPUMESH_HOST_IP", raising=False)
        monkeypatch.setattr(
            utils, "_gather_candidate_ips",
            lambda: ["192.168.1.10", "172.22.96.1", "172.22.96.1"],
        )
        utils.show_ip_alternatives("192.168.1.10", 8000)
        out = capsys.readouterr().out
        assert out.count("http://172.22.96.1:8000") == 1

    def test_duplicates_do_not_eat_a_fallback_slot(self, monkeypatch):
        """The other half: a peer was offered fewer *distinct* URLs than asked.

        coordinator_url_candidates(limit=4) spent its budget on repeats, so a
        worker that could not reach the first address had fewer genuinely
        different things left to try.
        """
        monkeypatch.delenv("GPUMESH_HOST_IP", raising=False)
        monkeypatch.setattr(utils, "local_ip_for_peer", lambda ip: None)
        monkeypatch.setattr(
            utils, "_gather_candidate_ips",
            lambda: ["10.0.0.1", "10.0.0.1", "10.0.0.2", "10.0.0.2",
                     "10.0.0.3", "10.0.0.4"],
        )
        assert utils.coordinator_url_candidates("192.168.1.55", 8000) == [
            "http://10.0.0.1:8000", "http://10.0.0.2:8000",
            "http://10.0.0.3:8000", "http://10.0.0.4:8000",
        ]

    def test_the_hostname_lookup_does_not_repeat_the_default_route(self, monkeypatch):
        """De-duplication starts at the source, where the pair is created.

        The default-route address is appended first, then getaddrinfo on the
        bare hostname usually reports the very same address.
        """
        class _RouteSocket:
            def connect(self, address):
                pass

            def getsockname(self):
                return ("10.0.0.7", 51234)

            def close(self):
                pass

        monkeypatch.setattr(utils.socket, "socket", lambda *a, **kw: _RouteSocket())
        monkeypatch.setattr(utils.socket, "gethostname", lambda: "this-box")
        monkeypatch.setattr(
            utils.socket, "getaddrinfo",
            lambda host, port: [
                (2, 1, 6, "", ("10.0.0.7", 0)),
                (2, 1, 6, "", ("172.22.96.1", 0)),
            ],
        )
        assert utils._gather_candidate_ips() == ["10.0.0.7", "172.22.96.1"]


# ── get_lan_ip ─────────────────────────────────────────────────────────────

@pytest.fixture
def warnable(monkeypatch):
    """Reset the once-only warning latch so each test can observe it."""
    monkeypatch.setattr(utils, "_warned_invalid_host_ip", False)


class TestGetLanIp:
    def test_override_is_stripped(self, monkeypatch):
        monkeypatch.setenv("GPUMESH_HOST_IP", "  203.0.113.9  ")
        assert utils.get_lan_ip() == "203.0.113.9"

    def test_invalid_override_is_rejected(self, monkeypatch, warnable, capsys):
        """A typo used to travel verbatim into every advertised URL.

        It then failed minutes later on another machine, as a connection error
        whose operator never set the variable and could not guess what it was.
        Reject it where it is read, say so, and let detection take over.
        """
        monkeypatch.setenv("GPUMESH_HOST_IP", "not-an-ip")
        monkeypatch.setattr(
            utils, "_gather_candidate_ips", lambda: ["192.168.1.10"],
        )
        assert utils.get_lan_ip() == "192.168.1.10"
        out = capsys.readouterr().out
        assert "GPUMESH_HOST_IP" in out
        assert "not-an-ip" in out

    @pytest.mark.parametrize("value", [
        "192.168.1..10",     # doubled dot
        "192.168.1.300",     # octet out of range
        "192.168.1",         # short
        "http://192.168.1.10",   # a URL, not an address
        "192.168.1.10:8000",     # address with a port
    ])
    def test_typo_shapes_are_all_rejected(self, value, monkeypatch, warnable):
        monkeypatch.setenv("GPUMESH_HOST_IP", value)
        monkeypatch.setattr(
            utils, "_gather_candidate_ips", lambda: ["192.168.1.10"],
        )
        assert utils.get_lan_ip() == "192.168.1.10"

    def test_ipv6_pin_is_accepted(self, monkeypatch, warnable):
        """Validation asks "is this an address", and does not narrow to v4.

        Whether the rest of the module can *use* one is a separate, older
        question — ``http://{ip}:{port}`` needs brackets around a v6 literal —
        but that was equally true before, and the check added here is not the
        place to start rejecting addresses it never rejected.
        """
        monkeypatch.setenv("GPUMESH_HOST_IP", "fd00::1")
        assert utils.get_lan_ip() == "fd00::1"

    def test_the_complaint_is_made_only_once(self, monkeypatch, warnable, capsys):
        """The override is re-read on every claim; one worker, one warning."""
        monkeypatch.setenv("GPUMESH_HOST_IP", "nonsense")
        monkeypatch.setattr(utils, "_gather_candidate_ips", lambda: [])
        for _ in range(3):
            utils.get_lan_ip()
        assert capsys.readouterr().out.count("GPUMESH_HOST_IP") == 1

    def test_an_invalid_pin_does_not_silence_the_alternatives(
            self, monkeypatch, warnable, capsys):
        """A valid pin means the user chose; an invalid one means they did not.

        Detection is back in charge, so the list of other addresses to try is
        worth printing again.
        """
        monkeypatch.setenv("GPUMESH_HOST_IP", "nonsense")
        monkeypatch.setattr(
            utils, "_gather_candidate_ips",
            lambda: ["192.168.1.10", "172.22.96.1"],
        )
        utils.show_ip_alternatives("192.168.1.10", 8000)
        assert "http://172.22.96.1:8000" in capsys.readouterr().out

    def test_link_local_only_machine_keeps_its_address(self, monkeypatch):
        """Everything is 'special', so the raw default-route answer is used."""
        monkeypatch.setattr(
            utils, "_gather_candidate_ips", lambda: ["169.254.10.20"],
        )
        assert utils.get_lan_ip() == "169.254.10.20"

    def test_unexpected_failure_falls_back_to_loopback(self, monkeypatch):
        def boom():
            raise RuntimeError("interface enumeration exploded")
        monkeypatch.setattr(utils, "lan_ip_candidates", boom)
        assert utils.get_lan_ip() == "127.0.0.1"

    def test_never_returns_none(self, monkeypatch):
        monkeypatch.setattr(utils, "_gather_candidate_ips", lambda: [])
        assert isinstance(utils.get_lan_ip(), str)


# ── local_ip_for_peer ──────────────────────────────────────────────────────

class _FakeSocket:
    """Minimal UDP socket stand-in; records use and never touches the network."""

    def __init__(self, *args, **kwargs):
        self.connected_to = None
        self.closed = False
        self.connect_error = None
        self.local_addr = ("192.168.1.10", 51234)

    def connect(self, address):
        self.connected_to = address
        if self.connect_error is not None:
            raise self.connect_error

    def getsockname(self):
        return self.local_addr

    def close(self):
        self.closed = True


@pytest.fixture
def fake_socket(monkeypatch):
    """Install a fake ``socket.socket`` and hand back the instance created."""
    created = []

    def _factory(*args, **kwargs):
        sock = _FakeSocket(*args, **kwargs)
        created.append(sock)
        return sock

    monkeypatch.setattr(utils.socket, "socket", _factory)
    return created


class TestLocalIpForPeer:
    def test_empty_peer_returns_none_without_a_socket(self, fake_socket):
        assert utils.local_ip_for_peer("") is None
        assert utils.local_ip_for_peer(None) is None
        assert fake_socket == []

    def test_returns_the_source_address_for_the_route(self, fake_socket):
        assert utils.local_ip_for_peer("192.168.1.55") == "192.168.1.10"
        assert fake_socket[0].connected_to == ("192.168.1.55", 9)
        assert fake_socket[0].closed is True

    def test_no_route_returns_none(self, monkeypatch):
        def _factory(*args, **kwargs):
            sock = _FakeSocket()
            sock.connect_error = OSError("network is unreachable")
            return sock

        monkeypatch.setattr(utils.socket, "socket", _factory)
        assert utils.local_ip_for_peer("10.9.9.9") is None

    def test_socket_is_closed_even_on_failure(self, monkeypatch):
        made = []

        def _factory(*args, **kwargs):
            sock = _FakeSocket()
            sock.connect_error = OSError("no route")
            made.append(sock)
            return sock

        monkeypatch.setattr(utils.socket, "socket", _factory)
        utils.local_ip_for_peer("10.9.9.9")
        assert made[0].closed is True

    def test_loopback_peer_resolves_locally(self):
        """One real-socket check: loopback always has a route, no network needed."""
        assert utils.local_ip_for_peer("127.0.0.1") == "127.0.0.1"

    def test_uses_a_udp_socket(self, monkeypatch):
        """Route lookup only — a TCP socket here would actually connect."""
        seen = []

        def _factory(family, type_, *args, **kwargs):
            seen.append((family, type_))
            return _FakeSocket()

        monkeypatch.setattr(utils.socket, "socket", _factory)
        utils.local_ip_for_peer("192.168.1.55")
        assert seen == [(socket.AF_INET, socket.SOCK_DGRAM)]


# ── coordinator_url_candidates ─────────────────────────────────────────────

class TestCoordinatorUrlCandidates:
    def test_route_derived_address_leads(self, monkeypatch):
        monkeypatch.delenv("GPUMESH_HOST_IP", raising=False)
        monkeypatch.setattr(utils, "local_ip_for_peer", lambda ip: "192.168.1.10")
        monkeypatch.setattr(utils, "lan_ip_candidates",
                            lambda: ["10.0.0.5", "192.168.1.10"])
        assert utils.coordinator_url_candidates("192.168.1.55", 8000) == [
            "http://192.168.1.10:8000",
            "http://10.0.0.5:8000",
        ]

    def test_pinned_host_ip_outranks_the_route(self, monkeypatch):
        monkeypatch.setenv("GPUMESH_HOST_IP", "203.0.113.9")
        monkeypatch.setattr(utils, "local_ip_for_peer", lambda ip: "192.168.1.10")
        monkeypatch.setattr(utils, "lan_ip_candidates", lambda: ["192.168.1.10"])
        assert utils.coordinator_url_candidates("192.168.1.55", 8000)[0] == \
            "http://203.0.113.9:8000"

    def test_pinned_host_ip_is_not_listed_twice(self, monkeypatch):
        monkeypatch.setenv("GPUMESH_HOST_IP", "192.168.1.10")
        monkeypatch.setattr(utils, "local_ip_for_peer", lambda ip: "192.168.1.10")
        monkeypatch.setattr(utils, "lan_ip_candidates", lambda: ["192.168.1.10"])
        assert utils.coordinator_url_candidates("192.168.1.55", 8000) == [
            "http://192.168.1.10:8000",
        ]

    def test_invalid_pin_is_ignored(self, monkeypatch, warnable):
        """A typo must not outrank the address the routing table computed."""
        monkeypatch.setenv("GPUMESH_HOST_IP", "192.168.1.10:8000")
        monkeypatch.setattr(utils, "local_ip_for_peer", lambda ip: "192.168.1.10")
        monkeypatch.setattr(utils, "lan_ip_candidates", lambda: [])
        assert utils.coordinator_url_candidates("192.168.1.55", 8000) == [
            "http://192.168.1.10:8000",
        ]

    def test_blank_pin_is_ignored(self, monkeypatch):
        monkeypatch.setenv("GPUMESH_HOST_IP", "   ")
        monkeypatch.setattr(utils, "local_ip_for_peer", lambda ip: "192.168.1.10")
        monkeypatch.setattr(utils, "lan_ip_candidates", lambda: [])
        assert utils.coordinator_url_candidates("192.168.1.55", 8000) == [
            "http://192.168.1.10:8000",
        ]

    def test_loopback_route_is_not_advertised(self, monkeypatch):
        """A worker handed http://127.0.0.1:8000 would dial *itself*."""
        monkeypatch.delenv("GPUMESH_HOST_IP", raising=False)
        monkeypatch.setattr(utils, "local_ip_for_peer", lambda ip: "127.0.0.1")
        monkeypatch.setattr(utils, "lan_ip_candidates", lambda: ["192.168.1.10"])
        assert utils.coordinator_url_candidates("192.168.1.55", 8000) == [
            "http://192.168.1.10:8000",
        ]

    def test_no_route_still_offers_the_general_candidates(self, monkeypatch):
        monkeypatch.delenv("GPUMESH_HOST_IP", raising=False)
        monkeypatch.setattr(utils, "local_ip_for_peer", lambda ip: None)
        monkeypatch.setattr(utils, "lan_ip_candidates",
                            lambda: ["192.168.1.10", "172.17.0.1"])
        assert utils.coordinator_url_candidates("192.168.1.55", 8000) == [
            "http://192.168.1.10:8000",
            "http://172.17.0.1:8000",
        ]

    def test_result_can_be_empty(self, monkeypatch):
        monkeypatch.delenv("GPUMESH_HOST_IP", raising=False)
        monkeypatch.setattr(utils, "local_ip_for_peer", lambda ip: None)
        monkeypatch.setattr(utils, "lan_ip_candidates", lambda: [])
        assert utils.coordinator_url_candidates("192.168.1.55", 8000) == []

    def test_limit_is_respected(self, monkeypatch):
        monkeypatch.delenv("GPUMESH_HOST_IP", raising=False)
        monkeypatch.setattr(utils, "local_ip_for_peer", lambda ip: "10.0.0.1")
        monkeypatch.setattr(
            utils, "lan_ip_candidates",
            lambda: ["10.0.0.2", "10.0.0.3", "10.0.0.4", "10.0.0.5", "10.0.0.6"],
        )
        assert len(utils.coordinator_url_candidates("192.168.1.55", 8000)) == 4
        assert len(
            utils.coordinator_url_candidates("192.168.1.55", 8000, limit=2)
        ) == 2

    def test_duplicate_candidates_are_collapsed(self, monkeypatch):
        """This caller *does* de-duplicate, unlike lan_ip_candidates itself."""
        monkeypatch.delenv("GPUMESH_HOST_IP", raising=False)
        monkeypatch.setattr(utils, "local_ip_for_peer", lambda ip: "192.168.1.10")
        monkeypatch.setattr(
            utils, "lan_ip_candidates",
            lambda: ["192.168.1.10", "192.168.1.10", "172.17.0.1"],
        )
        assert utils.coordinator_url_candidates("192.168.1.55", 8000) == [
            "http://192.168.1.10:8000",
            "http://172.17.0.1:8000",
        ]

    def test_port_is_carried_into_every_url(self, monkeypatch):
        monkeypatch.delenv("GPUMESH_HOST_IP", raising=False)
        monkeypatch.setattr(utils, "local_ip_for_peer", lambda ip: "192.168.1.10")
        monkeypatch.setattr(utils, "lan_ip_candidates", lambda: ["10.0.0.5"])
        urls = utils.coordinator_url_candidates("192.168.1.55", 9001)
        assert all(url.endswith(":9001") for url in urls)


# ── firewall helpers ───────────────────────────────────────────────────────

class TestFirewallHelpers:
    def test_add_rule_is_a_no_op_off_windows(self, monkeypatch, capsys):
        monkeypatch.setattr(utils.platform, "system", lambda: "Linux")
        assert utils.try_add_firewall_rule(8000) is True
        assert capsys.readouterr().out == ""

    def test_add_rule_reports_failure_rather_than_raising(self, monkeypatch):
        """netsh unavailable, or no admin rights: return False, never raise."""
        monkeypatch.setattr(utils.platform, "system", lambda: "Windows")

        def boom(*args, **kwargs):
            raise OSError("netsh is not on PATH")

        monkeypatch.setattr(utils.subprocess, "run", boom)
        assert utils.try_add_firewall_rule(8000) is False

    def test_hint_is_silent_off_windows(self, monkeypatch, capsys):
        monkeypatch.setattr(utils.platform, "system", lambda: "Linux")
        utils.show_firewall_hint(8000)
        assert capsys.readouterr().out == ""

    def test_hint_names_the_port_on_windows(self, monkeypatch, capsys):
        monkeypatch.setattr(utils.platform, "system", lambda: "Windows")
        utils.show_firewall_hint(9001)
        out = capsys.readouterr().out
        assert "9001" in out
        assert "Firewall" in out
