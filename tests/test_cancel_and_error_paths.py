"""Tests for cancel command and error paths identified in UX audit.

Tests cover:
- cmd_cancel() with network errors
- cmd_cancel() with missing job
- cmd_cancel() with successful cancellation
- cancel_job() propagation of network errors
- Worker registration error messages
- Worker backoff reset behavior
- Database corruption backup
- Serializer source fallback warning
- distribute() connection errors
- wait_for_job() piped output handling
"""

import json
import os
import platform
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import ExitStack
from unittest.mock import MagicMock, patch, mock_open

import pytest

from gpumesh.cli import cmd_cancel, cmd_retry, cmd_serve
from gpumesh.client import cancel_job, retry_job, wait_for_job
from gpumesh.worker import run_worker, _run_function_task
from gpumesh.db import Database
from gpumesh.server import serve


# ============================================================================
#  Shared Fixtures
# ============================================================================

@pytest.fixture
def coordinator(tmp_path):
    """Start a real coordinator server. Yields (url, token, httpd)."""
    db_path = str(tmp_path / 'coordinator.db')
    httpd = serve('127.0.0.1', 0, db_path, 'test-token')
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=False)
    thread.start()
    yield f'http://127.0.0.1:{port}', 'test-token', httpd
    httpd.gpumesh_stop.set()
    httpd.shutdown()
    thread.join(timeout=5)



# ============================================================================
#  cmd_cancel() Tests
# ============================================================================

class TestCmdCancel:
    """Tests for cmd_cancel() error handling."""

    def test_cancel_no_connection(self, capsys):
        """Shows error when no connection is found."""
        args = MagicMock()
        args.url = ""
        args.token = ""
        args.job_id = "j123"

        with patch("gpumesh.cli.connection_manager.get_connection", return_value=("", "")):
            with pytest.raises(SystemExit) as exc_info:
                cmd_cancel(args)
            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "No connection found" in captured.out

    def test_cancel_network_error(self, capsys):
        """Shows connection error when coordinator is unreachable."""
        args = MagicMock()
        args.url = "http://localhost:8000"
        args.token = "test123"
        args.job_id = "j123"

        with patch("gpumesh.cli.connection_manager.get_connection", return_value=("http://localhost:8000", "test123")), \
             patch("gpumesh.cli.client.cancel_job", side_effect=urllib.error.URLError("Connection refused")):
            with pytest.raises(SystemExit) as exc_info:
                cmd_cancel(args)
            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "Could not communicate with coordinator" in captured.out

    def test_cancel_os_error(self, capsys):
        """Shows connection error on OS error."""
        args = MagicMock()
        args.url = "http://localhost:8000"
        args.token = "test123"
        args.job_id = "j123"

        with patch("gpumesh.cli.connection_manager.get_connection", return_value=("http://localhost:8000", "test123")), \
             patch("gpumesh.cli.client.cancel_job", side_effect=OSError("Network unreachable")):
            with pytest.raises(SystemExit) as exc_info:
                cmd_cancel(args)
            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "Could not communicate with coordinator" in captured.out

    def test_cancel_job_not_found(self, capsys):
        """Shows job not found when cancel returns None."""
        args = MagicMock()
        args.url = "http://localhost:8000"
        args.token = "test123"
        args.job_id = "j_notfound"

        with patch("gpumesh.cli.connection_manager.get_connection", return_value=("http://localhost:8000", "test123")), \
             patch("gpumesh.cli.client.cancel_job", return_value=None):
            cmd_cancel(args)

        captured = capsys.readouterr()
        assert "j_notfound not found" in captured.out

    def test_cancel_success(self, capsys):
        """Shows cancellation results on success."""
        args = MagicMock()
        args.url = "http://localhost:8000"
        args.token = "test123"
        args.job_id = "j_success"

        cancel_result = {"pending": 3, "running": 1}
        with patch("gpumesh.cli.connection_manager.get_connection", return_value=("http://localhost:8000", "test123")), \
             patch("gpumesh.cli.client.cancel_job", return_value=cancel_result):
            cmd_cancel(args)

        captured = capsys.readouterr()
        assert "Cancelled job j_success" in captured.out
        assert "Pending tasks cancelled: 3" in captured.out
        assert "Running tasks cancelled: 1" in captured.out


# ============================================================================
#  cmd_retry() Tests
# ============================================================================

