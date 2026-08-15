import random
import sqlite3
import threading
import time

import pytest

from gpumesh.db import (
    SCHEMA,
    Database,
    MAX_ATTEMPTS,
    UNSATISFIABLE_AFTER,
    _median,
    _worker_can_run,
)


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


def test_register_and_list_workers(db):
    wid = db.register_worker("laptop-a", "cpu", 1.5)
    workers = db.list_workers()
    assert len(workers) == 1
    assert workers[0]["id"] == wid
    assert workers[0]["alive"] is True
    assert workers[0]["score"] == 1.5


def test_heartbeat_unknown_worker(db):
    assert db.heartbeat("nope") is False


def test_create_job_and_status(db):
    job_id = db.create_job("j", "print(1)", [{"x": 1, "cost": 2}, {"x": 2}])
    job = db.job_status(job_id)
    assert job["name"] == "j"
    assert job["finished"] is False
    assert job["counts"] == {"pending": 2}
    costs = sorted(t["cost"] for t in job["tasks"])
    assert costs == [1.0, 2.0]


def test_job_status_missing(db):
    assert db.job_status("nope") is None


def test_lease_solo_worker_gets_heaviest(db):
    wid = db.register_worker("a", "cpu", 1.0)
    db.create_job("j", "s", [{"cost": 1}, {"cost": 5}, {"cost": 3}])
    task = db.lease_task(wid)
    assert task["cost"] == 5.0


def test_lease_matches_strength_percentile(db):
    weak = db.register_worker("weak", "cpu", 1.0)
    strong = db.register_worker("strong", "cuda", 100.0)
    db.create_job("j", "s", [{"cost": 1}, {"cost": 10}])
    assert db.lease_task(strong)["cost"] == 10.0
    assert db.lease_task(weak)["cost"] == 1.0


def test_lease_no_double_assignment(db):
    wid = db.register_worker("a", "cpu", 1.0)
    db.create_job("j", "s", [{"cost": 1}])
    assert db.lease_task(wid) is not None
    assert db.lease_task(wid) is None


