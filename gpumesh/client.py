from __future__ import annotations

"""Client-side helpers: submit jobs, poll status, pretty-print results."""

import json
import shutil
import sys
import time
import urllib.error

from .ansi import safe_print, bold, cyan, green, yellow, red, dim, reset as _reset, esc as _esc_mod
from .worker import MeshClient

# Detect ANSI support - modern terminals all support ANSI
_ANSI = sys.stdout.isatty() and sys.stdout.seekable() if hasattr(sys.stdout, 'seekable') else sys.stdout.isatty()


def _esc(code: str) -> str:
    """Return ANSI escape code if supported, else empty string."""
    return f"\033[{code}" if _ANSI else ""


def _format_result(raw, compact: bool = False) -> str:
    """Render a task result for display.

    Function results arrive wrapped in a serializer envelope, and may hold
    objects (numpy arrays, tensors) that JSON cannot express. Unwrap first,
    then fall back to repr() for anything json.dumps() rejects, so the user
    sees their actual value instead of a base64 blob.
    """
    from . import serializer

    if isinstance(raw, dict):
        raw = dict(raw)  # don't mutate the caller's job dict
        raw.pop("_task_index", None)
    try:
        value = serializer.decode_result(raw)
    except Exception:
        value = raw
    separators = (",", ":") if compact else None
    try:
        return json.dumps(value, separators=separators)
    except (TypeError, ValueError):
        return repr(value)


def submit_job(url: str, token: str, script_path: str, payloads_path: str,
               name: str = "") -> str:
    try:
        with open(script_path) as f:
            script = f.read()
    except FileNotFoundError:
        raise SystemExit(f"{_esc('31m')}[ERROR]{_esc('0m')} Script not found: {script_path}")
    try:
        with open(payloads_path) as f:
            payloads = json.load(f)
    except FileNotFoundError:
        raise SystemExit(f"{_esc('31m')}[ERROR]{_esc('0m')} Payloads file not found: {payloads_path}")
    if not isinstance(payloads, list):
        raise SystemExit(f"{_esc('31m')}[ERROR]{_esc('0m')} Payloads file must contain a JSON list")

    mesh = MeshClient(url, token)
    # Warn about large uploads
    script_size = len(script.encode('utf-8'))
    if script_size > 100_000:  # > 100KB
        print(f"{_esc('33m')}[*] Uploading large script ({script_size // 1024}KB)...{_esc('0m')}")
    resp = mesh.call("POST", "/api/jobs", {
        "name": name or script_path,
        "script": script,
        "payloads": payloads,
    })
    return resp["job_id"]


def get_status(url: str, token: str, job_id: str) -> dict:
    return MeshClient(url, token).call("GET", f"/api/jobs/{job_id}")


def cancel_job(url: str, token: str, job_id: str) -> dict | None:
    """Cancel all pending and running tasks for a job.

    Returns {"pending": N, "running": N} or None if job not found.
    Raises URLError if coordinator is unreachable.
    """
    mesh = MeshClient(url, token)
    try:
        return mesh.call("POST", "/api/cancel", {"job_id": job_id})
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def retry_job(url: str, token: str, job_id: str) -> dict | None:
    """Re-queue all failed tasks of a job so workers run them again.

    Returns {"requeued": N, "counts": {...}} or None if job not found.
    Raises URLError if coordinator is unreachable.
    """
    mesh = MeshClient(url, token)
    try:
        return mesh.call("POST", "/api/retry", {"job_id": job_id})
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def _get_workers(url: str, token: str) -> dict:
    """Fetch worker list. Returns {worker_id: worker_info}."""
    try:
        mesh = MeshClient(url, token)
        resp = mesh.call("GET", "/api/workers")
        return {w["id"]: w for w in resp.get("workers", [])}
    except (ConnectionError, OSError, json.JSONDecodeError):
        return {}