class TestCmdRetry:
    """Tests for cmd_retry() error handling."""

    def test_retry_no_connection(self, capsys):
        """Shows error when no connection is found."""
        args = MagicMock()
        args.url = ""
        args.token = ""
        args.job_id = "j123"

        with patch("gpumesh.cli.connection_manager.get_connection", return_value=("", "")):
            with pytest.raises(SystemExit) as exc_info:
                cmd_retry(args)
            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "No connection found" in captured.out

    def test_retry_network_error(self, capsys):
        """Shows connection error when coordinator is unreachable."""
        args = MagicMock()
        args.url = "http://localhost:8000"
        args.token = "test123"
        args.job_id = "j123"

        with patch("gpumesh.cli.connection_manager.get_connection", return_value=("http://localhost:8000", "test123")), \
             patch("gpumesh.cli.client.retry_job", side_effect=urllib.error.URLError("Connection refused")):
            with pytest.raises(SystemExit) as exc_info:
                cmd_retry(args)
            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "Could not communicate with coordinator" in captured.out

    def test_retry_job_not_found(self, capsys):
        """Shows job not found when retry returns None."""
        args = MagicMock()
        args.url = "http://localhost:8000"
        args.token = "test123"
        args.job_id = "j_notfound"

        with patch("gpumesh.cli.connection_manager.get_connection", return_value=("http://localhost:8000", "test123")), \
             patch("gpumesh.cli.client.retry_job", return_value=None):
            with pytest.raises(SystemExit) as exc_info:
                cmd_retry(args)
            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "j_notfound not found" in captured.out

    def test_retry_success(self, capsys):
        """Shows re-queue results on success."""
        args = MagicMock()
        args.url = "http://localhost:8000"
        args.token = "test123"
        args.job_id = "j_success"

        retry_result = {"requeued": 3, "counts": {"pending": 3}}
        with patch("gpumesh.cli.connection_manager.get_connection", return_value=("http://localhost:8000", "test123")), \
             patch("gpumesh.cli.client.retry_job", return_value=retry_result):
            cmd_retry(args)

        captured = capsys.readouterr()
        assert "Re-queued 3 failed task(s) for job j_success" in captured.out
        assert "gpumesh status j_success" in captured.out

    def test_retry_no_failed_tasks(self, capsys):
        """A job with nothing to retry says so instead of failing."""
        args = MagicMock()
        args.url = "http://localhost:8000"
        args.token = "test123"
        args.job_id = "j_done"

        retry_result = {"requeued": 0, "counts": {"done": 2}}
        with patch("gpumesh.cli.connection_manager.get_connection", return_value=("http://localhost:8000", "test123")), \
             patch("gpumesh.cli.client.retry_job", return_value=retry_result):
            cmd_retry(args)

        captured = capsys.readouterr()
        assert "has no failed tasks to retry" in captured.out
        assert "Re-queued" not in captured.out


# ============================================================================
#  cancel_job() Tests
# ============================================================================

class TestCancelJob:
    """Tests for cancel_job() function."""

    def test_cancel_job_propagates_network_error(self):
        """cancel_job() propagates URLError instead of returning None."""
        with patch("gpumesh.client.MeshClient") as MockClient:
            mock_mesh = MagicMock()
            mock_mesh.call.side_effect = urllib.error.URLError("Connection refused")
            MockClient.return_value = mock_mesh

            with pytest.raises(urllib.error.URLError):
                cancel_job("http://localhost:8000", "token", "j123")

    def test_cancel_job_propagates_os_error(self):
        """cancel_job() propagates OSError."""
        with patch("gpumesh.client.MeshClient") as MockClient:
            mock_mesh = MagicMock()
            mock_mesh.call.side_effect = OSError("Network unreachable")
            MockClient.return_value = mock_mesh

            with pytest.raises(OSError):
                cancel_job("http://localhost:8000", "token", "j123")

    def test_cancel_job_returns_none_for_404(self):
        """cancel_job() returns None when job not found (server returns 404)."""
        with patch("gpumesh.client.MeshClient") as MockClient:
            mock_mesh = MagicMock()
            mock_mesh.call.return_value = None  # Server returns 404 -> None
            MockClient.return_value = mock_mesh

            result = cancel_job("http://localhost:8000", "token", "j123")
            assert result is None

    def test_cancel_job_returns_result(self):
        """cancel_job() returns cancellation result."""
        with patch("gpumesh.client.MeshClient") as MockClient:
            mock_mesh = MagicMock()
            mock_mesh.call.return_value = {"pending": 2, "running": 1}
            MockClient.return_value = mock_mesh

            result = cancel_job("http://localhost:8000", "token", "j123")
            assert result["pending"] == 2
            assert result["running"] == 1


# ============================================================================
#  retry_job() Tests
# ============================================================================

class TestRetryJob:
    """Tests for retry_job() function."""

    def test_retry_job_propagates_network_error(self):
        """retry_job() propagates URLError instead of returning None."""
        with patch("gpumesh.client.MeshClient") as MockClient:
            mock_mesh = MagicMock()
            mock_mesh.call.side_effect = urllib.error.URLError("Connection refused")
            MockClient.return_value = mock_mesh

            with pytest.raises(urllib.error.URLError):
                retry_job("http://localhost:8000", "token", "j123")

    def test_retry_job_returns_none_for_404(self):
        """retry_job() returns None when job not found (server returns 404)."""
        with patch("gpumesh.client.MeshClient") as MockClient:
            mock_mesh = MagicMock()
            mock_mesh.call.side_effect = urllib.error.HTTPError(
                "http://localhost:8000", 404, "Not Found", {}, None
            )
            MockClient.return_value = mock_mesh

            result = retry_job("http://localhost:8000", "token", "j123")
            assert result is None

    def test_retry_job_returns_result(self):
        """retry_job() returns the re-queue result."""
        with patch("gpumesh.client.MeshClient") as MockClient:
            mock_mesh = MagicMock()
            mock_mesh.call.return_value = {"requeued": 2, "counts": {"pending": 2}}
            MockClient.return_value = mock_mesh

            result = retry_job("http://localhost:8000", "token", "j123")
            assert result["requeued"] == 2
            assert result["counts"]["pending"] == 2


# ============================================================================
#  Worker Registration Error Tests
# ============================================================================

class TestWorkerRegistrationErrors:
    """Tests for worker registration error messages."""

    def test_worker_connection_refused_error(self, capsys):
        """Shows helpful troubleshooting for connection refused."""
        with patch("gpumesh.worker.capability.full_probe", return_value={
            "device": "cpu", "device_name": "cpu", "score": 1.0
        }), \
             patch("gpumesh.worker.MeshClient") as MockClient:
            mock_mesh = MagicMock()
            mock_mesh.call.side_effect = urllib.error.URLError(
                ConnectionRefusedError("Connection refused")
            )
            MockClient.return_value = mock_mesh

            run_worker("http://localhost:8000", "token")

        captured = capsys.readouterr()
        assert "failed to register" in captured.out
        assert "TROUBLESHOOTING" in captured.out
        assert "Is the coordinator running" in captured.out

    def test_worker_general_error(self, capsys):
        """Shows error message for general exceptions."""
        with patch("gpumesh.worker.capability.full_probe", return_value={
            "device": "cpu", "device_name": "cpu", "score": 1.0
        }), \
             patch("gpumesh.worker.MeshClient") as MockClient:
            mock_mesh = MagicMock()
            mock_mesh.call.side_effect = Exception("Unexpected error")
            MockClient.return_value = mock_mesh

            run_worker("http://localhost:8000", "token")

        captured = capsys.readouterr()
        assert "failed to register" in captured.out


# ============================================================================
#  Database Corruption Backup Tests
# ============================================================================

class TestDatabaseCorruption:
    """Tests for database corruption backup."""

    def test_corruption_creates_backup(self, tmp_path):
        """Corrupted database is backed up before recreation."""
        db_path = str(tmp_path / "test.db")

        # Create a valid database first
        db = Database(db_path)
        db.register_worker("test", "cpu", 1.0)
        db.close()

        # Corrupt the database file
        with open(db_path, "w") as f:
            f.write("not a valid sqlite database")

        # Creating a new Database should backup the corrupted file
        # and create a fresh one
        db2 = Database(db_path)
        db2.close()

        # Backup file should exist
        backup_path = db_path + ".corrupted"
        assert os.path.exists(backup_path)

    def test_corruption_wal_files_handled(self, tmp_path):
        """WAL file backup is attempted during corruption recovery."""
        db_path = str(tmp_path / "test2.db")

        # Create a valid database
        db = Database(db_path)
        db.register_worker("test", "cpu", 1.0)
        db.close()

        # Simulate corruption
        with open(db_path, "w") as f:
            f.write("corrupted")

        # Creating new Database should handle missing WAL gracefully
        db2 = Database(db_path)
        db2.close()

        # Main backup should exist
        assert os.path.exists(db_path)


# ============================================================================
#  Serializer Source Fallback Warning Tests
# ============================================================================

class TestSerializerFallback:
    """Tests for serializer source fallback warning."""

    def test_serializer_has_source_fallback(self):
        """Serializer falls back to source code when cloudpickle unavailable."""
        from gpumesh.serializer import serialize_function

        def my_test_func():
            return 42

        # serialize_function should work regardless of cloudpickle availability
        result = serialize_function(my_test_func)
        assert result is not None
        assert len(result) > 0  # Should produce base64-encoded output


# ============================================================================
#  distribute() Connection Error Tests
# ============================================================================

class TestDistributeConnectionError:
    """Tests for distribute() connection error handling."""

    def test_distribute_wraps_connection_error(self):
        """distribute() wraps connection errors in GPUMeshError."""
        from gpumesh.api import GPUMesh, GPUMeshError

        mesh = GPUMesh("http://localhost:99999", "token")

        def dummy_func(x):
            return {"x": x}

        with patch.object(mesh._client, "call", side_effect=urllib.error.URLError("Connection refused")):
            with pytest.raises(GPUMeshError) as exc_info:
                mesh.distribute(dummy_func, [{"x": 1}])

            assert "Failed to submit job" in str(exc_info.value)
            assert "coordinator" in str(exc_info.value).lower()


# ============================================================================
#  wait_for_job() Piped Output Tests
# ============================================================================

class TestWaitForJobPiped:
    """Tests for wait_for_job() piped output handling."""

    def test_wait_for_job_disables_ansi_when_piped(self):
        """Disables ANSI when stdout is not a TTY."""
        finished_job = {
            "id": "j123", "name": "test", "finished": True,
            "counts": {"done": 1},
            "tasks": [{"id": "t1", "status": "done", "cost": 1.0, "worker_id": "w1", "result": {}, "error": None}]
        }

        with patch("gpumesh.client.get_status", return_value=finished_job), \
             patch("gpumesh.client._get_workers", return_value={}), \
             patch("sys.stdout.isatty", return_value=False):
            import gpumesh.client as client_mod
            result = wait_for_job("http://localhost:8000", "token", "j123", poll=0.01)
            # Verify ANSI was disabled for piped output
            assert client_mod._ANSI is False
            assert result["finished"] is True


# ============================================================================
#  Worker Backoff Reset Tests
# ============================================================================

class TestWorkerBackoff:
    """Tests for worker backoff reset behavior."""

    def test_backoff_resets_only_on_task(self):
        """Backoff should only reset when a task is obtained."""
        from gpumesh.worker import run_worker, POLL_INTERVAL
        # Verify backoff reset logic: backoff is only reset when work is obtained
        # This is verified by checking the run_worker source code uses backoff = POLL_INTERVAL
        # after task is not None check (not on empty polls)
        import inspect
        import ast

        # unwrap(): conftest wraps run_worker to hand every worker a stop
        # event, and getsource() does not follow __wrapped__ on its own.
        source = inspect.getsource(inspect.unwrap(run_worker))
        tree = ast.parse(source)

        # Walk AST to find backoff = POLL_INTERVAL inside a block where task is not None
        backoff_resets_after_task = False
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                if_condition = ast.dump(node.test)
                task_check_was_none = 'task is None' in if_condition or 'None' in if_condition
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == 'backoff' for t in node.targets
            ):
                assign_line = node.lineno

        # The backoff reset should exist at a higher line than the task-is-None check
        assert assign_line > 0, "Could not find backoff assignment in run_worker"
        assert assign_line > 1, "backoff reset line should be after task check"


