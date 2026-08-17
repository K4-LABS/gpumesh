"""Tests for gpumesh.radar module — live terminal radar UI."""

import sys
from unittest.mock import MagicMock, patch

import pytest

from gpumesh.discovery import Peer
from gpumesh.radar import (
    _format_peer_line,
    _get_device_color,
    _get_status_icon,
    print_radar_header,
    print_radar_peers,
    select_peer,
    select_worker_for_claim,
)


class TestFormatPeerLine:
    """Tests for peer line formatting."""

    def test_format_cuda_peer(self):
        peer = Peer({
            "hostname": "gpu-pc",
            "device": "cuda",
            "device_name": "RTX 3080",
            "score": 85.2,
            "claim_port": 8001,
        }, ("192.168.1.5", 80))
        line = _format_peer_line(peer, 0)
        assert "gpu-pc" in line
        assert "RTX 3080" in line
        assert "85.2" in line
        assert "192.168.1.5" in line
        assert "GPU" in line
        assert "CLAIM" in line

    def test_format_cpu_peer(self):
        peer = Peer({
            "hostname": "laptop",
            "device": "cpu",
            "score": 1.5,
        }, ("192.168.1.8", 80))
        line = _format_peer_line(peer, 0)
        assert "laptop" in line
        assert "CPU" in line
        assert "1.5" in line
        assert "192.168.1.8" in line

    def test_format_mps_peer(self):
        peer = Peer({
            "hostname": "macbook",
            "device": "mps",
            "score": 20.0,
        }, ("192.168.1.10", 80))
        line = _format_peer_line(peer, 0)
        assert "macbook" in line
        assert "Apple Silicon" in line
        assert "GPU" in line

    def test_format_peer_with_claim_port(self):
        peer = Peer({
            "hostname": "gpu-worker",
            "device": "cuda",
            "score": 50.0,
            "claim_port": 8001,
        }, ("192.168.1.20", 80))
        line = _format_peer_line(peer, 0)
        assert "CLAIM" in line

    def test_format_peer_without_claim_port(self):
        peer = Peer({
            "hostname": "cpu-node",
            "device": "cpu",
            "score": 5.0,
        }, ("192.168.1.25", 80))
        line = _format_peer_line(peer, 0)
        assert "-----" in line


class TestGetDeviceColor:
    """Tests for device color function."""

    def test_cuda_device_color(self):
        from gpumesh.ansi import green
        color_func = _get_device_color("cuda")
        assert color_func == green

    def test_mps_device_color(self):
        from gpumesh.ansi import magenta
        color_func = _get_device_color("mps")
        assert color_func == magenta

    def test_cpu_device_color(self):
        from gpumesh.ansi import yellow
        color_func = _get_device_color("cpu")
        assert color_func == yellow


class TestGetStatusIcon:
    """Tests for status icon function."""

    def test_offline_peer_icon(self):
        peer = Peer({
            "hostname": "offline-pc",
            "device": "cpu",
            "score": 0.0,
        }, ("192.168.1.5", 80))
        peer.last_seen = 0  # Make peer stale
        icon = _get_status_icon(peer)
        assert "x" in icon

    def test_high_score_peer_icon(self):
        peer = Peer({
            "hostname": "fast-pc",
            "device": "cuda",
            "score": 85.0,
        }, ("192.168.1.5", 80))
        icon = _get_status_icon(peer)
        assert "+" in icon

    def test_medium_score_peer_icon(self):
        peer = Peer({
            "hostname": "medium-pc",
            "device": "cuda",
            "score": 25.0,
        }, ("192.168.1.5", 80))
        icon = _get_status_icon(peer)
        assert "+" in icon

    def test_low_score_peer_icon(self):
        peer = Peer({
            "hostname": "slow-pc",
            "device": "cpu",
            "score": 5.0,
        }, ("192.168.1.5", 80))
        icon = _get_status_icon(peer)
        assert "+" in icon


class TestPrintRadarHeader:
    """Tests for radar header output."""

    def test_coordinator_header(self, capsys):
        print_radar_header("coordinator")
        captured = capsys.readouterr()
        assert "RADAR" in captured.out
        assert "Workers" in captured.out
        assert "Scanning network for GPU nodes..." in captured.out

    def test_worker_header(self, capsys):
        print_radar_header("worker")
        captured = capsys.readouterr()
        assert "RADAR" in captured.out
        assert "Coordinators" in captured.out
        assert "Broadcasting presence" in captured.out

    def test_header_legend(self, capsys):
        print_radar_header("coordinator")
        captured = capsys.readouterr()
        assert "Legend:" in captured.out
        assert "online" in captured.out
        assert "offline" in captured.out


class TestPrintRadarPeers:
    """Tests for radar peer list printing."""

    def test_empty_peers(self, capsys):
        count = print_radar_peers([], prev_count=0)
        captured = capsys.readouterr()
        assert "Scanning for devices" in captured.out
        assert count == 1

    def test_with_peers(self, capsys):
        peers = [
            Peer({"hostname": "pc1", "device": "cuda", "score": 85.0},
                 ("1.2.3.4", 80)),
            Peer({"hostname": "pc2", "device": "cpu", "score": 1.0},
                 ("1.2.3.5", 80)),
        ]
        count = print_radar_peers(peers, prev_count=0)
        captured = capsys.readouterr()
        assert "pc1" in captured.out
        assert "pc2" in captured.out
        assert "GPU" in captured.out
        assert "CPU" in captured.out
        assert "Network Topology:" in captured.out

    def test_sorted_by_score(self, capsys):
        peers = [
            Peer({"hostname": "slow", "device": "cpu", "score": 1.0},
                 ("1.2.3.4", 80)),
            Peer({"hostname": "fast", "device": "cuda", "score": 85.0},
                 ("1.2.3.5", 80)),
        ]
        count = print_radar_peers(peers, prev_count=0)
        captured = capsys.readouterr()
        # fast should appear before slow (higher score first)
        assert captured.out.index("fast") < captured.out.index("slow")

    def test_network_topology_display(self, capsys):
        peers = [
            Peer({"hostname": "pc1", "device": "cuda", "score": 85.0},
                 ("1.2.3.4", 80)),
            Peer({"hostname": "pc2", "device": "cpu", "score": 1.0},
                 ("1.2.3.5", 80)),
        ]
        count = print_radar_peers(peers, prev_count=0)
        captured = capsys.readouterr()
        assert "CUDA GPUs: 1" in captured.out
        assert "CPUs: 1" in captured.out
        assert "Total: 2 nodes" in captured.out


class TestSelectPeer:
    """Tests for interactive peer selection."""

    def test_select_valid_peer(self):
        peers = [
            Peer({"hostname": "pc1", "device": "cuda", "score": 85.0},
                 ("1.2.3.4", 80)),
            Peer({"hostname": "pc2", "device": "cpu", "score": 1.0},
                 ("1.2.3.5", 80)),
        ]
        with patch("builtins.input", return_value="1"):
            selected = select_peer(peers)
            assert selected is not None
            assert selected.hostname == "pc1"

    def test_select_second_peer(self):
        peers = [
            Peer({"hostname": "pc1", "device": "cuda", "score": 85.0},
                 ("1.2.3.4", 80)),
            Peer({"hostname": "pc2", "device": "cpu", "score": 1.0},
                 ("1.2.3.5", 80)),
        ]
        with patch("builtins.input", return_value="2"):
            selected = select_peer(peers)
            assert selected is not None
            assert selected.hostname == "pc2"

    def test_select_empty_peers(self):
        with patch("builtins.input", return_value="1"):
            selected = select_peer([])
            assert selected is None

    def test_select_invalid_number(self, capsys):
        peers = [
            Peer({"hostname": "pc1", "device": "cpu", "score": 1.0},
                 ("1.2.3.4", 80)),
        ]
        with patch("builtins.input", return_value="99"):
            selected = select_peer(peers)
            assert selected is None
            captured = capsys.readouterr()
            assert "Invalid choice" in captured.out

    def test_select_non_number(self, capsys):
        peers = [
            Peer({"hostname": "pc1", "device": "cpu", "score": 1.0},
                 ("1.2.3.4", 80)),
        ]
        with patch("builtins.input", return_value="abc"):
            selected = select_peer(peers)
            assert selected is None

    def test_select_eof(self):
        peers = [
            Peer({"hostname": "pc1", "device": "cpu", "score": 1.0},
                 ("1.2.3.4", 80)),
        ]
        with patch("builtins.input", side_effect=EOFError):
            selected = select_peer(peers)
            assert selected is None

    def test_select_keyboard_interrupt(self):
        peers = [
            Peer({"hostname": "pc1", "device": "cpu", "score": 1.0},
                 ("1.2.3.4", 80)),
        ]
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            selected = select_peer(peers)
            assert selected is None

    def test_select_peer_sorted_by_score(self):
        peers = [
            Peer({"hostname": "slow", "device": "cpu", "score": 1.0},
                 ("1.2.3.4", 80)),
            Peer({"hostname": "fast", "device": "cuda", "score": 85.0},
                 ("1.2.3.5", 80)),
        ]
        with patch("builtins.input", return_value="1"):
            selected = select_peer(peers)
            assert selected is not None
            assert selected.hostname == "fast"

    def test_select_peer_shows_gpu_status(self, capsys):
        peers = [
            Peer({"hostname": "gpu-pc", "device": "cuda", "score": 85.0},
                 ("1.2.3.4", 80)),
        ]
        with patch("builtins.input", return_value="1"):
            selected = select_peer(peers)
            captured = capsys.readouterr()
            assert "GPU" in captured.out
            assert "CLAIM" in captured.out or "-----" in captured.out


