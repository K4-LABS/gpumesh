"""Isolated task execution.

Each task runs the job's script in a fresh Python subprocess:
  - the payload arrives as JSON on stdin
  - the script prints its result as JSON on the last stdout line
  - the process runs in its own session (process group) so a timeout can
    kill the whole tree, not just the parent
  - on POSIX an optional CPU-seconds rlimit caps runaway loops
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
from typing import Any


class TaskError(Exception):
    """A task failed.

    ``user_error`` marks failures caused by the task's own code (it raised,
    or returned something that cannot be sent back). Those are deterministic:
    re-running the task produces the same failure, so the coordinator fails
    them immediately instead of burning the retry budget. Infrastructure
    failures (timeouts, killed workers, network drops) leave it False and
    stay retryable.
    """

    def __init__(self, *args, user_error: bool = False):
        super().__init__(*args)
        self.user_error = user_error


def _cpu_limit_preamble(cpu_seconds: int) -> str:
    """Return a source preamble that caps the task's CPU time.

    This used to be a ``preexec_fn`` that called ``resource.setrlimit``
    between fork and exec. That is documented as unsafe when the parent has
    threads, and the worker always does — a child forked while another thread
    held an internal lock could block forever trying to acquire it, hanging
    the task where no timeout could reach it.

    Running the same call as the first statement of the child instead is
    thread-safe, needs no fork-time callback, and enforces exactly the same
    limit. It runs in the child's own interpreter, after exec.
    """
    return (
        "import resource as _r\n"
        "try:\n"
        f"    _r.setrlimit(_r.RLIMIT_CPU, ({cpu_seconds}, {cpu_seconds}))\n"
        "except Exception:\n"
        "    pass\n"
        "del _r\n"
    )


def run_task(script: str, payload, timeout: float = 240.0,
             cpu_seconds: int = 600, device: str = "cpu") -> Any:
    """Execute `script` with `payload`; return the parsed JSON result.

    The return type is ``Any``, not ``dict``: whatever the task's last stdout
    line decodes to comes straight back, so a task that prints ``[1, 2, 3]``
    yields a list. The annotation was corrected rather than the contract
    enforced, because enforcing it would only take working behaviour away —
    the worker hands this value to the coordinator as JSON, where every JSON
    type round-trips fine, and the function path
    (``worker._run_function_task``) already returns whatever the user's
    function returned. Rejecting a list here would break scripts that work
    today and make script tasks stricter than function tasks for no gain.
    """
    with tempfile.TemporaryDirectory(prefix="gpumesh-task-") as workdir:
        script_path = os.path.join(workdir, "task.py")
        with open(script_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(script)

        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "TEMP": os.environ.get("TEMP", ""),
            "TMP": os.environ.get("TMP", ""),
            "GPUMESH_DEVICE": device or "cpu",
            # The parent reads the child's stdout as UTF-8 (Popen text mode
            # below), but on Windows the child's stdout defaults to the ANSI
            # code page (cp1252) — a result containing e.g. "✓" or "é" would
            # raise UnicodeEncodeError inside the task and fail it spuriously.
            # Force UTF-8 so task output always matches what the parent expects.
            "PYTHONIOENCODING": "utf-8",
        }
        entry_path = script_path
        kwargs = {}
        if os.name == "posix":
            # Apply the CPU limit inside the child rather than via preexec_fn.
            # A separate launcher keeps the user's script line numbers intact
            # in tracebacks, which prepending the preamble to it would shift.
            # run_name="__main__" preserves the `if __name__ == "__main__":`
            # entry point that task scripts rely on.
            entry_path = os.path.join(workdir, "_gpumesh_launch.py")
            with open(entry_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(_cpu_limit_preamble(cpu_seconds))
                f.write("import runpy\n")
                f.write(f"runpy.run_path({script_path!r}, run_name='__main__')\n")
            kwargs["start_new_session"] = True

        proc = subprocess.Popen(
            [sys.executable, entry_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workdir,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            **kwargs,
        )
        try:
            out, err = proc.communicate(json.dumps(payload), timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            # Drain again after the kill. The first communicate() abandoned
            # its reader threads when it timed out, so our ends of the pipes
            # are still open and still being read; the TemporaryDirectory
            # cleanup a few lines below can then fail on Windows because the
            # directory is still in use, replacing this TaskError with a
            # PermissionError the caller does not handle. Bounded, because a
            # grandchild that outlived the kill must not hang the worker here
            # — this is cleanup, and the output is already worthless.
            try:
                proc.communicate(timeout=5)
            except (subprocess.TimeoutExpired, ValueError, OSError):
                pass
            # Deliberately retryable: a timeout says something about this
            # machine's load, not about the task's code, and the same task may
            # well finish inside the limit on another worker.
            raise TaskError(f"task timed out after {timeout}s")

        if proc.returncode != 0:
            tail = (err or "").strip().splitlines()[-5:]
            # A *positive* exit status is the script's own verdict on itself:
            # an uncaught exception exits 1, sys.exit(2) exits 2. Re-running it
            # produces the identical failure, so mark it deterministic and let
            # db.complete_task fail the task once instead of spending all
            # MAX_ATTEMPTS on it. Function tasks already get this treatment via
            # _function_subprocess's ``"user_error": True``; script tasks were
            # simply left behind.
            #
            # A negative status (POSIX: killed by a signal) is not the script's
            # verdict. SIGKILL from the OOM killer says the machine was full,
            # not that the code is wrong, and another worker may run it fine —
            # so those keep the retry budget.
            raise TaskError(
                f"task exited with code {proc.returncode}: " + " | ".join(tail),
                user_error=proc.returncode > 0,
            )

        lines = [ln for ln in (out or "").strip().splitlines() if ln.strip()]
        if not lines:
            # Exited cleanly having printed nothing. That is the second half of
            # what the TaskError docstring calls a user error — the task
            # "returned something that cannot be sent back", here nothing at
            # all — and it is just as deterministic as a raise.
            raise TaskError("task produced no output", user_error=True)
        try:
            return json.loads(lines[-1])
        except json.JSONDecodeError:
            raise TaskError(
                f"last stdout line is not JSON: {lines[-1][:200]}",
                user_error=True,
            )



def _kill_tree(proc):
    # Every wait here is bounded and every bounded wait can raise
    # TimeoutExpired. That must not escape: callers handle TaskError, and by
    # the time this runs the caller's own ``except subprocess.TimeoutExpired``
    # has already been exited, so an escaping one lands nowhere. On a wait
    # timeout, fall through to the unconditional kill below instead — which is
    # what the Windows branch has always done.
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=5)
            return
        except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
            pass
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=5,
            )
            proc.wait(timeout=5)
            return
        except (subprocess.TimeoutExpired, OSError):
            pass
    proc.kill()
    try:
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass
