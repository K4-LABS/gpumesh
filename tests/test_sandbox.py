import os
import signal
import subprocess
import sys
import time
from typing import Any

import pytest

from gpumesh import sandbox
from gpumesh.sandbox import TaskError, _kill_tree, run_task

ECHO = """
import json, sys
payload = json.load(sys.stdin)
print("some log line")
print(json.dumps({"echo": payload["x"] * 2}))
"""


def test_run_task_returns_last_json_line():
    result = run_task(ECHO, {"x": 21})
    assert result == {"echo": 42}


def test_run_task_timeout_kills_process():
    with pytest.raises(TaskError, match="timed out"):
        run_task("import time\ntime.sleep(60)", {}, timeout=1.0)


def test_run_task_nonzero_exit():
    with pytest.raises(TaskError, match="exited with code 1"):
        run_task("raise RuntimeError('kaput')", {})


def test_run_task_bad_output():
    with pytest.raises(TaskError, match="not JSON"):
        run_task("print('hello, not json')", {})


def test_run_task_no_output():
    with pytest.raises(TaskError, match="no output"):
        run_task("pass", {})


def test_device_env_passed_through():
    script = """
import json, os, sys
sys.stdin.read()
print(json.dumps({"device": os.environ["GPUMESH_DEVICE"]}))
"""
    assert run_task(script, {}, device="mps") == {"device": "mps"}


def test_run_task_timeout_kills_subprocesses():
    """Timeout kills the process tree, not just the parent."""
    script = """
import json, subprocess, sys, time
sys.stdin.read()
# Spawn a child that sleeps
p = subprocess.Popen(["python", "-c", "import time; time.sleep(60)"])
time.sleep(60)
print(json.dumps({"done": True}))
"""
    with pytest.raises(TaskError, match="timed out"):
        run_task(script, {}, timeout=2.0)


def test_run_task_binary_output():
    """Binary output doesn't crash the runner."""
    script = """
import json, sys
sys.stdin.read()
sys.stdout.buffer.write(b"\\xff\\xfe\\x00\\x01\\n")
print(json.dumps({"ok": True}))
"""
    result = run_task(script, {})
    assert result == {"ok": True}


def test_run_task_unicode_result():
    """Non-ASCII results survive the JSON pipe (Windows cp1252 safe).

    Regression: on Windows the task child's stdout defaults to the ANSI
    code page, so printing a result containing e.g. "✓" raised
    UnicodeEncodeError inside the task and failed it spuriously. The
    runner now forces PYTHONIOENCODING=utf-8 on the child.
    """
    script = """
import json, sys
sys.stdin.read()
print(json.dumps({"text": "caf\u00e9 \u2713"}))
"""
    result = run_task(script, {})
    assert result == {"text": "caf\u00e9 \u2713"}


# \u2500\u2500 child environment \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

READ_ENV = """
import json, os, sys
sys.stdin.read()
print(json.dumps({"env": dict(os.environ)}))
"""


def test_child_encoding_is_pinned_to_utf8():
    """Pins the deliberate PYTHONIOENCODING fix the unicode test depends on.

    Without it the child's stdout defaults to the ANSI code page on Windows,
    and any non-ASCII result dies with UnicodeEncodeError *inside* the task \u2014
    a failure that looks like the user's bug, not ours.
    """
    env = run_task(READ_ENV, {})["env"]
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_worker_environment_is_not_inherited(monkeypatch):
    """The child gets an allow-list, not os.environ.

    A worker runs someone else's script; anything in the operator's shell
    (cloud credentials, API keys, GPUMESH_TOKEN) must not travel with it.
    """
    monkeypatch.setenv("GPUMESH_TEST_SECRET", "hunter2")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "should-not-leak")
    env = run_task(READ_ENV, {})["env"]
    assert "GPUMESH_TEST_SECRET" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_device_defaults_to_cpu():
    script = """
import json, os, sys
sys.stdin.read()
print(json.dumps({"device": os.environ["GPUMESH_DEVICE"]}))
"""
    assert run_task(script, {}) == {"device": "cpu"}
    assert run_task(script, {}, device=None) == {"device": "cpu"}
    assert run_task(script, {}, device="") == {"device": "cpu"}


def test_task_runs_in_a_private_scratch_directory():
    script = """
import json, os, sys
sys.stdin.read()
open("scratch.txt", "w").write("x")
print(json.dumps({"cwd": os.getcwd()}))
"""
    cwd = run_task(script, {})["cwd"]
    assert "gpumesh-task-" in cwd
    # The directory (and anything the task wrote into it) is gone afterwards.
    assert not os.path.exists(cwd)


def test_payload_arrives_as_json_on_stdin():
    script = """
import json, sys
payload = json.load(sys.stdin)
print(json.dumps({"keys": sorted(payload), "nested": payload["a"]["b"]}))
"""
    result = run_task(script, {"a": {"b": [1, 2]}, "z": None})
    assert result == {"keys": ["a", "z"], "nested": [1, 2]}


def test_script_sees_dunder_main():
    """Task scripts rely on the ``if __name__ == '__main__':`` entry point.

    On POSIX the script is launched through a runpy shim (so the CPU rlimit
    can be applied inside the child); ``run_name='__main__'`` is what keeps
    this true there.
    """
    script = """
import json, sys
sys.stdin.read()
if __name__ == "__main__":
    print(json.dumps({"main": True}))
"""
    assert run_task(script, {}) == {"main": True}


def test_traceback_line_numbers_match_the_user_script():
    """The POSIX launcher must not shift the user's line numbers."""
    script = "import sys\nsys.stdin.read()\n\nraise RuntimeError('boom here')\n"
    with pytest.raises(TaskError) as excinfo:
        run_task(script, {})
    assert "line 4" in str(excinfo.value)
    assert "boom here" in str(excinfo.value)


# \u2500\u2500 output handling \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

def test_last_json_line_wins_over_earlier_ones():
    script = """
import json, sys
sys.stdin.read()
print(json.dumps({"n": 1}))
print(json.dumps({"n": 2}))
"""
    assert run_task(script, {}) == {"n": 2}


def test_trailing_blank_lines_are_ignored():
    script = """
import json, sys
sys.stdin.read()
print(json.dumps({"ok": True}))
print()
print("   ")
"""
    assert run_task(script, {}) == {"ok": True}


def test_whitespace_only_output_counts_as_no_output():
    with pytest.raises(TaskError, match="no output"):
        run_task("import sys\nsys.stdin.read()\nprint('   ')\nprint()", {})


def test_non_object_json_is_returned_as_is():
    """Any JSON value comes back, not just an object.

    ``json.loads`` happily returns a list or a scalar and run_task passes it
    straight through. That is the documented contract now: the annotation was
    corrected to ``Any`` rather than the behaviour narrowed to ``dict``,
    because the value only ever travels onward as JSON and rejecting a list
    would break scripts that work today.
    """
    script = "import json, sys\nsys.stdin.read()\nprint(json.dumps([1, 2, 3]))"
    assert run_task(script, {}) == [1, 2, 3]
    assert run_task("import sys\nsys.stdin.read()\nprint(7)", {}) == 7


def test_return_annotation_matches_the_behaviour():
    """The annotation claimed ``dict`` while the code returned anything.

    Pinned because the mismatch is the kind a reader trusts: someone writing a
    caller against ``-> dict`` would index the result without checking.
    """
    assert run_task.__annotations__["return"] is Any


def test_bad_output_message_is_truncated():
    """A task that dumps a megabyte of prose must not put it all in the error."""
    script = "import sys\nsys.stdin.read()\nprint('x' * 5000)"
    with pytest.raises(TaskError) as excinfo:
        run_task(script, {})
    assert len(str(excinfo.value)) < 400


def test_large_json_result_survives_the_pipe():
    """A result bigger than a pipe buffer must not deadlock the runner."""
    script = """
import json, sys
sys.stdin.read()
print(json.dumps({"blob": "y" * 200000}))
"""
    assert run_task(script, {})["blob"] == "y" * 200000


# \u2500\u2500 failure reporting \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

def test_failure_message_includes_the_stderr_tail():
    with pytest.raises(TaskError) as excinfo:
        run_task("import sys\nsys.stdin.read()\nraise RuntimeError('kaput')", {})
    assert "kaput" in str(excinfo.value)


def test_only_the_last_five_stderr_lines_are_reported():
    script = """
import sys
sys.stdin.read()
for i in range(30):
    print("noise-line-%d" % i, file=sys.stderr)
sys.exit(1)
"""
    with pytest.raises(TaskError) as excinfo:
        run_task(script, {})
    message = str(excinfo.value)
    assert "noise-line-29" in message
    assert "noise-line-0" not in message


def test_clean_nonzero_exit_without_stderr():
    with pytest.raises(TaskError, match="exited with code 3"):
        run_task("import sys\nsys.stdin.read()\nsys.exit(3)", {})


def test_task_error_carries_the_user_error_flag():
    assert TaskError("x").user_error is False
    assert TaskError("x", user_error=True).user_error is True


def test_script_failures_are_marked_deterministic():
    """A script that always raises, or never prints JSON, must fail once.

    Regression: run_task never set ``user_error``, so these burned the whole
    MAX_ATTEMPTS budget in db.complete_task re-running a script guaranteed to
    fail identically \u2014 the same problem already fixed for function tasks.
    """
    for script, pattern in (
        ("import sys\nsys.stdin.read()\nraise ValueError('always')", "exited with code"),
        ("import sys\nsys.stdin.read()\nprint('nope')", "not JSON"),
    ):
        with pytest.raises(TaskError, match=pattern) as excinfo:
            run_task(script, {})
        assert excinfo.value.user_error is True


def test_explicit_nonzero_exit_is_deterministic():
    with pytest.raises(TaskError) as excinfo:
        run_task("import sys\nsys.stdin.read()\nsys.exit(3)", {})
    assert excinfo.value.user_error is True


def test_no_output_is_deterministic():
    """Exited cleanly, printed nothing: re-running produces the same nothing."""
    with pytest.raises(TaskError, match="no output") as excinfo:
        run_task("import sys\nsys.stdin.read()", {})
    assert excinfo.value.user_error is True


def test_timeout_failure_is_retryable():
    """Infrastructure failures must stay retryable."""
    with pytest.raises(TaskError) as excinfo:
        run_task("import time\ntime.sleep(60)", {}, timeout=1.0)
    assert excinfo.value.user_error is False


@pytest.mark.skipif(os.name != "posix",
                    reason="only POSIX reports a signal as a negative status")
def test_death_by_signal_stays_retryable():
    """A signal is the machine's verdict, not the script's.

    SIGKILL from the OOM killer means this worker was full; the same task may
    run fine on the next one, so it must keep its retries. Only a *positive*
    exit status is the script failing on its own terms.
    """
    script = ("import os, signal, sys\n"
              "sys.stdin.read()\n"
              "os.kill(os.getpid(), signal.SIGKILL)\n")
    with pytest.raises(TaskError, match="exited with code") as excinfo:
        run_task(script, {})
    assert excinfo.value.user_error is False


def test_timeout_is_enforced_promptly():
    started = time.monotonic()
    with pytest.raises(TaskError, match="timed out"):
        run_task("import time\ntime.sleep(60)", {}, timeout=1.0)
    # Generous, but far below the 60s the task asked for.
    assert time.monotonic() - started < 30


# \u2500\u2500 _kill_tree \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

def test_kill_tree_kills_a_live_process():
    """The Windows branch shells out to taskkill; the POSIX one uses killpg.

    Either way the process must be dead when _kill_tree returns, and the call
    must not block: every wait inside it is bounded.
    """
    kwargs = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs
    )
    try:
        assert proc.poll() is None
        _kill_tree(proc)
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:  # pragma: no cover - only if the kill failed
            proc.kill()
            proc.wait(timeout=10)


def test_kill_tree_on_an_already_dead_process_is_harmless():
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    proc.wait(timeout=30)
    _kill_tree(proc)  # must not raise
    assert proc.poll() is not None


class _NeverExits:
    """Popen stand-in whose every wait() times out.

    Stands in for the child that survives its own SIGKILL — a process wedged
    in uninterruptible IO, which is exactly when a kill does not take effect
    promptly.
    """

    def __init__(self):
        self.pid = 4242
        self.kill_calls = 0

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired(cmd="task", timeout=timeout)

    def kill(self):
        self.kill_calls += 1

    def poll(self):
        return None


class _PosixOs:
    """Stand-in for the ``os`` module that claims to be POSIX.

    Only the three names the POSIX branch of _kill_tree touches are defined;
    everything else falls through to the real module. It is swapped in as
    ``sandbox.os`` rather than by patching the real ``os.name``, because that
    attribute is global: pathlib reads it while building a failure report, so
    a genuinely failing assertion inside the patched window crashes pytest
    itself instead of reporting the failure.
    """

    name = "posix"

    def __init__(self):
        self.signalled = []

    def getpgid(self, pid):
        return pid

    def killpg(self, pgid, sig):
        self.signalled.append((pgid, sig))

    def __getattr__(self, item):
        return getattr(os, item)


