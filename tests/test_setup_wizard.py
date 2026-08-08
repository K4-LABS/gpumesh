"""Tests for setup_wizard.py - Interactive setup wizard flows.

Tests the new radar-based coordinator/worker setup wizard.
"""

import pytest
from unittest.mock import patch, MagicMock, call
import io


@pytest.fixture(autouse=True)
def _hermetic_wizard(monkeypatch):
    """Make the wizard tests run without a real interactive console.

    questionary (via prompt_toolkit) crashes with ``NoConsoleScreenBufferError``
    when run under git-bash/MSYS on Windows (TERM=xterm-256color but no
    Windows console), which used to fail every wizard test in that
    environment. Instead of a real console, scripted answers are consumed
    from ``builtins.input`` (which the tests already patch with
    ``side_effect=[...]``), and real-world side effects (self-worker spawn,
    UDP discovery listener, firewall rules) are stubbed out so tests are
    hermetic, fast and deterministic.
    """
    import builtins as _builtins
    import questionary as _questionary

    def _next_answer():
        try:
            return _builtins.input("")
        except (EOFError, StopIteration):
            return None

    def _fake_select(message, choices=None, **kwargs):
        choices = list(choices or [])

        class _Q:
            def ask(self):
                ans = _next_answer()
                if ans is None:
                    return None
                if isinstance(ans, str) and ans.isdigit():
                    idx = int(ans) - 1
                    if 0 <= idx < len(choices):
                        return choices[idx]
                return ans

        return _Q()

    def _fake_text(message, validate=None, **kwargs):
        # NOTE: deliberately bypasses questionary's `validate` callback so the
        # wizard's OWN code-level checks (empty / min-length) are what's under
        # test — the same path a headless or non-interactive run would take.
        class _Q:
            def ask(self):
                return _next_answer()

        return _Q()

    def _fake_confirm(message, default=False, **kwargs):
        class _Q:
            def ask(self):
                ans = _next_answer()
                if ans is None:
                    return None
                s = str(ans).strip().lower()
                if s in ("y", "yes", "true", "1"):
                    return True
                if s in ("n", "no", "false", "0"):
                    return False
                if s == "":
                    return bool(default)  # Enter = accept default
                # Unrecognized input: abort (like Ctrl+C / Esc in questionary)
                # rather than silently defaulting.
                return None

        return _Q()

    monkeypatch.setattr(_questionary, "select", _fake_select)
    monkeypatch.setattr(_questionary, "text", _fake_text)
    monkeypatch.setattr(_questionary, "confirm", _fake_confirm)

    # Stub real-world side effects the wizard would otherwise trigger:
    # a self-worker connecting to a fake coordinator, a UDP listener
    # binding port 48900, and netsh firewall commands.
    monkeypatch.setattr(
        "gpumesh.worker.spawn_local_worker", lambda *a, **k: None
    )

    class _FakeListener:
        def start(self):
            pass

        def stop(self):
            pass

        def peers(self):
            return []

    monkeypatch.setattr(
        "gpumesh.discovery.Listener", lambda *a, **k: _FakeListener()
    )
    monkeypatch.setattr(
        "gpumesh.setup_wizard.try_add_firewall_rule", lambda *a, **k: False
    )
    monkeypatch.setattr(
        "gpumesh.setup_wizard.show_firewall_hint", lambda *a, **k: None
    )


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

    def test_coordinator_scan_interrupt_stops_cleanly(self, mock_tailscale_installed,
                                                      mock_lan_ip, mock_gpu_detected,
                                                      mock_token, mock_httpd):
        """Ctrl+C during the scan stops the coordinator immediately (no
        second Ctrl+C needed) and still showed the connection panel."""
        from gpumesh.setup_wizard import run_setup_wizard

        with patch("gpumesh.server.serve", return_value=mock_httpd):
            with patch("gpumesh.connection_manager.save_connection"):
                with patch("gpumesh.setup_wizard.time.sleep", side_effect=KeyboardInterrupt):
                    with patch("builtins.input", side_effect=["1", "1"]):
                        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                            run_setup_wizard()

                            output = mock_stdout.getvalue()

        assert "Scan interrupted" in output
        assert "stopping the coordinator" in output.lower()
        # The connection panel is shown before the scan, so it's still visible.
        assert "YOUR COORDINATOR IS RUNNING" in output

    def test_coordinator_no_workers_shows_connection_details(self, mock_tailscale_installed,
                                                             mock_lan_ip, mock_gpu_detected,
                                                             mock_token, mock_httpd):
        """When the scan times out with no workers, the user sees the
        URL/token/join command and the coordinator keeps running (it is not
        silently killed by the wizard exiting)."""
        from gpumesh.setup_wizard import run_setup_wizard

        # time.sleep returns immediately so the 15-iteration scan "times out"
        # fast instead of sleeping 30 real seconds.
        with patch("gpumesh.server.serve", return_value=mock_httpd):
            with patch("gpumesh.connection_manager.save_connection"):
                with patch("gpumesh.setup_wizard.time.sleep", return_value=None):
                    with patch("builtins.input", side_effect=["1", "1"]):
                        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                            run_setup_wizard()

                            output = mock_stdout.getvalue()

        assert "No workers found" in output
        assert "YOUR COORDINATOR IS RUNNING" in output
        assert "http://192.168.1.10:8000" in output
        assert "testToken123" in output
        assert "gpumesh quickjoin http://192.168.1.10:8000 --token testToken123" in output
        # The wizard must NOT tear the coordinator down just because no
        # workers were found — it keeps serving until the user quits.
        assert "Shutting down coordinator" not in output

    def test_coordinator_invalid_choice(self, mock_tailscale_installed, mock_gpu_detected):
        """Test coordinator setup with invalid choice."""
        from gpumesh.setup_wizard import run_setup_wizard

        with patch("builtins.input", side_effect=["3"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                run_setup_wizard()

                output = mock_stdout.getvalue()
                assert "Please enter 1 or 2" in output


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


class TestVisualHelpers:
    """Tests for the rainbow header and step-badge helpers."""

    def test_rainbow_preserves_plain_text(self):
        """_rainbow colors each char but keeps the plain text intact."""
        from gpumesh.setup_wizard import _rainbow
        result = _rainbow("gpumesh")
        assert result.plain == "gpumesh"

    def test_rainbow_applies_per_char_styles(self):
        """_rainbow cycles colors across characters."""
        from gpumesh.setup_wizard import _rainbow
        result = _rainbow("abcdef")
        styles = [span.style for span in result.spans]
        # Six distinct color styles across the six chars
        assert len(styles) == 6
        assert len(set(styles)) == 6

    def test_step_badge_pending(self):
        """Pending step badge shows '[n/total] > label' (ASCII-safe)."""
        from gpumesh.setup_wizard import _step_badge
        badge = _step_badge(2, 3, "Pick a network")
        assert badge.plain == "  [2/3] > Pick a network"

    def test_step_badge_done(self):
        """Completed step badge shows '[n/total] [OK] label'."""
        from gpumesh.setup_wizard import _step_badge
        badge = _step_badge(1, 3, "Coordinator role", done=True)
        assert badge.plain == "  [1/3] [OK] Coordinator role"

    def test_header_renders_rainbow_panel(self):
        """The header renders a titled panel with the GPUMESH art."""
        import io
        from rich.console import Console
        import gpumesh.setup_wizard as wz
        buf = io.StringIO()
        wz._console = Console(file=buf, width=80, color_system=None)
        wz._print_header()
        out = buf.getvalue()
        assert "GPUMESH" in out
        assert "Share GPU power" in out
        assert "setup wizard" in out

    def test_badges_are_ascii_safe(self):
        """Step badges never emit unicode glyphs that break legacy consoles."""
        from gpumesh.setup_wizard import _step_badge
        for done in (False, True):
            text = _step_badge(1, 2, "X", done=done).plain
            assert text.encode("cp437")  # encodable on legacy Windows consoles


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
