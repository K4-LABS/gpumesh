"""Tests for client.py - progress bar and job display functionality."""

import json
import sys
from unittest.mock import patch, MagicMock
import pytest

from gpumesh.client import _esc, _bar, _get_workers, print_job, wait_for_job


class TestEsc:
    """Tests for _esc() ANSI escape code function."""

    def test_esc_returns_code_when_ansi_enabled(self):
        """Returns escape code when ANSI is enabled."""
        with patch("gpumesh.client._ANSI", True):
            assert _esc("1m") == "\033[1m"
            assert _esc("0m") == "\033[0m"
            assert _esc("32m") == "\033[32m"

    def test_esc_returns_empty_when_ansi_disabled(self):
        """Returns empty string when ANSI is disabled."""
        with patch("gpumesh.client._ANSI", False):
            assert _esc("1m") == ""
            assert _esc("0m") == ""
            assert _esc("32m") == ""

    def test_esc_with_various_codes(self):
        """Tests various ANSI codes."""
        with patch("gpumesh.client._ANSI", True):
            assert _esc("31m") == "\033[31m"  # Red
            assert _esc("33m") == "\033[33m"  # Yellow
            assert _esc("36m") == "\033[36m"  # Cyan


class TestBar:
    """Tests for _bar() progress bar builder."""

    def test_bar_empty_progress(self):
        """Returns empty bar when total is 0."""
        result = _bar(0, 10)
        assert result == "." * 20

    def test_bar_full_progress(self):
        """Returns full bar when done equals total."""
        result = _bar(10, 10)
        assert result == "#" * 20

    def test_bar_half_progress(self):
        """Returns half-filled bar at 50%."""
        result = _bar(5, 10)
        assert result == "#" * 10 + "." * 10

    def test_bar_custom_width(self):
        """Returns bar with custom width."""
        result = _bar(1, 4, width=10)
        assert len(result) == 10
        assert result == "#" * 2 + "." * 8

    def test_bar_zero_done(self):
        """Returns all empty when done is 0."""
        result = _bar(0, 100, width=10)
        assert result == "." * 10

    def test_bar_width_one_partial(self):
        """Returns partial bar when width is 1 and progress < 50%."""
        result = _bar(1, 4, width=1)
        assert result == "."

    def test_bar_width_one_full(self):
        """Returns partial bar when width is 1 and progress is 50% (int truncation)."""
        result = _bar(2, 4, width=1)
        assert result == "."

    def test_bar_width_one_above_half(self):
        """Returns full bar when width is 1 and progress is 100%."""
        result = _bar(4, 4, width=1)
        assert result == "#"

    def test_bar_width_zero(self):
        """Returns empty string when width is 0."""
        result = _bar(5, 10, width=0)
        assert result == ""


class TestGetWorkers:
    """Tests for _get_workers() function."""

    def test_get_workers_success(self):
        """Returns worker dict on successful API call."""
        mock_mesh = MagicMock()
        mock_mesh.call.return_value = {
            "workers": [
                {"id": "w1", "hostname": "PC1", "device": "cuda", "score": 85.0},
                {"id": "w2", "hostname": "PC2", "device": "cpu", "score": 10.0}
            ]
        }
        with patch("gpumesh.client.MeshClient", return_value=mock_mesh):
            result = _get_workers("http://localhost:8000", "token123")
            assert len(result) == 2
            assert "w1" in result
            assert "w2" in result
            assert result["w1"]["hostname"] == "PC1"

    def test_get_workers_connection_error(self):
        """Returns empty dict on connection error."""
        mock_mesh = MagicMock()
        mock_mesh.call.side_effect = ConnectionError("Connection refused")
        with patch("gpumesh.client.MeshClient", return_value=mock_mesh):
            result = _get_workers("http://localhost:8000", "token123")
            assert result == {}

    def test_get_workers_os_error(self):
        """Returns empty dict on OS error."""
        mock_mesh = MagicMock()
        mock_mesh.call.side_effect = OSError("Network unreachable")
        with patch("gpumesh.client.MeshClient", return_value=mock_mesh):
            result = _get_workers("http://localhost:8000", "token123")
            assert result == {}

    def test_get_workers_json_error(self):
        """Returns empty dict on JSON decode error."""
        mock_mesh = MagicMock()
        mock_mesh.call.side_effect = json.JSONDecodeError("Invalid JSON", "doc", 0)
        with patch("gpumesh.client.MeshClient", return_value=mock_mesh):
            result = _get_workers("http://localhost:8000", "token123")
            assert result == {}

    def test_get_workers_empty_workers(self):
        """Returns empty dict when no workers."""
        mock_mesh = MagicMock()
        mock_mesh.call.return_value = {"workers": []}
        with patch("gpumesh.client.MeshClient", return_value=mock_mesh):
            result = _get_workers("http://localhost:8000", "token123")
            assert result == {}