class _PosixSignal:
    """Stand-in for ``signal``; Windows genuinely has no SIGKILL to offer."""

    SIGKILL = 9

    def __getattr__(self, item):
        return getattr(signal, item)


def test_kill_tree_survives_a_posix_wait_timeout(monkeypatch):
    """The POSIX wait() must not throw TimeoutExpired past the caller.

    Regression: ``proc.wait(timeout=5)`` after a successful killpg sat in a
    try that caught only ProcessLookupError/PermissionError. A wait timeout
    escaped _kill_tree *and* run_task's ``except subprocess.TimeoutExpired``
    (already exited), so the worker — which handles TaskError — got a
    TimeoutExpired instead. The Windows branch always got this right.
    """
    fake_os = _PosixOs()
    monkeypatch.setattr(sandbox, "os", fake_os)
    monkeypatch.setattr(sandbox, "signal", _PosixSignal())
    proc = _NeverExits()
    _kill_tree(proc)  # must not raise
    assert fake_os.signalled == [(proc.pid, _PosixSignal.SIGKILL)]
    # Falls through to the unconditional kill rather than returning early.
    assert proc.kill_calls == 1


class _FakePipe:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _TimesOutOnce:
    """Popen stand-in: the first communicate() times out, a second drains."""

    def __init__(self):
        self.pid = 4242
        self.returncode = None
        self.communicate_calls = 0
        self.stdin = _FakePipe()
        self.stdout = _FakePipe()
        self.stderr = _FakePipe()

    def communicate(self, input=None, timeout=None):
        self.communicate_calls += 1
        if self.communicate_calls == 1:
            raise subprocess.TimeoutExpired(cmd="task", timeout=timeout)
        # What a real second communicate() does once the child is dead: joins
        # the reader threads, which close our ends of the pipes.
        for pipe in (self.stdin, self.stdout, self.stderr):
            pipe.close()
        self.returncode = -9
        return "", ""

    def wait(self, timeout=None):
        self.returncode = -9
        return self.returncode

    def kill(self):
        pass

    def poll(self):
        return self.returncode


def test_timeout_drains_the_pipes_after_killing(monkeypatch):
    """A timeout must not leave the pipes open behind it.

    The first communicate() abandons its reader threads when it raises, so our
    ends stay open and in use; on Windows the TemporaryDirectory cleanup that
    immediately follows can then fail, turning this TaskError into a
    PermissionError the worker does not handle.

    _kill_tree is stubbed out deliberately: this stand-in has a made-up pid and
    the real one shells out to ``taskkill /F /T``.
    """
    proc = _TimesOutOnce()
    killed = []
    monkeypatch.setattr(sandbox.subprocess, "Popen", lambda *a, **kw: proc)
    monkeypatch.setattr(sandbox, "_kill_tree", killed.append)

    with pytest.raises(TaskError, match="timed out"):
        run_task("import time\ntime.sleep(60)", {}, timeout=0.5)

    assert killed == [proc]                 # killed first,
    assert proc.communicate_calls == 2      # then drained,
    assert proc.stdout.closed and proc.stderr.closed


def test_timeout_cleans_up_the_scratch_directory(monkeypatch):
    """End of the same story: the temp dir is gone and TaskError is what escapes."""
    made = []
    real_tempdir = sandbox.tempfile.TemporaryDirectory

    def _spy(*args, **kwargs):
        tmp = real_tempdir(*args, **kwargs)
        made.append(tmp.name)
        return tmp

    monkeypatch.setattr(sandbox.tempfile, "TemporaryDirectory", _spy)
    with pytest.raises(TaskError, match="timed out"):
        run_task("import time\ntime.sleep(60)", {}, timeout=1.0)
    assert made and not os.path.exists(made[0])


@pytest.mark.skipif(os.name != "posix",
                    reason="RLIMIT_CPU is POSIX-only by design")
def test_cpu_rlimit_stops_a_runaway_loop():
    """The rlimit is applied inside the child (not via preexec_fn).

    A busy loop with a 1-second CPU budget is killed by SIGXCPU long before
    the wall-clock timeout, which is the whole point: a task that never
    sleeps cannot be reached by a cooperative check.
    """
    script = "import sys\nsys.stdin.read()\nwhile True:\n    pass\n"
    with pytest.raises(TaskError, match="exited with code"):
        run_task(script, {}, timeout=60.0, cpu_seconds=1)