def list_devices(url: str, token: str) -> dict:
    """Fetch the unified device pool from the coordinator.

    Returns a dict with: total_devices, alive_devices, total_gpus,
    total_cpus, total_score, and a list of all devices.
    """
    try:
        mesh = MeshClient(url, token)
        return mesh.call("GET", "/api/devices")
    except urllib.error.HTTPError:
        raise
    except (ConnectionError, OSError, json.JSONDecodeError):
        return {
            "total_devices": 0,
            "alive_devices": 0,
            "total_gpus": 0,
            "total_cpus": 0,
            "total_score": 0,
            "devices": [],
        }


def _bar(done: int, total: int, width: int = 20) -> str:
    """Build a colored progress bar like '########........'.

    Uses colored chars when TTY supports it, falls back to ASCII.
    """
    if total == 0:
        return f"{_esc('90m')}{'.' * width}{_esc('0m')}"
    filled = int(width * done / total)
    empty = width - filled
    if _ANSI:
        return f"{_esc('32m')}{'#' * filled}{_esc('90m')}{'.' * empty}{_esc('0m')}"
    else:
        return f"{'#' * filled}{'.' * empty}"


def wait_for_job(url: str, token: str, job_id: str, poll: float = 2.0, timeout: float = 0.0) -> dict:
    """Wait for a job to finish, showing a rich progress display.

    Args:
        timeout: Maximum wait time in seconds. 0 = no timeout (default).
    """
    term_width = shutil.get_terminal_size().columns
    result_truncate = min(40, term_width // 4)
    # Disable ANSI when not a TTY (piped/redirected output)
    if not sys.stdout.isatty():
        global _ANSI
        _ANSI = False
    workers_cache = {}
    poll_count = 0
    start_time = time.time()
    prev_lines = 0
    max_height = 0  # Track max lines used for stable display

    while True:
        if timeout > 0 and time.time() - start_time > timeout:
            raise TimeoutError(f"Job {job_id} did not finish within {timeout}s")
        try:
            job = get_status(url, token, job_id)
        except urllib.error.HTTPError as exc:
            # 404: the job is gone (e.g. the coordinator restarted with a fresh
            # database). 401: the token is wrong. Neither will ever recover,
            # so fail fast instead of polling forever.
            if exc.code == 404:
                raise RuntimeError(
                    f"Job {job_id} not found on the coordinator — it may have "
                    "been restarted with a fresh database."
                )
            if exc.code == 401:
                raise RuntimeError(
                    f"Authentication failed for job {job_id} — the token does "
                    "not match the coordinator. Rejoin with the correct token."
                )
            time.sleep(poll)
            continue
        except (urllib.error.URLError, OSError):
            time.sleep(poll)
            continue
        if job is None:
            raise RuntimeError(f"Failed to get status for job {job_id}")
        counts = job["counts"]
        total = sum(counts.values())
        done = counts.get("done", 0) + counts.get("failed", 0)
        running = counts.get("running", 0)
        pending = counts.get("pending", 0)
        failed = counts.get("failed", 0)
        elapsed = time.time() - start_time

        # Refresh workers every 3rd poll
        poll_count += 1
        if poll_count % 3 == 1:
            workers_cache = _get_workers(url, token)

        # ── Build output ──────────────────────────────────────────────

        lines = []

        # Header
        job_name = job.get("name", job_id)
        lines.append(f"{_esc('1m')}Job:{_esc('0m')} {job_name} ({_esc('36m')}{job_id}{_esc('0m')})")
        lines.append("")

        # Progress bar
        bar = _bar(done, total)
        if total == 0:
            lines.append(f"  {_esc('90m')}No tasks yet...{_esc('0m')}")
        else:
            status_parts = [f"{_esc('32m')}{done}/{total} done{_esc('0m')}"]
            if failed:
                status_parts.append(f"{_esc('31m')}{failed} failed{_esc('0m')}")
            if running:
                status_parts.append(f"{_esc('33m')}{running} running{_esc('0m')}")
            if pending:
                status_parts.append(f"{_esc('90m')}{pending} pending{_esc('0m')}")

            # Percent complete tracks the bar (done + failed are terminal).
            pct = int(100 * done / total)
            lines.append(
                f"  {bar} "
                f"{_esc('1m')}{_esc('36m')}{pct:>3}%{_esc('0m')}  "
                f"{', '.join(status_parts)}  {_esc('90m')}({elapsed:.0f}s){_esc('0m')}"
            )

        # Per-worker status
        worker_tasks = {}
        for t in job.get("tasks", []):
            wid = t.get("worker_id")
            if wid and t["status"] == "running":
                worker_tasks[wid] = worker_tasks.get(wid, 0) + 1

        if worker_tasks:
            lines.append("")
            for wid, count in sorted(worker_tasks.items(), key=lambda x: -x[1]):
                winfo = workers_cache.get(wid, {})
                # Show device_name if available, else hostname
                display = winfo.get("device_name") or winfo.get("hostname", wid)
                display = display[:16]
                wb = _bar(count, max(count, 5), 10)
                lines.append(
                    f"  {_esc('36m')}{wid[:8]}{_esc('0m')}: "
                    f"{display:<16} "
                    f"{wb} {_esc('33m')}{count} running{_esc('0m')}"
                )

        # Recent results
        results = []
        for t in job.get("tasks", []):
            if t["status"] == "done" and t.get("result"):
                text = _format_result(t["result"], compact=True)
                if len(text) > result_truncate:
                    text = text[:result_truncate - 3] + "..."
                results.append(text)

        if results:
            recent = results[-3:]
            ellipsis = " ..." if len(results) > 3 else ""
            lines.append("")
            lines.append(f"  Results: {', '.join(recent)}{ellipsis}")

        # Pad to max height for stable display
        while len(lines) < max_height:
            lines.append("")
        max_height = max(max_height, len(lines))

        # ── Print (overwrite previous output) ─────────────────────────

        if _ANSI and prev_lines > 0:
            sys.stdout.write(f"\033[{prev_lines}A")

        for line in lines:
            erase = "\033[2K" if _ANSI else ""
            sys.stdout.write(f"{erase}{line}\n")
        sys.stdout.flush()
        prev_lines = len(lines)

        if job["finished"]:
            # Clear progress block and print final results
            if _ANSI and prev_lines > 0:
                for _ in range(prev_lines):
                    sys.stdout.write("\033[2K\033[1B")
                sys.stdout.write(f"\033[{prev_lines}A")
            print_job(job)
            return job
        time.sleep(poll)


def print_job(job: dict):
    """Print final job results in a clean format."""
    safe_print(f"{_esc('1m')}Job:{_esc('0m')} {job['name']} ({_esc('36m')}{job['id']}{_esc('0m')})")
    status = f"{_esc('32m')}finished{_esc('0m')}" if job["finished"] else f"{_esc('33m')}running{_esc('0m')}"
    safe_print(f"  Status: {status}")
    safe_print(f"  Counts: {job['counts']}")
    safe_print()

    for t in job["tasks"]:
        s = t["status"]
        if s == "done":
            icon = f"{_esc('32m')}\u2713{_esc('0m')}"
        elif s == "failed":
            icon = f"{_esc('31m')}\u2717{_esc('0m')}"
        elif s == "running":
            icon = f"{_esc('33m')}\u21bb{_esc('0m')}"
        else:
            icon = f"{_esc('90m')}\u00b7{_esc('0m')}"

        worker = f"  {_esc('36m')}worker={_esc('0m')}{t['worker_id']}" if t.get("worker_id") else ""
        safe_print(f"  {icon} task {t['id']:<12} [{s}]  cost={t['cost']}{worker}")
        if t["result"] is not None:
            safe_print(f"    result: {_format_result(t['result'])}")
        if t.get("error"):
            safe_print(f"    {_esc('31m')}error: {t['error']}{_esc('0m')}")
