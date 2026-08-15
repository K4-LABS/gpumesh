"""Tests for setup_wizard.py - Interactive setup wizard flows.

Tests the new radar-based coordinator/worker setup wizard.
"""

import pytest
from unittest.mock import patch, MagicMock, call
import inspect
import io
import json
import socket
import subprocess
import sys
import types
import urllib.error
import urllib.request


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

    # The coordinator bind is resolved through cli, which reads GPUMESH_HOST;
    # a developer with it exported must not change what these tests exercise.
    monkeypatch.delenv("GPUMESH_HOST", raising=False)
    # cli._print_exposure_warning names the local device, which on the real
    # path can mean importing torch.
    monkeypatch.setattr("gpumesh.capability.probe_device", lambda *a, **k: {
        "device": "cuda", "device_name": "RTX 3080",
    })

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

    # rich highlights numbers and URLs automatically, which injects ANSI
    # escapes *inside* the very strings these tests assert on: "Please enter
    # 1 or 2" reaches stdout as "Please enter \x1b[1;36m1\x1b[0m or ...", so a
    # plain substring check fails even though the user sees the right text.
    # Force a plain, wide console so assertions match what is actually read,
    # and so wrapping never splits a string mid-assertion.
    import rich.console as _rich_console

    _RealConsole = _rich_console.Console

    def _plain_console(*args, **kwargs):
        kwargs.setdefault("highlight", False)
        kwargs.setdefault("no_color", True)
        kwargs.setdefault("force_terminal", False)
        kwargs.setdefault("width", 200)
        return _RealConsole(*args, **kwargs)

    monkeypatch.setattr("gpumesh.setup_wizard.Console", _plain_console)

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

    # Three byte-identical copies of this flow used to live here, named
    # ..._starts_server / ..._shows_token / ..._shows_url, none of which
    # asserted anything at all. Two were deleted outright: their names promise
    # facts that are already checked with real assertions elsewhere in this
    # file (the URL and token in the panel by
    # test_coordinator_no_workers_shows_connection_details, the serve() bind
    # host and port by TestWizardCoordinatorBind). The third is kept, below,
    # with the assertion it always should have had — it is the only test that
    # follows the *menu* path all the way from run_setup_wizard() into
    # server.serve, so it is the one that would notice the wizard's two
    # halves being wired together wrongly.

    def test_coordinator_same_network_starts_server(self, mock_tailscale_installed, mock_lan_ip,
                                                    mock_gpu_detected, mock_token, mock_httpd):
        """Menu choice 1/1 starts a coordinator with the wizard's own settings.

        The bind host, port and token are asserted at the call, not inferred
        from the banner: a wizard that printed a perfect connection panel and
        then bound the wrong address (or served a different token from the one
        it displayed) would still be broken for every friend who tried to
        join, and only this assertion says so.
        """
        from gpumesh.setup_wizard import WIZARD_LAN_BIND_HOST, run_setup_wizard

        with patch("gpumesh.server.serve", return_value=mock_httpd) as serve:
            with patch("gpumesh.connection_manager.save_connection"):
                with patch("gpumesh.setup_wizard.time.sleep", side_effect=KeyboardInterrupt):
                    with patch("builtins.input", side_effect=["1", "1"]):
                        with patch("sys.stdout", new_callable=io.StringIO):
                            run_setup_wizard()

        serve.assert_called_once()
        host, port, db_path, token = serve.call_args[0][:4]
        assert host == WIZARD_LAN_BIND_HOST
        assert port == 8000
        assert db_path == "gpumesh.db"
        assert token == "testToken123"

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
                # The bind address is now part of the command: without it
                # `gpumesh serve` listens on 127.0.0.1 and the tailnet URL
                # advertised two lines above answers nothing.
                assert ("gpumesh serve --host 100.67.72.79 --port 8000 "
                        "--token testToken123 --tailscale") in output

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
    """Test worker setup wizard for broadcast/claim mode.

    The input sequences carry an extra answer since the worker side gained
    its own network question ("1" = same WiFi/LAN): role, network, then the
    broadcast prompts.
    """

    def test_worker_broadcast_flow(self, mock_tailscale_installed, mock_lan_ip,
                                    mock_worker, mock_connection_manager, mock_capability):
        """Test worker setup: enter token → start broadcasting."""
        from gpumesh.setup_wizard import run_setup_wizard

        # Worker flow: option 2, LAN, enter token (>= 8 chars), confirm
        with patch("builtins.input", side_effect=["2", "1", "testtoken123", "y"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                with patch("gpumesh.worker.run_worker_broadcast") as mock_broadcast:
                    run_setup_wizard()

                    output = mock_stdout.getvalue()
                    assert "broadcast" in output.lower()
                    mock_broadcast.assert_called_once_with("testtoken123")

    def test_worker_empty_token(self, mock_tailscale_installed, mock_lan_ip,
                                 mock_worker, mock_connection_manager, mock_capability):
        """Test worker setup with empty token → error, then re-prompt."""
        from gpumesh.setup_wizard import run_setup_wizard

        with patch("builtins.input", side_effect=["2", "1", ""]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                run_setup_wizard()

                output = mock_stdout.getvalue()
                assert "Token cannot be empty" in output
                mock_worker.assert_not_called()

    def test_worker_short_token(self, mock_tailscale_installed, mock_lan_ip,
                                 mock_worker, mock_connection_manager, mock_capability):
        """Test worker setup with short token → error, then re-prompt."""
        from gpumesh.setup_wizard import run_setup_wizard

        with patch("builtins.input", side_effect=["2", "1", "abc"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                run_setup_wizard()

                output = mock_stdout.getvalue()
                assert "at least 8 characters" in output
                mock_worker.assert_not_called()

    def test_worker_broadcast_declined(self, mock_tailscale_installed, mock_lan_ip,
                                        mock_worker, mock_connection_manager, mock_capability):
        """Test worker setup when user declines broadcast."""
        from gpumesh.setup_wizard import run_setup_wizard

        with patch("builtins.input", side_effect=["2", "1", "testtoken123", "n"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                run_setup_wizard()

                output = mock_stdout.getvalue()
                assert "Cancelled" in output
                mock_worker.assert_not_called()

    def test_worker_broadcast_default_confirm(self, mock_tailscale_installed, mock_lan_ip,
                                               mock_worker, mock_connection_manager, mock_capability):
        """Test worker setup: empty confirm = yes (default)."""
        from gpumesh.setup_wizard import run_setup_wizard

        with patch("builtins.input", side_effect=["2", "1", "testtoken123", ""]):
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


# ===========================================================================
#  Direct unit tests for the wizard's helpers and sub-flows.
#
#  Everything below drives the functions directly rather than through
#  run_setup_wizard, because most of them are only reachable after several
#  interactive answers and one of them (the coordinator radar) blocks on a
#  server thread. ``wz_console`` installs the same plain, non-highlighting,
#  wide console the module-level fixture forces: rich's automatic highlighting
#  of numbers and URLs injects ANSI escapes into the very substrings these
#  tests assert on.
# ===========================================================================


@pytest.fixture
def wz_console(monkeypatch):
    """Point the wizard's module-global console at a buffer; return the buffer."""
    from rich.console import Console
    import gpumesh.setup_wizard as wz

    buf = io.StringIO()
    monkeypatch.setattr(wz, "_console", Console(
        file=buf, width=200, highlight=False, no_color=True,
        force_terminal=False, color_system=None,
    ))
    return buf


def _fake_torch_module(*, has_mps=True, mps_available=True):
    """A stand-in torch for the Apple Silicon branch of _detect_gpu."""
    torch_mod = types.ModuleType("torch")
    backends = types.SimpleNamespace()
    if has_mps:
        backends.mps = types.SimpleNamespace(is_available=lambda: mps_available)
    torch_mod.backends = backends
    return torch_mod


def _peer(hostname="worker-box", ip="192.168.1.55", claim_port=49152,
          score=50.0, device="cuda"):
    from gpumesh.discovery import Peer

    return Peer({
        "type": "gpumesh_worker",
        "hostname": hostname,
        "device": device,
        "device_name": "RTX 3080",
        "score": score,
        "api_port": 8000,
        "claim_port": claim_port,
    }, (ip, 5000))


class _Resp:
    """Minimal urlopen() return value: a context manager with .read()."""

    def __init__(self, body, headers=None):
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.headers = dict(headers or {})

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code, body=b""):
    return urllib.error.HTTPError(
        "http://192.168.1.55:49152/api/claim", code, "err", {}, io.BytesIO(body)
    )


class TestDetectGpu:
    """_detect_gpu: nvidia-smi first, then Apple Silicon, then CPU."""

    def test_nvidia_smi_reports_a_gpu(self, wz_console):
        from gpumesh.setup_wizard import _detect_gpu

        result = MagicMock(returncode=0, stdout="NVIDIA GeForce RTX 3080\n")
        with patch("gpumesh.setup_wizard.subprocess.run", return_value=result):
            assert _detect_gpu() == "cuda"
        assert "GPU found: NVIDIA GeForce RTX 3080" in wz_console.getvalue()

    def test_first_gpu_wins_on_a_multi_gpu_box(self, wz_console):
        from gpumesh.setup_wizard import _detect_gpu

        result = MagicMock(returncode=0, stdout="RTX 4090\nRTX 3060\n")
        with patch("gpumesh.setup_wizard.subprocess.run", return_value=result):
            assert _detect_gpu() == "cuda"
        assert "GPU found: RTX 4090" in wz_console.getvalue()
        assert "3060" not in wz_console.getvalue()

    def test_nvidia_smi_queries_only_the_name(self, wz_console):
        from gpumesh.setup_wizard import _detect_gpu

        result = MagicMock(returncode=0, stdout="RTX 4090\n")
        with patch("gpumesh.setup_wizard.subprocess.run",
                   return_value=result) as run:
            _detect_gpu()
        cmd = run.call_args[0][0]
        assert cmd[0] == "nvidia-smi"
        assert "--query-gpu=name" in cmd
        # A hung nvidia-smi must not hang the wizard.
        assert run.call_args[1]["timeout"] == 5

    def test_nonzero_exit_means_no_gpu(self, wz_console):
        from gpumesh.setup_wizard import _detect_gpu

        result = MagicMock(returncode=9, stdout="")
        with patch("gpumesh.setup_wizard.subprocess.run", return_value=result):
            assert _detect_gpu() == "cpu"
        assert "No GPU found" in wz_console.getvalue()

    def test_empty_output_means_no_gpu(self, wz_console):
        from gpumesh.setup_wizard import _detect_gpu

        result = MagicMock(returncode=0, stdout="   \n")
        with patch("gpumesh.setup_wizard.subprocess.run", return_value=result):
            assert _detect_gpu() == "cpu"

    @pytest.mark.parametrize("exc", [
        FileNotFoundError("nvidia-smi"),
        subprocess.TimeoutExpired("nvidia-smi", 5),
        OSError("driver not loaded"),
    ])
    def test_nvidia_smi_failures_degrade_to_cpu(self, wz_console, exc):
        from gpumesh.setup_wizard import _detect_gpu

        with patch("gpumesh.setup_wizard.subprocess.run", side_effect=exc):
            assert _detect_gpu() == "cpu"
        assert "No GPU found" in wz_console.getvalue()

    def test_apple_silicon_detected(self, wz_console, monkeypatch):
        from gpumesh.setup_wizard import _detect_gpu

        monkeypatch.setattr("gpumesh.setup_wizard.platform.system", lambda: "Darwin")
        monkeypatch.setattr("gpumesh.setup_wizard.platform.machine", lambda: "arm64")
        monkeypatch.setitem(sys.modules, "torch", _fake_torch_module())
        with patch("gpumesh.setup_wizard.subprocess.run",
                   side_effect=FileNotFoundError()):
            assert _detect_gpu() == "mps"
        assert "Apple Silicon" in wz_console.getvalue()

    def test_apple_silicon_without_torch(self, wz_console, monkeypatch):
        from gpumesh.setup_wizard import _detect_gpu

        monkeypatch.setattr("gpumesh.setup_wizard.platform.system", lambda: "Darwin")
        monkeypatch.setattr("gpumesh.setup_wizard.platform.machine", lambda: "arm64")
        # None in sys.modules is what the import system turns into ImportError.
        monkeypatch.setitem(sys.modules, "torch", None)
        with patch("gpumesh.setup_wizard.subprocess.run",
                   side_effect=FileNotFoundError()):
            assert _detect_gpu() == "cpu"

    def test_apple_silicon_with_mps_unavailable(self, wz_console, monkeypatch):
        from gpumesh.setup_wizard import _detect_gpu

        monkeypatch.setattr("gpumesh.setup_wizard.platform.system", lambda: "Darwin")
        monkeypatch.setattr("gpumesh.setup_wizard.platform.machine", lambda: "arm64")
        monkeypatch.setitem(sys.modules, "torch",
                            _fake_torch_module(mps_available=False))
        with patch("gpumesh.setup_wizard.subprocess.run",
                   side_effect=FileNotFoundError()):
            assert _detect_gpu() == "cpu"

    def test_intel_mac_does_not_probe_mps(self, wz_console, monkeypatch):
        from gpumesh.setup_wizard import _detect_gpu

        monkeypatch.setattr("gpumesh.setup_wizard.platform.system", lambda: "Darwin")
        monkeypatch.setattr("gpumesh.setup_wizard.platform.machine", lambda: "x86_64")
        monkeypatch.setitem(sys.modules, "torch", _fake_torch_module())
        with patch("gpumesh.setup_wizard.subprocess.run",
                   side_effect=FileNotFoundError()):
            assert _detect_gpu() == "cpu"

    def test_nvidia_wins_over_apple_silicon(self, wz_console, monkeypatch):
        """An eGPU box should not be relabelled 'Apple Silicon'."""
        from gpumesh.setup_wizard import _detect_gpu

        monkeypatch.setattr("gpumesh.setup_wizard.platform.system", lambda: "Darwin")
        monkeypatch.setattr("gpumesh.setup_wizard.platform.machine", lambda: "arm64")
        monkeypatch.setitem(sys.modules, "torch", _fake_torch_module())
        result = MagicMock(returncode=0, stdout="RTX 4090\n")
        with patch("gpumesh.setup_wizard.subprocess.run", return_value=result):
            assert _detect_gpu() == "cuda"


class TestHasTailscale:
    def test_not_on_path(self):
        from gpumesh.setup_wizard import _has_tailscale

        with patch("gpumesh.setup_wizard.shutil.which", return_value=None):
            with patch("gpumesh.setup_wizard.subprocess.run") as run:
                assert _has_tailscale() is False
            run.assert_not_called()

    def test_installed_and_running(self):
        from gpumesh.setup_wizard import _has_tailscale

        with patch("gpumesh.setup_wizard.shutil.which",
                   return_value="/usr/bin/tailscale"):
            with patch("gpumesh.setup_wizard.subprocess.run",
                       return_value=MagicMock(returncode=0)) as run:
                assert _has_tailscale() is True
        # It must invoke the resolved path, not the bare name: on Windows the
        # bare name is not executable without a shell.
        assert run.call_args[0][0][0] == "/usr/bin/tailscale"
        assert run.call_args[1]["timeout"] == 5

    def test_installed_but_logged_out(self):
        from gpumesh.setup_wizard import _has_tailscale

        with patch("gpumesh.setup_wizard.shutil.which",
                   return_value="/usr/bin/tailscale"):
            with patch("gpumesh.setup_wizard.subprocess.run",
                       return_value=MagicMock(returncode=1)):
                assert _has_tailscale() is False

    @pytest.mark.parametrize("exc", [
        FileNotFoundError("tailscale"),
        subprocess.TimeoutExpired("tailscale", 5),
        OSError("permission denied"),
    ])
    def test_status_failures_are_swallowed(self, exc):
        from gpumesh.setup_wizard import _has_tailscale

        with patch("gpumesh.setup_wizard.shutil.which",
                   return_value="/usr/bin/tailscale"):
            with patch("gpumesh.setup_wizard.subprocess.run", side_effect=exc):
                assert _has_tailscale() is False


class TestParseUrlEdgeCases:
    def test_surrounding_whitespace_is_stripped(self):
        from gpumesh.setup_wizard import _parse_url
        assert _parse_url("  192.168.1.10  ") == "http://192.168.1.10:8000"

    def test_hostname_gets_the_default_port(self):
        from gpumesh.setup_wizard import _parse_url
        assert _parse_url("coordinator.local") == "http://coordinator.local:8000"

    def test_custom_default_port(self):
        from gpumesh.setup_wizard import _parse_url
        assert _parse_url("192.168.1.10", 9100) == "http://192.168.1.10:9100"

    def test_explicit_port_beats_the_default(self):
        from gpumesh.setup_wizard import _parse_url
        assert _parse_url("192.168.1.10:9000", 9100) == "http://192.168.1.10:9000"

    def test_https_url_with_a_path_is_untouched(self):
        from gpumesh.setup_wizard import _parse_url
        assert _parse_url("https://mesh.example/api") == "https://mesh.example/api"

    def test_non_numeric_port_is_rejected(self):
        """A typo'd port is refused here, not turned into a broken URL.

        This used to append the default port on top of what the user typed
        ("http://192.168.1.10:80o0:8000"), so the mistake only surfaced much
        later as an unexplained connection failure on the far side.
        """
        from gpumesh.setup_wizard import _parse_url
        with pytest.raises(ValueError) as excinfo:
            _parse_url("192.168.1.10:80o0")
        assert "80o0" in str(excinfo.value)
        # The message has to say what to type instead, not just "invalid".
        assert "192.168.1.10:8000" in str(excinfo.value)

    def test_port_out_of_range_is_rejected(self):
        from gpumesh.setup_wizard import _parse_url
        with pytest.raises(ValueError) as excinfo:
            _parse_url("192.168.1.10:70000")
        assert "1-65535" in str(excinfo.value)

    def test_empty_port_is_rejected(self):
        from gpumesh.setup_wizard import _parse_url
        with pytest.raises(ValueError):
            _parse_url("192.168.1.10:")

    def test_uppercase_scheme_is_normalised(self):
        """The scheme test is case-insensitive, as RFC 3986 says it is.

        It used to be a plain startswith(), so "HTTP://host" fell through to
        the bare-host branch and came back as "http://HTTP://host:8000".
        """
        from gpumesh.setup_wizard import _parse_url
        assert _parse_url("HTTP://192.168.1.10:8000") == \
            "http://192.168.1.10:8000"
        assert _parse_url("HttPs://mesh.example") == "https://mesh.example"

    def test_unsupported_scheme_is_rejected(self):
        from gpumesh.setup_wizard import _parse_url
        with pytest.raises(ValueError) as excinfo:
            _parse_url("ftp://192.168.1.10:8000")
        assert "ftp://" in str(excinfo.value)

    def test_empty_input_is_rejected(self):
        """Empty input used to become "http://:8000" — a hostless URL."""
        from gpumesh.setup_wizard import _parse_url
        for blank in ("", "   ", None):
            with pytest.raises(ValueError) as excinfo:
                _parse_url(blank)
            assert "192.168.1.10:8000" in str(excinfo.value)

    def test_scheme_without_a_host_is_rejected(self):
        from gpumesh.setup_wizard import _parse_url
        with pytest.raises(ValueError):
            _parse_url("http://")

    def test_ipv6_with_a_bad_port_is_rejected(self):
        from gpumesh.setup_wizard import _parse_url
        with pytest.raises(ValueError) as excinfo:
            _parse_url("[::1]:abc")
        assert "abc" in str(excinfo.value)

    def test_unclosed_ipv6_bracket_is_rejected(self):
        from gpumesh.setup_wizard import _parse_url
        with pytest.raises(ValueError) as excinfo:
            _parse_url("[::1:8000")
        assert "[::1]:8000" in str(excinfo.value)

    def test_address_with_whitespace_inside_is_rejected(self):
        """Whitespace inside is a paste accident, not a hostname."""
        from gpumesh.setup_wizard import _parse_url
        with pytest.raises(ValueError):
            _parse_url("192.168.1.10 8000")


class _FakeSock:
    """socket() stand-in for _probe_claim_port; never touches the network."""

    def __init__(self, open_ports, log):
        self._open = open_ports
        self._log = log
        self.timeout = None

    def settimeout(self, value):
        self.timeout = value
        self._log.setdefault("timeouts", []).append(value)

    def connect_ex(self, address):
        self._log.setdefault("tried", []).append(address[1])
        return 0 if address[1] in self._open else 1

    def close(self):
        pass


@pytest.fixture
def probe_sockets(monkeypatch):
    """Fake socket.socket for _probe_claim_port; returns the call log."""
    log = {}

    def _install(open_ports=()):
        monkeypatch.setattr(
            socket, "socket",
            lambda *a, **k: _FakeSock(set(open_ports), log),
        )
        return log

    return _install


class TestProbeClaimPort:
    def test_nothing_listening(self, probe_sockets):
        from gpumesh.setup_wizard import _probe_claim_port

        log = probe_sockets()
        assert _probe_claim_port("192.168.1.55") == 0
        assert log["tried"] == [49152, 49153, 49154, 8080, 9000, 10000, 12345, 50000]

    def test_connect_attempts_are_time_boxed(self, probe_sockets):
        """A firewalled host must not stall the wizard for a minute per port."""
        from gpumesh.setup_wizard import _probe_claim_port

        log = probe_sockets()
        _probe_claim_port("192.168.1.55")
        assert set(log["timeouts"]) == {0.5}

    @pytest.mark.parametrize("code", [400, 404, 405])
    def test_claim_endpoint_recognised_by_its_error_code(self, probe_sockets, code):
        from gpumesh.setup_wizard import _probe_claim_port

        probe_sockets([49153])
        with patch("urllib.request.urlopen", side_effect=_http_error(code)):
            assert _probe_claim_port("192.168.1.55") == 49153

    def test_any_http_answer_is_accepted_as_a_last_resort(self, probe_sockets):
        """An open port answering 500 is taken rather than giving up.

        Refusing it means reporting "could not find a claim server" about a
        machine that just broadcast a gpumesh beacon. A wrong guess costs one
        claim POST that fails immediately and visibly; a refusal ends the
        flow with nothing the user can act on.
        """
        from gpumesh.setup_wizard import _probe_claim_port

        probe_sockets([8080])
        with patch("urllib.request.urlopen", side_effect=_http_error(500)):
            assert _probe_claim_port("192.168.1.55") == 8080

    def test_a_self_identifying_claim_server_wins_over_a_stray_service(
            self, probe_sockets):
        """claimer sets Server: gpumesh-claim, so it is picked over a guess."""
        from gpumesh.setup_wizard import _probe_claim_port

        probe_sockets([8080, 9000])

        def _answer(req, timeout=None):
            if req.full_url.startswith("http://192.168.1.55:9000"):
                return _Resp(b"{}", headers={"Server": "gpumesh-claim"})
            raise _http_error(500)

        with patch("urllib.request.urlopen", side_effect=_answer):
            assert _probe_claim_port("192.168.1.55") == 9000

    def test_connection_errors_during_probe_are_swallowed(self, probe_sockets):
        from gpumesh.setup_wizard import _probe_claim_port

        probe_sockets([9000])
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("reset")):
            assert _probe_claim_port("192.168.1.55") == 0

    def test_socket_creation_failure_does_not_crash(self, monkeypatch):
        from gpumesh.setup_wizard import _probe_claim_port

        def boom(*args, **kwargs):
            raise OSError("no file descriptors")

        monkeypatch.setattr(socket, "socket", boom)
        assert _probe_claim_port("192.168.1.55") == 0

    def test_a_claim_server_answering_200_is_found(self, probe_sockets):
        """A GET answered with 200 is a listening HTTP server, so accept it.

        The probe used to accept only 400/404/405, so a claim endpoint that
        answered a GET normally was invisible and the coordinator reported
        'Could not find a claim server' about a claimable worker.
        """
        from gpumesh.setup_wizard import _probe_claim_port

        probe_sockets([49152])
        with patch("urllib.request.urlopen", return_value=_Resp(b"{}")):
            assert _probe_claim_port("192.168.1.55") == 49152

    def test_the_real_claim_servers_501_is_recognised(self, probe_sockets):
        """The exact response claimer.ClaimHandler gives a GET.

        It defines do_POST and no do_GET, so BaseHTTPRequestHandler answers
        501 "Unsupported method". 501 was not in the old 400/404/405
        allowlist, which meant the probe rejected the one server it exists
        to find.
        """
        from gpumesh.setup_wizard import _probe_claim_port

        probe_sockets([49152])
        with patch("urllib.request.urlopen", side_effect=_http_error(501)):
            assert _probe_claim_port("192.168.1.55") == 49152

    def test_claimer_really_answers_a_get_with_501(self):
        """Guards the assumption above against a change in claimer.py."""
        from http.server import BaseHTTPRequestHandler
        from gpumesh.claimer import ClaimHandler

        assert not hasattr(ClaimHandler, "do_GET")
        assert not hasattr(BaseHTTPRequestHandler, "do_GET")
        # ...which is what makes handle_one_request send 501.
        assert ClaimHandler.server_version == "gpumesh-claim"


class TestShowRunningCoordinatorPanel:
    def test_shows_url_token_and_join_command(self, wz_console):
        from gpumesh.setup_wizard import _show_running_coordinator_panel

        _show_running_coordinator_panel("http://192.168.1.10:8000", "abc123XYZ")
        out = wz_console.getvalue()
        assert "YOUR COORDINATOR IS RUNNING" in out
        assert "URL:   http://192.168.1.10:8000" in out
        assert "Token: abc123XYZ" in out
        assert "gpumesh quickjoin http://192.168.1.10:8000 --token abc123XYZ" in out

    def test_warns_that_the_token_is_a_secret(self, wz_console):
        from gpumesh.setup_wizard import _show_running_coordinator_panel

        _show_running_coordinator_panel("http://192.168.1.10:8000", "abc123XYZ")
        assert "SECURITY" in wz_console.getvalue()

    def test_mentions_the_join_alternative(self, wz_console):
        from gpumesh.setup_wizard import _show_running_coordinator_panel

        _show_running_coordinator_panel("http://10.0.0.4:8001", "tok")
        assert "gpumesh join <URL> --token <TOKEN>" in wz_console.getvalue()


class TestShowCoordinatorInstructions:
    def test_manual_mode_steps(self, wz_console):
        from gpumesh.setup_wizard import _show_coordinator_instructions

        _show_coordinator_instructions("http://192.168.1.10:8000", "tok12345",
                                       "manual")
        out = wz_console.getvalue()
        assert "YOUR CONNECTION DETAILS" in out
        assert "gpumesh serve --host 0.0.0.0 --port 8000 --token tok12345" in out
        assert "SAME WiFi/LAN" in out
        assert "gpumesh quickjoin http://192.168.1.10:8000 --token tok12345" in out
        assert "--tailscale" not in out

    def test_manual_mode_command_can_actually_be_reached(self, wz_console):
        """The printed serve command must bind wider than loopback.

        `gpumesh serve` defaults to 127.0.0.1, so the old command
        ("gpumesh serve --port 8000 --token X") started a coordinator that
        no worker could reach — while the panel above it advertised a LAN
        URL and step 3 told the friend to dial exactly that address.
        """
        from gpumesh.setup_wizard import _show_coordinator_instructions
        from gpumesh.cli import _is_loopback_bind

        _show_coordinator_instructions("http://192.168.1.10:8000", "tok12345",
                                       "manual")
        out = wz_console.getvalue()
        serve_line = next(ln for ln in out.splitlines() if "gpumesh serve" in ln)
        parts = serve_line.split()
        assert "--host" in parts, serve_line
        assert not _is_loopback_bind(parts[parts.index("--host") + 1])

    def test_manual_mode_names_the_exposure_next_to_the_command(self, wz_console):
        """Opening a LAN port is stated where the user is told to open it."""
        from gpumesh.setup_wizard import _show_coordinator_instructions

        _show_coordinator_instructions("http://192.168.1.10:8000", "tok12345",
                                       "manual")
        out = wz_console.getvalue()
        assert "EXPOSURE" in out
        assert "whole LAN" in out
        assert "runs code on this machine" in out

    def test_serve_command_uses_the_advertised_port(self, wz_console):
        """A non-8000 coordinator must not be told to serve on 8000."""
        from gpumesh.setup_wizard import _show_coordinator_instructions

        _show_coordinator_instructions("http://192.168.1.10:8001", "tok12345",
                                       "manual")
        out = wz_console.getvalue()
        assert "gpumesh serve --host 0.0.0.0 --port 8001 --token tok12345" in out
        assert "--port 8000" not in out

    def test_tailscale_mode_steps(self, wz_console):
        from gpumesh.setup_wizard import _show_coordinator_instructions

        _show_coordinator_instructions("http://100.67.72.79:8000", "tok12345",
                                       "tailscale")
        out = wz_console.getvalue()
        assert ("gpumesh serve --host 100.67.72.79 --port 8000 "
                "--token tok12345 --tailscale") in out
        assert "https://tailscale.com/download" in out
        assert "gpumesh quickjoin --token tok12345 --tailscale" in out

    def test_tailscale_mode_binds_the_tailnet_address_and_says_so(self, wz_console):
        """Binding the tailnet IP is narrower than 0.0.0.0 — but not nothing."""
        from gpumesh.setup_wizard import _show_coordinator_instructions
        from gpumesh.cli import _is_loopback_bind

        _show_coordinator_instructions("http://100.67.72.79:8000", "tok12345",
                                       "tailscale")
        out = wz_console.getvalue()
        serve_line = next(ln for ln in out.splitlines() if "gpumesh serve" in ln)
        parts = serve_line.split()
        assert parts[parts.index("--host") + 1] == "100.67.72.79"
        assert not _is_loopback_bind("100.67.72.79")
        assert "EXPOSURE" in out
        assert "tailnet" in out

    def test_unknown_mode_falls_back_to_the_lan_instructions(self, wz_console):
        from gpumesh.setup_wizard import _show_coordinator_instructions

        _show_coordinator_instructions("http://192.168.1.10:8000", "tok", "wat")
        assert "SAME WiFi/LAN" in wz_console.getvalue()

    def test_security_warning_is_always_shown(self, wz_console):
        from gpumesh.setup_wizard import _show_coordinator_instructions

        _show_coordinator_instructions("http://192.168.1.10:8000", "tok", "manual")
        assert "Treat this token like a password" in wz_console.getvalue()


class TestSetupCoordinatorManual:
    def test_says_the_server_is_not_running_yet(self, wz_console):
        from gpumesh.setup_wizard import _setup_coordinator_manual

        with patch("gpumesh.connection_manager.save_connection"):
            _setup_coordinator_manual("cpu", False, None, "192.168.1.10", "tok12345")
        out = wz_console.getvalue()
        assert "Manual setup mode" in out
        assert "The server is NOT running yet" in out

    def test_uses_the_lan_ip_without_tailscale(self, wz_console):
        from gpumesh.setup_wizard import _setup_coordinator_manual

        with patch("gpumesh.connection_manager.save_connection") as save:
            _setup_coordinator_manual("cpu", False, None, "192.168.1.10", "tok12345")
        save.assert_called_once_with("http://192.168.1.10:8000", "tok12345")
        assert "http://192.168.1.10:8000" in wz_console.getvalue()

    def test_tailscale_ip_wins_when_present(self, wz_console):
        from gpumesh.setup_wizard import _setup_coordinator_manual

        with patch("gpumesh.connection_manager.save_connection") as save:
            _setup_coordinator_manual("cpu", True, "100.67.72.79", "192.168.1.10",
                                      "tok12345")
        save.assert_called_once_with("http://100.67.72.79:8000", "tok12345")

    def test_generates_a_token_when_none_is_supplied(self, wz_console):
        from gpumesh.setup_wizard import _setup_coordinator_manual

        with patch("gpumesh.setup_wizard.secrets.token_urlsafe",
                   return_value="generated123"):
            with patch("gpumesh.connection_manager.save_connection") as save:
                _setup_coordinator_manual("cpu", False, None, "192.168.1.10")
        save.assert_called_once_with("http://192.168.1.10:8000", "generated123")
        assert "Token: generated123" in wz_console.getvalue()

    def test_shows_the_manual_instruction_block(self, wz_console):
        from gpumesh.setup_wizard import _setup_coordinator_manual

        with patch("gpumesh.connection_manager.save_connection"):
            _setup_coordinator_manual("cpu", False, None, "192.168.1.10", "tok12345")
        assert "STEP-BY-STEP INSTRUCTIONS" in wz_console.getvalue()


@pytest.fixture
def claim_env(monkeypatch, wz_console):
    """Patches shared by the _claim_worker tests; yields the output buffer."""
    monkeypatch.setattr(
        "gpumesh.setup_wizard.coordinator_url_candidates",
        lambda ip, port, limit=4: [f"http://192.168.1.10:{port}"],
    )
    return wz_console


class TestClaimWorker:
    def _select(self, peer, token="workerToken"):
        return patch("gpumesh.radar.select_worker_for_claim",
                     return_value=(peer, token))

    def test_cancelled_selection(self, claim_env):
        from gpumesh.setup_wizard import _claim_worker

        with patch("gpumesh.radar.select_worker_for_claim",
                   return_value=(None, None)):
            with patch("urllib.request.urlopen") as urlopen:
                _claim_worker([], "http://192.168.1.10:8000", "coordTok")
        urlopen.assert_not_called()
        assert "Claim cancelled" in claim_env.getvalue()

    def test_successful_claim(self, claim_env):
        from gpumesh.setup_wizard import _claim_worker

        peer = _peer()
        body = {"ok": True, "coordinator_url": "http://192.168.1.10:8000"}
        with self._select(peer):
            with patch("urllib.request.urlopen", return_value=_Resp(body)) as urlopen:
                _claim_worker([peer], "http://192.168.1.10:8000", "coordTok")
        out = claim_env.getvalue()
        assert "WORKER CLAIMED" in out
        assert "worker-box reached us at http://192.168.1.10:8000" in out
        request = urlopen.call_args[0][0]
        assert request.full_url == "http://192.168.1.55:49152/api/claim"

    def test_payload_carries_the_candidate_list(self, claim_env):
        from gpumesh.setup_wizard import _claim_worker

        peer = _peer()
        with self._select(peer, token="workerToken"):
            with patch("urllib.request.urlopen",
                       return_value=_Resp({"ok": True})) as urlopen:
                _claim_worker([peer], "http://192.168.1.10:8000", "coordTok")
        payload = json.loads(urlopen.call_args[0][0].data)
        assert payload["token"] == "workerToken"
        assert payload["coordinator_token"] == "coordTok"
        assert payload["coordinator_urls"] == ["http://192.168.1.10:8000"]
        # Kept for workers predating the candidate list.
        assert payload["coordinator_url"] == payload["coordinator_urls"][0]

    def test_the_wizards_own_url_is_appended_when_missing(self, claim_env, monkeypatch):
        """The URL the coordinator actually serves on is always offered."""
        from gpumesh.setup_wizard import _claim_worker

        monkeypatch.setattr(
            "gpumesh.setup_wizard.coordinator_url_candidates",
            lambda ip, port, limit=4: ["http://10.0.0.9:8000"],
        )
        peer = _peer()
        with self._select(peer):
            with patch("urllib.request.urlopen",
                       return_value=_Resp({"ok": True})) as urlopen:
                _claim_worker([peer], "http://192.168.1.10:8000", "coordTok")
        payload = json.loads(urlopen.call_args[0][0].data)
        assert payload["coordinator_urls"] == [
            "http://10.0.0.9:8000", "http://192.168.1.10:8000",
        ]

    def test_claim_timeout_outlasts_the_workers_probing(self, claim_env):
        """The worker tries every candidate in turn; 25s covers that."""
        from gpumesh.setup_wizard import _claim_worker

        peer = _peer()
        with self._select(peer):
            with patch("urllib.request.urlopen",
                       return_value=_Resp({"ok": True})) as urlopen:
                _claim_worker([peer], "http://192.168.1.10:8000", "coordTok")
        assert urlopen.call_args[1]["timeout"] == 25

    def test_rejected_claim_shows_the_reason(self, claim_env):
        from gpumesh.setup_wizard import _claim_worker

        peer = _peer()
        body = {"ok": False, "error": "bad token"}
        with self._select(peer):
            with patch("urllib.request.urlopen", return_value=_Resp(body)):
                _claim_worker([peer], "http://192.168.1.10:8000", "coordTok")
        assert "Claim rejected: bad token" in claim_env.getvalue()

    def test_http_error_lists_the_addresses_the_worker_tried(self, claim_env):
        from gpumesh.setup_wizard import _claim_worker

        peer = _peer()
        body = json.dumps({
            "error": "unreachable",
            "tried": ["http://172.22.96.1:8000", "http://192.168.1.10:8000"],
        }).encode()
        with self._select(peer):
            with patch("urllib.request.urlopen", side_effect=_http_error(502, body)):
                _claim_worker([peer], "http://192.168.1.10:8000", "coordTok")
        out = claim_env.getvalue()
        assert "Claim failed: unreachable" in out
        assert "http://172.22.96.1:8000" in out
        assert "--host-ip" in out

    def test_http_error_with_an_unreadable_body(self, claim_env):
        from gpumesh.setup_wizard import _claim_worker

        peer = _peer()
        with self._select(peer):
            with patch("urllib.request.urlopen",
                       side_effect=_http_error(500, b"<html>nope")):
                _claim_worker([peer], "http://192.168.1.10:8000", "coordTok")
        assert "Claim failed" in claim_env.getvalue()

    def test_unreachable_worker(self, claim_env):
        from gpumesh.setup_wizard import _claim_worker

        peer = _peer()
        with self._select(peer):
            with patch("urllib.request.urlopen",
                       side_effect=urllib.error.URLError("timed out")):
                _claim_worker([peer], "http://192.168.1.10:8000", "coordTok")
        out = claim_env.getvalue()
        assert "Could not reach worker at http://192.168.1.55:49152/api/claim" in out

    def test_missing_claim_port_is_probed(self, claim_env):
        from gpumesh.setup_wizard import _claim_worker

        peer = _peer(claim_port=0)
        with self._select(peer):
            with patch("gpumesh.setup_wizard._probe_claim_port",
                       return_value=49154) as probe:
                with patch("urllib.request.urlopen",
                           return_value=_Resp({"ok": True})) as urlopen:
                    _claim_worker([peer], "http://192.168.1.10:8000", "coordTok")
        probe.assert_called_once_with("192.168.1.55")
        assert urlopen.call_args[0][0].full_url == \
            "http://192.168.1.55:49154/api/claim"
        assert "Found claim server on port 49154" in claim_env.getvalue()

    def test_probe_failure_explains_how_to_start_the_worker(self, claim_env):
        from gpumesh.setup_wizard import _claim_worker

        peer = _peer(claim_port=0)
        with self._select(peer):
            with patch("gpumesh.setup_wizard._probe_claim_port", return_value=0):
                with patch("urllib.request.urlopen") as urlopen:
                    _claim_worker([peer], "http://192.168.1.10:8000", "coordTok")
        urlopen.assert_not_called()
        out = claim_env.getvalue()
        assert "Could not find a claim server" in out
        assert "gpumesh worker --token <token>" in out

    def test_non_default_coordinator_port_is_used_for_candidates(self, claim_env,
                                                                 monkeypatch):
        from gpumesh.setup_wizard import _claim_worker

        seen = {}

        def _candidates(ip, port, limit=4):
            seen["port"] = port
            return [f"http://192.168.1.10:{port}"]

        monkeypatch.setattr(
            "gpumesh.setup_wizard.coordinator_url_candidates", _candidates)
        peer = _peer()
        with self._select(peer):
            with patch("urllib.request.urlopen", return_value=_Resp({"ok": True})):
                _claim_worker([peer], "http://192.168.1.10:8001", "coordTok")
        assert seen["port"] == 8001


@pytest.fixture
def coordinator_env(monkeypatch, wz_console):
    """Neutralise everything _setup_coordinator_radar touches off-box."""
    # The bind address is now resolved through cli, which reads GPUMESH_HOST.
    # A developer with it exported would otherwise see these tests fail for a
    # reason that has nothing to do with the code.
    monkeypatch.delenv("GPUMESH_HOST", raising=False)
    # cli._print_exposure_warning probes the local device to name it; keep
    # that off the (possibly torch-importing) real path.
    monkeypatch.setattr("gpumesh.capability.probe_device", lambda *a, **k: {
        "device": "cuda", "device_name": "RTX 3080",
    })
    monkeypatch.setattr("gpumesh.setup_wizard._has_tailscale", lambda: False)
    monkeypatch.setattr("gpumesh.setup_wizard._get_tailscale_ip", lambda: None)
    monkeypatch.setattr("gpumesh.setup_wizard.get_lan_ip",
                        lambda: "192.168.1.10")
    monkeypatch.setattr("gpumesh.setup_wizard.show_ip_alternatives",
                        lambda *a, **k: None)
    monkeypatch.setattr("gpumesh.setup_wizard.secrets.token_urlsafe",
                        lambda n=16: "testToken123")
    return wz_console


def _httpd():
    """A MagicMock server whose serve_forever returns at once.

    That matters: the wizard joins the serve thread, so a serve_forever that
    actually blocked would hang the test rather than fail it.
    """
    mock = MagicMock()
    mock.serve_forever.return_value = None
    return mock


class TestSetupCoordinatorRadar:
    def test_cancelling_the_network_question_starts_nothing(self, coordinator_env):
        from gpumesh.setup_wizard import _setup_coordinator_radar

        with patch("gpumesh.server.serve") as serve:
            with patch("builtins.input", side_effect=[]):
                _setup_coordinator_radar("cpu")
        serve.assert_not_called()

    def test_manual_choice_delegates(self, coordinator_env):
        from gpumesh.setup_wizard import _setup_coordinator_radar

        with patch("gpumesh.setup_wizard._setup_coordinator_manual") as manual:
            with patch("gpumesh.server.serve") as serve:
                with patch("builtins.input", side_effect=["2"]):
                    _setup_coordinator_radar("cpu")
        serve.assert_not_called()
        manual.assert_called_once()
        assert manual.call_args[0][3] == "192.168.1.10"
        assert manual.call_args[0][4] == "testToken123"

    def test_tailscale_choice_only_prints_instructions(self, coordinator_env,
                                                       monkeypatch):
        from gpumesh.setup_wizard import _setup_coordinator_radar

        monkeypatch.setattr("gpumesh.setup_wizard._has_tailscale", lambda: True)
        monkeypatch.setattr("gpumesh.setup_wizard._get_tailscale_ip",
                            lambda: "100.67.72.79")
        with patch("gpumesh.server.serve") as serve:
            with patch("builtins.input", side_effect=["2"]):
                _setup_coordinator_radar("cpu")
        serve.assert_not_called()
        out = coordinator_env.getvalue()
        assert "http://100.67.72.79:8000" in out
        assert "--tailscale" in out

    def test_missing_tailscale_offers_only_two_options(self, coordinator_env):
        from gpumesh.setup_wizard import _setup_coordinator_radar

        with patch("gpumesh.setup_wizard._setup_coordinator_manual"):
            with patch("builtins.input", side_effect=["2"]):
                _setup_coordinator_radar("cpu")
        out = coordinator_env.getvalue()
        assert "Tailscale not found" in out
        assert "https://tailscale.com/download" in out

    def test_port_8000_busy_falls_back_to_8001(self, coordinator_env):
        from gpumesh.setup_wizard import _setup_coordinator_radar

        httpd = _httpd()
        with patch("gpumesh.server.serve",
                   side_effect=[OSError("in use"), httpd]):
            with patch("gpumesh.connection_manager.save_connection") as save:
                with patch("gpumesh.setup_wizard.time.sleep",
                           side_effect=KeyboardInterrupt):
                    with patch("builtins.input", side_effect=["1"]):
                        _setup_coordinator_radar("cpu")
        out = coordinator_env.getvalue()
        assert "Server started on port 8001 instead" in out
        assert "http://192.168.1.10:8001" in out
        save.assert_called_once_with("http://192.168.1.10:8001", "testToken123")

    def test_both_ports_busy_gives_up_with_advice(self, coordinator_env):
        from gpumesh.setup_wizard import _setup_coordinator_radar

        with patch("gpumesh.server.serve", side_effect=OSError("in use")):
            with patch("gpumesh.connection_manager.save_connection") as save:
                with patch("builtins.input", side_effect=["1"]):
                    _setup_coordinator_radar("cpu")
        out = coordinator_env.getvalue()
        assert "Ports 8000 and 8001 are both in use" in out
        assert "gpumesh serve --port 8002" in out
        save.assert_not_called()

    def test_saves_the_connection_and_shows_the_panel(self, coordinator_env):
        from gpumesh.setup_wizard import _setup_coordinator_radar

        with patch("gpumesh.server.serve", return_value=_httpd()):
            with patch("gpumesh.connection_manager.save_connection") as save:
                with patch("gpumesh.setup_wizard.time.sleep",
                           side_effect=KeyboardInterrupt):
                    with patch("builtins.input", side_effect=["1"]):
                        _setup_coordinator_radar("cpu")
        save.assert_called_once_with("http://192.168.1.10:8000", "testToken123")
        assert "YOUR COORDINATOR IS RUNNING" in coordinator_env.getvalue()

    def _run_coordinator(self, serve_mock):
        from gpumesh.setup_wizard import _setup_coordinator_radar

        with patch("gpumesh.server.serve", serve_mock):
            with patch("gpumesh.connection_manager.save_connection"):
                with patch("gpumesh.setup_wizard.time.sleep",
                           side_effect=KeyboardInterrupt):
                    with patch("builtins.input", side_effect=["1"]):
                        _setup_coordinator_radar("cpu")

    def test_binds_wider_than_loopback_by_default(self, coordinator_env):
        """The wizard's own coordinator must be reachable from other machines.

        This mode's whole job is LAN discovery and claiming, so a loopback
        bind would be a wizard that cannot finish its own flow. The choice is
        deliberate — but it must be made by the same resolver `gpumesh serve`
        uses, not by a hardcoded "0.0.0.0" that no flag can reach.
        """
        from gpumesh.cli import DEFAULT_BIND_HOST, _is_loopback_bind
        from gpumesh.setup_wizard import WIZARD_LAN_BIND_HOST

        serve = MagicMock(return_value=_httpd())
        self._run_coordinator(serve)
        assert serve.call_args[0][0] == WIZARD_LAN_BIND_HOST
        assert serve.call_args[0][1] == 8000
        assert not _is_loopback_bind(serve.call_args[0][0])
        # ...and it is a different answer from cmd_serve's, on purpose.
        assert WIZARD_LAN_BIND_HOST != DEFAULT_BIND_HOST

    def test_gpumesh_host_overrides_the_bind(self, coordinator_env, monkeypatch):
        """The env override cmd_serve honours reaches the wizard too.

        The old hardcoded server.serve("0.0.0.0", ...) ignored it entirely.
        """
        monkeypatch.setenv("GPUMESH_HOST", "10.1.2.3")
        serve = MagicMock(return_value=_httpd())
        self._run_coordinator(serve)
        assert serve.call_args[0][0] == "10.1.2.3"

    def test_bind_comes_from_clis_resolver(self, coordinator_env, monkeypatch):
        """One function decides the bind address for serve AND setup."""
        import gpumesh.cli as cli

        calls = []
        real = cli._resolve_bind_host

        def _spy(args):
            calls.append(getattr(args, "host", None))
            return real(args)

        monkeypatch.setattr(cli, "_resolve_bind_host", _spy)
        self._run_coordinator(MagicMock(return_value=_httpd()))
        assert len(calls) == 1

    def test_a_loopback_override_is_honoured_but_explained(self, coordinator_env,
                                                            monkeypatch):
        """GPUMESH_HOST=127.0.0.1 wins — and the wizard says what it costs."""
        monkeypatch.setenv("GPUMESH_HOST", "127.0.0.1")
        serve = MagicMock(return_value=_httpd())
        self._run_coordinator(serve)
        assert serve.call_args[0][0] == "127.0.0.1"
        out = coordinator_env.getvalue()
        assert "CANNOT reach this coordinator" in out
        assert "GPUMESH_HOST" in out

    def test_the_exposure_warning_is_printed(self, coordinator_env, capsys):
        """`gpumesh setup` must warn exactly as `gpumesh serve` does.

        The wizard bound wider than serve did and printed nothing at all, so
        the two commands shipped opposite security defaults with the warning
        only on the safer one.
        """
        self._run_coordinator(MagicMock(return_value=_httpd()))
        # _print_exposure_warning writes with plain print(), not the rich
        # console the rest of the wizard uses.
        out = capsys.readouterr().out
        assert "NETWORK-EXPOSED COORDINATOR" in out
        assert "0.0.0.0:8000" in out
        assert "arbitrary code on this machine" in out

    def test_no_exposure_warning_on_a_loopback_bind(self, coordinator_env,
                                                     capsys, monkeypatch):
        monkeypatch.setenv("GPUMESH_HOST", "127.0.0.1")
        self._run_coordinator(MagicMock(return_value=_httpd()))
        assert "NETWORK-EXPOSED COORDINATOR" not in capsys.readouterr().out

    def test_the_warning_names_the_port_actually_bound(self, coordinator_env,
                                                        capsys):
        """Port 8000 busy → the warning must say 8001, not 8000."""
        serve = MagicMock(side_effect=[OSError("in use"), _httpd()])
        self._run_coordinator(serve)
        assert "0.0.0.0:8001" in capsys.readouterr().out

    def test_the_fallback_port_uses_the_same_bind_host(self, coordinator_env,
                                                        monkeypatch):
        monkeypatch.setenv("GPUMESH_HOST", "10.1.2.3")
        serve = MagicMock(side_effect=[OSError("in use"), _httpd()])
        self._run_coordinator(serve)
        assert [c[0][0] for c in serve.call_args_list] == ["10.1.2.3", "10.1.2.3"]

    def test_firewall_hint_only_when_the_rule_could_not_be_added(self,
                                                                 coordinator_env,
                                                                 monkeypatch):
        from gpumesh.setup_wizard import _setup_coordinator_radar

        hint = MagicMock()
        monkeypatch.setattr("gpumesh.setup_wizard.try_add_firewall_rule",
                            lambda port: True)
        monkeypatch.setattr("gpumesh.setup_wizard.show_firewall_hint", hint)
        with patch("gpumesh.server.serve", return_value=_httpd()):
            with patch("gpumesh.connection_manager.save_connection"):
                with patch("gpumesh.setup_wizard.time.sleep",
                           side_effect=KeyboardInterrupt):
                    with patch("builtins.input", side_effect=["1"]):
                        _setup_coordinator_radar("cpu")
        hint.assert_not_called()

    def test_firewall_hint_shown_on_failure(self, coordinator_env, monkeypatch):
        from gpumesh.setup_wizard import _setup_coordinator_radar

        hint = MagicMock()
        monkeypatch.setattr("gpumesh.setup_wizard.try_add_firewall_rule",
                            lambda port: False)
        monkeypatch.setattr("gpumesh.setup_wizard.show_firewall_hint", hint)
        with patch("gpumesh.server.serve", return_value=_httpd()):
            with patch("gpumesh.connection_manager.save_connection"):
                with patch("gpumesh.setup_wizard.time.sleep",
                           side_effect=KeyboardInterrupt):
                    with patch("builtins.input", side_effect=["1"]):
                        _setup_coordinator_radar("cpu")
        hint.assert_called_once_with(8000)

    def test_self_worker_failure_is_not_fatal(self, coordinator_env, monkeypatch):
        from gpumesh.setup_wizard import _setup_coordinator_radar

        def boom(*args, **kwargs):
            raise RuntimeError("no capability probe")

        monkeypatch.setattr("gpumesh.worker.spawn_local_worker", boom)
        with patch("gpumesh.server.serve", return_value=_httpd()):
            with patch("gpumesh.connection_manager.save_connection"):
                with patch("gpumesh.setup_wizard.time.sleep",
                           side_effect=KeyboardInterrupt):
                    with patch("builtins.input", side_effect=["1"]):
                        _setup_coordinator_radar("cpu")
        assert "YOUR COORDINATOR IS RUNNING" in coordinator_env.getvalue()

    def test_discovery_listener_failure_is_not_fatal(self, coordinator_env,
                                                     monkeypatch):
        from gpumesh.setup_wizard import _setup_coordinator_radar

        class _BrokenListener:
            def start(self):
                raise OSError("port 48900 already bound")

            def stop(self):
                pass

            def peers(self):
                return []

        monkeypatch.setattr("gpumesh.discovery.Listener",
                            lambda *a, **k: _BrokenListener())
        with patch("gpumesh.server.serve", return_value=_httpd()):
            with patch("gpumesh.connection_manager.save_connection"):
                with patch("gpumesh.setup_wizard.time.sleep",
                           side_effect=KeyboardInterrupt):
                    with patch("builtins.input", side_effect=["1"]):
                        _setup_coordinator_radar("cpu")
        out = coordinator_env.getvalue()
        assert "Failed to start discovery listener" in out
        assert "manual join still works" in out
        assert "YOUR COORDINATOR IS RUNNING" in out

    def test_a_discovered_worker_triggers_the_claim_flow(self, coordinator_env,
                                                         monkeypatch):
        from gpumesh.setup_wizard import _setup_coordinator_radar

        peer = _peer()

        class _Listener:
            def start(self):
                pass

            def stop(self):
                pass

            def peers(self):
                return [peer]

        monkeypatch.setattr("gpumesh.discovery.Listener",
                            lambda *a, **k: _Listener())
        with patch("gpumesh.setup_wizard._claim_worker") as claim:
            with patch("gpumesh.server.serve", return_value=_httpd()):
                with patch("gpumesh.connection_manager.save_connection"):
                    with patch("builtins.input", side_effect=["1"]):
                        _setup_coordinator_radar("cpu")
        claim.assert_called_once_with([peer], "http://192.168.1.10:8000",
                                      "testToken123")
        assert "YOU'RE LIVE!" in coordinator_env.getvalue()

    def test_interrupted_scan_stops_the_listener_and_server(self, coordinator_env,
                                                            monkeypatch):
        from gpumesh.setup_wizard import _setup_coordinator_radar

        stopped = []

        class _Listener:
            def start(self):
                pass

            def stop(self):
                stopped.append(True)

            def peers(self):
                return []

        monkeypatch.setattr("gpumesh.discovery.Listener",
                            lambda *a, **k: _Listener())
        httpd = _httpd()
        with patch("gpumesh.server.serve", return_value=httpd):
            with patch("gpumesh.connection_manager.save_connection"):
                with patch("gpumesh.setup_wizard.time.sleep",
                           side_effect=KeyboardInterrupt):
                    with patch("builtins.input", side_effect=["1"]):
                        _setup_coordinator_radar("cpu")
        assert stopped == [True]
        httpd.shutdown.assert_called_once()


class TestSetupWorkerRadar:
    """The worker's network question — the mirror of the coordinator's."""

    @pytest.fixture(autouse=True)
    def _no_tailscale(self, monkeypatch):
        monkeypatch.setattr("gpumesh.setup_wizard._has_tailscale", lambda: False)

    def test_lan_choice_explains_the_broadcast_model_then_delegates(self, wz_console):
        from gpumesh.setup_wizard import _setup_worker_radar

        with patch("gpumesh.setup_wizard._setup_worker_radar_scan") as scan:
            with patch("builtins.input", side_effect=["1"]):
                _setup_worker_radar("cuda")
        scan.assert_called_once_with("cuda")
        out = wz_console.getvalue()
        assert "Worker role" in out
        assert "broadcast its presence on the LAN" in out

    def test_manual_choice_is_reachable(self, wz_console):
        """Without Tailscale, option 2 is manual URL/token entry."""
        from gpumesh.setup_wizard import _setup_worker_radar

        with patch("gpumesh.setup_wizard._setup_worker_manual") as manual:
            with patch("gpumesh.setup_wizard._setup_worker_radar_scan") as scan:
                with patch("builtins.input", side_effect=["2"]):
                    _setup_worker_radar("cuda")
        manual.assert_called_once_with("cuda")
        scan.assert_not_called()

    def test_tailscale_choice_is_reachable(self, wz_console, monkeypatch):
        from gpumesh.setup_wizard import _setup_worker_radar

        monkeypatch.setattr("gpumesh.setup_wizard._has_tailscale", lambda: True)
        with patch("gpumesh.setup_wizard._setup_worker_tailscale") as tail:
            with patch("gpumesh.setup_wizard._setup_worker_radar_scan") as scan:
                with patch("builtins.input", side_effect=["2"]):
                    _setup_worker_radar("cuda")
        tail.assert_called_once_with("cuda")
        scan.assert_not_called()

    def test_manual_is_option_three_when_tailscale_is_present(self, wz_console,
                                                               monkeypatch):
        """The numbering mirrors the coordinator side exactly."""
        from gpumesh.setup_wizard import _setup_worker_radar

        monkeypatch.setattr("gpumesh.setup_wizard._has_tailscale", lambda: True)
        with patch("gpumesh.setup_wizard._setup_worker_manual") as manual:
            with patch("builtins.input", side_effect=["3"]):
                _setup_worker_radar("cuda")
        manual.assert_called_once_with("cuda")

    def test_missing_tailscale_says_how_to_get_it(self, wz_console):
        from gpumesh.setup_wizard import _setup_worker_radar

        with patch("gpumesh.setup_wizard._setup_worker_manual"):
            with patch("builtins.input", side_effect=["2"]):
                _setup_worker_radar("cuda")
        out = wz_console.getvalue()
        assert "Tailscale not found" in out
        assert "https://tailscale.com/download" in out

    def test_cancelling_the_network_question_starts_nothing(self, wz_console):
        from gpumesh.setup_wizard import _setup_worker_radar

        with patch("gpumesh.setup_wizard._setup_worker_radar_scan") as scan:
            with patch("gpumesh.setup_wizard._setup_worker_manual") as manual:
                with patch("builtins.input", side_effect=[]):
                    _setup_worker_radar("cuda")
        scan.assert_not_called()
        manual.assert_not_called()


@pytest.fixture
def worker_scan_env(monkeypatch, wz_console):
    """capability probe stubbed out so the scan tests never benchmark."""
    monkeypatch.setattr("gpumesh.capability.full_probe", lambda *a, **k: {
        "device": "cuda", "device_name": "RTX 3080", "score": 85.2,
    })
    return wz_console


class TestSetupWorkerRadarScan:
    def test_shows_the_detected_hardware(self, worker_scan_env):
        from gpumesh.setup_wizard import _setup_worker_radar_scan

        with patch("gpumesh.worker.run_worker_broadcast") as broadcast:
            with patch("builtins.input", side_effect=["longenoughtoken", "y"]):
                _setup_worker_radar_scan("cuda")
        out = worker_scan_env.getvalue()
        assert "Device: cuda (RTX 3080)" in out
        assert "Score:  85.2 GFLOP/s" in out
        broadcast.assert_called_once_with("longenoughtoken")

    def test_cancelled_token_prompt_starts_nothing(self, worker_scan_env):
        from gpumesh.setup_wizard import _setup_worker_radar_scan

        with patch("gpumesh.worker.run_worker_broadcast") as broadcast:
            with patch("builtins.input", side_effect=[]):
                _setup_worker_radar_scan("cpu")
        broadcast.assert_not_called()

    def test_whitespace_only_token_is_rejected(self, worker_scan_env):
        from gpumesh.setup_wizard import _setup_worker_radar_scan

        with patch("gpumesh.worker.run_worker_broadcast") as broadcast:
            with patch("builtins.input", side_effect=["        "]):
                _setup_worker_radar_scan("cpu")
        broadcast.assert_not_called()
        assert "Token cannot be empty" in worker_scan_env.getvalue()

    def test_short_token_is_rejected(self, worker_scan_env):
        from gpumesh.setup_wizard import _setup_worker_radar_scan

        with patch("gpumesh.worker.run_worker_broadcast") as broadcast:
            with patch("builtins.input", side_effect=["short"]):
                _setup_worker_radar_scan("cpu")
        broadcast.assert_not_called()
        assert "at least 8 characters" in worker_scan_env.getvalue()

    def test_token_is_trimmed_before_use(self, worker_scan_env):
        from gpumesh.setup_wizard import _setup_worker_radar_scan

        with patch("gpumesh.worker.run_worker_broadcast") as broadcast:
            with patch("builtins.input", side_effect=["  paddedtoken  ", "y"]):
                _setup_worker_radar_scan("cpu")
        broadcast.assert_called_once_with("paddedtoken")

    def test_cancelled_confirmation(self, worker_scan_env):
        from gpumesh.setup_wizard import _setup_worker_radar_scan

        with patch("gpumesh.worker.run_worker_broadcast") as broadcast:
            with patch("builtins.input", side_effect=["longenoughtoken", "n"]):
                _setup_worker_radar_scan("cpu")
        broadcast.assert_not_called()
        assert "Cancelled" in worker_scan_env.getvalue()

    def test_ctrl_c_during_broadcast_exits_quietly(self, worker_scan_env):
        from gpumesh.setup_wizard import _setup_worker_radar_scan

        with patch("gpumesh.worker.run_worker_broadcast",
                   side_effect=KeyboardInterrupt):
            with patch("builtins.input", side_effect=["longenoughtoken", "y"]):
                _setup_worker_radar_scan("cpu")
        assert "[ERROR]" not in worker_scan_env.getvalue()

    def test_a_short_token_is_re_prompted_not_fatal(self, worker_scan_env):
        """A typo costs one line, not the whole wizard.

        A too-short token used to print an error and return, so the user had
        to re-run 'gpumesh setup' from the top — hardware detection and all.
        """
        from gpumesh.setup_wizard import _setup_worker_radar_scan

        with patch("gpumesh.worker.run_worker_broadcast") as broadcast:
            with patch("builtins.input",
                       side_effect=["short", "longenoughtoken", "y"]):
                _setup_worker_radar_scan("cpu")
        broadcast.assert_called_once_with("longenoughtoken")
        assert "at least 8 characters" in worker_scan_env.getvalue()

    def test_an_empty_token_is_re_prompted_not_fatal(self, worker_scan_env):
        from gpumesh.setup_wizard import _setup_worker_radar_scan

        with patch("gpumesh.worker.run_worker_broadcast") as broadcast:
            with patch("builtins.input",
                       side_effect=["", "   ", "longenoughtoken", "y"]):
                _setup_worker_radar_scan("cpu")
        broadcast.assert_called_once_with("longenoughtoken")

    def test_repeated_mistakes_keep_re_prompting(self, worker_scan_env):
        from gpumesh.setup_wizard import _setup_worker_radar_scan

        with patch("gpumesh.worker.run_worker_broadcast") as broadcast:
            with patch("builtins.input",
                       side_effect=["a", "bb", "ccc", "goodenoughtoken", "y"]):
                _setup_worker_radar_scan("cpu")
        broadcast.assert_called_once_with("goodenoughtoken")

    @pytest.mark.parametrize("word", ["q", "quit", "cancel", "QUIT"])
    def test_the_quit_word_is_an_explicit_way_out(self, worker_scan_env, word):
        """A re-prompting loop needs an exit that is not Ctrl+C."""
        from gpumesh.setup_wizard import _setup_worker_radar_scan

        with patch("gpumesh.worker.run_worker_broadcast") as broadcast:
            with patch("builtins.input", side_effect=["short", word]):
                _setup_worker_radar_scan("cpu")
        broadcast.assert_not_called()
        assert "Cancelled" in worker_scan_env.getvalue()

    def test_the_way_out_is_advertised_before_the_prompt(self, worker_scan_env):
        from gpumesh.setup_wizard import _setup_worker_radar_scan

        with patch("gpumesh.worker.run_worker_broadcast"):
            with patch("builtins.input", side_effect=["longenoughtoken", "y"]):
                _setup_worker_radar_scan("cpu")
        assert "'q' to quit" in worker_scan_env.getvalue()

    def test_the_loop_ends_when_input_runs_out(self, worker_scan_env):
        """A headless run must not spin forever on a closed stdin."""
        from gpumesh.setup_wizard import _setup_worker_radar_scan

        with patch("gpumesh.worker.run_worker_broadcast") as broadcast:
            with patch("builtins.input", side_effect=["short"]):
                _setup_worker_radar_scan("cpu")
        broadcast.assert_not_called()

    def test_broadcast_failure_is_reported(self, worker_scan_env):
        from gpumesh.setup_wizard import _setup_worker_radar_scan

        with patch("gpumesh.worker.run_worker_broadcast",
                   side_effect=OSError("cannot bind UDP 48900")):
            with patch("builtins.input", side_effect=["longenoughtoken", "y"]):
                _setup_worker_radar_scan("cpu")
        out = worker_scan_env.getvalue()
        assert "Failed to start broadcast: cannot bind UDP 48900" in out
        assert "Check your network settings" in out


class TestSetupWorkerTailscaleAndManual:
    """The two non-broadcast worker paths.

    Both are reachable from the wizard's worker menu (see
    ``TestSetupWorkerRadar``); this class drives them directly.
    """

    @pytest.fixture(autouse=True)
    def _probe(self, monkeypatch):
        monkeypatch.setattr("gpumesh.capability.full_probe", lambda *a, **k: {
            "device": "cpu", "device_name": "Test CPU", "score": 1.0,
        })

    def test_tailscale_happy_path(self, wz_console):
        from gpumesh.setup_wizard import _setup_worker_tailscale

        with patch("gpumesh.connection_manager.save_connection") as save:
            with patch("gpumesh.worker.run_worker") as run:
                with patch("builtins.input",
                           side_effect=["100.67.72.79:8000", "tok12345"]):
                    _setup_worker_tailscale("cpu")
        save.assert_called_once_with("http://100.67.72.79:8000", "tok12345")
        run.assert_called_once_with("http://100.67.72.79:8000", "tok12345")

    def test_tailscale_missing_url(self, wz_console):
        from gpumesh.setup_wizard import _setup_worker_tailscale

        with patch("gpumesh.worker.run_worker") as run:
            with patch("builtins.input", side_effect=["   "]):
                _setup_worker_tailscale("cpu")
        run.assert_not_called()
        assert "No URL provided" in wz_console.getvalue()

    def test_tailscale_missing_token(self, wz_console):
        from gpumesh.setup_wizard import _setup_worker_tailscale

        with patch("gpumesh.connection_manager.save_connection") as save:
            with patch("gpumesh.worker.run_worker") as run:
                with patch("builtins.input", side_effect=["100.67.72.79", ""]):
                    _setup_worker_tailscale("cpu")
        run.assert_not_called()
        save.assert_not_called()
        assert "No token provided" in wz_console.getvalue()

    def test_tailscale_connection_failure_is_reported(self, wz_console):
        from gpumesh.setup_wizard import _setup_worker_tailscale

        with patch("gpumesh.connection_manager.save_connection"):
            with patch("gpumesh.worker.run_worker",
                       side_effect=OSError("connection refused")):
                with patch("builtins.input",
                           side_effect=["100.67.72.79", "tok12345"]):
                    _setup_worker_tailscale("cpu")
        assert "Failed to connect: connection refused" in wz_console.getvalue()

    def test_manual_happy_path_normalises_a_bare_ip(self, wz_console):
        from gpumesh.setup_wizard import _setup_worker_manual

        with patch("gpumesh.connection_manager.save_connection") as save:
            with patch("gpumesh.worker.run_worker") as run:
                with patch("builtins.input",
                           side_effect=["192.168.1.10", "tok12345"]):
                    _setup_worker_manual("cpu")
        save.assert_called_once_with("http://192.168.1.10:8000", "tok12345")
        run.assert_called_once_with("http://192.168.1.10:8000", "tok12345")
        assert "Connecting to: http://192.168.1.10:8000" in wz_console.getvalue()

    def test_manual_missing_ip(self, wz_console):
        from gpumesh.setup_wizard import _setup_worker_manual

        with patch("gpumesh.worker.run_worker") as run:
            with patch("builtins.input", side_effect=[""]):
                _setup_worker_manual("cpu")
        run.assert_not_called()
        assert "No IP provided" in wz_console.getvalue()

    def test_manual_re_prompts_after_a_mistyped_port(self, wz_console):
        """A bad port is caught here, not 20 seconds later on the far side.

        "192.168.1.10:80o0" used to normalise to
        "http://192.168.1.10:80o0:8000" and be handed straight to run_worker.
        """
        from gpumesh.setup_wizard import _setup_worker_manual

        with patch("gpumesh.connection_manager.save_connection") as save:
            with patch("gpumesh.worker.run_worker") as run:
                with patch("builtins.input",
                           side_effect=["192.168.1.10:80o0", "192.168.1.10:8000",
                                        "tok12345"]):
                    _setup_worker_manual("cpu")
        out = wz_console.getvalue()
        assert "80o0" in out
        assert "is not a port number" in out
        save.assert_called_once_with("http://192.168.1.10:8000", "tok12345")
        run.assert_called_once_with("http://192.168.1.10:8000", "tok12345")

    def test_manual_never_saves_or_dials_a_malformed_url(self, wz_console):
        """A rejected address must not reach save_connection or run_worker.

        The second answer here is the token: under the old code the mistyped
        address sailed through as "http://192.168.1.10:80o0:8000", was saved
        as the machine's connection, and was handed to run_worker.
        """
        from gpumesh.setup_wizard import _setup_worker_manual

        with patch("gpumesh.connection_manager.save_connection") as save:
            with patch("gpumesh.worker.run_worker") as run:
                with patch("builtins.input",
                           side_effect=["192.168.1.10:80o0", "tok12345"]):
                    _setup_worker_manual("cpu")
        run.assert_not_called()
        save.assert_not_called()
        assert "80o0:8000" not in wz_console.getvalue()

    def test_manual_quit_word_stops_the_url_loop(self, wz_console):
        from gpumesh.setup_wizard import _setup_worker_manual

        with patch("gpumesh.worker.run_worker") as run:
            with patch("builtins.input", side_effect=["not a host", "q"]):
                _setup_worker_manual("cpu")
        run.assert_not_called()
        assert "No IP provided" in wz_console.getvalue()

    def test_manual_uppercase_scheme_is_accepted(self, wz_console):
        from gpumesh.setup_wizard import _setup_worker_manual

        with patch("gpumesh.connection_manager.save_connection") as save:
            with patch("gpumesh.worker.run_worker"):
                with patch("builtins.input",
                           side_effect=["HTTP://192.168.1.10:8000", "tok12345"]):
                    _setup_worker_manual("cpu")
        save.assert_called_once_with("http://192.168.1.10:8000", "tok12345")

    def test_tailscale_re_prompts_after_a_bad_url(self, wz_console):
        from gpumesh.setup_wizard import _setup_worker_tailscale

        with patch("gpumesh.connection_manager.save_connection") as save:
            with patch("gpumesh.worker.run_worker") as run:
                with patch("builtins.input",
                           side_effect=["ftp://100.67.72.79", "100.67.72.79:8000",
                                        "tok12345"]):
                    _setup_worker_tailscale("cpu")
        assert "not a supported scheme" in wz_console.getvalue()
        save.assert_called_once_with("http://100.67.72.79:8000", "tok12345")
        run.assert_called_once_with("http://100.67.72.79:8000", "tok12345")

    def test_an_empty_token_is_re_prompted(self, wz_console):
        """The coordinator's token has no length rule, but must exist."""
        from gpumesh.setup_wizard import _setup_worker_manual

        with patch("gpumesh.connection_manager.save_connection") as save:
            with patch("gpumesh.worker.run_worker"):
                with patch("builtins.input",
                           side_effect=["192.168.1.10", "", "   ", "tok12345"]):
                    _setup_worker_manual("cpu")
        save.assert_called_once_with("http://192.168.1.10:8000", "tok12345")

    def test_manual_ctrl_c_while_joining_is_quiet(self, wz_console):
        from gpumesh.setup_wizard import _setup_worker_manual

        with patch("gpumesh.connection_manager.save_connection"):
            with patch("gpumesh.worker.run_worker", side_effect=KeyboardInterrupt):
                with patch("builtins.input",
                           side_effect=["192.168.1.10", "tok12345"]):
                    _setup_worker_manual("cpu")
        assert "[ERROR]" not in wz_console.getvalue()


class TestWizardStructure:
    def test_worker_manual_and_tailscale_paths_are_reachable(self):
        """Closes a design gap: the wizard has a worker path off the LAN.

        ``_setup_worker_radar`` used to call only
        ``_setup_worker_radar_scan``, leaving ``_setup_worker_tailscale`` and
        ``_setup_worker_manual`` defined but never called — so a friend told
        by the coordinator's own instructions to run 'gpumesh setup' had
        nowhere to type the URL and token they had just been given.
        """
        import gpumesh.setup_wizard as wz

        radar = inspect.getsource(wz._setup_worker_radar)
        for name in ("_setup_worker_tailscale", "_setup_worker_manual",
                     "_setup_worker_radar_scan"):
            assert f"{name}(device)" in radar, f"{name} has no caller"

    def test_worker_options_mirror_the_coordinator_options(self):
        """Both sides must offer LAN / Tailscale / manual in the same order.

        The coordinator reads its options out loud to the person running the
        worker; a different menu on the two sides is how the flow came apart
        in the first place.
        """
        import gpumesh.setup_wizard as wz

        def _labels(func):
            source = inspect.getsource(func)
            return [
                token
                for token in ("Same WiFi / LAN", "Tailscale", "Manual setup")
                if token in source
            ]

        assert _labels(wz._setup_worker_radar) == \
            _labels(wz._setup_coordinator_radar)

    def test_the_wizard_has_exactly_one_bind_decision(self):
        """No hardcoded bind address anywhere in the wizard.

        The auto-discovery path used to call server.serve("0.0.0.0", ...)
        directly, which ignored --host, ignored GPUMESH_HOST, and skipped
        the exposure warning that `gpumesh serve` prints — so `gpumesh setup`
        and `gpumesh serve` shipped opposite security defaults with no
        warning on the more permissive one.
        """
        import gpumesh.setup_wizard as wz

        source = inspect.getsource(wz._setup_coordinator_radar)
        assert '"0.0.0.0"' not in source
        assert "serve(bind_host" in source

    def test_missing_ui_dependencies_exit_with_instructions(self, capsys,
                                                            monkeypatch):
        from gpumesh.setup_wizard import run_setup_wizard

        monkeypatch.setattr("gpumesh.setup_wizard._HAS_UI_DEPS", False)
        with pytest.raises(SystemExit) as excinfo:
            run_setup_wizard()
        assert excinfo.value.code == 1
        out = capsys.readouterr().out
        assert "pip install gpumesh[ui]" in out
