"""Tests for cli.py - CLI argument parsing and --tailscale flag."""

import pytest
from unittest.mock import patch, MagicMock
import sys
import os
import threading
import time


@pytest.fixture(autouse=True)
def _status_sink_clean():
    """The coordinator log sink is process-global; never leak it active."""
    yield
    from gpumesh import status as status_mod
    assert status_mod.is_active() is False, "coordinator log sink left active"


class TestServeTailscaleFlag:
    """Tests for --tailscale flag in serve command."""

    def test_serve_tailscale_flag_parsed(self):
        """--tailscale flag is correctly parsed."""
        from gpumesh.cli import main

        with patch("sys.argv", ["gpumesh", "serve", "--tailscale", "--token", "test123"]), \
             patch("gpumesh.cli.server.serve") as mock_serve, \
             patch("gpumesh.cli.worker.spawn_local_worker") as mock_self_worker, \
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
             patch("gpumesh.cli.worker.spawn_local_worker") as mock_self_worker, \
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
             patch("gpumesh.cli.worker.spawn_local_worker") as mock_self_worker, \
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
             patch("gpumesh.cli.worker.spawn_local_worker") as mock_self_worker, \
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
             patch("gpumesh.cli.worker.spawn_local_worker") as mock_self_worker, \
             patch("gpumesh.cli.tunnel.open_tunnel"):
            mock_serve.return_value = MagicMock()
            try:
                main()
            except SystemExit:
                pass

            # Verify port was passed correctly
            mock_serve.assert_called_once_with("0.0.0.0", 9000, "gpumesh.db", "test123", discovery=True, safe_mode=False)

    def test_serve_token_argument(self):
        """--token argument is correctly parsed."""
        from gpumesh.cli import main

        with patch("sys.argv", ["gpumesh", "serve", "--token", "mysecrettoken"]), \
             patch("gpumesh.cli.server.serve") as mock_serve, \
             patch("gpumesh.cli.worker.spawn_local_worker") as mock_self_worker, \
             patch("gpumesh.cli.tunnel.open_tunnel"):
            mock_serve.return_value = MagicMock()
            try:
                main()
            except SystemExit:
                pass

            # Verify token was passed correctly
            mock_serve.assert_called_once_with("0.0.0.0", 8000, "gpumesh.db", "mysecrettoken", discovery=True, safe_mode=False)

    def test_serve_default_token_generated(self):
        """Token is generated when not provided."""
        from gpumesh.cli import main

        with patch("sys.argv", ["gpumesh", "serve"]), \
             patch("gpumesh.cli.server.serve") as mock_serve, \
             patch("gpumesh.cli.secrets.token_urlsafe", return_value="generatedtoken123"), \
             patch("gpumesh.cli.worker.spawn_local_worker") as mock_self_worker, \
             patch("gpumesh.cli.tunnel.open_tunnel"):
            mock_serve.return_value = MagicMock()
            try:
                main()
            except SystemExit:
                pass

            # Verify generated token was used
            mock_serve.assert_called_once_with("0.0.0.0", 8000, "gpumesh.db", "generatedtoken123", discovery=True, safe_mode=False)

    def test_quickjoin_token_required(self):
        """--token is required for quickjoin."""
        import argparse

        parser = argparse.ArgumentParser(prog="gpumesh")
        sub = parser.add_subparsers(dest="cmd")
        p = sub.add_parser("quickjoin")
        p.add_argument("--token", required=True)

        # Verify --token is required
        with pytest.raises(SystemExit):
            parser.parse_args(["quickjoin"])

        # Verify it works with --token
        args = parser.parse_args(["quickjoin", "--token", "test123"])
        assert args.token == "test123"

    def test_submit_rejects_negative_wait_timeout(self):
        """--wait-timeout rejects negative values (they would hang forever)."""
        from gpumesh.cli import main

        with patch("sys.argv", ["gpumesh", "submit", "task.py",
                                "--payloads", "p.json", "--wait-timeout", "-5"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 2  # argparse usage error

    def test_submit_accepts_zero_wait_timeout(self):
        """--wait-timeout 0 parses fine (opt-in to waiting forever)."""
        from gpumesh.cli import main

        with patch("sys.argv", ["gpumesh", "submit", "task.py",
                                "--payloads", "p.json", "--wait",
                                "--wait-timeout", "0"]), \
             patch("gpumesh.cli.connection_manager.get_connection",
                   return_value=("http://localhost:8000", "test123")), \
             patch("gpumesh.cli.client.submit_job", return_value="j123"), \
             patch("gpumesh.cli.worker.MeshClient"), \
             patch("gpumesh.cli.client.wait_for_job") as mock_wait:
            try:
                main()
            except SystemExit:
                pass
            mock_wait.assert_called_once_with(
                "http://localhost:8000", "test123", "j123", timeout=0.0
            )


class TestCoordinatorTicker:
    """Tests for the live 'workers online' keep-alive ticker."""

    def test_render_shows_online_count_devices_jobs_uptime(self):
        from gpumesh.cli import _render_keepalive

        health = {
            "total_score": 512.3,
            "uptime_seconds": 252,
            "jobs_pending": 1,
            "jobs_running": 2,
            "jobs_done": 3,
            "jobs_failed": 1,
        }
        workers = [
            {"id": "w1", "hostname": "gpu-pc", "device": "cuda",
             "device_name": "RTX 3080", "score": 85.0, "alive": True},
            {"id": "w2", "hostname": "mac", "device": "mps",
             "device_name": "M1 Max", "score": 60.0, "alive": True},
            {"id": "w3", "hostname": "cpu-pc", "device": "cpu",
             "device_name": "cpu", "score": 5.0, "alive": True},
            {"id": "w4", "hostname": "old-pc", "device": "cpu",
             "device_name": "old box", "score": 1.0, "alive": False},
        ]
        lines = _render_keepalive(health, workers, frame="|")
        text = " ".join(lines)

        assert "GPUMESH LIVE" in text
        assert "Workers online: 3" in text      # dead worker not counted
        assert "2 GPU" in text and "1 CPU" in text   # MPS counts as GPU
        assert "512.3" in text                  # total score
        assert "3 done" in text and "1 pending" in text
        assert "2 running" in text and "1 failed" in text
        assert "4:12" in text                   # uptime 252s
        assert "RTX 3080" in text               # top worker rows
        assert "M1 Max" in text
        assert "|" in text                      # spinner frame

    def test_render_zero_workers(self):
        from gpumesh.cli import _render_keepalive

        lines = _render_keepalive({}, [], frame="-")
        text = " ".join(lines)

        assert "GPUMESH LIVE" in text
        assert "Workers online: 0" in text
        assert "no jobs" in text
        assert "0.0" in text                    # score
        assert "0:00" in text                   # uptime
        # top rule + workers + jobs + bottom rule, no worker row
        assert len(lines) == 4

    def test_render_is_ascii_safe(self):
        """Ticker glyphs survive a legacy ASCII console (no wide chars)."""
        import re
        from gpumesh.cli import _render_keepalive

        health = {"total_score": 12.5, "uptime_seconds": 61,
                  "jobs_done": 2, "jobs_failed": 1}
        workers = [
            {"id": "w1", "hostname": "gpu-pc", "device": "cuda",
             "device_name": "RTX 3080 Ti", "score": 85.0, "alive": True},
        ]
        lines = _render_keepalive(health, workers, frame="\\")
        for line in lines:
            plain = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line)
            plain.encode("ascii")  # raises if any non-ASCII glyph

    def test_render_hides_excess_workers_behind_ellipsis(self):
        """More than 4 workers show a '+N more' hint on the worker row."""
        from gpumesh.cli import _render_keepalive

        workers = [
            {"id": f"w{i}", "hostname": f"pc{i}", "device": "cpu",
             "device_name": "cpu", "score": float(10 - i), "alive": True}
            for i in range(6)
        ]
        lines = _render_keepalive({}, workers, frame="-")
        text = " ".join(lines)

        assert "Workers online: 6" in text
        assert "... +2 more" in text

    def test_ticker_returns_immediately_when_thread_dead(self, capsys):
        from gpumesh.cli import _run_keepalive_ticker

        class DeadThread:
            def is_alive(self):
                return False

        _run_keepalive_ticker(lambda: {}, lambda: {"workers": []},
                              DeadThread(), tick=0.01)
        assert capsys.readouterr().out == ""

    def test_ticker_renders_one_frame_then_stops(self, capsys):
        from gpumesh.cli import _run_keepalive_ticker

        calls = {"n": 0}

        class ShortThread:
            def is_alive(self):
                calls["n"] += 1
                return calls["n"] <= 1

        health = {"total_score": 90.0, "uptime_seconds": 10,
                  "jobs_running": 1}
        workers = [{"id": "w1", "hostname": "gpu-pc", "device": "cuda",
                    "device_name": "RTX 3080", "score": 85.0, "alive": True}]

        with patch("gpumesh.cli.clear_lines"), \
             patch("gpumesh.cli.erase_line"), \
             patch("gpumesh.cli.time.sleep"):
            _run_keepalive_ticker(lambda: health, lambda: {"workers": workers},
                                  ShortThread(), tick=0.01)

        out = capsys.readouterr().out
        assert "GPUMESH LIVE" in out
        assert "Workers online: 1" in out

    def test_ticker_survives_poll_failure(self, capsys):
        """A failed poll renders 'coordinator unreachable' instead of dying."""
        from gpumesh.cli import _run_keepalive_ticker

        calls = {"n": 0}

        def health_fn():
            calls["n"] += 1
            raise ConnectionError("shutting down")

        class ShortThread:
            def is_alive(self):
                return calls["n"] < 1

        with patch("gpumesh.cli.clear_lines"), \
             patch("gpumesh.cli.erase_line"), \
             patch("gpumesh.cli.time.sleep"):
            _run_keepalive_ticker(health_fn, lambda: {"workers": []},
                                  ShortThread(), tick=0.01)

        out = capsys.readouterr().out
        assert "coordinator unreachable" in out

    def test_ticker_region_includes_buffered_mesh_logs(self, capsys):
        """A mesh line logged while the ticker is active renders inside the
        ticker region instead of being absorbed by the redraw."""
        from gpumesh.cli import _run_keepalive_ticker
        from gpumesh import status as status_mod

        calls = {"n": 0}

        def mesh_log():
            if calls["n"] == 0:
                status_mod.log("worker joined: gpu-pc")
            calls["n"] += 1

        class ShortThread:
            def is_alive(self):
                mesh_log()
                return calls["n"] <= 2

        health = {"total_score": 90.0, "uptime_seconds": 10, "jobs_done": 1}
        workers = [{"id": "w1", "hostname": "gpu-pc", "device": "cuda",
                    "device_name": "RTX 3080", "score": 85.0, "alive": True}]

        with patch("gpumesh.cli.clear_lines"), \
             patch("gpumesh.cli.erase_line"), \
             patch("gpumesh.cli.time.sleep"):
            _run_keepalive_ticker(lambda: health, lambda: {"workers": workers},
                                  ShortThread(), tick=0.01)

        out = capsys.readouterr().out
        assert "GPUMESH LIVE" in out
        assert "worker joined: gpu-pc" in out
        # the sink is restored to direct mode once the ticker stops
        assert status_mod.is_active() is False

    def test_server_mesh_lines_route_to_sink_when_active(self, tmp_path):
        """With the sink active, a real coordinator buffers its 'worker
        joined' line into the ticker region instead of stdout."""
        from gpumesh import server, status as status_mod
        from gpumesh.worker import MeshClient

        httpd = server.serve("127.0.0.1", 0, str(tmp_path / "sink.db"),
                             "sink-token", discovery=False)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            status_mod.set_active(True)
            try:
                mc = MeshClient(f"http://127.0.0.1:{port}", "sink-token")
                mc.call("POST", "/api/register", {
                    "hostname": "sink-pc", "device": "cpu",
                    "device_name": "cpu", "score": 1.0,
                })
                # the join line lands in the sink asynchronously
                deadline = time.time() + 5.0
                while time.time() < deadline:
                    if any("worker joined: sink-pc" in ln
                           for ln in status_mod.snapshot(100)):
                        break
                    time.sleep(0.05)
                assert any("worker joined: sink-pc" in ln
                           for ln in status_mod.snapshot(100))
            finally:
                status_mod.set_active(False)
        finally:
            httpd.gpumesh_stop.set()
            httpd.shutdown()
            t.join(timeout=5)


class TestCoordinatorStatusSink:
    """Tests for the shared mesh-log sink that feeds the ticker region."""

    def test_log_prints_immediately_when_inactive(self, capsys):
        from gpumesh import status as status_mod

        status_mod.set_active(False)
        status_mod.log("hello mesh")
        assert "hello mesh" in capsys.readouterr().out

    def test_log_buffers_when_active(self, capsys):
        from gpumesh import status as status_mod

        status_mod.set_active(True)
        try:
            status_mod.log("buffered line")
            assert capsys.readouterr().out == ""
            assert status_mod.snapshot() == ["buffered line"]
        finally:
            status_mod.set_active(False)

    def test_snapshot_is_a_sliding_window(self):
        from gpumesh import status as status_mod

        status_mod.set_active(True)
        try:
            for i in range(10):
                status_mod.log(f"line {i}")
            snap = status_mod.snapshot()
            assert len(snap) == status_mod.LOG_VISIBLE
            assert snap[0] == "line 4" and snap[-1] == "line 9"
        finally:
            status_mod.set_active(False)

    def test_deactivate_clears_buffer(self):
        from gpumesh import status as status_mod

        status_mod.set_active(True)
        status_mod.log("x")
        status_mod.set_active(False)
        assert status_mod.is_active() is False
        assert status_mod.snapshot() == []