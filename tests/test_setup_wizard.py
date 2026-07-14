"""Tests for setup_wizard.py - Interactive setup wizard flows.

Tests the new radar-based coordinator/worker setup wizard.
"""

import os
import pytest
from unittest.mock import patch, MagicMock, call
import io
import sys
import time

_no_console = not os.environ.get("TERM") and not sys.stdout.isatty()


# Fixtures for common mocks
@pytest.fixture
def mock_tailscale_installed():
    """Mock Tailscale as installed and running."""
    with patch("gpumesh.setup_wizard._has_tailscale", return_value=True), \
         patch("gpumesh.setup_wizard._get_tailscale_ip", return_value="100.67.72.79"):
        yield


@pytest.fixture
def mock_tailscale_not_installed():
    """Mock Tailscale as not installed."""
    with patch("gpumesh.setup_wizard._has_tailscale", return_value=False), \
         patch("gpumesh.setup_wizard._get_tailscale_ip", return_value=None):
        yield


@pytest.fixture
def mock_lan_ip():
    """Mock LAN IP address."""
    with patch("gpumesh.setup_wizard.get_lan_ip", return_value="192.168.1.10"):
        yield


@pytest.fixture
def mock_gpu_detected():
    """Mock GPU detection."""
    with patch("gpumesh.setup_wizard._detect_gpu", return_value="cuda"):
        yield


@pytest.fixture
def mock_token():
    """Mock token generation."""
    with patch("gpumesh.setup_wizard.secrets.token_urlsafe", return_value="testToken123"):
        yield


@pytest.fixture
def mock_worker():
    """Mock worker execution to prevent actual connection."""
    with patch("gpumesh.worker.run_worker") as mock:
        yield mock


@pytest.fixture
def mock_connection_manager():
    """Mock connection manager to capture saved connections."""
    with patch("gpumesh.connection_manager.save_connection") as mock:
        yield mock


@pytest.fixture
def mock_capability():
    """Mock capability detection."""
    with patch("gpumesh.capability.full_probe") as mock_full, \
         patch("gpumesh.capability.probe_device") as mock_device:
        mock_full.return_value = {
            "device": "cuda",
            "device_name": "NVIDIA GeForce RTX 3080",
            "score": 85.2
        }
        mock_device.return_value = {
            "device": "cuda",
            "device_name": "NVIDIA GeForce RTX 3080"
        }
        yield mock_full, mock_device


@pytest.fixture
def mock_httpd():
    """Mock httpd server object."""
    mock = MagicMock()
    mock.gpumesh_stop = MagicMock()
    return mock