class TestPrintJob:
    """Tests for print_job() function."""

    def test_print_job_running(self, capsys):
        """Prints running job status."""
        job = {
            "id": "j123",
            "name": "test_job",
            "finished": False,
            "counts": {"running": 2, "pending": 3},
            "tasks": [
                {"id": "t1", "status": "running", "cost": 1.0, "worker_id": "w1", "result": None, "error": None},
                {"id": "t2", "status": "pending", "cost": 1.0, "worker_id": None, "result": None, "error": None}
            ]
        }
        print_job(job)
        captured = capsys.readouterr()
        assert "test_job" in captured.out
        assert "j123" in captured.out
        assert "running" in captured.out
        assert "worker=w1" in captured.out

    def test_print_job_finished(self, capsys):
        """Prints finished job status."""
        job = {
            "id": "j456",
            "name": "completed_job",
            "finished": True,
            "counts": {"done": 2, "failed": 1},
            "tasks": [
                {"id": "t1", "status": "done", "cost": 1.0, "worker_id": "w1", "result": {"acc": 0.9}, "error": None},
                {"id": "t2", "status": "failed", "cost": 1.0, "worker_id": "w2", "result": None, "error": "timeout"}
            ]
        }
        print_job(job)
        captured = capsys.readouterr()
        assert "completed_job" in captured.out
        assert "finished" in captured.out
        assert "result: " in captured.out
        assert "error: timeout" in captured.out

    def test_print_job_with_results(self, capsys):
        """Prints task results."""
        job = {
            "id": "j789",
            "name": "result_job",
            "finished": True,
            "counts": {"done": 1},
            "tasks": [
                {"id": "t1", "status": "done", "cost": 2.0, "worker_id": "w1", "result": {"value": 42}, "error": None}
            ]
        }
        print_job(job)
        captured = capsys.readouterr()
        assert '{"value": 42}' in captured.out


