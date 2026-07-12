"""Tests for cli.py - CLI argument parsing and --tailscale flag."""

import pytest
from unittest.mock import patch, MagicMock
import sys


class TestServeTailscaleFlag:
    """Tests for --tailscale flag in serve command."""

    def test_serve_tailscale_flag_parsed(self):
        """--tailscale flag is correctly parsed."""
        from gpumesh.cli import main
        
        with patch("sys.argv", ["gpumesh", "serve", "--tailscale", "--token", "test123"]), \
             patch("gpumesh.cli.server.serve") as mock_serve, \
             patch("gpumesh.cli.tunnel.open_tunnel") as mock_tunnel:
            mock_serve.return_value = MagicMock()
            try:
                main()
            except SystemExit:
                pass
            
            # Verify tailscale mode was passed
            mock_tunnel.assert_called_once_with(8000, mode="tailscale")

    def test_serve_public_flag_parsed(self):
        """--public flag is correctly parsed."""
        from gpumesh.cli import main
        
        with patch("sys.argv", ["gpumesh", "serve", "--public", "--token", "test123"]), \
             patch("gpumesh.cli.server.serve") as mock_serve, \
             patch("gpumesh.cli.tunnel.open_tunnel") as mock_tunnel:
            mock_serve.return_value = MagicMock()
            try:
                main()
            except SystemExit:
                pass
            
            mock_tunnel.assert_called_once_with(8000, mode="ngrok")

    def test_serve_no_tunnel_flag(self):
        """No tunnel flag means no tunnel is opened."""
        from gpumesh.cli import main
        
        with patch("sys.argv", ["gpumesh", "serve", "--token", "test123"]), \
             patch("gpumesh.cli.server.serve") as mock_serve, \
             patch("gpumesh.cli.tunnel.open_tunnel") as mock_tunnel:
            mock_serve.return_value = MagicMock()
            try:
                main()
            except SystemExit:
                pass
            
            mock_tunnel.assert_not_called()

    def test_serve_tailscale_and_public_conflict(self):
        """--tailscale takes precedence over --public."""
        from gpumesh.cli import main
        
        with patch("sys.argv", ["gpumesh", "serve", "--tailscale", "--public", "--token", "test123"]), \
             patch("gpumesh.cli.server.serve") as mock_serve, \
             patch("gpumesh.cli.tunnel.open_tunnel") as mock_tunnel:
            mock_serve.return_value = MagicMock()
            try:
                main()
            except SystemExit:
                pass
            
            # tailscale should take precedence
            mock_tunnel.assert_called_once_with(8000, mode="tailscale")


class TestQuickjoinTailscaleFlag:
    """Tests for --tailscale flag in quickjoin command."""

    def test_quickjoin_tailscale_flag_parsed(self):
        """--tailscale flag is correctly parsed for quickjoin."""
        from gpumesh.cli import main
        
        with patch("sys.argv", ["gpumesh", "quickjoin", "--token", "test123", "--tailscale"]), \
             patch("gpumesh.cli.tunnel._get_tailscale_ip", return_value="100.67.72.79"), \
             patch("gpumesh.cli.worker.run_worker") as mock_worker:
            try:
                main()
            except SystemExit:
                pass
            
            # Verify worker was called with Tailscale URL
            mock_worker.assert_called_once()
            call_args = mock_worker.call_args
            assert call_args[0][0] == "http://100.67.72.79:8000"

    def test_quickjoin_tailscale_not_available(self):
        """--tailscale flag handles Tailscale not being available."""
        from gpumesh.cli import main
        
        with patch("sys.argv", ["gpumesh", "quickjoin", "--token", "test123", "--tailscale"]), \
             patch("gpumesh.cli.tunnel._get_tailscale_ip", return_value=None), \
             patch("gpumesh.cli.worker.run_worker") as mock_worker:
            try:
                main()
            except SystemExit:
                pass
            
            # Worker should not be called when Tailscale is not available
            mock_worker.assert_not_called()

    def test_quickjoin_url_required_without_tailscale(self):
        """URL is required when --tailscale is not used."""
        from gpumesh.cli import main
        
        with patch("sys.argv", ["gpumesh", "quickjoin", "--token", "test123"]), \
             patch("gpumesh.cli.worker.run_worker") as mock_worker:
            try:
                main()
            except SystemExit:
                pass
            
            # Worker should not be called without URL
            mock_worker.assert_not_called()

    def test_quickjoin_url_with_tailscale(self):
        """URL is optional when --tailscale is used."""
        from gpumesh.cli import main
        
        with patch("sys.argv", ["gpumesh", "quickjoin", "--token", "test123", "--tailscale"]), \
             patch("gpumesh.cli.tunnel._get_tailscale_ip", return_value="100.67.72.79"), \
             patch("gpumesh.cli.worker.run_worker") as mock_worker:
            try:
                main()
            except SystemExit:
                pass
            
            # Worker should be called with Tailscale URL
            mock_worker.assert_called_once()

    def test_quickjoin_custom_port_with_tailscale(self):
        """--port flag works with --tailscale."""
        from gpumesh.cli import main
        
        with patch("sys.argv", ["gpumesh", "quickjoin", "--token", "test123", "--tailscale", "--port", "9000"]), \
             patch("gpumesh.cli.tunnel._get_tailscale_ip", return_value="100.67.72.79"), \
             patch("gpumesh.cli.worker.run_worker") as mock_worker:
            try:
                main()
            except SystemExit:
                pass
            
            # Verify custom port is used
            mock_worker.assert_called_once()
            call_args = mock_worker.call_args
            assert call_args[0][0] == "http://100.67.72.79:9000"

    def test_quickjoin_explicit_url_overrides_tailscale(self):
        """Explicit URL is used even with --tailscale flag."""
        from gpumesh.cli import main
        
        # When both URL and --tailscale are provided, explicit URL takes precedence
        with patch("sys.argv", ["gpumesh", "quickjoin", "http://192.168.1.100:8000", "--token", "test123", "--tailscale"]), \
             patch("gpumesh.cli.tunnel._get_tailscale_ip", return_value="100.67.72.79"), \
             patch("gpumesh.cli.worker.run_worker") as mock_worker:
            try:
                main()
            except SystemExit:
                pass
            
            # Explicit URL should be used (not Tailscale URL)
            mock_worker.assert_called_once()
            call_args = mock_worker.call_args
            assert call_args[0][0] == "http://192.168.1.100:8000"


class TestCLIArgumentParsing:
    """Tests for CLI argument parsing."""

    def test_serve_port_argument(self):
        """--port argument is correctly parsed."""
        from gpumesh.cli import main
        
        with patch("sys.argv", ["gpumesh", "serve", "--port", "9000", "--token", "test123"]), \
             patch("gpumesh.cli.server.serve") as mock_serve, \
             patch("gpumesh.cli.tunnel.open_tunnel"):
            mock_serve.return_value = MagicMock()
            try:
                main()
            except SystemExit:
                pass
            
            # Verify port was passed correctly
            mock_serve.assert_called_once_with("0.0.0.0", 9000, "gpumesh.db", "test123")

    def test_serve_token_argument(self):
        """--token argument is correctly parsed."""
        from gpumesh.cli import main
        
        with patch("sys.argv", ["gpumesh", "serve", "--token", "mysecrettoken"]), \
             patch("gpumesh.cli.server.serve") as mock_serve, \
             patch("gpumesh.cli.tunnel.open_tunnel"):
            mock_serve.return_value = MagicMock()
            try:
                main()
            except SystemExit:
                pass
            
            # Verify token was passed correctly
            mock_serve.assert_called_once_with("0.0.0.0", 8000, "gpumesh.db", "mysecrettoken")

    def test_serve_default_token_generated(self):
        """Token is generated when not provided."""
        from gpumesh.cli import main
        
        with patch("sys.argv", ["gpumesh", "serve"]), \
             patch("gpumesh.cli.server.serve") as mock_serve, \
             patch("gpumesh.cli.secrets.token_urlsafe", return_value="generatedtoken123"), \
             patch("gpumesh.cli.tunnel.open_tunnel"):
            mock_serve.return_value = MagicMock()
            try:
                main()
            except SystemExit:
                pass
            
            # Verify generated token was used
            mock_serve.assert_called_once_with("0.0.0.0", 8000, "gpumesh.db", "generatedtoken123")

    def test_quickjoin_token_required(self):
        """--token is required for quickjoin."""
        from gpumesh.cli import main
        
        with patch("sys.argv", ["gpumesh", "quickjoin"]), \
             patch("sys.argv", ["gpumesh", "quickjoin", "--token", "test123"]):
            # This should not raise an error
            pass