def test_lease_concurrent_workers_never_share_a_task(db):
    workers = [db.register_worker(f"w{i}", "cpu", 1.0) for i in range(8)]
    db.create_job("j", "s", [{"cost": i} for i in range(8)])
    leased = []
    lock = threading.Lock()

    def grab(wid):
        while True:
            t = db.lease_task(wid)
            if t is None:
                return
            with lock:
                leased.append(t["task_id"])

    threads = [threading.Thread(target=grab, args=(w,)) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(leased) == 8
    assert len(set(leased)) == 8  # no task handed out twice


def test_complete_task_success(db):
    wid = db.register_worker("a", "cpu", 1.0)
    job_id = db.create_job("j", "s", [{"cost": 1}])
    task = db.lease_task(wid)
    assert db.complete_task(task["task_id"], wid, True, result={"acc": 0.9})
    job = db.job_status(job_id)
    assert job["finished"] is True
    assert job["tasks"][0]["result"] == {"acc": 0.9}


def test_complete_task_failure_requeues_until_max_attempts(db):
    wid = db.register_worker("a", "cpu", 1.0)
    job_id = db.create_job("j", "s", [{"cost": 1}])
    for attempt in range(1, MAX_ATTEMPTS + 1):
        task = db.lease_task(wid)
        assert task is not None, f"attempt {attempt} should get the task back"
        db.complete_task(task["task_id"], wid, False, error="boom")
    job = db.job_status(job_id)
    assert job["tasks"][0]["status"] == "failed"
    assert db.lease_task(wid) is None


def test_user_error_fails_immediately_without_retrying(db):
    """A task whose own code raised is deterministic — don't burn retries.

    Re-running it produces the identical failure, so retrying only delays the
    error and triples the noise the user sees.
    """
    wid = db.register_worker("a", "cpu", 1.0)
    job_id = db.create_job("j", "s", [{"cost": 1}])
    task = db.lease_task(wid)

    db.complete_task(task["task_id"], wid, False,
                     error="ValueError: bad input", user_error=True)

    job = db.job_status(job_id)
    assert job["tasks"][0]["status"] == "failed"
    assert job["tasks"][0]["attempts"] == 1
    assert db.lease_task(wid) is None


def test_infrastructure_error_still_retries(db):
    """Failures that aren't the task's fault keep the full retry budget."""
    wid = db.register_worker("a", "cpu", 1.0)
    job_id = db.create_job("j", "s", [{"cost": 1}])
    task = db.lease_task(wid)

    db.complete_task(task["task_id"], wid, False,
                     error="connection reset", user_error=False)

    assert db.job_status(job_id)["tasks"][0]["status"] == "pending"
    assert db.lease_task(wid) is not None


def test_complete_task_wrong_worker_rejected(db):
    w1 = db.register_worker("a", "cpu", 1.0)
    w2 = db.register_worker("b", "cpu", 1.0)
    db.create_job("j", "s", [{"cost": 1}])
    task = db.lease_task(w1)
    assert db.complete_task(task["task_id"], w2, True, result={}) is False


def test_reaper_requeues_expired_lease(db, monkeypatch):
    import gpumesh.db as dbmod

    wid = db.register_worker("a", "cpu", 1.0)
    db.create_job("j", "s", [{"cost": 1}])
    monkeypatch.setattr(dbmod, "LEASE_SECONDS", -1.0)  # lease already expired
    task = db.lease_task(wid)
    assert task is not None
    assert db.reap_expired_leases() == 1
    job_tasks = db.job_status(task["job_id"])["tasks"]
    assert job_tasks[0]["status"] == "pending"


def test_db_close(tmp_path):
    """close() flushes WAL and closes connection."""
    db_path = str(tmp_path / "close_test.db")
    db = Database(db_path)
    db.register_worker("h", "cpu", 1.0)
    db.close()
    # After close, a new connection should see the data
    db2 = Database(db_path)
    workers = db2.list_workers()
    assert len(workers) == 1
    db2.close()


def test_register_worker_records_reported_capacity(db):
    db.register_worker("gpu-box", "cuda", 90.0, "NVIDIA A100",
                       cpu_cores=16, gpu_memory_total_mb=40000.0)
    device = db.list_devices()[0]
    assert device["cpu_cores"] == 16
    assert device["gpu_memory_total_mb"] == 40000.0
    assert device["gpu_memory_free_mb"] is None  # no heartbeat yet: unknown


def test_list_devices_reports_live_free_vram(db):
    wid = db.register_worker("gpu-box", "cuda", 90.0, "NVIDIA A100",
                             cpu_cores=16, gpu_memory_total_mb=40000.0)
    db.heartbeat(wid, gpu_memory_free_mb=12345.0)
    assert db.list_devices()[0]["gpu_memory_free_mb"] == 12345.0


# -- "not measured yet" vs "measured, and it is zero" ----------------------
#
# These two used to be the same number on the wire. A worker that had
# registered a second ago and a card running flat out both reported
# free=0/total=40000, because list_devices() COALESCEd the missing worker_stats
# reading to 0.0. Clients read an absent/unknown capacity permissively and a
# reported zero strictly — both correct readings, landing on the wrong cases.


def test_unmeasured_free_vram_is_distinguishable_from_a_measured_zero(db):
    """The whole defect, in one assertion: the two states differ on the wire."""
    fresh = db.register_worker("just-joined", "cuda", 90.0, "NVIDIA A100",
                               gpu_memory_total_mb=40000.0)
    full = db.register_worker("flat-out", "cuda", 90.0, "NVIDIA A100",
                              gpu_memory_total_mb=40000.0)
    db.heartbeat(full, gpu_memory_free_mb=0.0)

    by_id = {d["id"]: d for d in db.list_devices()}
    assert by_id[fresh]["gpu_memory_free_mb"] is None
    assert by_id[full]["gpu_memory_free_mb"] == 0.0
    # Same total, so the pair is only telling them apart by the free reading.
    assert by_id[fresh]["gpu_memory_total_mb"] == by_id[full]["gpu_memory_total_mb"]


def test_register_worker_records_the_free_vram_the_worker_sends(db):
    """The probe already measured it at join time — recording it costs nothing.

    This is what shrinks the unknown window to zero rather than to one
    heartbeat interval.
    """
    db.register_worker("gpu-box", "cuda", 90.0, "NVIDIA A100",
                       gpu_memory_total_mb=40000.0,
                       gpu_memory_free_mb=39000.0)
    assert db.list_devices()[0]["gpu_memory_free_mb"] == 39000.0


def test_register_worker_ignores_an_unusable_free_vram_figure(db):
    """A junk figure at registration is dropped, not stored, not crashed on."""
    db.register_worker("gpu-box", "cuda", 90.0, "NVIDIA A100",
                       gpu_memory_total_mb=40000.0,
                       gpu_memory_free_mb="lots")
    assert db.list_devices()[0]["gpu_memory_free_mb"] is None


def test_device_summary_passes_the_unknown_reading_through(db):
    """device_summary() aggregates counts and scores; it must not touch this."""
    db.register_worker("gpu-box", "cuda", 90.0, "NVIDIA A100",
                       gpu_memory_total_mb=40000.0)
    summary = db.device_summary()
    assert summary["alive_devices"] == 1
    assert summary["devices"][0]["gpu_memory_free_mb"] is None


def test_unknown_free_vram_does_not_break_the_leaser(db):
    """The consumer that would have hit a None in a comparison."""
    wid = db.register_worker("rig", "cuda", 90.0, "NVIDIA A100",
                             gpu_memory_total_mb=40000.0)
    db.create_job("j", "s", [{"cost": 1, "gpu_memory_mb": 16384.0}])
    assert db.lease_task(wid) is not None  # falls back to the registered total


def test_unknown_free_vram_does_not_break_the_unsatisfiable_detector(db):
    wid = db.register_worker("rig", "cuda", 90.0, "NVIDIA A100",
                             gpu_memory_total_mb=40000.0)
    db.create_job("j", "s", [{"cost": 1, "gpu_memory_mb": 16384.0}])
    _age_pending_tasks(db, UNSATISFIABLE_AFTER + 5)
    assert db.fail_unsatisfiable_tasks() == []  # not condemned, and no raise
    assert db.lease_task(wid) is not None


def test_a_measured_zero_keeps_the_worker_off_a_memory_hinted_task(db):
    """A full card is not a card with room, whatever its total says.

    The leaser used to test truthiness, so a measured 0 was read as "nothing
    reported" and fell back to the 40 GB total — routing the work to the one
    machine in the mesh certain not to fit it.
    """
    wid = db.register_worker("rig", "cuda", 90.0, "NVIDIA A100",
                             gpu_memory_total_mb=40000.0)
    db.heartbeat(wid, gpu_memory_free_mb=0.0)
    db.create_job("j", "s", [{"cost": 1, "gpu_memory_mb": 16384.0}])
    assert db.lease_task(wid) is None


def test_legacy_not_null_free_vram_column_becomes_nullable(tmp_path):
    """A pre-fix database can store 'nobody has measured this' after upgrade.

    Until the column loses NOT NULL DEFAULT 0.0 there is nowhere to *put* the
    distinction, so the wire stays ambiguous no matter what list_devices()
    selects.
    """
    path = str(tmp_path / "legacy_stats.db")
    legacy_schema = SCHEMA.replace(
        "    gpu_memory_free_mb REAL\n",
        "    gpu_memory_free_mb REAL NOT NULL DEFAULT 0.0\n",
    )
    assert "gpu_memory_free_mb REAL NOT NULL" in legacy_schema
    conn = sqlite3.connect(path)
    conn.executescript(legacy_schema)
    now = time.time()
    conn.execute(
        "INSERT INTO workers (id, hostname, device, device_name, score,"
        " gpu_memory_total_mb, registered_at, last_seen) VALUES ('old1',"
        " 'legacy', 'cuda', 'NVIDIA A100', 50.0, 40000.0, ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO worker_stats (worker_id, tasks_completed, gpu_memory_free_mb)"
        " VALUES ('old1', 3, 1234.0)"
    )
    conn.commit()
    conn.close()

    db = Database(path)
    try:
        # Values already recorded survive verbatim — the migration must not
        # invent a NULL over a reading somebody actually took.
        assert db.list_devices()[0]["gpu_memory_free_mb"] == 1234.0
        wid = db.register_worker("new", "cuda", 60.0, "NVIDIA A100",
                                 gpu_memory_total_mb=24000.0)
        by_id = {d["id"]: d for d in db.list_devices()}
        assert by_id[wid]["gpu_memory_free_mb"] is None
    finally:
        db.close()


# -- a junk heartbeat must not silently disqualify a healthy worker ---------


@pytest.mark.parametrize("junk", ["lots", True, float("nan"), float("inf"), -1.0])
def test_junk_heartbeat_keeps_the_worker_alive_and_leasable(db, junk):
    """Ignore the field, keep the worker.

    Refusing the heartbeat outright would stop refreshing last_seen and mark a
    running machine dead over one malformed number. Storing the value is what
    used to happen — SQLite takes a string in a REAL column without complaint,
    every later comparison raised, and _worker_can_run answered False because
    False is the only answer it may give. The 40 GB rig went on looking healthy
    in ``gpumesh workers`` and quietly stopped being offered any memory-hinted
    task.
    """
    wid = db.register_worker("rig", "cuda", 90.0, "NVIDIA A100",
                             gpu_memory_total_mb=40000.0)
    assert db.heartbeat(wid, gpu_memory_free_mb=junk) is True
    assert db.list_workers()[0]["alive"] is True
    assert db.list_devices()[0]["gpu_memory_free_mb"] is None

    db.create_job("j", "s", [{"cost": 1, "gpu_memory_mb": 1024.0}])
    assert db.lease_task(wid) is not None


def test_junk_heartbeat_keeps_the_last_good_reading(db):
    wid = db.register_worker("rig", "cuda", 90.0, "NVIDIA A100",
                             gpu_memory_total_mb=40000.0)
    db.heartbeat(wid, gpu_memory_free_mb=20000.0)
    db.heartbeat(wid, gpu_memory_free_mb="lots")
    assert db.list_devices()[0]["gpu_memory_free_mb"] == 20000.0


def test_junk_heartbeat_is_announced(db, capsys):
    """Silent is the one thing it must not be."""
    wid = db.register_worker("rig", "cuda", 90.0, "NVIDIA A100")
    db.heartbeat(wid, gpu_memory_free_mb="lots")
    out = capsys.readouterr().out
    assert wid in out
    assert "'lots'" in out
    assert "gpu_memory_free_mb" in out


def test_repeated_junk_heartbeats_do_not_flood_the_log(db, capsys):
    """A worker heartbeats every few seconds; one line per beat buries it."""
    wid = db.register_worker("rig", "cuda", 90.0, "NVIDIA A100")
    for _ in range(5):
        db.heartbeat(wid, gpu_memory_free_mb="lots")
    assert capsys.readouterr().out.count("gpu_memory_free_mb") == 1
    # A reading that goes wrong in a *new* way is news again.
    db.heartbeat(wid, gpu_memory_free_mb="plenty")
    assert capsys.readouterr().out.count("gpu_memory_free_mb") == 1


def test_a_pre_existing_junk_reading_does_not_disqualify_a_worker(db):
    """Rows written before heartbeat() validated anything still exist.

    The database outlives the upgrade, so the read side has to cope too.
    """
    wid = db.register_worker("rig", "cuda", 90.0, "NVIDIA A100",
                             gpu_memory_total_mb=40000.0)
    with db._lock, db._conn:
        db._conn.execute(
            "UPDATE worker_stats SET gpu_memory_free_mb = 'lots'"
            " WHERE worker_id = ?", (wid,),
        )
    assert db.list_devices()[0]["gpu_memory_free_mb"] is None
    db.create_job("j", "s", [{"cost": 1, "gpu_memory_mb": 1024.0}])
    assert db.lease_task(wid) is not None


def test_device_summary_carries_capacity(db):
    db.register_worker("gpu-box", "cuda", 90.0, "NVIDIA A100",
                       cpu_cores=16, gpu_memory_total_mb=40000.0)
    summary = db.device_summary()
    assert summary["devices"][0]["cpu_cores"] == 16


def test_legacy_database_gains_capacity_columns(tmp_path):
    """A 1.3.0 database keeps working and picks up the new columns."""
    path = str(tmp_path / "legacy.db")
    legacy_schema = (
        SCHEMA
        .replace("    cpu_cores     INTEGER NOT NULL DEFAULT 0,\n", "")
        .replace("    gpu_memory_total_mb REAL NOT NULL DEFAULT 0.0,\n", "")
    )
    assert "cpu_cores" not in legacy_schema
    conn = sqlite3.connect(path)
    conn.executescript(legacy_schema)
    now = time.time()
    conn.execute(
        "INSERT INTO workers (id, hostname, device, device_name, score,"
        " registered_at, last_seen) VALUES ('old1', 'legacy', 'cuda',"
        " 'NVIDIA A100', 50.0, ?, ?)",
        (now, now),
    )
    conn.commit()
    conn.close()

    db = Database(path)
    try:
        legacy = db.list_devices()[0]
        assert legacy["id"] == "old1"
        assert legacy["cpu_cores"] == 0
        assert legacy["gpu_memory_total_mb"] == 0.0

        wid = db.register_worker("new", "cuda", 60.0, "NVIDIA A100",
                                 cpu_cores=8, gpu_memory_total_mb=24000.0)
        db.create_job("j", "s", [{"cost": 1}])
        assert db.lease_task(wid) is not None
    finally:
        db.close()


def test_lease_honours_gpu_model_hint(db):
    cpu = db.register_worker("laptop", "cpu", 1.0, "Intel i7")
    gpu = db.register_worker("rig", "cuda", 90.0, "NVIDIA A100-SXM4-40GB")
    db.create_job("j", "s", [{"cost": 1, "gpu": "A100"}])

    assert db.lease_task(cpu) is None  # CPU laptop must not take it
    task = db.lease_task(gpu)
    assert task is not None
    assert task["payload"]["gpu"] == "A100"


def test_lease_honours_generic_device_hint(db):
    cpu = db.register_worker("laptop", "cpu", 1.0, "Intel i7")
    gpu = db.register_worker("rig", "cuda", 90.0, "NVIDIA GeForce RTX 4090")
    db.create_job("j", "s", [{"cost": 1, "gpu": "cuda"}])

    assert db.lease_task(cpu) is None
    assert db.lease_task(gpu) is not None


def test_lease_gpu_hint_leaves_other_tasks_available(db):
    cpu = db.register_worker("laptop", "cpu", 1.0, "Intel i7")
    db.create_job("j", "s", [{"cost": 5, "gpu": "A100"}, {"cost": 1}])

    task = db.lease_task(cpu)
    assert task is not None
    assert "gpu" not in task["payload"]  # only the unhinted task is eligible
    assert db.lease_task(cpu) is None


def test_lease_memory_hint_falls_back_to_registered_total(db):
    """Without a heartbeat the registered total VRAM is all we know — use it.

    Validation in @accelerate accepts a worker on that same basis, so
    ignoring it here would strand a task that passed the pre-flight check.
    """
    wid = db.register_worker("rig", "cuda", 90.0, "NVIDIA A100",
                             gpu_memory_total_mb=40000.0)
    db.create_job("j", "s", [{"cost": 1, "gpu_memory_mb": 16384.0}])
    assert db.lease_task(wid) is not None


def test_lease_memory_hint_skips_worker_without_enough_vram(db):
    small = db.register_worker("small", "cuda", 20.0, "GTX 1650",
                               gpu_memory_total_mb=4096.0)
    big = db.register_worker("big", "cuda", 90.0, "NVIDIA A100",
                             gpu_memory_total_mb=40000.0)
    db.create_job("j", "s", [{"cost": 1, "gpu_memory_mb": 16384.0}])

    assert db.lease_task(small) is None
    assert db.lease_task(big) is not None


def _age_pending_tasks(db, seconds):
    """Pretend every pending task was submitted ``seconds`` ago."""
    with db._lock, db._conn:
        db._conn.execute(
            "UPDATE tasks SET created_at = created_at - ? WHERE status = 'pending'",
            (seconds,),
        )


def test_lease_strong_worker_gets_the_heavy_task_on_two_worker_mesh(db):
    """The weaker machine must not walk off with the expensive task.

    The weak worker asks first on purpose: that is the order that exposed the
    inversion, because the percentile was 1-based and put the weakest worker
    at the halfway mark of the queue instead of the bottom.
    """
    weak = db.register_worker("weak", "cpu", 5.0)
    strong = db.register_worker("strong", "cuda", 500.0)
    db.create_job("j", "s", [{"cost": 1}, {"cost": 1000}])

    assert db.lease_task(weak)["cost"] == 1.0
    assert db.lease_task(strong)["cost"] == 1000.0


def test_lease_three_workers_map_onto_matching_task_weights(tmp_path):
    """Each worker's first pick lands on its own slot, not one slot up.

    Every worker is asked on a fresh mesh so the assertion is about the
    mapping itself and not about what earlier leases already took.
    """
    picks = []
    for idx in range(3):
        db = Database(str(tmp_path / ("mesh%d.db" % idx)))
        try:
            workers = [
                db.register_worker("weak", "cpu", 1.0),
                db.register_worker("mid", "cpu", 50.0),
                db.register_worker("strong", "cuda", 100.0),
            ]
            db.create_job("j", "s", [{"cost": 1}, {"cost": 10}, {"cost": 100}])
            picks.append(db.lease_task(workers[idx])["cost"])
        finally:
            db.close()
    assert picks == [1.0, 10.0, 100.0]


def test_lease_equal_scores_do_not_collapse_onto_one_task(db):
    """Tied workers share the queue instead of fighting over one slot."""
    workers = [db.register_worker("w%d" % i, "cpu", 7.0) for i in range(3)]
    db.create_job("j", "s", [{"cost": 1}, {"cost": 2}, {"cost": 3}])
    costs = sorted(db.lease_task(w)["cost"] for w in workers)
    assert costs == [1.0, 2.0, 3.0]


# -- sequential leasing from one shared queue ------------------------------
#
# The test above and test_lease_three_workers_map_onto_matching_task_weights
# both hand every worker a full queue. That pins the ranking function, but a
# real mesh never looks like it: workers poll independently and every lease
# removes a task from under the next caller. The queue dynamics are their own
# failure surface, and an inversion that has been fixed twice already lived
# there both times, so it gets its own coverage below.


def _mesh(tmp_path, tag, scores, costs):
    """A fresh coordinator with these workers and one job of these costs."""
    db = Database(str(tmp_path / ("mesh-%s.db" % tag)))
    workers = [db.register_worker("w%d" % i, "cpu", s)
               for i, s in enumerate(scores)]
    db.create_job("j", "s", [{"cost": c} for c in costs])
    return db, workers


def _drain(db, workers, order):
    """One lease per worker, in ``order``, from a single shrinking queue.

    Nothing is completed in between on purpose: a completed task leaves the
    queue for good and re-frames every later lease, which the scheduler does
    not promise anything about. Held-but-unfinished is the state a mesh is
    actually in while it hands work out.
    """
    return {w: (db.lease_task(workers[w]) or {}).get("cost") for w in order}


def _assert_monotonic(scores, got, note=""):
    """No worker may hold a heavier task than a strictly stronger worker."""
    for a, cost_a in got.items():
        for b, cost_b in got.items():
            if cost_a is None or cost_b is None or scores[a] >= scores[b]:
                continue
            assert cost_a <= cost_b, (
                "%s: worker score %s got cost %s while stronger score %s got"
                " cost %s (scores=%s, leased=%s)"
                % (note, scores[a], cost_a, scores[b], cost_b, scores, got)
            )


def test_lease_sequential_queue_places_the_reported_shapes(tmp_path):
    """The two shapes that the earlier fix left inverted.

    Both come out right when each worker sees a full queue, which is why they
    survived; leased one after another they used to hand the strongest worker
    a lighter task than a weaker one had already taken.
    """
    cases = [
        ("a", [1.0, 50.0, 900.0], [1.0, 10.0, 100.0], [1.0, 10.0, 100.0]),
        ("b", [1.0, 10.0, 100.0, 1000.0], [1.0, 2.0, 3.0, 4.0],
         [1.0, 2.0, 3.0, 4.0]),
    ]
    for tag, scores, costs, expected in cases:
        db, workers = _mesh(tmp_path, tag, scores, costs)
        try:
            got = _drain(db, workers, range(len(scores)))
            assert [got[i] for i in range(len(scores))] == expected
        finally:
            db.close()


def test_lease_sequential_queue_is_monotonic_across_shapes(tmp_path):
    """The documented invariant, over the shapes and poll orders a mesh sees.

    Equal workers and tasks, more tasks than workers, tied scores, and — the
    part nothing in reality guarantees — poll orders other than weakest-first.
    The orders are seeded so a failure reproduces.
    """
    rng = random.Random(20240815)
    shapes = []
    for k in range(2, 7):
        for scores in ([float(2 ** i) for i in range(k)],      # all distinct
                       [float(1 + i // 2) for i in range(k)]):  # tied in pairs
            for m in (k, k + 1, 2 * k):                # never fewer than k
                shapes.append((scores, [float(i + 1) for i in range(m)]))

    for si, (scores, costs) in enumerate(shapes):
        k = len(scores)
        orders = [list(range(k)), list(reversed(range(k)))]
        for _ in range(3):
            shuffled = list(range(k))
            rng.shuffle(shuffled)
            orders.append(shuffled)
        for oi, order in enumerate(orders):
            db, workers = _mesh(tmp_path, "%d-%d" % (si, oi), scores, costs)
            try:
                got = _drain(db, workers, order)
                assert sorted(c for c in got.values() if c is not None) == \
                    sorted(set(c for c in got.values() if c is not None)), \
                    "a task was handed out twice"
                _assert_monotonic(scores, got,
                                  "shape %d order %s" % (si, order))
            finally:
                db.close()


def test_lease_sequential_queue_two_workers(tmp_path):
    """Two machines is the mesh shape this project sees most often.

    It came out right by luck under the old arithmetic, which is part of why
    the inversion on longer rosters went unnoticed. Pin it either way.
    """
    for oi, order in enumerate(([0, 1], [1, 0])):
        db, workers = _mesh(tmp_path, "two-%d" % oi, [5.0, 500.0],
                            [1.0, 7.0, 9.0, 1000.0])
        try:
            got = _drain(db, workers, order)
            assert got[0] == 1.0
            assert got[1] == 1000.0
        finally:
            db.close()


def test_lease_sequential_queue_with_tied_scores(tmp_path):
    """Identical laptops must still split the queue rather than collide."""
    db, workers = _mesh(tmp_path, "tied", [7.0, 7.0, 7.0, 7.0],
                        [1.0, 2.0, 3.0, 4.0])
    try:
        got = _drain(db, workers, [2, 0, 3, 1])
        assert sorted(got.values()) == [1.0, 2.0, 3.0, 4.0]
    finally:
        db.close()


def test_lease_sequential_queue_more_workers_than_tasks(tmp_path):
    """Fewer tasks than workers: the queue still drains, somebody misses out.

    The docstring does not promise monotonicity here — the bands overlap once
    the queue is shorter than the roster — but the queue must still empty, and
    weakest-first (the order the coordinator's own polling tends to produce)
    must still come out in order.
    """
    scores = [1.0, 10.0, 100.0, 1000.0]
    db, workers = _mesh(tmp_path, "starved", scores, [1.0, 2.0])
    try:
        got = _drain(db, workers, range(len(scores)))
        assert sorted(c for c in got.values() if c is not None) == [1.0, 2.0]
        assert sum(1 for c in got.values() if c is None) == 2
        _assert_monotonic(scores, got, "more workers than tasks")
    finally:
        db.close()


def test_lease_solo_worker_short_circuit_survives_the_in_flight_frame(db):
    """One live worker still takes the heaviest task left, lease after lease.

    The ranking now measures against running tasks as well as pending ones,
    and the solo path must not start counting its own held tasks as rivals.
    """
    wid = db.register_worker("only", "cpu", 1.0)
    db.create_job("j", "s", [{"cost": 1}, {"cost": 5}, {"cost": 3}])
    assert db.lease_task(wid)["cost"] == 5.0
    assert db.lease_task(wid)["cost"] == 3.0
    assert db.lease_task(wid)["cost"] == 1.0
    assert db.lease_task(wid) is None


def test_lease_ignores_tasks_that_already_finished(db):
    """Done and failed tasks leave the queue; only in-flight work frames it."""
    weak = db.register_worker("weak", "cpu", 1.0)
    strong = db.register_worker("strong", "cuda", 100.0)
    db.create_job("j", "s", [{"cost": c} for c in (1, 2, 3, 4)])

    first = db.lease_task(weak)
    assert first["cost"] == 1.0
    db.complete_task(first["task_id"], weak, True, result={}, elapsed=1.0)

    # Cost 1 is gone for good, so the queue the strong worker is measured
    # against is [2, 3, 4] and it takes the top of that.
    assert db.lease_task(strong)["cost"] == 4.0
    assert db.lease_task(weak)["cost"] == 2.0


def test_straggler_deprioritization_survives_a_partly_leased_queue(db):
    """A straggler stays in the light half even when the light half is gone.

    The band a straggler's score owns is at the heavy end of the queue; the
    deprioritization filter has to win that argument. When every light task is
    already running the fallback is the lightest task left — never the heavy
    one its band points at.
    """
    fast = db.register_worker("fast", "cpu", 1.0)
    slow = db.register_worker("slow", "cuda", 100.0)

    db.create_job("warmup", "s", [{"cost": 1}, {"cost": 1}])
    t = db.lease_task(fast)
    db.complete_task(t["task_id"], fast, True, result={}, elapsed=1.0)
    t = db.lease_task(slow)
    db.complete_task(t["task_id"], slow, True, result={}, elapsed=500.0)

    db.create_job("j", "s", [{"cost": c} for c in (1, 2, 3, 4)])
    # Let the fast worker claim both light tasks first.
    assert db.lease_task(fast)["cost"] == 1.0
    assert db.lease_task(fast)["cost"] == 2.0
    # Highest score, so its band is the heavy end; deprioritization pulls it
    # back down to the lightest thing still pending.
    assert db.lease_task(slow)["cost"] == 3.0


def test_median_averages_the_two_middle_samples():
    """Even-length lists have no single middle element."""
    assert _median([]) == 0.0
    assert _median([4.0]) == 4.0
    assert _median([1.0, 500.0]) == 250.5
    assert _median([1.0, 1.0, 500.0]) == 1.0


def test_straggler_detected_on_two_worker_mesh(db):
    """A 500x-slower worker is deprioritized even with only two machines.

    Taking the upper middle element made the slow worker its own median, so
    ``avg > 2 * median`` could never fire on the mesh size this project sees
    most often.
    """
    fast = db.register_worker("fast", "cpu", 1.0)
    slow = db.register_worker("slow", "cuda", 100.0)

    db.create_job("warmup", "s", [{"cost": 1}, {"cost": 1}])
    t = db.lease_task(fast)
    db.complete_task(t["task_id"], fast, True, result={}, elapsed=1.0)
    t = db.lease_task(slow)
    db.complete_task(t["task_id"], slow, True, result={}, elapsed=500.0)

    db.create_job("j", "s", [{"cost": 1}, {"cost": 2}, {"cost": 3}, {"cost": 4}])
    # Highest score, so without straggler handling it would take cost=4.
    assert db.lease_task(slow)["cost"] == 2.0


def test_lease_cores_hint_skips_under_provisioned_worker(db):
    """@accelerate(cores=64) must not land on a 2-core laptop."""
    small = db.register_worker("laptop", "cpu", 1.0, "Intel i5", cpu_cores=2)
    big = db.register_worker("server", "cpu", 90.0, "EPYC", cpu_cores=64)
    db.create_job("j", "s", [{"cost": 1, "cpu_cores": 64}])

    assert db.lease_task(small) is None
    task = db.lease_task(big)
    assert task is not None
    assert task["payload"]["cpu_cores"] == 64


def test_lease_cores_hint_skips_worker_that_reported_no_cores(db):
    """0 cores means "never told us", which cannot satisfy a cores request."""
    wid = db.register_worker("legacy", "cpu", 1.0)  # cpu_cores defaults to 0
    db.create_job("j", "s", [{"cost": 1, "cpu_cores": 4}])
    assert db.lease_task(wid) is None


def test_lease_cores_hint_leaves_other_tasks_available(db):
    small = db.register_worker("laptop", "cpu", 1.0, "Intel i5", cpu_cores=2)
    db.create_job("j", "s", [{"cost": 5, "cpu_cores": 64}, {"cost": 1}])

    task = db.lease_task(small)
    assert task is not None
    assert "cpu_cores" not in task["payload"]
    assert db.lease_task(small) is None


def test_unsatisfiable_not_failed_when_mesh_is_empty(db):
    """No workers means nobody has joined yet — that is a wait, not an error."""
    job_id = db.create_job("j", "s", [{"cost": 1, "gpu": "A100"}])
    _age_pending_tasks(db, UNSATISFIABLE_AFTER * 10)

    assert db.fail_unsatisfiable_tasks() == []
    assert db.job_status(job_id)["tasks"][0]["status"] == "pending"


def test_unsatisfiable_not_failed_when_only_dead_workers_remain(db):
    """A worker that stopped heartbeating does not count as a live roster."""
    db.register_worker("laptop", "cpu", 1.0, "Intel i7")
    with db._lock, db._conn:
        db._conn.execute("UPDATE workers SET last_seen = 0")
    job_id = db.create_job("j", "s", [{"cost": 1, "gpu": "A100"}])
    _age_pending_tasks(db, UNSATISFIABLE_AFTER * 10)

    assert db.fail_unsatisfiable_tasks() == []
    assert db.job_status(job_id)["tasks"][0]["status"] == "pending"


def test_unsatisfiable_not_failed_inside_grace_period(db):
    """A GPU box joining a minute late must still find its work waiting."""
    db.register_worker("cpu-laptop", "cpu", 1.0, "Intel i7")
    job_id = db.create_job("j", "s", [{"cost": 1, "gpu": "A100"}])

    assert db.fail_unsatisfiable_tasks() == []
    assert db.job_status(job_id)["tasks"][0]["status"] == "pending"


def test_unsatisfiable_gpu_hint_failed_after_grace_period(db):
    db.register_worker("cpu-laptop", "cpu", 1.0, "Intel i7")
    job_id = db.create_job("j", "s", [{"cost": 1, "gpu": "A100"}])
    _age_pending_tasks(db, 63.0)

    failed = db.fail_unsatisfiable_tasks()
    assert len(failed) == 1
    assert failed[0]["job_id"] == job_id

    task = db.job_status(job_id)["tasks"][0]
    assert task["status"] == "failed"
    assert "gpu='A100'" in task["error"]
    assert "cpu-laptop [cpu]" in task["error"]
    assert "pending for 6" in task["error"]  # 63s, give or take clock jitter


def test_unsatisfiable_spares_tasks_a_live_worker_can_run(db):
    db.register_worker("cpu-laptop", "cpu", 1.0, "Intel i7")
    db.register_worker("rig", "cuda", 90.0, "NVIDIA A100-SXM4-40GB")
    job_id = db.create_job("j", "s", [{"cost": 1, "gpu": "A100"}, {"cost": 1}])
    _age_pending_tasks(db, UNSATISFIABLE_AFTER * 10)

    assert db.fail_unsatisfiable_tasks() == []
    statuses = {t["status"] for t in db.job_status(job_id)["tasks"]}
    assert statuses == {"pending"}


def test_unsatisfiable_names_a_cores_requirement(db):
    db.register_worker("cpu-laptop", "cpu", 1.0, "Intel i7", cpu_cores=2)
    job_id = db.create_job("j", "s", [{"cost": 1, "cpu_cores": 64}])
    _age_pending_tasks(db, UNSATISFIABLE_AFTER + 5)

    assert len(db.fail_unsatisfiable_tasks()) == 1
    assert "cpu_cores=64" in db.job_status(job_id)["tasks"][0]["error"]


def test_unsatisfiable_reports_a_combination_no_single_worker_has(db):
    """Each requirement is met somewhere, but never on the same machine."""
    db.register_worker("gpu-box", "cuda", 90.0, "NVIDIA A100", cpu_cores=2)
    db.register_worker("big-cpu", "cpu", 40.0, "EPYC", cpu_cores=64)
    job_id = db.create_job("j", "s", [{"cost": 1, "gpu": "A100", "cpu_cores": 64}])
    _age_pending_tasks(db, UNSATISFIABLE_AFTER + 5)

    assert len(db.fail_unsatisfiable_tasks()) == 1
    error = db.job_status(job_id)["tasks"][0]["error"]
    assert "on one worker" in error


def test_unsatisfiable_memory_hint_uses_live_free_vram(db):
    """Validation was a submit-time snapshot; the heartbeat moves the number."""
    wid = db.register_worker("rig", "cuda", 90.0, "NVIDIA A100",
                             gpu_memory_total_mb=40000.0)
    job_id = db.create_job("j", "s", [{"cost": 1, "gpu_memory_mb": 16384.0}])
    _age_pending_tasks(db, UNSATISFIABLE_AFTER + 5)

    # Plenty of free memory at submit time: nothing to fail.
    assert db.fail_unsatisfiable_tasks() == []
    db.heartbeat(wid, gpu_memory_free_mb=512.0)
    assert len(db.fail_unsatisfiable_tasks()) == 1
    assert "gpu_memory_mb=16384" in db.job_status(job_id)["tasks"][0]["error"]


def _legacy_db_with_a_pending_hinted_task(path, payload='{"gpu": "A100"}'):
    """A 1.3.0-era database: tasks table with no created_at column at all.

    Returns nothing; the caller opens a Database on ``path`` to trigger the
    migration under test.
    """
    legacy_schema = SCHEMA.replace(
        "    created_at    REAL NOT NULL DEFAULT 0.0,\n", ""
    )
    assert "created_at    REAL" not in legacy_schema
    conn = sqlite3.connect(path)
    conn.executescript(legacy_schema)
    conn.execute(
        "INSERT INTO jobs (id, name, script, created_at)"
        " VALUES ('j1', 'j', 's', ?)",
        (time.time(),),
    )
    conn.execute(
        "INSERT INTO tasks (id, job_id, payload, cost)"
        " VALUES ('t1', 'j1', ?, 1.0)",
        (payload,),
    )
    conn.commit()
    conn.close()


def test_upgraded_database_gives_pre_migration_tasks_a_fresh_grace_period(tmp_path):
    """An upgraded row is judged as if submitted now — not condemned at once.

    The coordinator has just restarted, so the roster is whichever machine
    reconnected first. Failing a GPU task because only the CPU laptop is back
    yet is exactly the false positive UNSATISFIABLE_AFTER exists to prevent.
    """
    path = str(tmp_path / "legacy_tasks.db")
    _legacy_db_with_a_pending_hinted_task(path)

    db = Database(path)
    try:
        db.register_worker("cpu-laptop", "cpu", 1.0, "Intel i7")
        assert db.fail_unsatisfiable_tasks() == []
        assert db.job_status("j1")["tasks"][0]["status"] == "pending"
    finally:
        db.close()


def test_upgraded_database_eventually_fails_a_stranded_hinted_task(tmp_path):
    """The pre-migration row must age out, not sit pending forever.

    This is the whole defect: created_at defaulted to 0, the detector refused
    to age a row whose age it could not know, and a gpu=-hinted task that no
    live worker could satisfy was never leased *and* never failed — the client
    only ever saw its own 300s timeout, across every restart, forever.
    """
    path = str(tmp_path / "legacy_stranded.db")
    _legacy_db_with_a_pending_hinted_task(path)

    db = Database(path)
    try:
        db.register_worker("cpu-laptop", "cpu", 1.0, "Intel i7")
        _age_pending_tasks(db, UNSATISFIABLE_AFTER + 5)

        failed = db.fail_unsatisfiable_tasks()
        assert len(failed) == 1
        task = db.job_status("j1")["tasks"][0]
        assert task["status"] == "failed"
        assert "gpu='A100'" in task["error"]
    finally:
        db.close()


def test_migration_backfills_created_at_at_the_migration_timestamp(tmp_path):
    path = str(tmp_path / "legacy_stamp.db")
    _legacy_db_with_a_pending_hinted_task(path)

    before = time.time()
    db = Database(path)
    try:
        after = time.time()
        created_at = db._conn.execute(
            "SELECT created_at FROM tasks WHERE id = 't1'"
        ).fetchone()[0]
        assert before <= created_at <= after
    finally:
        db.close()


def test_migration_heals_rows_a_previous_upgrade_left_at_zero(tmp_path):
    """The column already exists and holds 0 — the common real-world case.

    A coordinator upgraded to the release that ADDED created_at wrote 0 into
    every queued row. Those rows are the ones actually stranded in the wild,
    and they are invisible to a backfill that only runs when the ALTER does.
    """
    path = str(tmp_path / "half_migrated.db")
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)  # created_at exists, DEFAULT 0.0
    conn.execute(
        "INSERT INTO jobs (id, name, script, created_at)"
        " VALUES ('j1', 'j', 's', ?)",
        (time.time(),),
    )
    conn.execute(
        "INSERT INTO tasks (id, job_id, payload, cost)"
        " VALUES ('t1', 'j1', '{\"gpu\": \"A100\"}', 1.0)"
    )
    conn.commit()
    conn.close()

    db = Database(path)
    try:
        assert db._conn.execute(
            "SELECT created_at FROM tasks WHERE id = 't1'"
        ).fetchone()[0] > 0
        db.register_worker("cpu-laptop", "cpu", 1.0, "Intel i7")
        _age_pending_tasks(db, UNSATISFIABLE_AFTER + 5)
        assert len(db.fail_unsatisfiable_tasks()) == 1
    finally:
        db.close()


def test_migration_does_not_disturb_a_healthy_created_at(tmp_path):
    """Reopening a current database must not restart anybody's clock."""
    path = str(tmp_path / "healthy.db")
    db = Database(path)
    db.create_job("j", "s", [{"cost": 1}])
    original = db._conn.execute("SELECT created_at FROM tasks").fetchone()[0]
    db.close()

    db2 = Database(path)
    try:
        assert db2._conn.execute(
            "SELECT created_at FROM tasks"
        ).fetchone()[0] == original
    finally:
        db2.close()


# -- malformed placement hints ------------------------------------------
#
# The trigger throughout this section is a typo, not an attack: ``gpu=123``
# instead of ``gpu="cuda"``. GPUMesh.distribute() passes gpu=/gpu_memory_mb=/
# cores= straight into the payload with no validation, so the value the user
# typed is the value the scheduler compares against worker capacity.


@pytest.mark.parametrize("payload", [
    {"gpu": 123},                 # gpu=123 instead of gpu="cuda"
    {"gpu": ["A100"]},
    {"gpu": {"model": "A100"}},
    {"gpu_memory_mb": "8GB"},     # memory="8GB" leaking through unparsed
    {"gpu_memory_mb": [16384]},
    {"cpu_cores": "many"},
    {"cpu_cores": None, "gpu": 1.5},
    {"gpu_memory_mb": float("nan")},
    "not-a-dict",
    None,
])
def test_worker_can_run_is_total(payload):
    """Any payload at all must produce a verdict, never an exception.

    Not politeness: this helper runs inside lease_task (where a raise 500s
    every worker's /api/lease for every task) and inside the coordinator's
    daemon reaper (where a raise kills the thread for good).
    """
    verdict = _worker_can_run(payload, "cuda", "NVIDIA A100", 8, 40000.0)
    assert verdict is True or verdict is False


def test_worker_can_run_refuses_an_uninterpretable_hint():
    """False, not True: a hint we cannot read must not be silently dropped.

    Ignoring it would run a task the user restricted to an A100 on whatever
    laptop asked first, and never tell them — the map(gpu=...) silent-local-
    fallback failure all over again.
    """
    assert _worker_can_run({"gpu": 123}, "cuda", "NVIDIA A100", 8, 40000.0) is False
    assert _worker_can_run({"gpu_memory_mb": "8GB"}, "cuda", "NVIDIA A100",
                           8, 40000.0) is False
    assert _worker_can_run({"cpu_cores": "eight"}, "cuda", "NVIDIA A100",
                           8, 40000.0) is False
    # ...while the same hints expressed properly still match.
    assert _worker_can_run({"gpu": "A100"}, "cuda", "NVIDIA A100",
                           8, 40000.0) is True
    assert _worker_can_run({"gpu_memory_mb": 8192}, "cuda", "NVIDIA A100",
                           8, 40000.0) is True


def _poison_one_task(db, cost, payload_json):
    """Rewrite one task's payload behind create_job's back.

    Models the row an older coordinator wrote, which is the only way a
    malformed hint reaches the scheduler now that create_job() rejects one.
    """
    with db._lock, db._conn:
        db._conn.execute(
            "UPDATE tasks SET payload = ? WHERE cost = ?", (payload_json, cost)
        )


def test_lease_survives_a_string_memory_hint_and_still_serves_the_queue(db):
    """One bad task must not stop the mesh from leasing every other task.

    Unguarded, the ``float < str`` comparison raised TypeError out of
    lease_task, the handler turned it into a 500, and every worker polling
    /api/lease got that 500 for every task in the queue.
    """
    wid = db.register_worker("laptop", "cpu", 1.0, "Intel i7")
    db.create_job("j", "s", [{"cost": 1}, {"cost": 2}, {"cost": 3}])
    _poison_one_task(db, 2.0, '{"gpu_memory_mb": "8GB"}')

    leased = []
    for _ in range(3):
        task = db.lease_task(wid)
        if task is None:
            break
        leased.append(task["cost"])
    assert sorted(leased) == [1.0, 3.0]  # the poisoned one skipped, no raise


def test_lease_survives_a_non_string_gpu_hint(db):
    wid = db.register_worker("rig", "cuda", 90.0, "NVIDIA A100")
    db.create_job("j", "s", [{"cost": 1}, {"cost": 2}])
    _poison_one_task(db, 2.0, '{"gpu": 123}')

    task = db.lease_task(wid)
    assert task is not None
    assert task["cost"] == 1.0
    assert db.lease_task(wid) is None


def test_unsatisfiable_fails_a_malformed_hint_without_raising(db):
    """The reaper-killer: AttributeError from ``123``.strip() used to escape.

    fail_unsatisfiable_tasks caught only (TypeError, ValueError), so this
    propagated into the coordinator's daemon reaper and killed it silently
    for the rest of the process's life.
    """
    db.register_worker("rig", "cuda", 90.0, "NVIDIA A100")
    job_id = db.create_job("j", "s", [{"cost": 1}])
    _poison_one_task(db, 1.0, '{"gpu": 123}')

    failed = db.fail_unsatisfiable_tasks()
    assert len(failed) == 1
    task = db.job_status(job_id)["tasks"][0]
    assert task["status"] == "failed"
    # Names the bad value, and says "malformed" rather than sending the user
    # off to buy hardware they already own.
    assert "gpu=123" in task["error"]
    assert "unusable placement hint" in task["error"]


def test_unsatisfiable_fails_a_malformed_hint_with_no_live_workers(db):
    """No roster is needed: no machine that ever joins can read 123 as a name."""
    job_id = db.create_job("j", "s", [{"cost": 1}])
    _poison_one_task(db, 1.0, '{"gpu_memory_mb": "8GB"}')

    assert len(db.fail_unsatisfiable_tasks()) == 1
    assert db.job_status(job_id)["tasks"][0]["status"] == "failed"


def test_unsatisfiable_malformed_task_does_not_condemn_its_neighbours(db):
    db.register_worker("rig", "cuda", 90.0, "NVIDIA A100")
    job_id = db.create_job("j", "s", [{"cost": 1}, {"cost": 2, "gpu": "A100"}])
    _poison_one_task(db, 1.0, '{"gpu": 123}')
    _age_pending_tasks(db, UNSATISFIABLE_AFTER * 10)

    assert len(db.fail_unsatisfiable_tasks()) == 1
    statuses = sorted(t["status"] for t in db.job_status(job_id)["tasks"])
    assert statuses == ["failed", "pending"]


@pytest.mark.parametrize("payload,fragment", [
    ({"gpu": 123}, "gpu=123"),
    ({"gpu_memory_mb": "8GB"}, "gpu_memory_mb='8GB'"),
    ({"cpu_cores": "eight"}, "cpu_cores='eight'"),
    ({"cpu_cores": True}, "cpu_cores=True"),
])
def test_create_job_rejects_a_malformed_hint_at_submission(db, payload, fragment):
    """Answer the submitter now, while they are still holding the typo."""
    with pytest.raises(ValueError) as exc:
        db.create_job("j", "s", [{"cost": 1}, payload])
    assert fragment in str(exc.value)
    # Nothing was written: a refused job leaves no half-created rows behind.
    assert db.job_status_counts() == {"pending": 0, "running": 0,
                                      "done": 0, "failed": 0}


def test_create_job_accepts_every_well_formed_hint(db):
    job_id = db.create_job("j", "s", [
        {"cost": 1, "gpu": "A100"},
        {"cost": 1, "gpu": "cuda:1"},
        {"cost": 1, "gpu_memory_mb": 16384},
        {"cost": 1, "gpu_memory_mb": 16384.5},
        {"cost": 1, "cpu_cores": 8},
        {"cost": 1, "gpu": None, "gpu_memory_mb": None, "cpu_cores": None},
        {"cost": 1},
        {"cost": 1, "n": "not a hint at all"},
    ])
    assert db.job_status(job_id)["counts"] == {"pending": 8}


def test_db_corruption_recovery(tmp_path):
    """Corrupted DB file is recreated automatically."""
    import os
    db_path = str(tmp_path / "corrupt.db")
    # Create a valid DB first
    db = Database(db_path)
    db.register_worker("h", "cpu", 1.0)
    db.close()
    # Corrupt the file
    with open(db_path, "wb") as f:
        f.write(b"not a valid sqlite database")
    # Open should recreate it (old data lost, but no crash)
    db2 = Database(db_path)
    workers = db2.list_workers()
    assert len(workers) == 0  # fresh DB
    db2.close()