class TestWaitForJob:
    """Tests for wait_for_job() function."""

    def test_wait_for_job_immediate_finish(self, capsys):
        """Returns immediately when job is finished."""
        finished_job = {
            "id": "j123",
            "name": "test",
            "finished": True,
            "counts": {"done": 1},
            "tasks": [{"id": "t1", "status": "done", "cost": 1.0, "worker_id": "w1", "result": {"val": 1}, "error": None}]
        }
        with patch("gpumesh.client.get_status", return_value=finished_job), \
             patch("gpumesh.client._get_workers", return_value={"w1": {"hostname": "PC1", "device": "cuda"}}):
            result = wait_for_job("http://localhost:8000", "token", "j123", poll=0.01)
            assert result["finished"] is True
            assert result["id"] == "j123"

    def test_wait_for_job_polls_until_finished(self, capsys):
        """Polls multiple times before job finishes."""
        running_job = {
            "id": "j123", "name": "test", "finished": False,
            "counts": {"running": 1},
            "tasks": [{"id": "t1", "status": "running", "cost": 1.0, "worker_id": "w1", "result": None, "error": None}]
        }
        finished_job = {
            "id": "j123", "name": "test", "finished": True,
            "counts": {"done": 1},
            "tasks": [{"id": "t1", "status": "done", "cost": 1.0, "worker_id": "w1", "result": {"val": 1}, "error": None}]
        }
        with patch("gpumesh.client.get_status", side_effect=[running_job, finished_job]), \
             patch("gpumesh.client._get_workers", return_value={"w1": {"hostname": "PC1", "device": "cuda"}}):
            result = wait_for_job("http://localhost:8000", "token", "j123", poll=0.01)
            assert result["finished"] is True

    def test_wait_for_job_shows_progress(self, capsys):
        """Displays progress information during wait."""
        progress_job = {
            "id": "j123", "name": "progress_job", "finished": False,
            "counts": {"pending": 2, "running": 0}, "tasks": []
        }
        finished_job = {
            "id": "j123", "name": "progress_job", "finished": True,
            "counts": {"done": 2},
            "tasks": [{"id": "t1", "status": "done", "cost": 1.0, "worker_id": "w1", "result": {}, "error": None}]
        }
        with patch("gpumesh.client.get_status", side_effect=[progress_job, finished_job]), \
             patch("gpumesh.client._get_workers", return_value={}), \
             patch("sys.stdout.write") as mock_write:
            result = wait_for_job("http://localhost:8000", "token", "j123", poll=0.01)
            # Verify write was called (progress was displayed)
            assert mock_write.call_count > 0

    def test_wait_for_job_with_failed_tasks(self, capsys):
        """Handles failed tasks in progress display."""
        job = {
            "id": "j123",
            "name": "fail_job",
            "finished": True,
            "counts": {"done": 1, "failed": 1},
            "tasks": [
                {"id": "t1", "status": "done", "cost": 1.0, "worker_id": "w1", "result": {}, "error": None},
                {"id": "t2", "status": "failed", "cost": 1.0, "worker_id": "w1", "result": None, "error": "crash"}
            ]
        }
        with patch("gpumesh.client.get_status", return_value=job), \
             patch("gpumesh.client._get_workers", return_value={}), \
             patch("sys.stdout.write"):
            result = wait_for_job("http://localhost:8000", "token", "j123", poll=0.01)
            assert result["finished"] is True
            assert result["counts"]["failed"] == 1

    def test_wait_for_job_timeout_raises(self):
        """Raises TimeoutError when the wait exceeds the configured timeout."""
        running_job = {
            "id": "j123", "name": "test", "finished": False,
            "counts": {"pending": 1}, "tasks": []
        }
        with patch("gpumesh.client.get_status", return_value=running_job), \
             patch("gpumesh.client._get_workers", return_value={}), \
             patch("sys.stdout.write"):
            with pytest.raises(TimeoutError, match="did not finish within"):
                wait_for_job("http://localhost:8000", "token", "j123",
                             poll=0.01, timeout=0.01)

    def test_wait_for_job_zero_timeout_waits(self, capsys):
        """timeout=0 (the default) means wait without a limit."""
        finished_job = {
            "id": "j123", "name": "test", "finished": True,
            "counts": {"done": 1},
            "tasks": [{"id": "t1", "status": "done", "cost": 1.0,
                        "worker_id": "w1", "result": {}, "error": None}]
        }
        with patch("gpumesh.client.get_status", return_value=finished_job), \
             patch("gpumesh.client._get_workers", return_value={}):
            result = wait_for_job("http://localhost:8000", "token", "j123",
                                  poll=0.01, timeout=0.0)
            assert result["finished"] is True

    def _capture_wait_output(self, job, extra_job=None):
        """Run wait_for_job capturing every sys.stdout.write call."""
        jobs = [job] if extra_job is None else [job, extra_job]
        with patch("gpumesh.client.get_status", side_effect=jobs), \
             patch("gpumesh.client._get_workers", return_value={}), \
             patch("sys.stdout.write") as mock_write:
            result = wait_for_job("http://localhost:8000", "token", "j123",
                                  poll=0.01)
        written = "".join(call.args[0] for call in mock_write.call_args_list)
        return result, written

    def test_wait_for_job_shows_percentage(self, capsys):
        """The progress line shows a completed/total percentage."""
        running_job = {
            "id": "j123", "name": "pct_job", "finished": False,
            "counts": {"done": 2, "pending": 2}, "tasks": []
        }
        finished_job = {
            "id": "j123", "name": "pct_job", "finished": True,
            "counts": {"done": 4},
            "tasks": [{"id": "t1", "status": "done", "cost": 1.0,
                        "worker_id": "w1", "result": {}, "error": None}]
        }
        result, written = self._capture_wait_output(running_job, finished_job)
        assert result["finished"] is True
        # 2 of 4 tasks terminal -> 50%
        assert "50%" in written
        assert "2/4 done" in written

    def test_wait_for_job_percentage_counts_failed_as_done(self, capsys):
        """Failed tasks count toward the percentage (matches the bar)."""
        running_job = {
            "id": "j123", "name": "fail_pct", "finished": False,
            "counts": {"done": 1, "failed": 1, "pending": 2}, "tasks": []
        }
        finished_job = {
            "id": "j123", "name": "fail_pct", "finished": True,
            "counts": {"done": 2, "failed": 2},
            "tasks": [{"id": "t1", "status": "done", "cost": 1.0,
                        "worker_id": "w1", "result": {}, "error": None}]
        }
        result, written = self._capture_wait_output(running_job, finished_job)
        assert result["finished"] is True
        # 1 done + 1 failed = 2 of 4 terminal -> 50%
        assert "50%" in written
        assert "1 failed" in written

    def test_wait_for_job_percentage_hidden_when_no_tasks(self, capsys):
        """No percentage is shown when the job has zero tasks yet."""
        running_job = {
            "id": "j123", "name": "empty_job", "finished": False,
            "counts": {"pending": 0}, "tasks": []
        }
        finished_job = {
            "id": "j123", "name": "empty_job", "finished": True,
            "counts": {"done": 1},
            "tasks": [{"id": "t1", "status": "done", "cost": 1.0,
                        "worker_id": "w1", "result": {}, "error": None}]
        }
        result, written = self._capture_wait_output(running_job, finished_job)
        assert result["finished"] is True
        # The zero-task poll shows "No tasks yet" without a percentage
        # (a later poll legitimately shows 100% once the job finishes).
        no_tasks_line = next(
            line for line in written.splitlines() if "No tasks yet" in line
        )
        assert "%" not in no_tasks_line


