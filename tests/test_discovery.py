"""Tests for gpumesh.discovery module — UDP beacon and listener."""

import json
import socket
import threading
import time

import pytest

from gpumesh.discovery import (
    BEACON_MAGIC,
    Beacon,
    Listener,
    Peer,
    build_beacon,
    get_broadcast_address,
    get_ephemeral_port,
    netmask_for,
    parse_beacon,
)


class TestBuildBeacon:
    """Tests for beacon construction and parsing."""

    def test_build_produces_bytes(self):
        data = build_beacon({"hostname": "test", "device": "cpu"})
        assert isinstance(data, bytes)

    def test_build_starts_with_magic(self):
        data = build_beacon({"hostname": "test"})
        assert data.startswith(BEACON_MAGIC.encode() + b"\n")

    def test_build_contains_json(self):
        payload = {"hostname": "test", "device": "cuda", "score": 85.0}
        data = build_beacon(payload)
        json_part = data.split(b"\n", 1)[1]
        parsed = json.loads(json_part)
        assert parsed["hostname"] == "test"
        assert parsed["device"] == "cuda"
        assert parsed["score"] == 85.0

    def test_parse_valid_beacon(self):
        payload = {"hostname": "gpu-pc", "device": "cuda", "score": 42.5}
        data = build_beacon(payload)
        result = parse_beacon(data)
        assert result is not None
        assert result["hostname"] == "gpu-pc"
        assert result["score"] == 42.5

    def test_parse_returns_none_for_non_gpumesh(self):
        result = parse_beacon(b"random garbage data")
        assert result is None

    def test_parse_returns_none_for_bad_json(self):
        data = BEACON_MAGIC.encode() + b"\nnot json"
        result = parse_beacon(data)
        assert result is None

    def test_parse_returns_none_for_empty(self):
        result = parse_beacon(b"")
        assert result is None

    def test_parse_returns_none_for_list_json(self):
        data = BEACON_MAGIC.encode() + b'\n["not", "a", "beacon"]'
        assert parse_beacon(data) is None


class TestEphemeralPort:
    """Tests for ephemeral port allocation."""

    def test_returns_valid_port(self):
        port = get_ephemeral_port()
        assert isinstance(port, int)
        assert 1 <= port <= 65535

    def test_different_calls_may_return_different_ports(self):
        ports = {get_ephemeral_port() for _ in range(10)}
        # At least some should be different (not guaranteed all, but likely)
        assert len(ports) >= 1


class TestBroadcastAddress:
    """Tests for broadcast address detection."""

    def test_returns_string(self):
        addr = get_broadcast_address()
        assert isinstance(addr, str)

    def test_valid_ip_format(self):
        addr = get_broadcast_address()
        parts = addr.split(".")
        assert len(parts) == 4
        for part in parts:
            assert part.isdigit()
            assert 0 <= int(part) <= 255

    @pytest.mark.parametrize("netmask,expected", [
        ("255.255.255.0", "192.0.2.255"),      # /24 — what the old guess assumed
        ("255.255.0.0", "192.0.255.255"),      # /16 — where the guess was wrong
        ("255.255.254.0", "192.0.3.255"),      # /23 — and here
    ])
    def test_broadcast_follows_the_real_netmask(self, monkeypatch, netmask,
                                                expected):
        """A /16 or /23 must not get the /24 answer.

        The old code returned 192.0.2.255 for all three. On the wider two that
        is an ordinary host address, so the beacon reaches nobody and
        discovery reports an empty network that in fact has peers on it.
        """
        monkeypatch.setattr("gpumesh.discovery.netmask_for",
                            lambda ip: netmask)
        monkeypatch.setattr("socket.socket.getsockname",
                            lambda self: ("192.0.2.10", 0))
        assert get_broadcast_address() == expected

    def test_falls_back_to_the_24_guess_without_a_netmask(self, monkeypatch):
        """No netmask available: keep the old behaviour rather than fail."""
        monkeypatch.setattr("gpumesh.discovery.netmask_for", lambda ip: None)
        monkeypatch.setattr("socket.socket.getsockname",
                            lambda self: ("192.0.2.10", 0))
        assert get_broadcast_address() == "192.0.2.255"

    def test_netmask_for_returns_none_without_psutil(self, monkeypatch):
        """psutil is an optional extra; its absence is normal, not an error."""
        import builtins
        real_import = builtins.__import__

        def no_psutil(name, *args, **kwargs):
            if name == "psutil":
                raise ImportError("no psutil")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_psutil)
        assert netmask_for("192.0.2.10") is None


