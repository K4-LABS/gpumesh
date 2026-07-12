"""Tests for tunnel.py - Tailscale auto-detection and tunnel modes."""

import pytest
from unittest.mock import patch, MagicMock


class TestGetTailscaleIP:
    """Tests for _get_tailscale_ip() function."""

    def test_tailscale_not_installed(self):
        """Returns None when tailscale CLI is not found."""
        from gpumesh.tunnel import _get_tailscale_ip
        
        with patch("gpumesh.tunnel.shutil.which", return_value=None):
            result = _get_tailscale_ip()
            assert result is None

    def test_tailscale_installed_returns_ip(self):
        """Returns Tailscale IP when tailscale is installed and running."""
        from gpumesh.tunnel import _get_tailscale_ip
        
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "100.67.72.79\n"
        
        with patch("gpumesh.tunnel.shutil.which", return_value="/usr/bin/tailscale"), \
             patch("gpumesh.tunnel.subprocess.run", return_value=mock_result):
            result = _get_tailscale_ip()
            assert result == "100.67.72.79"

    def test_tailscale_returns_non_tailscale_ip(self):
        """Returns None when tailscale returns non-Tailscale IP."""
        from gpumesh.tunnel import _get_tailscale_ip
        
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "192.168.1.100\n"  # Not a Tailscale IP
        
        with patch("gpumesh.tunnel.shutil.which", return_value="/usr/bin/tailscale"), \
             patch("gpumesh.tunnel.subprocess.run", return_value=mock_result):
            result = _get_tailscale_ip()
            assert result is None

    def test_tailscale_command_fails(self):
        """Returns None when tailscale command fails."""
        from gpumesh.tunnel import _get_tailscale_ip
        from subprocess import SubprocessError
        
        with patch("gpumesh.tunnel.shutil.which", return_value="/usr/bin/tailscale"), \
             patch("gpumesh.tunnel.subprocess.run", side_effect=SubprocessError("fail")):
            result = _get_tailscale_ip()
            assert result is None

    def test_tailscale_empty_output(self):
        """Returns None when tailscale returns empty output."""
        from gpumesh.tunnel import _get_tailscale_ip
        
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "\n"
        
        with patch("gpumesh.tunnel.shutil.which", return_value="/usr/bin/tailscale"), \
             patch("gpumesh.tunnel.subprocess.run", return_value=mock_result):
            result = _get_tailscale_ip()
            assert result is None

    def test_tailscale_nonzero_exit_code(self):
        """Returns None when tailscale command returns non-zero exit code."""
        from gpumesh.tunnel import _get_tailscale_ip
        
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        
        with patch("gpumesh.tunnel.shutil.which", return_value="/usr/bin/tailscale"), \
             patch("gpumesh.tunnel.subprocess.run", return_value=mock_result):
            result = _get_tailscale_ip()
            assert result is None


