from __future__ import annotations

"""SQLite persistence layer for the coordinator.

Single connection guarded by a lock (the coordinator's HTTP server is
threaded). WAL mode keeps readers from blocking the writer.
"""

import json
import os
import sqlite3
import threading
import time
import uuid

from .accelerate import device_matches
from .ansi import safe_print, green, yellow, red, bold, dim

SCHEMA = """
CREATE TABLE IF NOT EXISTS workers (
    id            TEXT PRIMARY KEY,
    hostname      TEXT NOT NULL,
    device        TEXT NOT NULL,
    device_name   TEXT NOT NULL DEFAULT '',
    score         REAL NOT NULL,
    cpu_cores     INTEGER NOT NULL DEFAULT 0,
    gpu_memory_total_mb REAL NOT NULL DEFAULT 0.0,
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
    created_at    REAL NOT NULL DEFAULT 0.0,
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
    -- Nullable on purpose, and it is the only column here that is. NULL means
    -- "no worker has ever reported a free-VRAM reading for this machine",
    -- which is a different fact from "this card has 0 MB free" and has to stay
    -- a different value. The column used to be NOT NULL DEFAULT 0.0, so every
    -- row this table gained for any other reason (a completed task, an expired
    -- lease, registration itself) silently asserted a measurement nobody took
    -- — and a freshly joined 40 GB A100 was indistinguishable on the wire from
    -- one running flat out. See list_devices().
    gpu_memory_free_mb REAL
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
# How long a pending task may go unmatched before the coordinator decides no
# live worker will ever take it. This is a trade-off between two bad outcomes:
# fail too eagerly and a GPU box that boots a few seconds after the job was
# submitted arrives to find its work already dead; wait forever and the client
# sits through the full 300s poll only to be told "mesh task did not complete",
# which says nothing about the actual cause. A minute is comfortably longer
# than a worker's start-up-and-register path and far shorter than the client
# timeout, so the user gets a real diagnosis while there is still time to act.
UNSATISFIABLE_AFTER = 60.0


def _median(values: list) -> float:
    """True median of an already-sorted list (0.0 when empty).

    The even case has to average the two middle samples. Indexing the upper
    middle one instead makes the slowest of two workers its own yardstick,
    and "am I more than twice the median?" can then never be true — which
    silently disabled straggler handling on two-machine meshes, this
    project's most common shape.
    """
    n = len(values)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2.0


def _is_number(value) -> bool:
    """Is this hint value something we can compare against a capacity number?

    ``bool`` is excluded even though Python calls it an int: ``cpu_cores=True``
    is a typo, not a request for one core, and quietly reading it as 1 is the
    same silent reinterpretation this module is trying to stamp out. NaN is
    excluded because every comparison against it is False, so a NaN hint would
    read as "no worker has enough" in one place and "the hint is met" in
    another depending on which way the comparison happened to be written —
    the exact kind of disagreement _worker_can_run exists to prevent.
    """
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value == value  # NaN is the only value unequal to itself
    )


def _is_capacity_reading(value) -> bool:
    """Is this a number a worker could truthfully have measured?

    Free VRAM in MB: any finite, non-negative number. The exclusions each cost
    a real failure mode. ``bool`` and NaN are out for the reasons _is_number
    gives. A negative reading compares below every requirement, so it
    disqualifies its own worker from all memory-hinted work forever. An
    infinite one does the opposite and satisfies every requirement, routing
    work to a machine on the strength of a number that means nothing. Neither
    is a measurement, and both are silent — which is what makes them worth a
    check rather than a comparison.
    """
    return _is_number(value) and 0.0 <= value < float("inf")


def _free_vram_reading(value):
    """A stored free-VRAM value, or ``None`` when the row holds no usable one.

    Read-side counterpart to the validation in heartbeat(). heartbeat() is
    where junk is kept out of the column, but it cannot help rows written
    before that check existed — SQLite's REAL column happily stored the string
    a worker sent, and every later comparison against it raised, which
    _worker_can_run turned into "this worker cannot run the task". A 40 GB card
    quietly stopped being offered memory-hinted work and nothing anywhere said
    why.

    Mapping the unusable value to None puts it back in the "unknown" bucket,
    where the caller's existing fallback to the registered total takes over.
    Unknown-and-usable is the recoverable direction; silently-disqualified is
    not.
    """
    return value if _is_capacity_reading(value) else None


def _effective_free_mb(stored_free, registered_total) -> float:
    """The free VRAM a scheduling decision should use for one worker.

    Both places that judge a ``gpu_memory_mb`` hint — lease_task(), which picks
    what to run, and fail_unsatisfiable_tasks(), which decides nobody can —
    call this and nothing else. They have to reach the same number for the same
    worker: a detector stricter than the leaser condemns work that would have
    run, and a leaser stricter than the detector leaves work pending with
    nobody ever explaining why. One function is how that stays true when
    somebody edits one of them.

    Not measured (NULL, or a stored value that is not a plausible reading)
    falls back to the total the worker registered, which is what @accelerate
    validates ``memory=`` against, so a task that passed validation is leasable
    here. A measured 0.0 is a measurement and is returned as 0.0.
    """
    free = _free_vram_reading(stored_free)
    if free is None:
        return registered_total or 0.0
    return free


def _unusable_hints(payload) -> list:
    """Placement hints whose *value* this coordinator cannot interpret at all.

    Distinct from "no worker satisfies this": ``gpu="A100"`` on a CPU-only
    mesh is a request nobody can meet today and could be met tomorrow, while
    ``gpu=123`` is not a request at all — no worker that ever joins will turn
    an int into a device name. Separating the two is what lets the coordinator
    say "your hint is malformed" instead of "no worker satisfies gpu=123",
    which reads like a topology problem and sends the user hunting for a GPU
    box they already own.

    A falsy ``gpu`` (absent, ``None``, ``""``) means "not requested", matching
    what _worker_can_run has always done with it, so it is not flagged here.
    """
    if not isinstance(payload, dict):
        return []
    bad = []
    gpu = payload.get("gpu")
    if gpu and not isinstance(gpu, str):
        bad.append("gpu=%r is a %s, not a device name string"
                   % (gpu, type(gpu).__name__))
    for key in ("gpu_memory_mb", "cpu_cores"):
        value = payload.get(key)
        if value is not None and not _is_number(value):
            bad.append("%s=%r cannot be read as a number" % (key, value))
    return bad


def _evaluate_hints(payload, device: str, device_name: str,
                    cpu_cores, gpu_free_mb) -> bool:
    """The actual hint arithmetic. See _worker_can_run for the contract."""
    if not isinstance(payload, dict):
        return True
    mem_hint = payload.get("gpu_memory_mb")
    if mem_hint is not None:
        # An uninterpretable hint is answered here rather than compared: the
        # comparison below would raise TypeError on a string, and a raise is
        # the one answer this function is not allowed to give.
        if not _is_number(mem_hint):
            return False
        if (gpu_free_mb or 0.0) < mem_hint:
            return False
    cores_hint = payload.get("cpu_cores")
    if cores_hint is not None:
        if not _is_number(cores_hint):
            return False
        if (cpu_cores or 0) < cores_hint:
            return False
    gpu_hint = payload.get("gpu")
    if gpu_hint:
        # device_matches() calls .strip() on this, so a non-string reaches it
        # as an AttributeError rather than a mismatch.
        if not isinstance(gpu_hint, str):
            return False
        if not device_matches(gpu_hint, device, device_name):
            return False
    return True


def _worker_can_run(payload, device: str, device_name: str,
                    cpu_cores, gpu_free_mb: float) -> bool:
    """Does one worker satisfy every placement hint in a task's payload?

    This is the single source of truth for placement: lease_task() calls it to
    pick eligible tasks and fail_unsatisfiable_tasks() calls it to decide that
    nobody can. Keeping one implementation is what stops the two from ever
    disagreeing — a scheduler that quietly skips a task while the detector
    believes it is placeable (or the reverse) is worse than either bug alone.

    A hint of ``None`` means "not requested". A worker that reported no
    capacity for a resource reads back as 0 here (rows written before the
    capacity migration do too), and 0 cannot satisfy a positive requirement:
    a machine that never told us how many cores it has is not a machine we can
    promise 64 of them on. ``gpu_memory_total_mb`` already behaves this way.

    ``gpu_free_mb`` is the exception, and it is resolved before it gets here:
    both callers pass _effective_free_mb(), which has already turned "not
    measured" into the worker's registered total. So a 0 arriving in this
    argument means a card that was measured and has nothing free, not a card
    nobody has asked. It still cannot satisfy a positive requirement — but for
    the opposite reason, and that difference is the whole of defect 1.

    **This function is total: for any payload whatsoever it returns True or
    False and never raises.** That is not politeness, it is the load-bearing
    property. Both callers run where an exception is catastrophically out of
    proportion to its cause: in lease_task() one malformed row turns every
    worker's /api/lease into a 500 and the whole mesh stops leasing *any*
    task, and in fail_unsatisfiable_tasks() the exception escapes into the
    coordinator's daemon reaper thread and kills it for the life of the
    process. A payload of ``{"gpu": 123}`` — a user typing ``gpu=123`` instead
    of ``gpu="cuda"`` — is enough to do both.

    A value we cannot interpret answers **False**, not True. The alternative,
    ignoring a hint we do not understand, means a task that asked to run only
    on an A100 quietly runs anywhere; that is precisely the failure mode of
    the ``map(gpu=...)`` silent-local-fallback bug, and it is worse than a
    stopped task because the user never finds out. False costs the task, and
    fail_unsatisfiable_tasks() turns that into a message naming the bad value
    (see _unusable_hints), so the user is told rather than misled.
    """
    try:
        return _evaluate_hints(payload, device, device_name,
                               cpu_cores, gpu_free_mb)
    except Exception:
        # The type checks inside _evaluate_hints are what produce the *right*
        # answer; this is what guarantees there is *an* answer. It stays even
        # though those checks are exhaustive today, because the cost of the
        # two being out of step for one release is a mesh-wide outage from a
        # single typo, and the cost of the belt is one unreachable except.
        return False


def _placement_requirements(payload) -> list:
    """The placement hints a payload carries, as (key, human text) pairs.

    Used only to build the error message for an unplaceable task, so it names
    what the user actually asked for rather than a generic "no worker".
    """
    if not isinstance(payload, dict):
        return []
    reqs = []
    if payload.get("gpu"):
        reqs.append(("gpu", "gpu=%r" % (payload["gpu"],)))
    if payload.get("gpu_memory_mb") is not None:
        reqs.append(("gpu_memory_mb", "gpu_memory_mb=%s" % (payload["gpu_memory_mb"],)))
    if payload.get("cpu_cores") is not None:
        reqs.append(("cpu_cores", "cpu_cores=%s" % (payload["cpu_cores"],)))
    return reqs


class Database:
    def __init__(self, path: str):
        self._path = path
        self._conn = self._open_db(path)
        self._lock = threading.Lock()
        # (worker_id, repr of the value) pairs already reported by
        # _warn_bad_capacity. Process-local and deliberately not persisted:
        # its only job is to keep one misbehaving worker from writing a line
        # every heartbeat, and a coordinator restart is a fine moment to say
        # it again. Guarded by its own lock so heartbeats on different threads
        # cannot both decide they are the first.
        self._bad_capacity_seen = set()
        self._bad_capacity_lock = threading.Lock()

    def _warn_bad_capacity(self, worker_id: str, value) -> None:
        """Announce a free-VRAM reading we are dropping, once per worker+value."""
        key = (worker_id, repr(value))
        with self._bad_capacity_lock:
            if key in self._bad_capacity_seen:
                return
            self._bad_capacity_seen.add(key)
        safe_print(
            f"{bold('[gpumesh]')} {yellow('WARNING')}: worker {worker_id} sent"
            f" gpu_memory_free_mb={value!r}, which is not a usable measurement."
            f" Ignoring it and keeping the last known value — this worker's"
            f" free VRAM may be stale, and memory= placement decisions about it"
            f" will fall back to the total it registered."
        )

    @staticmethod
    def _open_db(path: str):
        # Create the parent directory so a fresh ``~/.gpumesh`` (or any other
        # first-run location) does not fail with "unable to open database
        # file". sqlite3.connect() creates the file but not its parents.
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
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
            # Migrate: add the reported-capacity columns that back the
            # cores=/memory= requirements of @accelerate
            try:
                conn.execute("SELECT cpu_cores FROM workers LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute(
                    "ALTER TABLE workers ADD COLUMN cpu_cores INTEGER NOT NULL DEFAULT 0"
                )
            try:
                conn.execute("SELECT gpu_memory_total_mb FROM workers LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute(
                    "ALTER TABLE workers ADD COLUMN gpu_memory_total_mb REAL NOT NULL DEFAULT 0.0"
                )
            # Migrate: add the task submission timestamp. The unsatisfiable
            # detector measures how long a task has been waiting against it,
            # so without a value it cannot tell "just submitted" from "stuck
            # for an hour". Rows written before this migration default to 0,
            # which the detector reads as "age unknown" and refuses to fail.
            try:
                conn.execute("SELECT created_at FROM tasks LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute(
                    "ALTER TABLE tasks ADD COLUMN created_at REAL NOT NULL DEFAULT 0.0"
                )
            # Migrate: ensure worker_stats table exists (may not be in old schema)
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS worker_stats (
                    worker_id     TEXT PRIMARY KEY,
                    tasks_completed INTEGER NOT NULL DEFAULT 0,
                    total_time    REAL NOT NULL DEFAULT 0.0,
                    avg_time      REAL NOT NULL DEFAULT 0.0,
                    last_task_time REAL NOT NULL DEFAULT 0.0,
                    gpu_memory_free_mb REAL
                );
            """)
            # Migrate: add gpu_memory_free_mb column to existing databases
            try:
                conn.execute("SELECT gpu_memory_free_mb FROM worker_stats LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute(
                    "ALTER TABLE worker_stats ADD COLUMN gpu_memory_free_mb REAL"
                )
            # Migrate: drop the NOT NULL DEFAULT 0.0 that earlier releases put
            # on gpu_memory_free_mb.
            #
            # This one needs a table rebuild because SQLite's ALTER TABLE
            # cannot relax a constraint, and it cannot be skipped: while the
            # column refuses NULL there is no way to write down "nobody has
            # measured this machine yet", so every row created by registration
            # (or by a completed task, or an expired lease) claims 0 MB free
            # and a brand-new GPU box reads as a full one. Emitting NULL from
            # list_devices() is the whole fix; this is what makes NULL
            # storable.
            #
            # Existing values are copied verbatim rather than reinterpreted. A
            # 0.0 already in an upgraded database is genuinely ambiguous — it
            # may be a real reading, it may be the old default — and inventing
            # a NULL for it would erase measurements we do have. The next
            # heartbeat overwrites it either way, seconds later.
            #
            # The rebuild is wrapped because a failure here must not reach the
            # handler below: sqlite3.OperationalError is a DatabaseError, and
            # that handler answers a DatabaseError by moving the file aside as
            # corrupt and starting a fresh one. Losing a coordinator's queue
            # over a stats-table constraint would be wildly out of proportion.
            # Skipping leaves the pre-fix behaviour (0.0 for "unknown"), which
            # is the bug, not a broken database.
            columns = conn.execute("PRAGMA table_info(worker_stats)").fetchall()
            notnull = any(c[1] == "gpu_memory_free_mb" and c[3] for c in columns)
            if notnull:
                try:
                    conn.executescript("""
                        CREATE TABLE worker_stats_migrated (
                            worker_id     TEXT PRIMARY KEY,
                            tasks_completed INTEGER NOT NULL DEFAULT 0,
                            total_time    REAL NOT NULL DEFAULT 0.0,
                            avg_time      REAL NOT NULL DEFAULT 0.0,
                            last_task_time REAL NOT NULL DEFAULT 0.0,
                            gpu_memory_free_mb REAL
                        );
                        INSERT INTO worker_stats_migrated
                            (worker_id, tasks_completed, total_time, avg_time,
                             last_task_time, gpu_memory_free_mb)
                            SELECT worker_id, tasks_completed, total_time,
                                   avg_time, last_task_time, gpu_memory_free_mb
                            FROM worker_stats;
                        DROP TABLE worker_stats;
                        ALTER TABLE worker_stats_migrated RENAME TO worker_stats;
                    """)
                except sqlite3.OperationalError as exc:
                    safe_print(f"{bold('[gpumesh]')} {yellow('WARNING')}: could not"
                               f" migrate worker_stats.gpu_memory_free_mb to"
                               f" nullable ({exc}); free VRAM will keep reading"
                               f" as 0 until each worker's first heartbeat")
                    try:
                        conn.execute("DROP TABLE IF EXISTS worker_stats_migrated")
                    except sqlite3.OperationalError:
                        pass
            # Backfill created_at for any task row that still carries the
            # column's 0.0 default.
            #
            # Without this, upgrading a coordinator that had queued work
            # strands it permanently. The ALTER above gives every pre-existing
            # row created_at = 0, fail_unsatisfiable_tasks() refuses to age a
            # row whose age it cannot know, and so a gpu=-hinted task that no
            # live worker can satisfy is never leased *and* never failed. It
            # sits pending until the client's own timeout fires with the
            # useless "mesh task did not complete" — forever, across every
            # subsequent restart.
            #
            # The value is the migration timestamp, i.e. "as old as this
            # coordinator process". The tempting alternative is to treat them
            # as already-aged (created_at far in the past) so they are judged
            # on the next reap, and it is wrong for the situation this
            # actually occurs in: the coordinator has just started, its
            # workers have not re-registered yet, and the roster the detector
            # would judge against is whichever machine happened to reconnect
            # first. Condemning a GPU task because only the CPU laptop is back
            # yet is the false positive UNSATISFIABLE_AFTER exists to prevent.
            # Restarting the clock gives these rows exactly the grace a task
            # submitted right now would get — no more, and no less than the
            # one thing they need, which is that the clock runs at all.
            #
            # Run unconditionally rather than inside the ALTER branch above,
            # because the rows that need it most are in databases where the
            # column already exists: a coordinator upgraded to the release
            # that ADDED created_at wrote 0 into every queued row and has been
            # stranding them ever since. Once healed the WHERE matches nothing
            # and this costs one indexless scan of a small table per open.
            conn.execute(
                "UPDATE tasks SET created_at = ?"
                " WHERE created_at IS NULL OR created_at <= 0",
                (time.time(),),
            )
            conn.commit()
            return conn
        except sqlite3.DatabaseError as exc:
            # ``os`` is module-level; only ``shutil`` needs importing here.
            # (Importing ``os`` locally would shadow the module-level name for
            # the whole function and break the parent-dir creation above.)
            import shutil
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
                        device_name: str = "", cpu_cores: int = 0,
                        gpu_memory_total_mb: float = 0.0,
                        gpu_memory_free_mb: float | None = None) -> str:
        """Record a worker and the capacity it reports.

        ``cpu_cores`` and ``gpu_memory_total_mb`` come straight from the
        worker's capability probe; they are what ``@accelerate(cores=...,
        memory=...)`` is checked against, so a worker that omits them simply
        never satisfies those requirements.

        ``gpu_memory_free_mb`` is optional and closes the gap between a worker
        joining and its first heartbeat. ``capability.full_probe()`` already
        measures free VRAM and the worker already posts it in the registration
        body, so the reading exists — it was simply thrown away here, leaving
        the machine reporting "unknown" for the second or so until a heartbeat
        arrived. Recording it shrinks that window to nothing. Omitted (or
        unusable) it stays NULL, which reads as unknown rather than as zero.
        """
        worker_id = uuid.uuid4().hex[:12]
        now = time.time()
        free_mb = _free_vram_reading(gpu_memory_free_mb)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO workers (id, hostname, device, device_name, score,"
                " cpu_cores, gpu_memory_total_mb, registered_at, last_seen)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (worker_id, hostname, device, device_name, score,
                 cpu_cores, gpu_memory_total_mb, now, now),
            )
            # The stats row is created here so later UPDATEs have something to
            # hit, and its gpu_memory_free_mb is left NULL unless the worker
            # actually sent a reading. That NULL is the point: a row asserting
            # 0.0 because a row had to exist is how "just joined" came to look
            # exactly like "card is full".
            self._conn.execute(
                "INSERT OR IGNORE INTO worker_stats (worker_id, gpu_memory_free_mb)"
                " VALUES (?, ?)",
                (worker_id, free_mb),
            )
        return worker_id

    def heartbeat(self, worker_id: str, task_id: str | None = None,
                   score: float | None = None,
                   gpu_memory_free_mb: float | None = None) -> bool:
        """Mark a worker alive, and record the free VRAM it reports.

        A ``gpu_memory_free_mb`` that is not a plausible measurement is
        **ignored**: the liveness half of the heartbeat still lands, the lease
        is still extended, and the column keeps its last good value. The
        alternative — refusing the whole heartbeat — would stop refreshing
        ``last_seen`` and so mark a running, responsive worker dead over a
        malformed number in one field. That is a far bigger blast radius than
        the field deserves, and it would take the machine's real work down with
        it.

        Ignoring it silently is not acceptable either, and that is what used to
        happen: SQLite's REAL column stored ``'lots'`` without complaint, every
        later comparison against it raised, and _worker_can_run answered False
        because False is the one answer it is allowed to give. A healthy 40 GB
        worker stopped being offered any memory-hinted task, kept showing up
        green in ``gpumesh workers``, and nothing anywhere named the cause.
        Since the recent totality work that is no longer a crash — which is an
        improvement in blast radius and a regression in diagnosability, because
        a crash at least leaves a traceback.

        So the value is dropped *and announced*, once per worker per distinct
        bad value. Once, because a worker heartbeats every few seconds and a
        line per heartbeat would bury the coordinator's log in the one message
        the operator most needs to read; per distinct value, because a worker
        whose reading goes wrong in a new way has told us something new.
        """
        now = time.time()
        if gpu_memory_free_mb is not None and not _is_capacity_reading(gpu_memory_free_mb):
            self._warn_bad_capacity(worker_id, gpu_memory_free_mb)
            gpu_memory_free_mb = None
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
        status (alive/dead), a virtual device index for the user, and the
        capacity fields (cpu_cores, gpu_memory_total_mb, gpu_memory_free_mb)
        that @accelerate's cores=/memory= requirements are checked against.

        Free VRAM comes from worker_stats, where the heartbeat keeps it
        current, and it is **null until a worker actually reports one**. That
        null is load-bearing. It used to be ``COALESCE(..., 0.0)``, so a worker
        that had registered a moment ago and a card running flat out sent the
        identical pair of numbers — free 0, total 40000 — and no client could
        tell them apart. Clients read an absent or null capacity as "unknown,
        allow it and let the scheduler decide" and a reported zero strictly
        ("this card is full, do not send it 8 GB of work"), which are both the
        right readings; they were simply being handed the wrong one. Answering
        null when we have no measurement is what makes those two readings land
        on the two situations they were written for.
        """
        with self._lock:
            cutoff = time.time() - WORKER_DEAD_AFTER
            rows = self._conn.execute(
                "SELECT w.id, w.hostname, w.device, w.device_name, w.score,"
                " w.last_seen, w.cpu_cores, w.gpu_memory_total_mb,"
                " s.gpu_memory_free_mb"
                " FROM workers w LEFT JOIN worker_stats s ON s.worker_id = w.id"
                " ORDER BY w.last_seen DESC"
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
                "cpu_cores": r[6],
                "gpu_memory_total_mb": r[7],
                # None (JSON null) means "never measured". A junk value stored
                # by a coordinator that predates heartbeat()'s validation is
                # mapped to the same thing rather than shipped to clients that
                # would try to compare it.
                "gpu_memory_free_mb": _free_vram_reading(r[8]),
            })
        return devices

    def device_summary(self) -> dict:
        """Return a summary of all available compute resources.

        Pure aggregation over list_devices(): it counts devices and sums
        ``score``, and never touches ``gpu_memory_free_mb``, so a null there
        passes straight through to the caller untouched.
        """
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
        """Write a job and its tasks. Raises ValueError on a malformed hint.

        Validating here, at the point of entry, is the layer that turns a bad
        hint into something the submitter can act on. The two guards further
        in (a total _worker_can_run, a lease_task that skips what it cannot
        evaluate) only make a bad payload *harmless* — the task still has to
        wait out UNSATISFIABLE_AFTER before anyone says why, and the person
        who typed ``gpu=123`` has long since walked away. Refusing the
        submission answers them in the same second, in the same call, with
        the offending value in hand.

        It cannot replace those guards, and is not meant to. Rows already in
        the database were written before this check existed, and
        ``Database`` is reachable without going through here at all, so the
        scheduler still has to survive whatever it reads back.

        The check is deliberately narrow: only hint values that are of an
        impossible *type*. Whether any worker can meet the request is not
        knowable at submit time — free VRAM is rewritten by every heartbeat,
        and the GPU box often joins after the job does — so it stays where it
        belongs, in fail_unsatisfiable_tasks().
        """
        for index, item in enumerate(payloads):
            bad = _unusable_hints(item)
            if bad:
                raise ValueError(
                    "payload %d has an unusable placement hint: %s"
                    % (index, "; ".join(bad))
                )
        job_id = uuid.uuid4().hex[:12]
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO jobs (id, name, script, created_at) VALUES (?, ?, ?, ?)",
                (job_id, name, script, now),
            )
            for item in payloads:
                try:
                    cost = float(item.get("cost", 1.0)) if isinstance(item, dict) else 1.0
                except (ValueError, TypeError):
                    cost = 1.0
                # created_at is per task, not per job: it is what the
                # unsatisfiable detector ages against, and a re-queued task
                # keeps the original submission time on purpose — the user
                # has been waiting since then, not since the last retry.
                self._conn.execute(
                    "INSERT INTO tasks (id, job_id, payload, cost, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (uuid.uuid4().hex[:12], job_id, json.dumps(item), cost, now),
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

        The queue this ranks against is every task *in flight* — pending plus
        already-leased — sorted by cost. Each distinct score among the live
        workers owns a contiguous band of that queue, weakest score at the
        light end, strongest at the heavy end, and the worker is handed the
        pending task nearest its own band.

        What that buys, stated so it can be tested: **as long as the in-flight
        queue is at least as long as the live worker roster, no worker is ever
        handed a task heavier than one handed to a strictly stronger worker,
        in whatever order the workers happen to poll.** Workers poll
        independently and one at a time, so poll order is not something this
        scheduler gets to choose; the guarantee has to survive all of them.

        Three things it deliberately does not promise:

        * Fewer eligible tasks than live workers. Somebody must go without,
          and whoever polls first takes the task nearest its band, so a weak
          early poller can end up above a strong late one. The alternative is
          a worker that refuses available work, which risks a task nobody ever
          picks up.
        * Across completions. A finished task leaves the queue for good, so
          the next lease is measured against a genuinely different queue. The
          guarantee is per queue state, not for all time.
        * Under straggler deprioritization, which overrides score order on
          purpose (see below).

        Straggler tolerance (Petals/Swarm-Tune pattern): workers with
        avg_time > 2x the median avg_time are deprioritized — they get
        lighter tasks to reduce the impact of their slowness.

        Memory-aware scheduling (HAMi pattern): if a task's payload contains
        a ``gpu_memory_mb`` hint, prefer workers with enough free GPU memory.
        Workers with insufficient free memory are filtered out before scoring.

        Hardware-aware scheduling: a ``gpu`` hint (from ``@accelerate(gpu=...)``)
        restricts the task to workers matching that device kind or model, so
        "only run on an A100" is enforced here and not just advertised. A
        ``cpu_cores`` hint (from ``@accelerate(cores=...)``) is enforced the
        same way against the core count the worker registered.
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT score, device, device_name, gpu_memory_total_mb, cpu_cores"
                " FROM workers WHERE id = ?",
                (worker_id,),
            ).fetchone()
            if row is None:
                return None
            my_score, my_device, my_device_name, my_total_mb, my_cores = row

            # -- gather straggler stats (median avg_time across the *other*
            # active workers). The requesting worker is excluded because it
            # cannot be its own benchmark: on a two-worker mesh its sample is
            # half of any median that includes it, and "slower than twice the
            # median" then reduces to "slower than the sum of both times",
            # which is arithmetically impossible. Comparing against peers is
            # what the Petals-style rule actually means.
            stats_rows = self._conn.execute(
                "SELECT avg_time FROM worker_stats"
                " WHERE avg_time > 0 AND worker_id != ?",
                (worker_id,),
            ).fetchall()
            median_avg = _median(sorted([r[0] for r in stats_rows]))

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
            #
            # Three distinct states now arrive here and only two of them mean
            # the same thing. NULL (no stats row, or a row nobody has reported
            # a reading into) and a stored value we cannot use both mean "not
            # measured", and fall back to the total the worker registered —
            # @accelerate validates memory= on that same basis, so a task that
            # passed validation stays leasable. A reading of 0.0 is neither: it
            # is a measurement, it says the card is full, and it is left to
            # fail the comparison below. The old code tested truthiness, which
            # folded that third state into the first and handed a memory-hinted
            # task to the one worker in the mesh with no room for it.
            my_mem_row = self._conn.execute(
                "SELECT gpu_memory_free_mb FROM worker_stats WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
            my_free_mb = _effective_free_mb(
                my_mem_row[0] if my_mem_row else None, my_total_mb
            )

            # -- fetch the in-flight queue: everything pending *and* everything
            # already leased out, cheapest first.
            #
            # Only a pending row can be handed to this worker, but the running
            # ones have to stay visible, because they are what holds the ruler
            # still. Rank a worker against nothing but the leftovers and the
            # ruler shrinks with every lease: on a three-task queue the middle
            # worker is measured against three tasks, the strong one that polls
            # after it against the two survivors, and "the top of what is left"
            # stops meaning "the top of the queue". That is how the inversion
            # this scheduler has now grown twice keeps coming back — the
            # full-queue case the arithmetic was tuned for happens exactly once
            # per queue, on the very first lease, and never again. Against
            # pending+running the frame is fixed for as long as the round
            # lasts, so every worker in that round reads the same ruler and
            # their bands cannot overlap.
            raw_tasks = self._conn.execute(
                "SELECT id, cost, payload, status FROM tasks"
                " WHERE status IN ('pending', 'running')"
                " ORDER BY cost ASC, rowid ASC"
            ).fetchall()
            if not raw_tasks:
                return None

            # Filter tasks by their placement hints (HAMi Filter pattern).
            # Skipping rather than failing is deliberate: the task stays
            # pending for a worker that does match. fail_unsatisfiable_tasks()
            # is what eventually notices when no such worker exists.
            #
            # Every task is evaluated inside its own try. One row's payload
            # must never be able to decide the fate of the other rows, and
            # without this it can: an exception raised while judging task 3
            # unwinds out of lease_task, the handler turns it into a 500, and
            # every worker polling /api/lease gets that 500 for every task in
            # the queue — a single malformed payload stops the entire mesh
            # from leasing anything at all. That is the blast radius this
            # guard bounds, and it is why it exists even though
            # _worker_can_run() is itself total: totality is a property of
            # one function that someone may edit, whereas this is a property
            # of the loop, and the loop is where the damage would spread.
            eligible_tasks = []
            for t in raw_tasks:
                try:
                    payload = json.loads(t[2])
                except (json.JSONDecodeError, TypeError):
                    payload = {}
                try:
                    ok = _worker_can_run(payload, my_device, my_device_name,
                                         my_cores, my_free_mb)
                except Exception:
                    # Skip, exactly as for a hint this worker cannot meet: the
                    # row stays pending and fail_unsatisfiable_tasks() is what
                    # eventually gives it a verdict and an error message.
                    # Dropping one task beats refusing to lease any.
                    ok = False
                if not ok:
                    continue
                eligible_tasks.append(t)

            if not eligible_tasks:
                return None

            # Positions of the rows that can actually be leased. The running
            # ones stay in eligible_tasks to hold the queue's shape; they are
            # simply never candidates.
            free = [i for i, t in enumerate(eligible_tasks) if t[3] == "pending"]
            if not free:
                return None

            # Straggler deprioritization: stragglers get lighter tasks.
            #
            # This is a filter on what the worker may be *given*, applied after
            # the queue has been measured and before its band is read off, so
            # it composes with the ranking instead of fighting it: the band
            # arithmetic still sees the true queue, the straggler just cannot
            # be handed anything out of the heavy half of it. When the light
            # half has already been claimed the fallback is the single lightest
            # task left, not the whole queue — "deprioritized" must never
            # quietly turn into "hands it the worst task in the mesh".
            if is_straggler and len(eligible_tasks) > 1:
                light_half = len(eligible_tasks) // 2
                lighter = [i for i in free if i < light_half]
                free = lighter or [free[0]]

            cutoff_active = time.time() - WORKER_DEAD_AFTER
            scores = self._active_scores(cutoff_active)
            if len(scores) <= 1:
                idx = free[-1]  # solo worker: take the heaviest
            else:
                # Give each distinct score a band of the queue and hand the
                # worker the free task nearest its own band.
                #
                # The band is [weaker, weaker+same-1] slots wide — the slots
                # this worker's score jointly occupies among the live roster —
                # scaled onto the queue's 0..n-1 positions. Distinct scores get
                # a one-slot band each and, whenever the queue is at least as
                # long as the roster, those bands are disjoint and in ascending
                # order, which is the whole guarantee: a weaker worker cannot
                # reach a slot a stronger worker owns. Equal scores share one
                # band and settle it between themselves, which is what makes
                # identical laptops share the queue instead of all piling onto
                # the same task.
                #
                # The arithmetic is integer on purpose. Scaling through a float
                # fraction lets a rank that is mathematically exact land just
                # under its own slot boundary — 15/22 of 22 steps evaluates to
                # 14.999999999999998 — and floor() then quietly demotes that
                # worker a whole task down the queue. Rosters that big are rare
                # but the failure is silent, and integers cost nothing.
                #
                # A worker whose own row has gone stale is not in `scores` at
                # all, which would put weaker+same-1 below weaker; clamping
                # collapses it to a single slot rather than an inverted band.
                n = len(eligible_tasks)
                span = len(scores) - 1
                weaker = min(sum(1 for s in scores if s < my_score), span)
                same = sum(1 for s in scores if s == my_score)
                top = min(max(weaker + same - 1, weaker), span)
                lo = (weaker * (n - 1)) // span
                hi = (top * (n - 1)) // span
                # Ties aim at the middle of their shared band, as before, so a
                # group of equals fans out around its centre.
                pref = (lo + hi) // 2
                band = [i for i in free if lo <= i <= hi]
                # Nothing free inside the band means every task it owns is
                # already running; take the nearest free one instead, breaking
                # ties downward. Stepping down cannot rob a stronger worker of
                # the heavy task it is owed, and stepping up can.
                idx = min(band or free, key=lambda i: (abs(i - pref), i))
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
                      elapsed: float | None = None,
                      user_error: bool = False) -> bool:
        """Record a task outcome.

        ``user_error`` marks a deterministic failure (the task's own code
        raised, or returned something unsendable). Re-running it would fail
        identically, so it is failed immediately rather than consuming the
        retry budget — retries exist for flaky infrastructure, not for bugs.
        """
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
                final = user_error or (row is not None and row[0] >= MAX_ATTEMPTS)
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

    def fail_unsatisfiable_tasks(self) -> list[dict]:
        """Fail pending tasks that no live worker could ever accept.

        The scheduler skips a task whose placement hints it cannot meet, which
        is right for one worker and disastrous for the mesh: ask for an A100 on
        a CPU-only mesh and every worker skips it forever, the job never
        finishes, and the only thing the user ever sees is a bare
        ``TimeoutError: mesh task did not complete within 300s`` — which names
        neither the task nor the reason. Client-side validation does not close
        this hole either, because it is a snapshot taken at submit time of
        values (free VRAM above all) that the heartbeat rewrites every ten
        seconds. Somebody has to notice, and the coordinator is the only party
        that sees both the queue and the whole live roster.

        Two conditions keep this from turning normal waiting into failure:

        * At least one worker must be alive. An empty mesh means nothing has
          joined *yet* — the user is very often starting the coordinator and
          the workers in that order — so there is nothing to conclude.
        * The task must have been pending for UNSATISFIABLE_AFTER seconds, so
          a GPU box that joins a minute into the job still gets the work.

        Neither applies to a payload whose hint is malformed rather than
        merely unmet, which is why those are failed in a first pass before
        the roster is even fetched: waiting cannot help a task asking for
        ``gpu=123``, and there is no roster to compare it against — the
        diagnosis is entirely inside the payload. Both gates exist to avoid
        condemning work a machine that has not joined yet would have taken,
        and no machine can ever take this one.

        Matching is delegated to _worker_can_run(), the same helper the leaser
        uses, so the detector can never condemn a task some worker would in
        fact have taken.

        Returns one dict per failed task ({"task_id", "job_id", "error"}) so
        the caller can log the topology problem it just diagnosed.
        """
        now = time.time()
        cutoff = now - WORKER_DEAD_AFTER
        with self._lock, self._conn:
            failed = []
            pending = []
            for task_id, job_id, payload_json, created_at in self._conn.execute(
                "SELECT id, job_id, payload, created_at FROM tasks"
                " WHERE status = 'pending'"
            ).fetchall():
                try:
                    payload = json.loads(payload_json)
                except (json.JSONDecodeError, TypeError):
                    payload = {}
                bad = _unusable_hints(payload)
                if not bad:
                    pending.append((task_id, job_id, payload, created_at))
                    continue
                # Say "malformed", not "no worker satisfies". The scheduler
                # treats an uninterpretable hint as unsatisfiable-by-everyone
                # (see _worker_can_run), so this task is genuinely never going
                # to run — but phrasing that as a placement failure would send
                # the user looking for hardware, when what they need is to
                # look at the value they typed. Naming it is the whole point.
                error = (
                    "task payload has an unusable placement hint: %s."
                    " Placement hints must be a device name string"
                    " (gpu='cuda', gpu='A100') and numbers"
                    " (gpu_memory_mb=16384, cpu_cores=8)" % ("; ".join(bad),)
                )
                cur = self._conn.execute(
                    "UPDATE tasks SET status = 'failed', error = ?"
                    " WHERE id = ? AND status = 'pending'",
                    (error, task_id),
                )
                if cur.rowcount:
                    failed.append({
                        "task_id": task_id,
                        "job_id": job_id,
                        "error": error,
                    })

            live = self._conn.execute(
                "SELECT w.hostname, w.device, w.device_name, w.cpu_cores,"
                " w.gpu_memory_total_mb, s.gpu_memory_free_mb"
                " FROM workers w LEFT JOIN worker_stats s ON s.worker_id = w.id"
                " WHERE w.last_seen >= ?",
                (cutoff,),
            ).fetchall()
            if not live:
                # Nothing has joined yet: a wait, not a failure. Any malformed
                # payloads found above are still reported — they did not need
                # a roster to be judged.
                return failed

            # Same free-VRAM fallback the leaser applies, and it has to stay
            # character-for-character the same rule: this detector condemns
            # tasks the leaser refuses to lease, so any gap between the two
            # either fails work somebody would have run or strands work nobody
            # will. Not-measured (NULL, or a value we cannot use) falls back to
            # the registered total; a measured 0.0 stays 0.0 and fails the
            # comparison, exactly as it does in lease_task.
            workers = [
                (r[0], r[1], r[2], r[3], _effective_free_mb(r[5], r[4]))
                for r in live
            ]
            roster = ", ".join(
                "%s [%s]" % (w[0], w[1]) for w in workers[:6]
            )
            if len(workers) > 6:
                roster += ", +%d more" % (len(workers) - 6,)

            for task_id, job_id, payload, created_at in pending:
                # created_at is 0 only for a row this process has not opened
                # since the column was added, which _open_db backfills on
                # sight — so in practice this never fires any more. It stays
                # because the fallback if it ever did is the safe one: (now -
                # 0) reads as five decades of waiting and would condemn the
                # row instantly, and a task stuck pending is recoverable
                # while a task wrongly failed is not.
                if not created_at:
                    continue
                waited = now - created_at
                if waited < UNSATISFIABLE_AFTER:
                    continue
                try:
                    if any(_worker_can_run(payload, w[1], w[2], w[3], w[4])
                           for w in workers):
                        continue
                    # Name the requirement(s) that no single worker meets. If
                    # each one is individually satisfiable the task is asking
                    # for a combination no one machine has, which is a
                    # different diagnosis and worth saying so.
                    reqs = _placement_requirements(payload)
                    unmet = [
                        text for key, text in reqs
                        if not any(_worker_can_run({key: payload[key]},
                                                   w[1], w[2], w[3], w[4])
                                   for w in workers)
                    ]
                    if unmet:
                        reason = " and ".join(unmet)
                    else:
                        reason = " + ".join(t for _, t in reqs) + " on one worker"
                except Exception:
                    # One payload's problem must not take down the thread that
                    # calls this — it is the coordinator's reaper, a daemon
                    # loop whose death is silent and total. The narrower
                    # (TypeError, ValueError) that used to be here is what let
                    # ``gpu=123`` through as an AttributeError and killed it.
                    # Skipping leaves the row pending, which is the mild
                    # outcome; the malformed-hint pass above is what stops it
                    # from being the *permanent* outcome.
                    continue
                error = (
                    "no live worker satisfies %s (live workers: %s);"
                    " task was pending for %ds" % (reason, roster, int(waited))
                )
                cur = self._conn.execute(
                    "UPDATE tasks SET status = 'failed', error = ?"
                    " WHERE id = ? AND status = 'pending'",
                    (error, task_id),
                )
                if cur.rowcount:
                    failed.append({
                        "task_id": task_id,
                        "job_id": job_id,
                        "error": error,
                    })
        return failed

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
