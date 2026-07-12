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


class TaskError(Exception):
    pass


def _posix_limits(cpu_seconds: int):
    def apply():
        try:
            import resource

            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        except Exception:
            pass

    return apply


def run_task(script: str, payload, timeout: float = 240.0,
             cpu_seconds: int = 600, device: str = "cpu") -> dict:
    """Execute `script` with `payload`; return the parsed JSON result."""
    with tempfile.TemporaryDirectory(prefix="gpumesh-task-") as workdir:
        script_path = os.path.join(workdir, "task.py")
        with open(script_path, "w") as f:
            f.write(script)

        env = dict(os.environ, GPUMESH_DEVICE=device)
        kwargs = {}
        if os.name == "posix":
            kwargs["preexec_fn"] = _posix_limits(cpu_seconds)
            kwargs["start_new_session"] = True

        proc = subprocess.Popen(
            [sys.executable, script_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workdir,
            env=env,
            text=True,
            **kwargs,
        )
        try:
            out, err = proc.communicate(json.dumps(payload), timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            raise TaskError(f"task timed out after {timeout}s")

        if proc.returncode != 0:
            tail = (err or "").strip().splitlines()[-5:]
            raise TaskError(
                f"task exited with code {proc.returncode}: " + " | ".join(tail)
            )

        lines = [ln for ln in (out or "").strip().splitlines() if ln.strip()]
        if not lines:
            raise TaskError("task produced no output")
        try:
            return json.loads(lines[-1])
        except json.JSONDecodeError:
            raise TaskError(f"last stdout line is not JSON: {lines[-1][:200]}")


def _kill_tree(proc):
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError):
            pass
    proc.kill()
    # On Windows, wait for process to fully terminate so file handles
    # are released before TemporaryDirectory cleanup
    if os.name == "nt":
        try:
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass
