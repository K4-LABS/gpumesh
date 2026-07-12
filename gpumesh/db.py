"""SQLite persistence layer for the coordinator.

Single connection guarded by a lock (the coordinator's HTTP server is
threaded). WAL mode keeps readers from blocking the writer.
"""

import json
import sqlite3
import threading
import time
import uuid

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

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_job ON tasks(job_id);
"""

WORKER_DEAD_AFTER = 30.0   # seconds without a heartbeat
LEASE_SECONDS = 300.0      # task lease before it is re-queued
MAX_ATTEMPTS = 3


class Database:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        # Migrate: add device_name column to existing databases
        try:
            self._conn.execute("SELECT device_name FROM workers LIMIT 1")
        except sqlite3.OperationalError:
            self._conn.execute(
                "ALTER TABLE workers ADD COLUMN device_name TEXT NOT NULL DEFAULT ''"
            )
        self._lock = threading.Lock()

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
        return worker_id

    def heartbeat(self, worker_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE workers SET last_seen = ? WHERE id = ?",
                (time.time(), worker_id),
            )
        return cur.rowcount > 0

    def list_workers(self) -> list:
        cutoff = time.time() - WORKER_DEAD_AFTER
        with self._lock:
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

    def _active_scores(self) -> list:
        cutoff = time.time() - WORKER_DEAD_AFTER
        rows = self._conn.execute(
            "SELECT score FROM workers WHERE last_seen >= ?", (cutoff,)
        ).fetchall()
        return [r[0] for r in rows]

    # -- jobs ---------------------------------------------------------------

    def create_job(self, name: str, script: str, payloads: list) -> str:
        job_id = uuid.uuid4().hex[:12]
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO jobs (id, name, script, created_at) VALUES (?, ?, ?, ?)",
                (job_id, name, script, time.time()),
            )
            for item in payloads:
                cost = float(item.get("cost", 1.0)) if isinstance(item, dict) else 1.0
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
        task_list = [
            {
                "id": t[0],
                "status": t[1],
                "cost": t[2],
                "worker_id": t[3],
                "result": json.loads(t[4]) if t[4] else None,
                "error": t[5],
                "attempts": t[6],
            }
            for t in tasks
        ]
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

    # -- scheduling ---------------------------------------------------------

    def lease_task(self, worker_id: str):
        """Assign a pending task matched to this worker's relative strength.

        The worker's score percentile among live workers selects a matching
        percentile in the pending tasks sorted by cost: strong workers get
        heavy tasks, weak workers get light ones.
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT score FROM workers WHERE id = ?", (worker_id,)
            ).fetchone()
            if row is None:
                return None
            my_score = row[0]

            tasks = self._conn.execute(
                "SELECT id, cost FROM tasks WHERE status = 'pending' ORDER BY cost ASC"
            ).fetchall()
            if not tasks:
                return None

            scores = self._active_scores()
            if len(scores) <= 1:
                task_id = tasks[-1][0]  # solo worker: take the heaviest
            else:
                rank = sum(1 for s in scores if s <= my_score) / len(scores)
                idx = min(int(rank * len(tasks)), len(tasks) - 1)
                task_id = tasks[idx][0]

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
        return {
            "task_id": task[0],
            "job_id": task[1],
            "payload": json.loads(task[2]),
            "cost": task[3],
            "script": task[4],
        }

    def complete_task(self, task_id: str, worker_id: str, ok: bool,
                      result=None, error: str = "") -> bool:
        with self._lock, self._conn:
            if ok:
                cur = self._conn.execute(
                    "UPDATE tasks SET status = 'done', result = ?, error = NULL"
                    " WHERE id = ? AND worker_id = ? AND status = 'running'",
                    (json.dumps(result), task_id, worker_id),
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
        return cur.rowcount > 0

    def reap_expired_leases(self) -> int:
        """Re-queue tasks whose worker died or whose lease ran out."""
        now = time.time()
        cutoff = now - WORKER_DEAD_AFTER
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE tasks SET status = 'pending', worker_id = NULL"
                " WHERE status = 'running' AND (lease_expires < ? OR worker_id IN"
                "   (SELECT id FROM workers WHERE last_seen < ?))"
                " AND attempts < ?",
                (now, cutoff, MAX_ATTEMPTS),
            )
            requeued = cur.rowcount
            self._conn.execute(
                "UPDATE tasks SET status = 'failed', error = 'lease expired, max attempts'"
                " WHERE status = 'running' AND (lease_expires < ? OR worker_id IN"
                "   (SELECT id FROM workers WHERE last_seen < ?))"
                " AND attempts >= ?",
                (now, cutoff, MAX_ATTEMPTS),
            )
        return requeued
