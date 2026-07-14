"""Tests for gpumesh.radar module — live terminal radar UI."""

import sys
from unittest.mock import MagicMock, patch

import pytest

from gpumesh.discovery import Peer
from gpumesh.radar import (
    _format_peer_line,
    print_radar_header,
    print_radar_peers,
    select_peer,
)


class TestFormatPeerLine:
    """Tests for peer line formatting."""

    def test_format_cuda_peer(self):
        peer = Peer({
            "hostname": "gpu-pc",
            "device": "cuda",
            "device_name": "RTX 3080",
            "score": 85.2,
        }, ("192.168.1.5", 80))
        line = _format_peer_line(peer, 0)
        assert "gpu-pc" in line
        assert "RTX 3080" in line
        assert "85.2" in line
        assert "192.168.1.5" in line

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

    def test_format_mps_peer(self):
        peer = Peer({
            "hostname": "macbook",
            "device": "mps",
            "score": 20.0,
        }, ("192.168.1.10", 80))
        line = _format_peer_line(peer, 0)
        assert "macbook" in line
        assert "Apple Silicon" in line


class TestPrintRadarHeader:
    """Tests for radar header output."""

    def test_coordinator_header(self, capsys):
        print_radar_header("coordinator")
        captured = capsys.readouterr()
        assert "RADAR" in captured.out
        assert "Workers" in captured.out

    def test_worker_header(self, capsys):
        print_radar_header("worker")
        captured = capsys.readouterr()
        assert "RADAR" in captured.out
        assert "Coordinators" in captured.out


class TestPrintRadarPeers:
    """Tests for radar peer list printing."""

    def test_empty_peers(self, capsys):
        count = print_radar_peers([], prev_count=0)
        captured = capsys.readouterr()
        assert "No devices found" in captured.out
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
        assert count == 2

    def test_sorted_by_hostname(self, capsys):
        peers = [
            Peer({"hostname": "zebra", "device": "cpu", "score": 1.0},
                 ("1.2.3.4", 80)),
            Peer({"hostname": "alpha", "device": "cpu", "score": 1.0},
                 ("1.2.3.5", 80)),
        ]
        count = print_radar_peers(peers, prev_count=0)
        captured = capsys.readouterr()
        # alpha should appear before zebra
        assert captured.out.index("alpha") < captured.out.index("zebra")


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