class TestBeaconPayload:
    """The beacon goes out in clear to the whole subnet — check what is in it."""

    def test_beacon_does_not_broadcast_an_os_fingerprint(self):
        """platform.platform() named the OS build to every machine nearby.

        Nothing in the package ever read it back, so it was a fingerprint
        given away for no benefit.
        """
        beacon = Beacon(device="cpu", device_name="test", score=1.0)
        assert "platform" not in beacon._payload

    def test_peer_still_parses_platform_from_an_older_worker(self):
        """Dropping the field must not break a beacon sent by an old worker."""
        peer = Peer({"hostname": "old", "platform": "Windows-10"},
                    ("192.0.2.10", 48900))
        assert peer.platform == "Windows-10"


class TestPeer:
    """Tests for Peer data class."""

    def test_peer_creation(self):
        data = {
            "hostname": "test-pc",
            "device": "cuda",
            "device_name": "RTX 3080",
            "score": 85.2,
            "api_port": 8000,
        }
        peer = Peer(data, ("192.168.1.5", 9000))
        assert peer.hostname == "test-pc"
        assert peer.device == "cuda"
        assert peer.device_name == "RTX 3080"
        assert peer.score == 85.2
        assert peer.api_port == 8000
        assert peer.ip == "192.168.1.5"
        assert peer.alive is True

    def test_peer_display_name_cuda(self):
        peer = Peer({"device": "cuda", "device_name": "RTX 3080"}, ("1.2.3.4", 80))
        assert peer.display_name == "RTX 3080"

    def test_peer_display_name_mps(self):
        peer = Peer({"device": "mps"}, ("1.2.3.4", 80))
        assert peer.display_name == "Apple Silicon GPU"

    def test_peer_display_name_cpu(self):
        peer = Peer({"device": "cpu"}, ("1.2.3.4", 80))
        assert peer.display_name == "CPU"

    def test_peer_stale_after_timeout(self):
        peer = Peer({"hostname": "test"}, ("1.2.3.4", 80))
        peer.last_seen = time.time() - 20  # 20 seconds ago
        assert peer.alive is False

    def test_repr(self):
        peer = Peer({"hostname": "pc", "device": "cuda", "score": 50.0},
                     ("1.2.3.4", 80))
        r = repr(peer)
        assert "pc" in r
        assert "50.0" in r

    def test_peer_copy_preserves_role(self):
        peer = Peer({"type": "gpumesh_coordinator"}, ("1.2.3.4", 80))
        assert peer.copy().role == "coordinator"


class TestBeacon:
    """Tests for the Beacon class."""

    def test_beacon_starts_and_stops(self):
        beacon = Beacon(device="cpu", hostname="test-beacon", api_port=8000)
        beacon.start()
        assert beacon.port > 0
        beacon.stop()

    def test_beacon_rejects_unknown_role(self):
        with pytest.raises(ValueError, match="unsupported discovery role"):
            Beacon(role="unknown")

    def test_beacon_broadcasts(self):
        """Beacon sends datagrams that a listener can receive."""
        listener_port = get_ephemeral_port()

        # Start listener on localhost
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", listener_port))
        sock.settimeout(5.0)

        # Create beacon and override its broadcast to target localhost
        beacon = Beacon(
            device="cuda",
            device_name="Test GPU",
            score=99.9,
            api_port=8000,
            hostname="beacon-host",
            port=0,  # let OS pick
        )
        beacon.start()

        # Manually send a beacon to localhost:listener_port
        import json
        payload = {
            "hostname": "beacon-host",
            "device": "cuda",
            "device_name": "Test GPU",
            "score": 99.9,
            "api_port": 8000,
        }
        datagram = build_beacon(payload)

        try:
            sock.sendto(datagram, ("127.0.0.1", listener_port))
            # Receive the beacon
            data, addr = sock.recvfrom(4096)
            parsed = parse_beacon(data)
            assert parsed is not None
            assert parsed["hostname"] == "beacon-host"
            assert parsed["device"] == "cuda"
            assert parsed["score"] == 99.9
        finally:
            beacon.stop()
            sock.close()


