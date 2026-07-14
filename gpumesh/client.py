from __future__ import annotations

"""Client-side helpers: submit jobs, poll status, pretty-print results."""

import json
import shutil
import sys
import time
import urllib.error

from .ansi import safe_print
from .worker import MeshClient

# Detect ANSI support - modern terminals all support ANSI
_ANSI = sys.stdout.isatty() and sys.stdout.seekable() if hasattr(sys.stdout, 'seekable') else sys.stdout.isatty()


def _esc(code: str) -> str:
    """Return ANSI escape code if supported, else empty string."""
    return f"\033[{code}" if _ANSI else ""


def submit_job(url: str, token: str, script_path: str, payloads_path: str,
               name: str = "") -> str:
    try:
        with open(script_path) as f:
            script = f.read()
    except FileNotFoundError:
        raise SystemExit(f"Script not found: {script_path}")
    try:
        with open(payloads_path) as f:
            payloads = json.load(f)
    except FileNotFoundError:
        raise SystemExit(f"Payloads file not found: {payloads_path}")
    if not isinstance(payloads, list):
        raise SystemExit("payloads file must contain a JSON list")

    mesh = MeshClient(url, token)
    # Warn about large uploads
    script_size = len(script.encode('utf-8'))
    if script_size > 100_000:  # > 100KB
        print(f"[gpumesh] Uploading large script ({script_size // 1024}KB)...")
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
    """Build a progress bar like '################....'."""
    if total == 0:
        return "." * width
    filled = int(width * done / total)
    return "#" * filled + "." * (width - filled)


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
        job = get_status(url, token, job_id)
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
        lines.append(f"{_esc('1m')}Job:{_esc('0m')} {job_name} ({job_id})")
        lines.append("")

        # Progress bar
        bar = _bar(done, total)
        if total == 0:
            lines.append(f"  No tasks yet...")
        else:
            status_parts = [f"{done}/{total} done"]
            if failed:
                status_parts.append(f"{failed} failed")
            if running:
                status_parts.append(f"{running} running")
            if pending:
                status_parts.append(f"{pending} pending")

            lines.append(
                f"  [{_esc('32m')}{bar}{_esc('0m')}] "
                f"{', '.join(status_parts)}  ({elapsed:.0f}s)"
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
                    f"  {_esc('36m')}{wid}{_esc('0m')}: "
                    f"{display:<16} "
                    f"{wb} {count} running"
                )

        # Recent results
        results = []
        for t in job.get("tasks", []):
            if t["status"] == "done" and t.get("result"):
                text = json.dumps(t["result"], separators=(",", ":"))
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
    safe_print(f"{_esc('1m')}Job:{_esc('0m')} {job['name']} ({job['id']})")
    status = "finished" if job["finished"] else "running"
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

        worker = f"  worker={t['worker_id']}" if t.get("worker_id") else ""
        safe_print(f"  {icon} task {t['id']:<12} [{s}]  cost={t['cost']}{worker}")
        if t["result"] is not None:
            safe_print(f"    result: {json.dumps(t['result'])}")
        if t.get("error"):
            safe_print(f"    error: {t['error']}")
