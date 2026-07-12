import threading

import pytest

from gpumesh.db import Database, MAX_ATTEMPTS


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