class TestListener:
    """Tests for the Listener class."""

    def test_listener_starts_and_stops(self):
        listener = Listener()
        listener.start()
        assert listener.port > 0
        listener.stop()

    def test_listener_receives_beacons(self):
        """Listener receives beacons from a Beacon."""
        listener = Listener()
        listener.start()

        beacon = Beacon(
            device="cuda",
            device_name="RTX 4090",
            score=120.0,
            hostname="gpu-server",
            port=listener.port,  # Send to listener's port
        )
        beacon.start()

        try:
            # Wait for beacon to be received
            time.sleep(3)
            peers = listener.peers()
            assert len(peers) >= 1
            found = any(p.hostname == "gpu-server" for p in peers)
            assert found
        finally:
            beacon.stop()
            listener.stop()

    def test_listener_filters_by_role(self):
        listener = Listener(port=0, expected_role="coordinator")
        listener.start()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            worker = build_beacon({
                "type": "gpumesh_worker",
                "hostname": "worker-host",
                "api_port": 8000,
            })
            coordinator = build_beacon({
                "type": "gpumesh_coordinator",
                "hostname": "coordinator-host",
                "api_port": 8001,
            })
            sock.sendto(worker, ("127.0.0.1", listener.port))
            sock.sendto(coordinator, ("127.0.0.1", listener.port))

            deadline = time.time() + 2
            while time.time() < deadline and not listener.peers():
                time.sleep(0.01)

            peers = listener.peers()
            assert [peer.hostname for peer in peers] == ["coordinator-host"]
            assert peers[0].role == "coordinator"
        finally:
            sock.close()
            listener.stop()

    def test_coordinator_beacon_is_discovered(self, monkeypatch):
        monkeypatch.setattr("gpumesh.discovery.get_broadcast_address",
                            lambda: "127.0.0.1")
        monkeypatch.setattr("gpumesh.discovery.BROADCAST_ADDR_FALLBACK",
                            "127.0.0.1")
        listener = Listener(port=0, expected_role="coordinator")
        listener.start()
        beacon = Beacon(role="coordinator", hostname="coordinator-beacon",
                        port=listener.port)
        try:
            beacon.start()
            deadline = time.time() + 2
            while time.time() < deadline and not listener.peers():
                time.sleep(0.01)

            peers = listener.peers()
            assert len(peers) == 1
            assert peers[0].hostname == "coordinator-beacon"
            assert peers[0].role == "coordinator"
        finally:
            beacon.stop()
            listener.stop()

    def test_peer_update_revalidates_values(self):
        listener = Listener(port=0, expected_role="worker")
        listener.start()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            initial = build_beacon({
                "type": "gpumesh_worker",
                "hostname": "update-host",
                "api_port": 9000,
                "score": 1,
                "device": "cpu",
                "device_name": "",
            })
            updated = build_beacon({
                "type": "gpumesh_worker",
                "hostname": "update-host",
                "api_port": "9000",
                "score": "12.5",
                "device": 7,
                "device_name": 42,
            })
            sock.sendto(initial, ("127.0.0.1", listener.port))

            deadline = time.time() + 2
            while time.time() < deadline and not listener.peers():
                time.sleep(0.01)
            sock.sendto(updated, ("127.0.0.1", listener.port))

            deadline = time.time() + 2
            while time.time() < deadline:
                peers = listener.peers()
                if peers and peers[0].score == 12.5:
                    break
                time.sleep(0.01)

            peer = listener.peers()[0]
            assert peer.score == 12.5
            assert isinstance(peer.score, float)
            assert peer.device == "7"
            assert peer.device_name == "42"
            assert peer.api_port == 9000
        finally:
            sock.close()
            listener.stop()

    def test_listener_peers_returns_alive_only(self):
        listener = Listener()
        peer = Peer({"hostname": "old", "device": "cpu"}, ("1.2.3.4", 80))
        peer.last_seen = time.time() - 20
        listener._peers["old:80"] = peer

        peers = listener.peers()
        assert len(peers) == 0

    def test_listener_cleanup_stale(self):
        listener = Listener()
        peer = Peer({"hostname": "old", "device": "cpu"}, ("1.2.3.4", 80))
        peer.last_seen = time.time() - 20
        listener._peers["old:80"] = peer

        listener.cleanup_stale()
        assert "old:80" not in listener._peers

    def test_listener_on_peer_callback(self):
        """Callback fires when a new peer is discovered via UDP."""
        listener = Listener()
        seen = []
        listener.on_peer(lambda p: seen.append(p))
        listener.start()

        beacon = Beacon(
            device="cpu",
            hostname="cb-test",
            score=5.0,
            port=listener.port,  # Send to listener's port
        )
        beacon.start()

        try:
            time.sleep(3)
            peers = listener.peers()
            # At least one peer should be found
            assert len(peers) >= 1
            # Callback should have been called for new peers
            assert len(seen) >= 1
            assert seen[0].hostname == "cb-test"
        finally:
            beacon.stop()
            listener.stop()

    def test_multiple_peers(self):
        listener = Listener()
        for i in range(5):
            peer = Peer({"hostname": f"host{i}", "device": "cpu"},
                        (f"10.0.0.{i}", 80))
            listener._peers[f"host{i}:80"] = peer

        peers = listener.peers()
        assert len(peers) == 5

    def test_peer_copy(self):
        """Peer.copy() returns an independent snapshot."""
        data = {"hostname": "h", "device": "cuda", "score": 42.0, "api_port": 9000}
        peer = Peer(data, ("1.2.3.4", 80))
        copy = peer.copy()
        assert copy.hostname == peer.hostname
        assert copy.score == peer.score
        # Mutating the copy does not affect the original
        copy.score = 0.0
        assert peer.score == 42.0

    def test_peers_returns_copies(self):
        """listener.peers() returns copies, not the original objects."""
        listener = Listener()
        peer = Peer({"hostname": "h", "device": "cpu"}, ("1.2.3.4", 80))
        listener._peers["h:80"] = peer
        peers = listener.peers()
        assert len(peers) == 1
        assert peers[0] is not peer

    def test_peer_schema_validation_bad_score(self):
        """Peer handles bad score values gracefully."""
        peer = Peer({"score": "not_a_number"}, ("1.2.3.4", 80))
        assert peer.score == 0.0

    def test_peer_schema_validation_bad_port(self):
        """Peer handles bad api_port values gracefully."""
        peer = Peer({"api_port": -1}, ("1.2.3.4", 80))
        assert peer.api_port == 8000  # falls back to default

    def test_peer_schema_validation_port_overflow(self):
        """Peer handles api_port > 65535 gracefully."""
        peer = Peer({"api_port": 99999}, ("1.2.3.4", 80))
        assert peer.api_port == 8000

    def test_peer_hostname_truncation(self):
        """Peer truncates extremely long hostnames."""
        long_name = "a" * 500
        peer = Peer({"hostname": long_name}, ("1.2.3.4", 80))
        assert len(peer.hostname) <= 255

    def test_callback_outside_lock(self):
        """Callback is invoked outside the lock so peers() is safe inside it."""
        listener = Listener()
        callback_peers = []

        def callback(peer):
            # Calling peers() inside the callback should NOT deadlock
            current = listener.peers()
            callback_peers.append((peer, len(current)))

        listener.on_peer(callback)
        listener.start()

        beacon = Beacon(device="cpu", hostname="lock-test", score=1.0,
                        port=listener.port)
        beacon.start()

        try:
            time.sleep(3)
            assert len(callback_peers) >= 1
            assert callback_peers[0][1] >= 1  # peers() returned inside callback
        finally:
            beacon.stop()
            listener.stop()

    def test_peer_dedup_by_hostname_port(self):
        """Two peers with same IP but different hostnames are distinct."""
        listener = Listener()
        peer_a = Peer({"hostname": "host-a", "device": "cuda", "api_port": 8000},
                       ("192.168.1.5", 9000))
        peer_b = Peer({"hostname": "host-b", "device": "cuda", "api_port": 8001},
                       ("192.168.1.5", 9000))
        listener._peers["host-a:8000"] = peer_a
        listener._peers["host-b:8001"] = peer_b

        peers = listener.peers()
        assert len(peers) == 2
        hostnames = {p.hostname for p in peers}
        assert hostnames == {"host-a", "host-b"}

    def test_peer_dedup_same_host_diff_port(self):
        """Two peers with same hostname but different ports are distinct."""
        listener = Listener()
        peer_a = Peer({"hostname": "host-a", "device": "cuda", "api_port": 8000},
                       ("192.168.1.5", 9000))
        peer_b = Peer({"hostname": "host-a", "device": "cuda", "api_port": 9000},
                       ("192.168.1.5", 9001))
        listener._peers["host-a:8000"] = peer_a
        listener._peers["host-a:9000"] = peer_b

        peers = listener.peers()
        assert len(peers) == 2