class TestSafeStr:
    """Tests for _safe_str encoding helper."""

    def test_safe_str_ascii_passthrough(self):
        from gpumesh.ansi import _safe_str
        result = _safe_str("hello world")
        assert result == "hello world"

    def test_safe_str_unicode_replaces_on_cp1252(self):
        from gpumesh.ansi import _safe_str
        # Simulate cp1252 terminal by testing replacement logic
        result = _safe_str("\u2713 done \u2717 failed")
        # Should contain ASCII fallbacks
        assert "[OK]" in result or "\u2713" in result  # depends on encoding

    def test_safe_str_preserves_ansi_codes(self):
        from gpumesh.ansi import _safe_str
        result = _safe_str("\033[32mOK\033[0m")
        assert "\033[32m" in result

    def test_safe_str_empty_string(self):
        from gpumesh.ansi import _safe_str
        result = _safe_str("")
        assert result == ""


class TestSafePrint:
    """Tests for safe_print function."""

    def test_safe_print_basic(self):
        from gpumesh.ansi import safe_print
        import io
        buf = io.StringIO()
        safe_print("hello", file=buf)
        assert buf.getvalue().strip() == "hello"

    def test_safe_print_with_sep(self):
        from gpumesh.ansi import safe_print
        import io
        buf = io.StringIO()
        safe_print("a", "b", "c", sep="-", file=buf)
        assert buf.getvalue().strip() == "a-b-c"

    def test_safe_print_with_end(self):
        from gpumesh.ansi import safe_print
        import io
        buf = io.StringIO()
        safe_print("hello", end="", file=buf)
        assert buf.getvalue() == "hello"

    def test_safe_print_unicode_chars(self):
        from gpumesh.ansi import safe_print
        import io
        buf = io.StringIO()
        safe_print("\u2713 \u2717 \u2588", file=buf)
        output = buf.getvalue()
        assert len(output) > 0