@pytest.mark.skipif(_no_console, reason="questionary requires a real console")
class TestCoordinatorFlowSameNetwork:
    """Test coordinator setup wizard for Same Network mode."""

    def test_coordinator_same_network_starts_server(self, mock_tailscale_installed, mock_lan_ip,
                                                    mock_gpu_detected, mock_token, mock_httpd):
        """Test coordinator setup starts server in background."""
        from gpumesh.setup_wizard import run_setup_wizard

        with patch("gpumesh.server.serve", return_value=mock_httpd):
            with patch("gpumesh.connection_manager.save_connection"):
                with patch("gpumesh.setup_wizard.time.sleep", side_effect=KeyboardInterrupt):
                    with patch("builtins.input", side_effect=["1", "1"]):
                        with patch("sys.stdout", new_callable=io.StringIO):
                            run_setup_wizard()

        # NOTE: The coordinator server is intentionally left running in the background.

    def test_coordinator_same_network_shows_token(self, mock_tailscale_installed, mock_lan_ip,
                                                    mock_gpu_detected, mock_token, mock_httpd):
        """Test coordinator setup starts server."""
        from gpumesh.setup_wizard import run_setup_wizard

        with patch("gpumesh.server.serve", return_value=mock_httpd):
            with patch("gpumesh.connection_manager.save_connection"):
                with patch("gpumesh.setup_wizard.time.sleep", side_effect=KeyboardInterrupt):
                    with patch("builtins.input", side_effect=["1", "1"]):
                        with patch("sys.stdout", new_callable=io.StringIO):
                            run_setup_wizard()

        # Server was started (no crash)

    def test_coordinator_same_network_shows_url(self, mock_tailscale_installed, mock_lan_ip,
                                                  mock_gpu_detected, mock_token, mock_httpd):
        """Test coordinator setup runs without error."""
        from gpumesh.setup_wizard import run_setup_wizard

        with patch("gpumesh.server.serve", return_value=mock_httpd):
            with patch("gpumesh.connection_manager.save_connection"):
                with patch("gpumesh.setup_wizard.time.sleep", side_effect=KeyboardInterrupt):
                    with patch("builtins.input", side_effect=["1", "1"]):
                        with patch("sys.stdout", new_callable=io.StringIO):
                            run_setup_wizard()

        # No crash

    def test_coordinator_same_network_shows_scan_message(self, mock_tailscale_installed,
                                                           mock_lan_ip, mock_gpu_detected,
                                                           mock_token, mock_httpd):
        """Test coordinator setup scans for workers."""
        from gpumesh.setup_wizard import run_setup_wizard

        with patch("gpumesh.server.serve", return_value=mock_httpd):
            with patch("gpumesh.connection_manager.save_connection"):
                with patch("gpumesh.setup_wizard.time.sleep", side_effect=KeyboardInterrupt):
                    with patch("builtins.input", side_effect=["1", "1"]):
                        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                            run_setup_wizard()

                            output = mock_stdout.getvalue()
                            # Should show "No workers found" (no workers in test)
                            assert "No workers found" in output

    def test_coordinator_invalid_choice(self, mock_tailscale_installed, mock_gpu_detected):
        """Test coordinator setup with invalid choice."""
        from gpumesh.setup_wizard import run_setup_wizard

        with patch("builtins.input", side_effect=["3"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                run_setup_wizard()

                output = mock_stdout.getvalue()
                assert "Please enter 1 or 2" in output


@pytest.mark.skipif(_no_console, reason="questionary requires a real console")
class TestCoordinatorFlowTailscale:
    """Test coordinator setup wizard for Tailscale mode."""

    def test_coordinator_tailscale_flow(self, mock_tailscale_installed, mock_lan_ip,
                                        mock_gpu_detected, mock_token, mock_connection_manager):
        """Test complete coordinator setup with Tailscale choice."""
        from gpumesh.setup_wizard import run_setup_wizard

        with patch("builtins.input", side_effect=["1", "2"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                run_setup_wizard()

                output = mock_stdout.getvalue()

                assert "YOUR CONNECTION DETAILS" in output
                assert "URL:   http://100.67.72.79:8000" in output
                assert "Token: testToken123" in output
                assert "gpumesh serve --port 8000 --token testToken123 --tailscale" in output

    def test_coordinator_tailscale_not_available(self, mock_tailscale_not_installed, mock_lan_ip,
                                                  mock_gpu_detected, mock_token,
                                                  mock_connection_manager):
        """Test coordinator setup when Tailscale is not installed."""
        from gpumesh.setup_wizard import run_setup_wizard

        with patch("builtins.input", side_effect=["1", "1"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                with patch("gpumesh.setup_wizard.time.sleep", side_effect=KeyboardInterrupt):
                    with patch("gpumesh.server.serve"):
                        with patch("gpumesh.connection_manager.save_connection"):
                            run_setup_wizard()

                output = mock_stdout.getvalue()

                assert "Tailscale not found" in output
                assert "Install from: https://tailscale.com/download" in output


@pytest.mark.skipif(_no_console, reason="questionary requires a real console")
class TestWorkerFlowSameNetwork:
    """Test worker setup wizard for Same Network mode."""

    def test_worker_same_network_flow(self, mock_tailscale_installed, mock_lan_ip,
                                      mock_worker, mock_connection_manager, mock_capability):
        """Test complete worker setup with Same Network choice."""
        from gpumesh.setup_wizard import run_setup_wizard

        # _setup_worker_radar_scan has a while True loop that can only exit via
        # KeyboardInterrupt (returns early). Mock it to simulate the full happy path.
        with patch("gpumesh.setup_wizard._setup_worker_radar_scan") as mock_scan:
            def fake_scan(device):
                from gpumesh import connection_manager, worker
                url = "http://192.168.1.10:8000"
                token = "testToken123"
                connection_manager.save_connection(url, token)
                worker.run_worker(url, token)
            mock_scan.side_effect = fake_scan

            with patch("builtins.input", side_effect=["2", "1"]):
                with patch("sys.stdout", new_callable=io.StringIO):
                    run_setup_wizard()

            mock_connection_manager.assert_called_once_with(
                "http://192.168.1.10:8000", "testToken123"
            )
            mock_worker.assert_called_once_with(
                "http://192.168.1.10:8000", "testToken123"
            )

    def test_worker_same_network_no_peer_selected(self, mock_tailscale_installed, mock_lan_ip,
                                                   mock_worker, mock_connection_manager):
        """Test worker setup when no peer is selected."""
        from gpumesh.setup_wizard import run_setup_wizard

        with patch("gpumesh.setup_wizard._setup_worker_radar_scan") as mock_scan:
            def fake_scan(device):
                from gpumesh import connection_manager
                # Simulate no peer selected — don't save or connect
                pass
            mock_scan.side_effect = fake_scan

            with patch("builtins.input", side_effect=["2", "1"]):
                with patch("sys.stdout", new_callable=io.StringIO):
                    run_setup_wizard()

            mock_connection_manager.assert_not_called()
            mock_worker.assert_not_called()

    def test_worker_same_network_no_token(self, mock_tailscale_installed, mock_lan_ip,
                                          mock_worker, mock_connection_manager):
        """Test worker setup when no token is provided."""
        from gpumesh.setup_wizard import run_setup_wizard

        with patch("gpumesh.setup_wizard._setup_worker_radar_scan") as mock_scan:
            def fake_scan(device):
                # Simulate no token — don't save or connect
                pass
            mock_scan.side_effect = fake_scan

            with patch("builtins.input", side_effect=["2", "1"]):
                with patch("sys.stdout", new_callable=io.StringIO):
                    run_setup_wizard()

            mock_worker.assert_not_called()


@pytest.mark.skipif(_no_console, reason="questionary requires a real console")
class TestWorkerFlowBroadcast:
    """Test worker setup wizard for broadcast/claim mode."""

    def test_worker_broadcast_flow(self, mock_tailscale_installed, mock_lan_ip,
                                    mock_worker, mock_connection_manager, mock_capability):
        """Test worker setup: enter token → start broadcasting."""
        from gpumesh.setup_wizard import run_setup_wizard

        # Worker flow: option 2, enter token (>= 8 chars), confirm broadcast
        with patch("builtins.input", side_effect=["2", "testtoken123", "y"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                with patch("gpumesh.worker.run_worker_broadcast") as mock_broadcast:
                    run_setup_wizard()

                    output = mock_stdout.getvalue()
                    assert "broadcast" in output.lower()
                    mock_broadcast.assert_called_once_with("testtoken123")

    def test_worker_empty_token(self, mock_tailscale_installed, mock_lan_ip,
                                 mock_worker, mock_connection_manager, mock_capability):
        """Test worker setup with empty token → error."""
        from gpumesh.setup_wizard import run_setup_wizard

        with patch("builtins.input", side_effect=["2", ""]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                run_setup_wizard()

                output = mock_stdout.getvalue()
                assert "Token cannot be empty" in output
                mock_worker.assert_not_called()

    def test_worker_short_token(self, mock_tailscale_installed, mock_lan_ip,
                                 mock_worker, mock_connection_manager, mock_capability):
        """Test worker setup with short token → error."""
        from gpumesh.setup_wizard import run_setup_wizard

        with patch("builtins.input", side_effect=["2", "abc"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                run_setup_wizard()

                output = mock_stdout.getvalue()
                assert "at least 8 characters" in output
                mock_worker.assert_not_called()

    def test_worker_broadcast_declined(self, mock_tailscale_installed, mock_lan_ip,
                                        mock_worker, mock_connection_manager, mock_capability):
        """Test worker setup when user declines broadcast."""
        from gpumesh.setup_wizard import run_setup_wizard

        with patch("builtins.input", side_effect=["2", "testtoken123", "n"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                run_setup_wizard()

                output = mock_stdout.getvalue()
                assert "Cancelled" in output
                mock_worker.assert_not_called()

    def test_worker_broadcast_default_confirm(self, mock_tailscale_installed, mock_lan_ip,
                                               mock_worker, mock_connection_manager, mock_capability):
        """Test worker setup: empty confirm = yes (default)."""
        from gpumesh.setup_wizard import run_setup_wizard

        with patch("builtins.input", side_effect=["2", "testtoken123", ""]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                with patch("gpumesh.worker.run_worker_broadcast") as mock_broadcast:
                    run_setup_wizard()

                    mock_broadcast.assert_called_once_with("testtoken123")


@pytest.mark.skipif(_no_console, reason="questionary requires a real console")
class TestSetupWizardEdgeCases:
    """Test edge cases and error handling in setup wizard."""

    def test_empty_choice(self, mock_tailscale_installed, mock_gpu_detected):
        """Test setup wizard with empty choice."""
        from gpumesh.setup_wizard import run_setup_wizard

        with patch("builtins.input", side_effect=[""]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                run_setup_wizard()

                output = mock_stdout.getvalue()
                assert "Please enter 1 or 2" in output

    def test_keyboard_interrupt(self, mock_tailscale_installed, mock_gpu_detected):
        """Test setup wizard propagates keyboard interrupt (Ctrl+C exits cleanly)."""
        from gpumesh.setup_wizard import run_setup_wizard

        with patch("builtins.input", side_effect=KeyboardInterrupt):
            with pytest.raises(KeyboardInterrupt):
                run_setup_wizard()

    def test_eof_error(self, mock_tailscale_installed, mock_gpu_detected):
        """Test setup wizard handles EOF error gracefully."""
        from gpumesh.setup_wizard import run_setup_wizard

        with patch("builtins.input", side_effect=EOFError):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                run_setup_wizard()

                output = mock_stdout.getvalue()
                assert "\n" in output

    def test_gpu_detection_failure(self, mock_tailscale_installed, mock_lan_ip, mock_token,
                                    mock_httpd):
        """Test setup wizard handles GPU detection failure."""
        from gpumesh.setup_wizard import run_setup_wizard

        def mock_detect_gpu():
            print("  No GPU found (CPU only — still works!)")
            return "cpu"

        with patch("gpumesh.setup_wizard._detect_gpu", side_effect=mock_detect_gpu):
            with patch("gpumesh.server.serve", return_value=mock_httpd):
                with patch("gpumesh.connection_manager.save_connection"):
                    with patch("gpumesh.setup_wizard.time.sleep", side_effect=KeyboardInterrupt):
                        with patch("builtins.input", side_effect=["1", "1"]):
                            with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                                run_setup_wizard()

                                output = mock_stdout.getvalue()
                                assert "No GPU found" in output


class TestShowConnectionCommand:
    """Test show-connection command."""

    def test_show_connection_command(self):
        """Test that show-connection command displays saved connection."""
        from gpumesh.cli import cmd_show_connection

        args = MagicMock()

        mock_saved = {
            "url": "http://192.168.1.10:8000",
            "token": "testToken123"
        }

        with patch("gpumesh.cli.connection_manager.load_connection", return_value=mock_saved):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                cmd_show_connection(args)

                output = mock_stdout.getvalue()

                assert "SAVED CONNECTION DETAILS" in output
                assert "URL:   http://192.168.1.10:8000" in output
                assert "Token: testToken123" in output
                assert "gpumesh quickjoin http://192.168.1.10:8000 --token testToken123" in output


class TestParseUrl:
    """Tests for _parse_url URL normalization."""

    def test_ip_default_port(self):
        from gpumesh.setup_wizard import _parse_url
        assert _parse_url("192.168.1.10") == "http://192.168.1.10:8000"

    def test_ip_custom_port(self):
        from gpumesh.setup_wizard import _parse_url
        assert _parse_url("192.168.1.10:9000") == "http://192.168.1.10:9000"

    def test_http_url_unchanged(self):
        from gpumesh.setup_wizard import _parse_url
        assert _parse_url("http://100.x:9000") == "http://100.x:9000"

    def test_https_url_unchanged(self):
        from gpumesh.setup_wizard import _parse_url
        assert _parse_url("https://example.com:8080") == "https://example.com:8080"

    def test_ipv6_no_port(self):
        from gpumesh.setup_wizard import _parse_url
        assert _parse_url("[::1]") == "http://[::1]:8000"

    def test_ipv6_with_port(self):
        from gpumesh.setup_wizard import _parse_url
        assert _parse_url("[::1]:9000") == "http://[::1]:9000"

    def test_ipv6_loopback(self):
        from gpumesh.setup_wizard import _parse_url
        assert _parse_url("[::1]:8080") == "http://[::1]:8080"