class TestOpenTunnel:
    """Tests for open_tunnel() function."""

    def test_mode_none_returns_none(self):
        """mode='none' returns None without trying anything."""
        from gpumesh.tunnel import open_tunnel
        
        result = open_tunnel(8000, mode="none")
        assert result is None

    def test_mode_tailscale_success(self):
        """mode='tailscale' returns URL when Tailscale is available."""
        from gpumesh.tunnel import open_tunnel
        
        with patch("gpumesh.tunnel._get_tailscale_ip", return_value="100.67.72.79"):
            result = open_tunnel(8000, mode="tailscale")
            assert result == "http://100.67.72.79:8000"

    def test_mode_tailscale_not_available(self):
        """mode='tailscale' returns None when Tailscale not installed."""
        from gpumesh.tunnel import open_tunnel
        
        with patch("gpumesh.tunnel._get_tailscale_ip", return_value=None):
            result = open_tunnel(8000, mode="tailscale")
            assert result is None

    def test_mode_ngrok_not_installed(self):
        """mode='ngrok' returns None when pyngrok not installed."""
        from gpumesh.tunnel import open_tunnel
        
        with patch("gpumesh.tunnel._get_tailscale_ip", return_value=None), \
             patch.dict("sys.modules", {"pyngrok": None, "pyngrok.ngrok": None}):
            # pyngrok is not installed, so it should fail gracefully
            result = open_tunnel(8000, mode="ngrok")
            assert result is None

    def test_mode_auto_with_tailscale(self):
        """mode='auto' uses Tailscale when available."""
        from gpumesh.tunnel import open_tunnel
        
        with patch("gpumesh.tunnel._get_tailscale_ip", return_value="100.67.72.79"):
            result = open_tunnel(8000, mode="auto")
            assert result == "http://100.67.72.79:8000"

    def test_mode_auto_without_tailscale(self):
        """mode='auto' falls back to LAN-only when Tailscale not available."""
        from gpumesh.tunnel import open_tunnel
        
        with patch("gpumesh.tunnel._get_tailscale_ip", return_value=None), \
             patch.dict("sys.modules", {"pyngrok": None, "pyngrok.ngrok": None}):
            # No Tailscale, no ngrok -> LAN-only
            result = open_tunnel(8000, mode="auto")
            assert result is None

    def test_different_port(self):
        """Port is correctly included in the URL."""
        from gpumesh.tunnel import open_tunnel
        
        with patch("gpumesh.tunnel._get_tailscale_ip", return_value="100.67.72.79"):
            result = open_tunnel(9000, mode="tailscale")
            assert result == "http://100.67.72.79:9000"

    def test_mode_ngrok_success(self):
        """mode='ngrok' returns URL when pyngrok is installed and working."""
        from gpumesh.tunnel import open_tunnel
        
        mock_tunnel = MagicMock()
        mock_tunnel.public_url = "https://abc123.ngrok-free.app"
        mock_ngrok = MagicMock()
        mock_ngrok.connect.return_value = mock_tunnel
        
        mock_module = MagicMock()
        mock_module.ngrok = mock_ngrok
        
        with patch("gpumesh.tunnel._get_tailscale_ip", return_value=None), \
             patch.dict("sys.modules", {"pyngrok": mock_module, "pyngrok.ngrok": mock_ngrok}):
            result = open_tunnel(8000, mode="ngrok")
            assert result == "https://abc123.ngrok-free.app"
            mock_ngrok.connect.assert_called_once_with(8000, "http")

    def test_mode_auto_fallback_to_ngrok(self):
        """mode='auto' falls back to ngrok when Tailscale not available."""
        from gpumesh.tunnel import open_tunnel
        
        mock_tunnel = MagicMock()
        mock_tunnel.public_url = "https://xyz789.ngrok-free.app"
        mock_ngrok = MagicMock()
        mock_ngrok.connect.return_value = mock_tunnel
        
        mock_module = MagicMock()
        mock_module.ngrok = mock_ngrok
        
        with patch("gpumesh.tunnel._get_tailscale_ip", return_value=None), \
             patch.dict("sys.modules", {"pyngrok": mock_module, "pyngrok.ngrok": mock_ngrok}):
            result = open_tunnel(8000, mode="auto")
            assert result == "https://xyz789.ngrok-free.app"

    def test_ngrok_connection_error(self):
        """ngrok connection error is handled gracefully."""
        from gpumesh.tunnel import open_tunnel
        
        mock_ngrok = MagicMock()
        mock_ngrok.connect.side_effect = Exception("ngrok connection failed")
        
        mock_module = MagicMock()
        mock_module.ngrok = mock_ngrok
        
        with patch("gpumesh.tunnel._get_tailscale_ip", return_value=None), \
             patch.dict("sys.modules", {"pyngrok": mock_module, "pyngrok.ngrok": mock_ngrok}):
            result = open_tunnel(8000, mode="ngrok")
            assert result is None

    def test_tailscale_subprocess_timeout(self):
        """Handles subprocess timeout when tailscale is slow."""
        from gpumesh.tunnel import _get_tailscale_ip
        from subprocess import TimeoutExpired
        
        with patch("gpumesh.tunnel.shutil.which", return_value="/usr/bin/tailscale"), \
             patch("gpumesh.tunnel.subprocess.run", side_effect=TimeoutExpired("tailscale", 5)):
            result = _get_tailscale_ip()
            assert result is None

    def test_tailscale_os_error(self):
        """Handles OS error when tailscale binary is corrupted."""
        from gpumesh.tunnel import _get_tailscale_ip
        
        with patch("gpumesh.tunnel.shutil.which", return_value="/usr/bin/tailscale"), \
             patch("gpumesh.tunnel.subprocess.run", side_effect=OSError("permission denied")):
            result = _get_tailscale_ip()
            assert result is None
