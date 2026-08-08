from __future__ import annotations

"""SQLite persistence layer for the coordinator.

Single connection guarded by a lock (the coordinator's HTTP server is
threaded). WAL mode keeps readers from blocking the writer.
"""

import json
import sqlite3
import threading
import time
import uuid

from .ansi import safe_print, green, yellow, red, bold, dim

SCHEMA = """
CREATE TABLE IF NOT EXISTS workers (
    id            TEXT PRIMARY KEY,
    hostname      TEXT NOT NULL,
    device        TEXT NOT NULL,
    device_name   TEXT NOT NULL DEFAULT '',
    score         REAL NOT NULL,
    registered_at REAL NOT NULL,
    last_seen     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    script     TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    job_id        TEXT NOT NULL REFERENCES jobs(id),
    payload       TEXT NOT NULL,
    cost          REAL NOT NULL DEFAULT 1.0,
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|failed
    worker_id     TEXT,
    lease_expires REAL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    result        TEXT,
    error         TEXT
);

CREATE TABLE IF NOT EXISTS worker_stats (
    worker_id     TEXT PRIMARY KEY,
    tasks_completed INTEGER NOT NULL DEFAULT 0,
    total_time    REAL NOT NULL DEFAULT 0.0,
    avg_time      REAL NOT NULL DEFAULT 0.0,
    last_task_time REAL NOT NULL DEFAULT 0.0,
    gpu_memory_free_mb REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    type       TEXT NOT NULL,
    worker_id  TEXT NOT NULL,
    time       REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_time ON events(time DESC);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_job ON tasks(job_id);
"""

WORKER_DEAD_AFTER = 30.0   # seconds without a heartbeat
WORKER_DELETE_AFTER = 300.0 # seconds before stale worker row is deleted (Hivemind TTL)
LEASE_SECONDS = 300.0      # task lease before it is re-queued
MAX_ATTEMPTS = 3


class Database:
    def __init__(self, path: str):
        self._path = path
        self._conn = self._open_db(path)
        self._lock = threading.Lock()

    @staticmethod
    def _open_db(path: str):
        try:
            conn = sqlite3.connect(path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(SCHEMA)
            # Migrate: add device_name column to existing databases
            try:
                conn.execute("SELECT device_name FROM workers LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute(
                    "ALTER TABLE workers ADD COLUMN device_name TEXT NOT NULL DEFAULT ''"
                )
            # Migrate: ensure worker_stats table exists (may not be in old schema)
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS worker_stats (
                    worker_id     TEXT PRIMARY KEY,
                    tasks_completed INTEGER NOT NULL DEFAULT 0,
                    total_time    REAL NOT NULL DEFAULT 0.0,
                    avg_time      REAL NOT NULL DEFAULT 0.0,
                    last_task_time REAL NOT NULL DEFAULT 0.0,
                    gpu_memory_free_mb REAL NOT NULL DEFAULT 0.0
                );
            """)
            # Migrate: add gpu_memory_free_mb column to existing databases
            try:
                conn.execute("SELECT gpu_memory_free_mb FROM worker_stats LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute(
                    "ALTER TABLE worker_stats ADD COLUMN gpu_memory_free_mb REAL NOT NULL DEFAULT 0.0"
                )
            return conn
        except sqlite3.DatabaseError as exc:
            import os, shutil
            backup_path = path + ".corrupted"
            safe_print(f"{bold('[gpumesh]')} {yellow('WARNING')}: Database corrupted ({exc})")
            try:
                conn.close()
            except (NameError, Exception):
                pass
            # Backup the corrupted file before deleting
            try:
                shutil.copy2(path, backup_path)
                safe_print(f"{bold('[gpumesh]')} Backup saved to: {green(backup_path)}")
                for suffix in ("-wal", "-shm", "-journal"):
                    try:
                        shutil.copy2(path + suffix, backup_path + suffix)
                    except FileNotFoundError:
                        pass
            except Exception:
                pass
            # Remove corrupted files
            try:
                os.remove(path)
                for suffix in ("-wal", "-shm", "-journal"):
                    try:
                        os.remove(path + suffix)
                    except FileNotFoundError:
                        pass
            except (FileNotFoundError, PermissionError):
                pass
            safe_print(f"{bold('[gpumesh]')} {green('Creating fresh database...')}")
            conn = sqlite3.connect(path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(SCHEMA)
            return conn

    def close(self):
        """Close the database connection and flush WAL."""
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except Exception:
                pass
            try:
                self._conn.close()
            except Exception:
                pass

    # -- workers ------------------------------------------------------------

    def register_worker(self, hostname: str, device: str, score: float,
                        device_name: str = "") -> str:
        worker_id = uuid.uuid4().hex[:12]
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO workers (id, hostname, device, device_name, score,"
                " registered_at, last_seen)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (worker_id, hostname, device, device_name, score, now, now),
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO worker_stats (worker_id)"
                " VALUES (?)",
                (worker_id,),
            )
        return worker_id

    def heartbeat(self, worker_id: str, task_id: str | None = None,
                   score: float | None = None,
                   gpu_memory_free_mb: float | None = None) -> bool:
        now = time.time()
        with self._lock, self._conn:
            if score is not None:
                cur = self._conn.execute(
                    "UPDATE workers SET last_seen = ?, score = ? WHERE id = ?",
                    (now, score, worker_id),
                )
            else:
                cur = self._conn.execute(
                    "UPDATE workers SET last_seen = ? WHERE id = ?",
                    (now, worker_id),
                )
            if cur.rowcount > 0 and task_id:
                self._conn.execute(
                    "UPDATE tasks SET lease_expires = ?"
                    " WHERE id = ? AND worker_id = ? AND status = 'running'",
                    (now + LEASE_SECONDS, task_id, worker_id),
                )
            # Store latest GPU free memory for memory-aware scheduling (HAMi pattern)
            if cur.rowcount > 0 and gpu_memory_free_mb is not None:
                self._conn.execute(
                    "INSERT INTO worker_stats (worker_id, gpu_memory_free_mb)"
                    " VALUES (?, ?)"
                    " ON CONFLICT(worker_id) DO UPDATE SET gpu_memory_free_mb = ?",
                    (worker_id, gpu_memory_free_mb, gpu_memory_free_mb),
                )
        return cur.rowcount > 0

    def list_workers(self) -> list:
        with self._lock:
            cutoff = time.time() - WORKER_DEAD_AFTER
            rows = self._conn.execute(
                "SELECT id, hostname, device, device_name, score, last_seen"
                " FROM workers"
            ).fetchall()
        return [
            {
                "id": r[0],
                "hostname": r[1],
                "device": r[2],
                "device_name": r[3],
                "score": r[4],
                "alive": r[5] >= cutoff,
            }
            for r in rows
        ]

    def _active_scores(self, cutoff: float) -> list:
        rows = self._conn.execute(
            "SELECT score FROM workers WHERE last_seen >= ?", (cutoff,)
        ).fetchall()
        return [r[0] for r in rows]

    def list_devices(self) -> list:
        """Return all devices (workers) as a unified pool.

        Each device includes: id, hostname, device, device_name, score,
        status (alive/dead), and a virtual device index for the user.
        """
        with self._lock:
            cutoff = time.time() - WORKER_DEAD_AFTER
            rows = self._conn.execute(
                "SELECT id, hostname, device, device_name, score, last_seen"
                " FROM workers ORDER BY last_seen DESC"
            ).fetchall()
        devices = []
        for idx, r in enumerate(rows):
            devices.append({
                "index": idx,
                "id": r[0],
                "hostname": r[1],
                "device": r[2],
                "device_name": r[3],
                "score": r[4],
                "status": "alive" if r[5] >= cutoff else "dead",
            })
        return devices

    def device_summary(self) -> dict:
        """Return a summary of all available compute resources."""
        devices = self.list_devices()
        alive = [d for d in devices if d["status"] == "alive"]
        total_gpus = sum(1 for d in alive if d["device"] in ("cuda", "mps"))
        total_cpus = sum(1 for d in alive if d["device"] == "cpu")
        total_score = sum(d["score"] for d in alive)
        return {
            "total_devices": len(devices),
            "alive_devices": len(alive),
            "total_gpus": total_gpus,
            "total_cpus": total_cpus,
            "total_score": round(total_score, 2),
            "devices": devices,
        }

    def worker_ranking(self) -> list[dict]:
        """Return workers sorted by score (descending) with percentile info.

        Each entry includes: id, hostname, device, device_name, score,
        percentile (0-100, higher = better), and alive status.
        Useful for load balancing, diagnostics, and display.
        """
        cutoff = time.time() - WORKER_DEAD_AFTER
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, hostname, device, device_name, score, last_seen"
                " FROM workers ORDER BY score DESC"
            ).fetchall()
        if not rows:
            return []
        alive_scores = [r[4] for r in rows if r[5] >= cutoff]
        alive_scores.sort()
        n = len(alive_scores)
        result = []
        for r in rows:
            alive = r[5] >= cutoff
            if alive and n > 0:
                rank_idx = sum(1 for s in alive_scores if s <= r[4])
                percentile = round(rank_idx / n * 100, 1)
            else:
                percentile = 0.0
            result.append({
                "id": r[0],
                "hostname": r[1],
                "device": r[2],
                "device_name": r[3],
                "score": r[4],
                "percentile": percentile,
                "alive": alive,
            })
        return result

    # -- TTL cleanup (Hivemind pattern) ------------------------------------

    def cleanup_dead_workers(self) -> list[str]:
        """Delete workers not seen for WORKER_DELETE_AFTER seconds.

        Returns the list of deleted worker IDs so callers can log topology
        changes.  Unlike list_workers() which only marks workers dead/alive,
        this actually removes stale rows from the database.
        """
        cutoff = time.time() - WORKER_DELETE_AFTER
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT id FROM workers WHERE last_seen < ?", (cutoff,)
            ).fetchall()
            if rows:
                ids = [r[0] for r in rows]
                self._conn.execute(
                    "DELETE FROM workers WHERE last_seen < ?", (cutoff,)
                )
                return ids
            return []

    # -- events (Exo pattern) -----------------------------------------------

    def record_event(self, event_type: str, worker_id: str):
        """Record a worker join/leave event."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO events (type, worker_id, time) VALUES (?, ?, ?)",
                (event_type, worker_id, time.time()),
            )

    def list_events(self, limit: int = 100) -> list[dict]:
        """Return the most recent events (newest first)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT type, worker_id, time FROM events"
                " ORDER BY time DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"type": r[0], "worker_id": r[1], "time": r[2]}
            for r in rows
        ]

    # -- jobs ---------------------------------------------------------------

    def create_job(self, name: str, script: str, payloads: list) -> str:
        job_id = uuid.uuid4().hex[:12]
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO jobs (id, name, script, created_at) VALUES (?, ?, ?, ?)",
                (job_id, name, script, time.time()),
            )
            for item in payloads:
                try:
                    cost = float(item.get("cost", 1.0)) if isinstance(item, dict) else 1.0
                except (ValueError, TypeError):
                    cost = 1.0
                self._conn.execute(
                    "INSERT INTO tasks (id, job_id, payload, cost) VALUES (?, ?, ?, ?)",
                    (uuid.uuid4().hex[:12], job_id, json.dumps(item), cost),
                )
        return job_id

    def job_status(self, job_id: str):
        with self._lock:
            job = self._conn.execute(
                "SELECT id, name, created_at FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job is None:
                return None
            tasks = self._conn.execute(
                "SELECT id, status, cost, worker_id, result, error, attempts"
                " FROM tasks WHERE job_id = ? ORDER BY rowid",
                (job_id,),
            ).fetchall()
        task_list = []
        for t in tasks:
            try:
                result = json.loads(t[4]) if t[4] else None
            except json.JSONDecodeError:
                result = {"_error": "malformed result"}
            task_list.append({
                "id": t[0],
                "status": t[1],
                "cost": t[2],
                "worker_id": t[3],
                "result": result,
                "error": t[5],
                "attempts": t[6],
            })
        counts = {}
        for t in task_list:
            counts[t["status"]] = counts.get(t["status"], 0) + 1
        done = all(t["status"] in ("done", "failed") for t in task_list)
        return {
            "id": job[0],
            "name": job[1],
            "created_at": job[2],
            "finished": done,
            "counts": counts,
            "tasks": task_list,
        }

    def job_status_counts(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) FROM tasks GROUP BY status"
            ).fetchall()
        counts = {"pending": 0, "running": 0, "done": 0, "failed": 0}
        for status, count in rows:
            counts[status] = count
        return counts

    def cancel_job(self, job_id: str) -> dict | None:
        """Cancel all pending and running tasks for a job.

        Returns {"pending": N, "running": N} or None if job not found.
        """
        with self._lock, self._conn:
            job = self._conn.execute(
                "SELECT id FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job is None:
                return None

            cur_pending = self._conn.execute(
                "UPDATE tasks SET status = 'failed', error = 'cancelled'"
                " WHERE job_id = ? AND status = 'pending'",
                (job_id,),
            )
            cur_running = self._conn.execute(
                "UPDATE tasks SET status = 'failed', error = 'cancelled',"
                " worker_id = NULL"
                " WHERE job_id = ? AND status = 'running'",
                (job_id,),
            )
        return {
            "pending": cur_pending.rowcount,
            "running": cur_running.rowcount,
        }

    def retry_job(self, job_id: str) -> dict | None:
        """Re-queue all failed tasks of a job so workers run them again.

        Failed (and timed-out) tasks are reset to 'pending' with a fresh
        attempt budget (attempts = 0) and their error cleared. Done, running
        and pending tasks are left untouched.

        Returns {"requeued": N, "counts": {...}} or None if job not found.
        """
        with self._lock, self._conn:
            job = self._conn.execute(
                "SELECT id FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job is None:
                return None

            cur = self._conn.execute(
                "UPDATE tasks SET status = 'pending', error = NULL,"
                " worker_id = NULL, lease_expires = NULL, result = NULL,"
                " attempts = 0"
                " WHERE job_id = ? AND status = 'failed'",
                (job_id,),
            )
        counts = self.job_status(job_id)["counts"]
        return {"requeued": cur.rowcount, "counts": counts}

    # -- scheduling ---------------------------------------------------------

    def lease_task(self, worker_id: str):
        """Assign a pending task matched to this worker's relative strength.

        The worker's score percentile among live workers selects a matching
        percentile in the pending tasks sorted by cost: strong workers get
        heavy tasks, weak workers get light ones.

        Straggler tolerance (Petals/Swarm-Tune pattern): workers with
        avg_time > 2x the median avg_time are deprioritized — they get
        lighter tasks to reduce the impact of their slowness.

        Memory-aware scheduling (HAMi pattern): if a task's payload contains
        a ``gpu_memory_mb`` hint, prefer workers with enough free GPU memory.
        Workers with insufficient free memory are filtered out before scoring.
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT score FROM workers WHERE id = ?", (worker_id,)
            ).fetchone()
            if row is None:
                return None
            my_score = row[0]

            # -- gather straggler stats (median avg_time across active workers)
            stats_rows = self._conn.execute(
                "SELECT avg_time FROM worker_stats WHERE avg_time > 0"
            ).fetchall()
            avg_times = sorted([r[0] for r in stats_rows])
            median_avg = avg_times[len(avg_times) // 2] if avg_times else 0.0

            my_stats = self._conn.execute(
                "SELECT avg_time FROM worker_stats WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
            is_straggler = (
                my_stats is not None
                and median_avg > 0
                and my_stats[0] > 2 * median_avg
            )

            # -- get my free GPU memory (stored by heartbeat via gpu_memory_free_mb column)
            my_mem_row = self._conn.execute(
                "SELECT gpu_memory_free_mb FROM worker_stats WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
            my_free_mb = my_mem_row[0] if my_mem_row else None

            # -- fetch pending tasks; filter by memory hint if present
            raw_tasks = self._conn.execute(
                "SELECT id, cost, payload FROM tasks WHERE status = 'pending' ORDER BY cost ASC"
            ).fetchall()
            if not raw_tasks:
                return None

            # Filter tasks by gpu_memory_mb hint (HAMi Filter pattern)
            eligible_tasks = []
            for t in raw_tasks:
                try:
                    payload = json.loads(t[2])
                except (json.JSONDecodeError, TypeError):
                    payload = {}
                mem_hint = payload.get("gpu_memory_mb")
                if mem_hint is not None and my_free_mb is not None and my_free_mb < mem_hint:
                    continue  # not enough memory — skip this task
                eligible_tasks.append(t)

            if not eligible_tasks:
                return None

            # Straggler deprioritization: stragglers get lighter tasks
            if is_straggler and len(eligible_tasks) > 1:
                # Take from the bottom half (lighter tasks)
                half = len(eligible_tasks) // 2
                eligible_tasks = eligible_tasks[:half] if half > 0 else eligible_tasks[:1]

            cutoff_active = time.time() - WORKER_DEAD_AFTER
            scores = self._active_scores(cutoff_active)
            if len(scores) <= 1:
                task_id = eligible_tasks[-1][0]  # solo worker: take the heaviest
            else:
                rank = sum(1 for s in scores if s <= my_score) / len(scores)
                idx = min(int(rank * len(eligible_tasks)), len(eligible_tasks) - 1)
                task_id = eligible_tasks[idx][0]

            cur = self._conn.execute(
                "UPDATE tasks SET status = 'running', worker_id = ?,"
                " lease_expires = ?, attempts = attempts + 1"
                " WHERE id = ? AND status = 'pending'",
                (worker_id, time.time() + LEASE_SECONDS, task_id),
            )
            if cur.rowcount == 0:
                return None

            task = self._conn.execute(
                "SELECT t.id, t.job_id, t.payload, t.cost, j.script"
                " FROM tasks t JOIN jobs j ON j.id = t.job_id WHERE t.id = ?",
                (task_id,),
            ).fetchone()
        try:
            payload = json.loads(task[2])
        except json.JSONDecodeError:
            payload = {}
        return {
            "task_id": task[0],
            "job_id": task[1],
            "payload": payload,
            "cost": task[3],
            "script": task[4],
        }

    def complete_task(self, task_id: str, worker_id: str, ok: bool,
                      result=None, error: str = "",
                      elapsed: float | None = None) -> bool:
        with self._lock, self._conn:
            if ok:
                cur = self._conn.execute(
                    "UPDATE tasks SET status = 'done', result = ?, error = NULL"
                    " WHERE id = ? AND worker_id = ? AND status = 'running'",
                    (json.dumps(result), task_id, worker_id),
                )
                if cur.rowcount == 0:
                    cur = self._conn.execute(
                        "UPDATE tasks SET status = 'done', result = ?, error = NULL"
                        " WHERE id = ? AND status = 'pending'",
                        (json.dumps(result), task_id),
                    )
            else:
                row = self._conn.execute(
                    "SELECT attempts FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                final = row is not None and row[0] >= MAX_ATTEMPTS
                cur = self._conn.execute(
                    "UPDATE tasks SET status = ?, error = ?, worker_id = NULL"
                    " WHERE id = ? AND worker_id = ? AND status = 'running'",
                    ("failed" if final else "pending", error, task_id, worker_id),
                )
                if cur.rowcount == 0:
                    cur = self._conn.execute(
                        "UPDATE tasks SET status = ?, error = ?, worker_id = NULL"
                        " WHERE id = ? AND status = 'pending'",
                        ("failed" if final else "pending", error, task_id),
                    )
            # Update straggler stats (Petals pattern) — only on success
            if ok and elapsed is not None and elapsed > 0:
                existing = self._conn.execute(
                    "SELECT tasks_completed, total_time FROM worker_stats"
                    " WHERE worker_id = ?",
                    (worker_id,),
                ).fetchone()
                if existing:
                    new_count = existing[0] + 1
                    new_total = existing[1] + elapsed
                    new_avg = new_total / new_count
                    self._conn.execute(
                        "UPDATE worker_stats SET tasks_completed = ?,"
                        " total_time = ?, avg_time = ?"
                        " WHERE worker_id = ?",
                        (new_count, new_total, new_avg, worker_id),
                    )
                else:
                    self._conn.execute(
                        "INSERT INTO worker_stats"
                        " (worker_id, tasks_completed, total_time, avg_time)"
                        " VALUES (?, 1, ?, ?)",
                        (worker_id, elapsed, elapsed),
                    )
        return cur.rowcount > 0

    def reap_expired_leases(self) -> int:
        """Re-queue tasks whose worker died or whose lease ran out.

        Also tracks straggler stats: when a lease expires, we record the
        elapsed time (lease_duration * attempts) so slow workers are
        identified even when they never successfully complete tasks.
        """
        now = time.time()
        cutoff = now - WORKER_DEAD_AFTER
        with self._lock, self._conn:
            # Gather info about tasks that will be re-queued (for straggler tracking)
            expiring = self._conn.execute(
                "SELECT worker_id, attempts, lease_expires FROM tasks"
                " WHERE status = 'running' AND (lease_expires < ? OR worker_id IN"
                "   (SELECT id FROM workers WHERE last_seen < ?))"
                " AND attempts < ?",
                (now, cutoff, MAX_ATTEMPTS),
            ).fetchall()

            cur = self._conn.execute(
                "UPDATE tasks SET status = 'pending', worker_id = NULL"
                " WHERE status = 'running' AND (lease_expires < ? OR worker_id IN"
                "   (SELECT id FROM workers WHERE last_seen < ?))"
                " AND attempts < ?",
                (now, cutoff, MAX_ATTEMPTS),
            )
            requeued = cur.rowcount

            # Record straggler stats for workers whose tasks expired
            # Use LEASE_SECONDS * attempts as a conservative lower bound
            for wid, attempts, _ in expiring:
                if wid:
                    estimated_time = LEASE_SECONDS * attempts
                    existing = self._conn.execute(
                        "SELECT tasks_completed, total_time FROM worker_stats"
                        " WHERE worker_id = ?",
                        (wid,),
                    ).fetchone()
                    if existing:
                        new_count = existing[0] + 1
                        new_total = existing[1] + estimated_time
                        new_avg = new_total / new_count
                        self._conn.execute(
                            "UPDATE worker_stats SET tasks_completed = ?,"
                            " total_time = ?, avg_time = ?"
                            " WHERE worker_id = ?",
                            (new_count, new_total, new_avg, wid),
                        )
                    else:
                        self._conn.execute(
                            "INSERT INTO worker_stats"
                            " (worker_id, tasks_completed, total_time, avg_time)"
                            " VALUES (?, 1, ?, ?)",
                            (wid, estimated_time, estimated_time),
                        )

            self._conn.execute(
                "UPDATE tasks SET status = 'failed', error = 'lease expired, max attempts'"
                " WHERE status = 'running' AND (lease_expires < ? OR worker_id IN"
                "   (SELECT id FROM workers WHERE last_seen < ?))"
                " AND attempts >= ?",
                (now, cutoff, MAX_ATTEMPTS),
            )
        return requeued

    def cancel_worker_tasks(self, worker_id: str, force: bool = False) -> dict:
        """Cancel tasks for a specific worker.

        If force=True, cancel both running and pending tasks.
        If force=False, only cancel running tasks assigned to this worker
        (let them finish, pending tasks are unaffected).
        """
        with self._lock, self._conn:
            # Only cancel running tasks assigned to this worker
            cur_running = self._conn.execute(
                "UPDATE tasks SET status = 'failed', error = 'killed',"
                " worker_id = NULL"
                " WHERE worker_id = ? AND status = 'running'",
                (worker_id,),
            )
            # When force=True, also cancel pending tasks (not assigned to any worker)
            cur_pending = self._conn.execute(
                "UPDATE tasks SET status = 'failed', error = 'killed'"
                " WHERE status = 'pending'"
                + (" AND 1=1" if force else " AND 0=1"),
            )
        return {
            "pending": cur_pending.rowcount,
            "running": cur_running.rowcount,
        }

    def cancel_all_tasks(self, force: bool = False) -> dict:
        """Cancel all tasks across all workers.

        If force=True, cancel immediately (running + pending).
        If force=False, only cancel pending tasks (let running finish).
        """
        with self._lock, self._conn:
            cur_pending = self._conn.execute(
                "UPDATE tasks SET status = 'failed', error = 'killed'"
                " WHERE status = 'pending'",
            )
            cur_running = self._conn.execute(
                "UPDATE tasks SET status = 'failed', error = 'killed',"
                " worker_id = NULL"
                " WHERE status = 'running'"
                + (" AND 1=1" if force else " AND 0=1"),
            )
        return {
            "pending": cur_pending.rowcount,
            "running": cur_running.rowcount,
        }