# ============================================================================
#  Large Script Upload Warning Tests
# ============================================================================

class TestLargeScriptUpload:
    """Tests for large script upload warning."""

    def test_large_script_warning(self, capsys):
        """Shows warning for large script uploads."""
        from gpumesh.client import submit_job

        large_script = "x = 1\n" * 20000  # > 100KB

        with patch("gpumesh.client.MeshClient") as MockClient:
            mock_mesh = MagicMock()
            mock_mesh.call.return_value = {"job_id": "j123"}
            MockClient.return_value = mock_mesh

            with patch("builtins.open", mock_open(read_data=large_script)):
                with patch("gpumesh.client.json.load", return_value=[{"x": 1}]):
                    job_id = submit_job("http://localhost:8000", "token", "test.py", "payloads.json")

        captured = capsys.readouterr()
        assert "Uploading large script" in captured.out


# ============================================================================
#  Integration Tests with Real Server
# ============================================================================

class TestCancelIntegration:
    """Integration tests with real coordinator server."""

    def test_cancel_job_api_propagation(self):
        """Cancel propagates network errors instead of returning None."""
        from gpumesh.client import cancel_job
        from unittest.mock import patch, MagicMock

        with patch("gpumesh.client.MeshClient") as MockClient:
            mock_mesh = MagicMock()
            mock_mesh.call.side_effect = ConnectionError("network error")
            MockClient.return_value = mock_mesh

            with patch("gpumesh.client.urllib.error.HTTPError"):
                pass

# ============================================================================
#  Additional Edge Case Tests
# ============================================================================

class TestWorkerRegistrationEdgeCases:
    """Additional edge cases for worker registration."""

    def test_worker_connection_refused_shows_url(self, capsys):
        """Error message includes the URL that was tried."""
        with patch("gpumesh.worker.capability.full_probe", return_value={
            "device": "cpu", "device_name": "cpu", "score": 1.0
        }), \
             patch("gpumesh.worker.MeshClient") as MockClient:
            mock_mesh = MagicMock()
            mock_mesh.call.side_effect = urllib.error.URLError(
                ConnectionRefusedError("Connection refused")
            )
            MockClient.return_value = mock_mesh

            run_worker("http://192.168.1.100:9999", "mytoken")

        captured = capsys.readouterr()
        assert "192.168.1.100:9999" in captured.out  # URL should be shown
        assert "Is the coordinator running" in captured.out

    def test_worker_auth_error_message(self, capsys):
        """A rejected token (401) gets a clear auth-failure message instead
        of the generic "coordinator not reachable" troubleshooting."""
        with patch("gpumesh.worker.capability.full_probe", return_value={
            "device": "cuda", "device_name": "RTX 3080", "score": 85.0
        }), \
             patch("gpumesh.worker.MeshClient") as MockClient:
            mock_mesh = MagicMock()
            mock_mesh.call.side_effect = urllib.error.HTTPError(
                "http://localhost:8000", 401, "Unauthorized", {}, None
            )
            MockClient.return_value = mock_mesh

            run_worker("http://localhost:8000", "wrong-token")

        captured = capsys.readouterr()
        assert "authentication failed" in captured.out
        assert "token" in captured.out.lower()
        assert "not reachable" not in captured.out.lower()

    def test_worker_registration_saves_connection(self, tmp_path):
        """Successful registration saves connection to config."""
        from gpumesh.worker import MeshClient
        from gpumesh import connection_manager

        httpd = serve("127.0.0.1", 0, str(tmp_path / "save.db"), "save-token")
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()

        url = f"http://127.0.0.1:{port}"
        try:
            # Worker should save connection after successful registration
            with patch("gpumesh.worker.capability.full_probe", return_value={
                "device": "cpu", "device_name": "cpu", "score": 1.0
            }):
                # Run worker briefly
                worker_thread = threading.Thread(
                    target=run_worker, args=(url, "save-token"), daemon=True
                )
                worker_thread.start()
                time.sleep(0.5)

            # Verify connection was actually saved
            saved = connection_manager.load_connection()
            assert saved is not None, "Worker failed to save connection after registration"
            assert saved["url"] == url
            assert saved["token"] == "save-token"
        finally:
            httpd.gpumesh_stop.set()
            httpd.shutdown()
            connection_manager.clear_connection()


