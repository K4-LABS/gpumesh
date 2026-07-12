"""Client-side helpers: submit jobs, poll status, pretty-print results."""

import json
import os
import shutil
import sys
import time

from .worker import MeshClient

# Detect ANSI support - modern terminals all support ANSI
_ANSI = sys.stdout.isatty()


def _esc(code: str) -> str:
    """Return ANSI escape code if supported, else empty string."""
    return code if _ANSI else ""


def submit_job(url: str, token: str, script_path: str, payloads_path: str,
               name: str = "") -> str:
    with open(script_path) as f:
        script = f.read()
    with open(payloads_path) as f:
        payloads = json.load(f)
    if not isinstance(payloads, list):
        raise SystemExit("payloads file must contain a JSON list")

    mesh = MeshClient(url, token)
    resp = mesh.call("POST", "/api/jobs", {
        "name": name or script_path,
        "script": script,
        "payloads": payloads,
    })
    return resp["job_id"]


def get_status(url: str, token: str, job_id: str) -> dict:
    return MeshClient(url, token).call("GET", f"/api/jobs/{job_id}")


def _get_workers(url: str, token: str) -> dict:
    """Fetch worker list. Returns {worker_id: worker_info}."""
    try:
        mesh = MeshClient(url, token)
        resp = mesh.call("GET", "/api/workers")
        return {w["id"]: w for w in resp.get("workers", [])}
    except (ConnectionError, OSError, json.JSONDecodeError):
        return {}


def _bar(done: int, total: int, width: int = 20) -> str:
    """Build a progress bar like '████████████░░░░░░░░'."""
    if total == 0:
        return "░" * width
    filled = int(width * done / total)
    return "█" * filled + "░" * (width - filled)


def wait_for_job(url: str, token: str, job_id: str, poll: float = 2.0) -> dict:
    """Wait for a job to finish, showing a rich progress display."""
    term_width = shutil.get_terminal_size().columns
    result_truncate = min(40, term_width // 4)
    workers_cache = {}
    poll_count = 0
    start_time = time.time()
    prev_lines = 0
    max_height = 0  # Track max lines used for stable display

    while True:
        job = get_status(url, token, job_id)
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
            sys.stdout.write(f"\033[2K{line}\n")
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
    print(f"{_esc('1m')}Job:{_esc('0m')} {job['name']} ({job['id']})")
    status = "finished" if job["finished"] else "running"
    print(f"  Status: {status}")
    print(f"  Counts: {job['counts']}")
    print()

    for t in job["tasks"]:
        s = t["status"]
        if s == "done":
            icon = f"{_esc('32m')}✓{_esc('0m')}"
        elif s == "failed":
            icon = f"{_esc('31m')}✗{_esc('0m')}"
        elif s == "running":
            icon = f"{_esc('33m')}↻{_esc('0m')}"
        else:
            icon = f"{_esc('90m')}·{_esc('0m')}"

        worker = f"  worker={t['worker_id']}" if t.get("worker_id") else ""
        print(f"  {icon} task {t['id']:<12} [{s}]  cost={t['cost']}{worker}")
        if t["result"] is not None:
            print(f"    result: {json.dumps(t['result'])}")
        if t.get("error"):
            print(f"    error: {t['error']}")