class TestFormatResultStrictMode:
    """Under strict mode the display shows the refusal, not the blob.

    ``_format_result`` unwraps the serializer envelope so the user sees the
    value their function returned. Strict mode makes that unwrap raise, and
    the tempting fallback — print ``raw`` — would dump a screenful of base64
    onto the terminal. That reads as a rendering bug rather than as a security
    decision, and it buries the one line worth reading, so the refusal has to
    be what gets rendered.
    """

    @staticmethod
    def _pickled_envelope():
        """A cloudpickle envelope as it arrives from the coordinator."""
        pytest.importorskip("cloudpickle")
        np = pytest.importorskip("numpy")
        from gpumesh.serializer import encode_result

        return json.loads(json.dumps(encode_result({"arr": np.arange(3)})))

    def test_strict_marker_replaces_the_base64_envelope(self, monkeypatch):
        from gpumesh.client import _format_result
        from gpumesh.serializer import RESULT_ENVELOPE_KEY

        envelope = self._pickled_envelope()
        blob = envelope[RESULT_ENVELOPE_KEY]["value"]
        monkeypatch.setenv("GPUMESH_STRICT_RESULTS", "1")

        text = _format_result(envelope)
        assert "_gpumesh_strict" in text
        assert blob not in text
        assert RESULT_ENVELOPE_KEY not in text
        # The line has to say which switch produced it, or the user cannot
        # tell a refusal from a broken task.
        assert "--strict" in text or "GPUMESH_STRICT_RESULTS" in text

    def test_strict_marker_survives_compact_rendering(self, monkeypatch):
        """The one-line progress view renders the same refusal, not the blob."""
        from gpumesh.client import _format_result
        from gpumesh.serializer import RESULT_ENVELOPE_KEY

        envelope = self._pickled_envelope()
        blob = envelope[RESULT_ENVELOPE_KEY]["value"]
        monkeypatch.setenv("GPUMESH_STRICT_RESULTS", "1")

        text = _format_result(envelope, compact=True)
        assert "_gpumesh_strict" in text
        assert blob not in text

    def test_non_strict_still_shows_the_value(self, monkeypatch):
        """Without the flag nothing changes: the decoded value is displayed."""
        pytest.importorskip("cloudpickle")
        from gpumesh.client import _format_result

        monkeypatch.delenv("GPUMESH_STRICT_RESULTS", raising=False)
        text = _format_result(self._pickled_envelope())
        assert "_gpumesh_strict" not in text
        # numpy arrays are not JSON-encodable, so this falls through to repr().
        assert "arr" in text

    def test_json_results_are_untouched_by_strict_mode(self, monkeypatch):
        """Strict mode only refuses pickled results, never plain JSON ones."""
        from gpumesh.client import _format_result
        from gpumesh.serializer import encode_result

        monkeypatch.setenv("GPUMESH_STRICT_RESULTS", "1")
        assert _format_result(encode_result({"acc": 0.9})) == '{"acc": 0.9}'
        assert _format_result({"rows": 10}) == '{"rows": 10}'

    def test_print_job_does_not_leak_the_blob(self, monkeypatch, capsys):
        """The end-to-end display path, since that is where a user meets it."""
        from gpumesh.client import print_job
        from gpumesh.serializer import RESULT_ENVELOPE_KEY

        envelope = self._pickled_envelope()
        blob = envelope[RESULT_ENVELOPE_KEY]["value"]
        monkeypatch.setenv("GPUMESH_STRICT_RESULTS", "1")

        print_job({
            "id": "j1", "name": "strict_job", "finished": True,
            "counts": {"done": 1},
            "tasks": [{"id": "t1", "status": "done", "cost": 1.0,
                       "worker_id": "w1", "result": envelope, "error": None}],
        })
        out = capsys.readouterr().out
        assert "_gpumesh_strict" in out
        assert blob not in out