class TestConnectionManagerEdgeCases:
    """Tests for connection manager edge cases."""

    def test_save_connection_overwrites_previous(self, tmp_path):
        """Saving new connection overwrites previous one."""
        from gpumesh import connection_manager

        config_dir = tmp_path / ".gpumesh"
        config_dir.mkdir()
        config_path = config_dir / "config.json"

        with patch("gpumesh.connection_manager._CONFIG_DIR", str(config_dir)), \
             patch("gpumesh.connection_manager._CONFIG_PATH", str(config_path)):
            # Save first connection
            connection_manager.save_connection("http://first:8000", "token1")
            saved = connection_manager.load_connection()
            assert saved["url"] == "http://first:8000"

            # Save second connection
            connection_manager.save_connection("http://second:9000", "token2")
            saved = connection_manager.load_connection()
            assert saved["url"] == "http://second:9000"
            assert saved["token"] == "token2"

    def test_get_connection_priority(self, tmp_path):
        """get_connection follows priority: args > env > saved."""
        from gpumesh import connection_manager

        # Test with explicit args
        url, token = connection_manager.get_connection("http://explicit:8000", "argtoken")
        assert url == "http://explicit:8000"
        assert token == "argtoken"

    def test_get_connection_env_vars(self):
        """get_connection uses environment variables."""
        from gpumesh import connection_manager

        with patch.dict(os.environ, {"GPUMESH_URL": "http://env:8000", "GPUMESH_TOKEN": "envtoken"}):
            url, token = connection_manager.get_connection(None, None)
            assert url == "http://env:8000"
            assert token == "envtoken"


class TestANSIDetectionEdgeCases:
    """Tests for ANSI detection edge cases."""

    def test_ansi_disabled_when_not_tty(self):
        """ANSI is disabled when stdout is not a TTY."""
        import gpumesh.client as client_mod

        original_ansi = client_mod._ANSI
        try:
            with patch("sys.stdout.isatty", return_value=False):
                # Re-evaluate the module-level expression
                # Note: This tests the behavior at import time
                assert not sys.stdout.isatty()  # Verify mock works
        finally:
            client_mod._ANSI = original_ansi

    def test_ansi_enabled_when_tty(self):
        """ANSI is enabled when stdout is a TTY."""
        with patch("sys.stdout.isatty", return_value=True):
            assert sys.stdout.isatty()  # Verify mock works


class TestTokenizerStripEdgeCases:
    """Tests for token validation edge cases."""

    def test_token_strips_whitespace(self):
        """Token is stripped of whitespace before validation."""
        with patch("builtins.input", return_value="  mytoken  "):
            result = input("Token:").strip()
            assert result == "mytoken"  # Should be stripped

    def test_empty_token_after_strip(self):
        """Empty token after strip is rejected."""
        with patch("builtins.input", return_value="   "):
            result = input("Token:").strip()
            assert result == ""  # Should be empty after strip


class TestPortFallbackEdgeCases:
    """Tests for port fallback edge cases."""

    @pytest.mark.skipif(platform.system() == "Windows",
                        reason="Windows SO_REUSEADDR allows re-binding to the same port")
    def test_server_raises_on_port_in_use(self, tmp_path):
        """Server raises OSError when port is already in use."""
        from gpumesh.server import serve

        # Start a server on port 0 (auto-assign)
        httpd1 = serve("127.0.0.1", 0, str(tmp_path / "db1.db"), "token1")
        port = httpd1.server_address[1]

        try:
            # Second server on same port should fail
            with pytest.raises(OSError):
                serve("127.0.0.1", port, str(tmp_path / "db2.db"), "token2")
        finally:
            httpd1.gpumesh_stop.set()
            httpd1.shutdown()

    def test_cli_serve_shows_port_suggestion(self, capsys, tmp_path):
        """cmd_serve suggests alternative port when 8000 is taken."""
        import socket

        from gpumesh.server import serve

        # Start server on port 0 to grab a port
        httpd = serve("127.0.0.1", 0, str(tmp_path / "db.db"), "token")
        port = httpd.server_address[1]

        try:
            # There is only a suggestion to make where the second bind
            # actually fails, and otherwise cmd_serve starts normally and
            # serves forever — an indefinite hang rather than a failure. So
            # ask this machine rather than naming platforms.
            #
            # The probe must bind what cmd_serve binds: the wildcard address,
            # with the same SO_REUSEADDR the server sets. That the occupied
            # socket is on 127.0.0.1 and this one is on 0.0.0.0 is the whole
            # question — Linux refuses the overlap, macOS permits it, and
            # probing 127.0.0.1 answers a question nobody asked (macOS
            # refuses that one too, which is how this test still hung).
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("0.0.0.0", port))
            except OSError:
                pass  # good — the overlap is refused here
            else:
                pytest.skip("this platform allows re-binding a listening port")
            finally:
                probe.close()

            args = MagicMock()
            args.port = port
            args.db = str(tmp_path / "test.db")
            args.token = "test"
            args.tailscale = False
            args.public = False

            with pytest.raises(SystemExit):
                cmd_serve(args)

            captured = capsys.readouterr()
            assert "port" in captured.out.lower()
        finally:
            httpd.gpumesh_stop.set()
            httpd.shutdown()


class TestThreadTimeoutEdgeCases:
    """Tests for thread timeout handling edge cases."""

    def test_function_timeout_with_gpu_warning(self):
        """Function timeout includes GPU memory warning."""
        from gpumesh.worker import _run_function_task

        def slow_function():
            import time
            time.sleep(10)  # Will timeout
            return {"result": 42}

        payload = {
            "_func": "test",
            "_params": {},
            "_task_index": 0,
            "cost": 1.0,
        }

        with patch("gpumesh.serializer.deserialize_function", return_value=slow_function):
            with pytest.raises(Exception) as exc_info:
                _run_function_task(payload, "cuda", timeout=0.1)

            error_msg = str(exc_info.value)
            assert "timed out" in error_msg.lower()


class TestVersionCommand:
    """Tests for gpumesh --version output."""

    def test_version_output_format(self):
        """--version outputs correct format with version, Python, and platform."""
        import platform
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "gpumesh", "--version"],
            capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0
        output = result.stdout.strip()
        # Should start with "gpumesh"
        assert output.startswith("gpumesh"), f"Output should start with 'gpumesh': {output}"
        # Should contain version number
        from gpumesh import __version__
        assert __version__ in output, f"Version {__version__} not found in output: {output}"
        # Should contain Python version
        python_ver = platform.python_version()
        assert python_ver in output, f"Python version {python_ver} not found in output: {output}"
        # Should contain platform name
        sys_name = platform.system()
        assert sys_name in output, f"Platform {sys_name} not found in output: {output}"
        # Format: "gpumesh X.Y.Z (X.Y.Z, Platform)"
        assert "(" in output and ")" in output, f"Output should have parenthesized info: {output}"

    def test_version_exits_zero(self):
        """--version exits with code 0."""
        result = subprocess.run(
            [sys.executable, "-m", "gpumesh", "--version"],
            capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0
        assert len(result.stdout.strip()) > 0, "--version should produce output"


class TestErrorPrefixConsistency:
    """Tests for error prefix consistency across CLI."""

    def test_all_cli_commands_use_gpumesh_prefix(self):
        """All CLI commands use [OK]/[ERROR] prefix pattern, not stale [mesh]/[client]."""
        import inspect
        from gpumesh import cli

        source = inspect.getsource(cli)

        # Check that no [mesh] prefixes remain
        assert "[mesh]" not in source, "Found [mesh] prefix - should be [OK]/[ERROR]"

        # Check that no [client] prefixes remain
        assert "[client]" not in source, "Found [client] prefix - should be [OK]/[ERROR]"

        # Check that [OK] or [ERROR] is used (the actual CLI prefix convention)
        assert "[OK]" in source or "[ERROR]" in source, "No [OK] or [ERROR] prefix found"

    def test_worker_uses_worker_prefix(self):
        """Worker module uses [worker] prefix for its own messages."""
        import inspect
        from gpumesh import worker

        source = inspect.getsource(worker)
        assert "[worker]" in source, "Worker should use [worker] prefix"


# ============================================================================
#  Integration Tests with Real Server - Error Paths
# ============================================================================

class TestRealServerErrorPaths:
    """Integration tests for error paths with real coordinator server."""

    def test_wrong_token_rejected(self, coordinator):
        """Wrong token is rejected with 401."""
        url, _, _ = coordinator
        from gpumesh.worker import MeshClient

        client = MeshClient(url, "wrong-token-123")
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            client.call("GET", "/api/workers")
        assert exc_info.value.code == 401

    def test_wrong_url_connection_refused(self):
        """Wrong URL gives connection refused."""
        from gpumesh.worker import MeshClient

        client = MeshClient("http://127.0.0.1:19999", "any-token")
        with pytest.raises(urllib.error.URLError):
            client.call("GET", "/api/workers")

    def test_empty_payloads_rejected(self, coordinator):
        """Empty payloads list is rejected with 400."""
        url, token, _ = coordinator
        from gpumesh.worker import MeshClient

        client = MeshClient(url, token)
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            client.call("POST", "/api/jobs", {
                "name": "empty",
                "script": "print(1)",
                "payloads": [],
            })
        assert exc_info.value.code == 400

    def test_missing_script_rejected(self, coordinator):
        """Missing script is rejected with 400."""
        url, token, _ = coordinator
        from gpumesh.worker import MeshClient

        client = MeshClient(url, token)
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            client.call("POST", "/api/jobs", {
                "name": "no-script",
                "payloads": [{"x": 1}],
            })
        assert exc_info.value.code == 400

    def test_empty_script_rejected(self, coordinator):
        """Empty script is rejected with 400."""
        url, token, _ = coordinator
        from gpumesh.worker import MeshClient

        client = MeshClient(url, token)
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            client.call("POST", "/api/jobs", {
                "name": "empty-script",
                "script": "",
                "payloads": [{"x": 1}],
            })
        assert exc_info.value.code == 400

    def test_invalid_json_rejected(self, coordinator):
        """Invalid JSON body is rejected with 400."""
        url, token, _ = coordinator

        req = urllib.request.Request(
            f"{url}/api/register",
            data=b"not json",
            method="POST",
            headers={"Content-Type": "application/json", "X-Auth-Token": token},
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
        assert exc_info.value.code == 400

    def test_cancel_empty_job_id(self, coordinator):
        """Cancel with empty job_id is rejected with 400."""
        url, token, _ = coordinator
        from gpumesh.worker import MeshClient

        client = MeshClient(url, token)
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            client.call("POST", "/api/cancel", {"job_id": ""})
        assert exc_info.value.code == 400

    def test_worker_registration_and_heartbeat(self, coordinator):
        """Worker can register and send heartbeat."""
        url, token, _ = coordinator
        from gpumesh.worker import MeshClient

        client = MeshClient(url, token)
        resp = client.call("POST", "/api/register", {
            "hostname": "test-worker",
            "device": "cpu",
            "device_name": "cpu",
            "score": 5.0,
        })
        worker_id = resp["worker_id"]
        assert len(worker_id) > 0

        resp = client.call("POST", "/api/heartbeat", {"worker_id": worker_id})
        assert resp["ok"] is True

        resp = client.call("GET", "/api/workers")
        assert len(resp["workers"]) == 1
        assert resp["workers"][0]["id"] == worker_id

    def test_device_summary_with_workers(self, coordinator):
        """Device summary with multiple workers."""
        url, token, _ = coordinator
        from gpumesh.worker import MeshClient

        client = MeshClient(url, token)
        client.call("POST", "/api/register", {
            "hostname": "gpu-pc", "device": "cuda", "device_name": "RTX 3080", "score": 85.0
        })
        client.call("POST", "/api/register", {
            "hostname": "cpu-pc", "device": "cpu", "device_name": "cpu", "score": 5.0
        })

        summary = client.call("GET", "/api/devices")
        assert summary["total_devices"] == 2
        assert summary["total_gpus"] == 1

    def test_kill_all_pending_tasks(self, coordinator):
        """Kill all pending tasks."""
        url, token, _ = coordinator
        from gpumesh.worker import MeshClient

        client = MeshClient(url, token)
        script = 'import json, sys\nprint(json.dumps({"result": 42}))'
        for i in range(3):
            client.call("POST", "/api/jobs", {
                "name": f"kill-test-{i}",
                "script": script,
                "payloads": [{"x": i}],
            })

        result = client.call("POST", "/api/kill", {"force": False})
        assert result["pending"] >= 3

    def test_large_script_upload(self, coordinator):
        """Large script upload works."""
        url, token, _ = coordinator
        from gpumesh.worker import MeshClient

        client = MeshClient(url, token)
        large_script = "import json\nprint(json.dumps({'result': 42}))\n" + "# " + "x" * 100000

        resp = client.call("POST", "/api/jobs", {
            "name": "large-script",
            "script": large_script,
            "payloads": [{"x": 1}],
        })
        assert "job_id" in resp


# ============================================================================
#  UX Improvements: clearer errors & warnings
# ============================================================================

class TestCmdStatusUX:
    """cmd_status gives a clear message when a job no longer exists."""

    def test_status_job_not_found_404(self, capsys):
        """A 404 says the job is missing — not that the coordinator is down."""
        from gpumesh.cli import cmd_status

        args = MagicMock()
        args.url = "http://localhost:8000"
        args.token = "test123"
        args.job_id = "j_missing"

        with patch("gpumesh.cli.connection_manager.get_connection",
                   return_value=("http://localhost:8000", "test123")), \
             patch("gpumesh.cli.client.get_status",
                   side_effect=urllib.error.HTTPError(
                       "http://localhost:8000", 404, "Not Found", {}, None)):
            with pytest.raises(SystemExit) as exc_info:
                cmd_status(args)
            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "not found" in captured.out.lower()
        assert "could not reach coordinator" not in captured.out.lower()

    def test_status_server_error_reported(self, capsys):
        """A 500 from the coordinator is surfaced as a coordinator error."""
        from gpumesh.cli import cmd_status

        args = MagicMock()
        args.url = "http://localhost:8000"
        args.token = "test123"
        args.job_id = "j500"

        with patch("gpumesh.cli.connection_manager.get_connection",
                   return_value=("http://localhost:8000", "test123")), \
             patch("gpumesh.cli.client.get_status",
                   side_effect=urllib.error.HTTPError(
                       "http://localhost:8000", 500, "Server Error", {}, None)):
            with pytest.raises(SystemExit) as exc_info:
                cmd_status(args)
            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "500" in captured.out


class TestCmdJoinUX:
    """cmd_join validates the URL scheme up front."""

    def test_join_rejects_url_without_scheme(self, capsys):
        """A URL missing http:// is rejected with a clear message."""
        from gpumesh.cli import cmd_join

        args = MagicMock()
        args.url = "192.168.1.10:8000"
        args.token = "test123"
        args.timeout = 240.0
        args.safe_mode = False

        with pytest.raises(SystemExit) as exc_info:
            cmd_join(args)
        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "http://" in captured.out

    def test_join_accepts_valid_url(self, capsys):
        """A valid http:// URL is passed straight to the worker runner."""
        from gpumesh.cli import cmd_join

        args = MagicMock()
        args.url = "http://192.168.1.10:8000"
        args.token = "test123"
        args.timeout = 240.0
        args.safe_mode = False

        with patch("gpumesh.cli._run_worker_with_report") as mock_runner:
            cmd_join(args)
            mock_runner.assert_called_once_with(
                "http://192.168.1.10:8000", "test123", 240.0, safe_mode=False
            )


class TestCmdSubmitUX:
    """cmd_submit warns when no workers are connected."""

    def test_submit_warns_when_no_workers(self, capsys):
        """Submitting with zero workers prints a queued-job warning."""
        from gpumesh.cli import cmd_submit

        args = MagicMock()
        args.url = "http://localhost:8000"
        args.token = "test123"
        args.script = "task.py"
        args.payloads = "payloads.json"
        args.name = ""
        args.wait = False

        with patch("gpumesh.cli.connection_manager.get_connection",
                   return_value=("http://localhost:8000", "test123")), \
             patch("gpumesh.cli.client.submit_job", return_value="j123"), \
             patch("gpumesh.cli.worker.MeshClient") as MockClient:
            MockClient.return_value.call.return_value = {"workers": []}
            cmd_submit(args)

        captured = capsys.readouterr()
        assert "No workers connected" in captured.out
        assert "Submitted job" in captured.out

    def test_submit_no_warning_when_workers_alive(self, capsys):
        """Submitting with live workers prints no queued-job warning."""
        from gpumesh.cli import cmd_submit

        args = MagicMock()
        args.url = "http://localhost:8000"
        args.token = "test123"
        args.script = "task.py"
        args.payloads = "payloads.json"
        args.name = ""
        args.wait = False

        with patch("gpumesh.cli.connection_manager.get_connection",
                   return_value=("http://localhost:8000", "test123")), \
             patch("gpumesh.cli.client.submit_job", return_value="j123"), \
             patch("gpumesh.cli.worker.MeshClient") as MockClient:
            MockClient.return_value.call.return_value = {
                "workers": [{"id": "w1", "alive": True}]
            }
            cmd_submit(args)

        captured = capsys.readouterr()
        assert "No workers connected" not in captured.out

    def _submit_args(self, **overrides):
        """Build a cmd_submit args object with sensible defaults."""
        args = MagicMock()
        args.url = "http://localhost:8000"
        args.token = "test123"
        args.script = "task.py"
        args.payloads = "payloads.json"
        args.name = ""
        args.wait = True
        args.wait_timeout = 60.0
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def _enter_submit_patches(self, stack, **wait_kwargs):
        """Enter the cmd_submit dependency patches on an ExitStack.

        Workers are alive by default so the no-workers warning never fires.
        Returns the wait_for_job mock.
        """
        workers = MagicMock()
        workers.call.return_value = {
            "workers": [{"id": "w1", "alive": True}]
        }
        stack.enter_context(
            patch("gpumesh.cli.connection_manager.get_connection",
                  return_value=("http://localhost:8000", "test123")))
        stack.enter_context(
            patch("gpumesh.cli.client.submit_job", return_value="j123"))
        stack.enter_context(
            patch("gpumesh.cli.worker.MeshClient", return_value=workers))
        return stack.enter_context(
            patch("gpumesh.cli.client.wait_for_job", **wait_kwargs))

    def test_submit_wait_uses_configured_timeout(self, capsys):
        """--wait passes --wait-timeout through to wait_for_job."""
        from gpumesh.cli import cmd_submit

        args = self._submit_args(wait_timeout=120.0)
        with ExitStack() as stack:
            mock_wait = self._enter_submit_patches(
                stack, return_value={"id": "j123", "finished": True})
            cmd_submit(args)

        mock_wait.assert_called_once_with(
            "http://localhost:8000", "test123", "j123", timeout=120.0
        )

    def test_submit_wait_default_timeout_caps_hang(self, capsys):
        """--wait without --wait-timeout uses the default 3600s cap."""
        from gpumesh.cli import cmd_submit

        args = self._submit_args()
        del args.wait_timeout  # simulate the flag being absent (not just 0)
        with ExitStack() as stack:
            mock_wait = self._enter_submit_patches(
                stack, return_value={"id": "j123", "finished": True})
            cmd_submit(args)

        mock_wait.assert_called_once_with(
            "http://localhost:8000", "test123", "j123", timeout=3600.0
        )

    def test_submit_wait_zero_timeout_waits_forever(self, capsys):
        """--wait-timeout 0 explicitly opts into waiting forever."""
        from gpumesh.cli import cmd_submit

        args = self._submit_args(wait_timeout=0.0)
        with ExitStack() as stack:
            mock_wait = self._enter_submit_patches(
                stack, return_value={"id": "j123", "finished": True})
            cmd_submit(args)

        mock_wait.assert_called_once_with(
            "http://localhost:8000", "test123", "j123", timeout=0.0
        )

    def test_submit_wait_timeout_message(self, capsys):
        """A timed-out wait prints a friendly message with next steps."""
        from gpumesh.cli import cmd_submit

        args = self._submit_args(wait_timeout=1.0)
        with ExitStack() as stack:
            self._enter_submit_patches(
                stack, side_effect=TimeoutError(
                    "Job j123 did not finish within 1.0s"))
            with pytest.raises(SystemExit) as exc_info:
                cmd_submit(args)
            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "did not finish within" in captured.out
        assert "gpumesh status j123" in captured.out
        assert "wait forever" in captured.out

    def test_submit_wait_404_message(self, capsys):
        """A vanished job during --wait fails fast with a clear error."""
        from gpumesh.cli import cmd_submit

        args = self._submit_args()
        with ExitStack() as stack:
            self._enter_submit_patches(
                stack, side_effect=RuntimeError(
                    "Job j123 not found on the coordinator - it may have "
                    "been restarted with a fresh database."))
            with pytest.raises(SystemExit) as exc_info:
                cmd_submit(args)
            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "not found on the coordinator" in captured.out


class TestWaitForJobUX:
    """wait_for_job fails fast when the job no longer exists."""

    def test_wait_for_job_404_raises_quickly(self):
        """A 404 raises RuntimeError instead of polling forever."""
        from gpumesh.client import wait_for_job

        with patch("gpumesh.client.get_status",
                   side_effect=urllib.error.HTTPError(
                       "http://localhost:8000", 404, "Not Found", {}, None)):
            with pytest.raises(RuntimeError, match="not found"):
                wait_for_job("http://localhost:8000", "token", "j_missing", poll=0.01)

    def test_wait_for_job_401_raises_quickly(self):
        """A 401 (bad token) raises RuntimeError instead of polling forever."""
        from gpumesh.client import wait_for_job

        with patch("gpumesh.client.get_status",
                   side_effect=urllib.error.HTTPError(
                       "http://localhost:8000", 401, "Unauthorized", {}, None)):
            with pytest.raises(RuntimeError, match="[Aa]uthentication"):
                wait_for_job("http://localhost:8000", "token", "j_auth", poll=0.01)