class TestSelectWorkerForClaim:
    """The claim flow must read the token without echoing it."""

    @staticmethod
    def _peer(hostname="pc1", score=85.0, claim_port=8001):
        return Peer({
            "hostname": hostname,
            "device": "cpu",
            "score": score,
            "claim_port": claim_port,
        }, ("1.2.3.4", 80))

    def test_token_read_via_getpass_not_input(self):
        """The token prompt must not echo — getpass, never input()."""
        peers = [self._peer()]
        with patch("builtins.input", return_value="1") as mock_input, \
             patch("gpumesh.radar.getpass.getpass",
                   return_value="s3cret-token") as mock_getpass:
            peer, token = select_worker_for_claim(peers)
        assert peer is not None and peer.hostname == "pc1"
        assert token == "s3cret-token"
        # Exactly one plaintext input() (the peer pick), one getpass (the
        # token). The token must never travel through the echoing input().
        assert mock_input.call_count == 1
        assert mock_getpass.call_count == 1
        assert "s3cret-token" not in str(mock_input.call_args)

    def test_getpass_prompt_names_the_worker(self):
        peers = [self._peer(hostname="shreyash")]
        with patch("builtins.input", return_value="1"), \
             patch("gpumesh.radar.getpass.getpass") as mock_getpass:
            select_worker_for_claim(peers)
        prompt = mock_getpass.call_args[0][0]
        assert "shreyash" in prompt

    def test_getpass_eof_returns_none(self):
        peers = [self._peer()]
        with patch("builtins.input", return_value="1"), \
             patch("gpumesh.radar.getpass.getpass", side_effect=EOFError):
            peer, token = select_worker_for_claim(peers)
        assert peer is None and token is None

    def test_getpass_keyboard_interrupt_returns_none(self):
        peers = [self._peer()]
        with patch("builtins.input", return_value="1"), \
             patch("gpumesh.radar.getpass.getpass",
                   side_effect=KeyboardInterrupt):
            peer, token = select_worker_for_claim(peers)
        assert peer is None and token is None

    def test_empty_token_rejected(self, capsys):
        peers = [self._peer()]
        with patch("builtins.input", return_value="1"), \
             patch("gpumesh.radar.getpass.getpass", return_value="   "):
            peer, token = select_worker_for_claim(peers)
        assert peer is None and token is None
        assert "No token provided" in capsys.readouterr().out

    def test_invalid_choice_never_prompts_for_token(self):
        peers = [self._peer()]
        with patch("builtins.input", return_value="99"), \
             patch("gpumesh.radar.getpass.getpass") as mock_getpass:
            peer, token = select_worker_for_claim(peers)
        assert peer is None and token is None
        mock_getpass.assert_not_called()

    def test_empty_peers_returns_none(self):
        with patch("gpumesh.radar.getpass.getpass") as mock_getpass:
            peer, token = select_worker_for_claim([])
        assert peer is None and token is None
        mock_getpass.assert_not_called()
