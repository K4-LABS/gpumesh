"""Work that has already been computed must not be thrown away.

Three separate defects all ended the same way: a task ran to completion, the
result reached the coordinator or very nearly did, and it was discarded anyway
— so the task sat 'running' until LEASE_SECONDS expired and the whole thing
was recomputed.

  1. The coordinator's shutdown gate answered 503 to everything, /api/result
     included. Because HTTPError subclasses URLError, that 503 landed in the
     worker's "failed to submit result" warning arm: it printed a line,
     counted the task as done, and dropped the payload. The coordinator was
     refusing the delivery of work it had itself handed out.

  2. A junk ``elapsed`` raised TypeError *inside* complete_task's transaction,
     so the UPDATE that marked the task done was rolled back with it. A timing
     figure — an input to an average — un-recorded a task's output.

  3. The worker POSTed a result exactly once and dropped it on any failure,
     despite being the one component explicitly designed to outlive
     coordinator outages.

These tests are the story end to end. Every wait here is bounded: the failure
mode under test is "the result never arrives", and a test that could block
while waiting for it would be no test at all.
"""

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from gpumesh import worker as worker_mod
from gpumesh.server import serve
from gpumesh.worker import MeshClient

TOKEN = "delivery-token"


# ── coordinator plumbing ────────────────────────────────────────────────────


def _start(db_path, port=0):
    httpd = serve("127.0.0.1", port, str(db_path), TOKEN, discovery=False)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_address[1], thread


@pytest.fixture
def coord(tmp_path):
    """A live coordinator plus the pieces a test needs to talk to it."""
    httpd, port, thread = _start(tmp_path / "delivery.db")
    client = MeshClient(f"http://127.0.0.1:{port}", TOKEN)
    yield {
        "httpd": httpd,
        "port": port,
        "client": client,
        "db": httpd.RequestHandlerClass.db,
        "handler": httpd.RequestHandlerClass,
    }
    httpd.gpumesh_stop.set()
    httpd.shutdown()
    thread.join(timeout=15)


def _lease_one(client):
    """Register a worker, queue one task, and lease it. Returns the ids."""
    reg = client.call("POST", "/api/register", {
        "hostname": "delivery-test", "device": "cpu", "score": 1.0,
    })
    worker_id = reg["worker_id"]
    job = client.call("POST", "/api/jobs", {
        "name": "delivery", "script": "print('{}')", "payloads": [{"n": 1}],
    })
    task = client.call("POST", "/api/lease", {"worker_id": worker_id})
    assert task is not None, "the coordinator queued nothing to lease"
    return worker_id, job["job_id"], task["task_id"]


def _task_status(db, job_id, task_id):
    for task in db.job_status(job_id)["tasks"]:
        if task["id"] == task_id:
            return task
    raise AssertionError(f"task {task_id} vanished from job {job_id}")


def _avg_time(db, worker_id):
    """The straggler statistic ``elapsed`` exists to feed. None if untouched."""
    with db._lock:
        row = db._conn.execute(
            "SELECT avg_time FROM worker_stats WHERE worker_id = ?",
            (worker_id,),
        ).fetchone()
    return None if row is None else row[0]


def _post_result(port, body, timeout=15.0):
    """POST /api/result and return (status, parsed body).

    Raw urllib rather than MeshClient because several of these cases are about
    the status code itself, which MeshClient turns into an exception.
    """
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/result",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "X-Auth-Token": TOKEN},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, None


# ── DEFECT 1: the shutdown gate refused completed work ──────────────────────


class TestShutdownGate:

    def test_a_result_posted_during_shutdown_is_recorded(self, coord):
        """The defect itself: the coordinator refusing its own work back.

        The shutdown flag is set directly rather than by calling shutdown(),
        because shutdown() also stops the accept loop — and the window under
        test is precisely the one where the flag is set and the coordinator is
        still listening. That window is real: BaseServer.shutdown() only
        notices its stop flag on the next poll tick, so every worker that
        finishes a task in that stretch used to lose it.
        """
        worker_id, job_id, task_id = _lease_one(coord["client"])
        coord["handler"]._shutdown_event.set()

        status, body = _post_result(coord["port"], {
            "task_id": task_id, "worker_id": worker_id,
            "ok": True, "result": {"value": 42}, "elapsed": 1.25,
        })

        assert status == 200, (
            "a finished result was refused by the shutdown gate — this is the "
            "only copy of work that has already been paid for"
        )
        assert body == {"ok": True}
        task = _task_status(coord["db"], job_id, task_id)
        assert task["status"] == "done"
        assert task["result"] == {"value": 42}

    def test_new_work_is_still_refused_during_shutdown(self, coord):
        """The exemption is for completions, not a hole in the gate.

        register / lease / jobs all ask the coordinator to take on something
        new, and a coordinator that is going away must refuse them: a job it
        will never schedule or a lease it will never collect is worse than a
        503.
        """
        coord["handler"]._shutdown_event.set()

        for path, body in (
            ("/api/register", {"hostname": "late", "device": "cpu", "score": 1.0}),
            ("/api/lease", {"worker_id": "nobody"}),
            ("/api/jobs", {"name": "late", "script": "print(1)",
                           "payloads": [{"n": 1}]}),
        ):
            req = urllib.request.Request(
                f"http://127.0.0.1:{coord['port']}{path}",
                data=json.dumps(body).encode(), method="POST",
                headers={"Content-Type": "application/json",
                         "X-Auth-Token": TOKEN},
            )
            with pytest.raises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(req, timeout=15.0)
            assert caught.value.code == 503, f"{path} should refuse new work"

    def test_heartbeat_stays_refused_during_shutdown(self, coord):
        """Documented on purpose, because it is the near miss.

        A heartbeat is also a message about work already handed out, so it
        looks like it belongs with /api/result. It does not: all it does is
        push a lease expiry forward on a coordinator whose database is about
        to close. Nothing is lost by refusing it, and the worker reads the
        failure as "reconnect later" — the correct conclusion.
        """
        worker_id, _, task_id = _lease_one(coord["client"])
        coord["handler"]._shutdown_event.set()

        req = urllib.request.Request(
            f"http://127.0.0.1:{coord['port']}/api/heartbeat",
            data=json.dumps({"worker_id": worker_id,
                             "task_id": task_id}).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "X-Auth-Token": TOKEN},
        )
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=15.0)
        assert caught.value.code == 503

    def test_the_exempt_route_still_checks_the_token(self, coord):
        """Routing before the gate must not mean routing before auth."""
        coord["handler"]._shutdown_event.set()
        req = urllib.request.Request(
            f"http://127.0.0.1:{coord['port']}/api/result",
            data=json.dumps({"task_id": "x", "worker_id": "y",
                             "ok": True}).encode(),
            method="POST",
            headers={"Content-Type": "application/json",
                     "X-Auth-Token": "wrong-token"},
        )
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=15.0)
        assert caught.value.code == 401

    def test_shutdown_waits_for_a_request_it_already_accepted(self, tmp_path):
        """The gate change is only worth anything if the database outlives it.

        An /api/result let through the door and then met with a closed
        database has lost exactly the work the exemption exists to save. The
        old drain was a flat half-second sleep — a guess. This holds a request
        inside the handler for longer than that guess and checks the database
        was still open when it landed.
        """
        httpd, port, thread = _start(tmp_path / "drain.db")
        client = MeshClient(f"http://127.0.0.1:{port}", TOKEN)
        worker_id, job_id, task_id = _lease_one(client)
        db = httpd.RequestHandlerClass.db

        # Make one complete_task slow, so it is guaranteed to still be running
        # when shutdown starts. 1.5s is comfortably past the old 0.5s sleep.
        real_complete = db.complete_task
        entered = threading.Event()

        def slow_complete(*args, **kwargs):
            entered.set()
            time.sleep(1.5)
            return real_complete(*args, **kwargs)

        db.complete_task = slow_complete
        outcome = {}

        def post():
            outcome["status"], outcome["body"] = _post_result(port, {
                "task_id": task_id, "worker_id": worker_id,
                "ok": True, "result": {"slow": True}, "elapsed": 1.5,
            }, timeout=30.0)

        poster = threading.Thread(target=post, daemon=True)
        poster.start()
        assert entered.wait(15.0), "the result POST never reached the database"

        httpd.gpumesh_stop.set()
        httpd.shutdown()
        poster.join(timeout=30)
        thread.join(timeout=15)
        db.complete_task = real_complete

        assert outcome.get("status") == 200, (
            f"an in-flight /api/result was answered {outcome.get('status')} — "
            f"the database was closed out from under it"
        )
        # Read the row back through a fresh connection: this coordinator's own
        # database is closed by now, which is the whole point.
        from gpumesh.db import Database

        reopened = Database(str(tmp_path / "drain.db"))
        try:
            assert _task_status(reopened, job_id, task_id)["status"] == "done"
        finally:
            reopened.close()


# ── DEFECT 2: a junk 'elapsed' rolled back the write ────────────────────────


class TestElapsedCannotDestroyAResult:

    @pytest.mark.parametrize("elapsed", [
        "fast", "", "1.5s", {"seconds": 2}, [1], True, False, -3.0, 0,
        float("inf"), float("nan"),
    ])
    def test_junk_elapsed_still_records_the_result(self, coord, elapsed):
        """The timing figure feeds an average; the result is the work.

        Losing a task's output to protect a straggler statistic is exactly
        backwards, so anything unusable degrades to "no timing information".
        """
        worker_id, job_id, task_id = _lease_one(coord["client"])

        status, body = _post_result(coord["port"], {
            "task_id": task_id, "worker_id": worker_id,
            "ok": True, "result": {"value": 7}, "elapsed": elapsed,
        })

        assert status == 200, f"elapsed={elapsed!r} cost the task its result"
        assert body == {"ok": True}
        task = _task_status(coord["db"], job_id, task_id)
        assert task["status"] == "done", (
            f"elapsed={elapsed!r} left the task 'running' — it will be "
            f"recomputed when the lease expires"
        )
        assert task["result"] == {"value": 7}

    def test_missing_elapsed_is_fine(self, coord):
        worker_id, job_id, task_id = _lease_one(coord["client"])
        status, _ = _post_result(coord["port"], {
            "task_id": task_id, "worker_id": worker_id,
            "ok": True, "result": {"value": 1},
        })
        assert status == 200
        assert _task_status(coord["db"], job_id, task_id)["status"] == "done"

    def test_a_good_elapsed_still_reaches_the_straggler_stats(self, coord):
        """The fix must not have been "ignore elapsed"."""
        worker_id, _, task_id = _lease_one(coord["client"])
        status, _ = _post_result(coord["port"], {
            "task_id": task_id, "worker_id": worker_id,
            "ok": True, "result": {"value": 1}, "elapsed": 4.0,
        })
        assert status == 200
        assert _avg_time(coord["db"], worker_id) == pytest.approx(4.0)

    def test_a_numeric_string_elapsed_is_coerced_not_dropped(self, coord):
        """A client that stringified a float is not a client sending junk."""
        worker_id, _, task_id = _lease_one(coord["client"])
        status, _ = _post_result(coord["port"], {
            "task_id": task_id, "worker_id": worker_id,
            "ok": True, "result": {"value": 1}, "elapsed": "2.5",
        })
        assert status == 200
        assert _avg_time(coord["db"], worker_id) == pytest.approx(2.5)

    def test_a_junk_error_field_still_records_the_failure(self, coord):
        """Same shape one field along: a dict bound to a TEXT column.

        sqlite raises InterfaceError inside the same transaction, so a
        malformed error message used to discard the failure it was describing
        — and with it the task's retry accounting.
        """
        worker_id, job_id, task_id = _lease_one(coord["client"])

        status, _ = _post_result(coord["port"], {
            "task_id": task_id, "worker_id": worker_id,
            "ok": False, "error": {"type": "ValueError", "msg": "boom"},
        })

        assert status == 200
        task = _task_status(coord["db"], job_id, task_id)
        assert task["status"] in ("pending", "failed")
        assert "boom" in (task["error"] or "")

    def test_a_non_string_task_id_is_a_400_not_a_500(self, coord):
        """These are bound into WHERE clauses; there is nothing to degrade to."""
        status, body = _post_result(coord["port"], {
            "task_id": {"oops": 1}, "worker_id": "w", "ok": True,
        })
        assert status == 400
        assert "task_id" in body["error"]

    def test_a_non_bool_ok_is_still_refused(self, coord):
        """The validation that was already there stays there."""
        status, body = _post_result(coord["port"], {
            "task_id": "t", "worker_id": "w", "ok": "yes",
        })
        assert status == 400
        assert "ok" in body["error"]


# ── DEFECT 3: the worker gave up after one attempt ──────────────────────────


def _http_error(code):
    return urllib.error.HTTPError(
        "http://127.0.0.1/api/result", code, f"status {code}", None, None
    )


class _ScriptedMesh:
    """A MeshClient stand-in that plays a fixed script of outcomes.

    Anything left over after the script runs out succeeds, so a test only has
    to spell out the failures it cares about.
    """

    def __init__(self, script=()):
        self.script = list(script)
        self.calls = []

    def call(self, method, path, body=None, timeout=30.0):
        self.calls.append((method, path, body))
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, BaseException):
                raise step
            return step
        return {"ok": True}


class _UnreachableMesh:
    """A coordinator that is gone for as long as the worker keeps trying.

    Every test that asks "does the retry loop give up in time?" needs a
    coordinator that never comes back, and the obvious way to write that —
    ``_ScriptedMesh([error] * 10_000)`` — does not say it. It says "fails ten
    thousand times, then succeeds", and whether the worker ever reaches that
    ten-thousand-and-first call is a race between the retry pacing and the
    budget, decided by how fast the machine is and how coarse its sleeps are.
    Lose that race and the loop drains the script, the call succeeds, and the
    test reports DELIVERED — failing the assertion it was written to protect,
    for a reason that has nothing to do with the code.

    A mesh that simply never succeeds removes the race rather than widening the
    margin, and it is also the more honest model: a shutting-down worker is not
    waiting for its ten-thousandth POST to land.
    """

    def __init__(self, error=None):
        self.calls = []
        self._error = error if error is not None else urllib.error.URLError("gone")

    def call(self, method, path, body=None, timeout=30.0):
        self.calls.append((method, path, body))
        raise self._error


@pytest.fixture
def fast_retries(monkeypatch):
    """Collapse the backoff so these tests cost milliseconds, not minutes."""
    monkeypatch.setattr(worker_mod, "RESULT_RETRY_BASE", 0.01)
    monkeypatch.setattr(worker_mod, "RESULT_RETRY_MAX", 0.02)
    monkeypatch.setattr(worker_mod, "RESULT_RETRY_BUDGET", 5.0)
    monkeypatch.setattr(worker_mod, "RESULT_RETRY_SHUTDOWN_BUDGET", 0.2)


_PAYLOAD = {"task_id": "t1", "worker_id": "w1", "ok": True,
            "result": {"v": 1}, "elapsed": 0.5}


class TestWorkerRetry:

    def test_the_good_path_posts_exactly_once(self, fast_retries):
        mesh = _ScriptedMesh()
        assert worker_mod._deliver_result(mesh, _PAYLOAD) == worker_mod.DELIVERED
        assert len(mesh.calls) == 1
        assert mesh.calls[0][1] == "/api/result"
        assert mesh.calls[0][2] is _PAYLOAD

    def test_a_transient_failure_is_retried_until_it_lands(self, fast_retries):
        """The 503 from a shutting-down coordinator, and the connection error
        from one that vanished, are both "come back in a moment"."""
        mesh = _ScriptedMesh([
            urllib.error.URLError("connection refused"),
            _http_error(503),
            _http_error(500),
        ])

        outcome = worker_mod._deliver_result(mesh, _PAYLOAD)

        assert outcome == worker_mod.DELIVERED
        assert len(mesh.calls) == 4, "the result was not held across failures"

    def test_a_401_is_not_retried(self, fast_retries):
        """A token does not change while we sleep."""
        mesh = _ScriptedMesh([_http_error(401)] * 10)
        assert worker_mod._deliver_result(mesh, _PAYLOAD) == worker_mod.AUTH_FAILED
        assert len(mesh.calls) == 1

    def test_a_409_is_not_retried(self, fast_retries):
        """The task was re-leased and finished elsewhere — ours is obsolete."""
        mesh = _ScriptedMesh([_http_error(409)] * 10)
        assert worker_mod._deliver_result(mesh, _PAYLOAD) == worker_mod.OBSOLETE
        assert len(mesh.calls) == 1

    @pytest.mark.parametrize("code", [400, 403, 404, 411, 413, 426])
    def test_a_refusal_of_these_bytes_is_not_retried(self, fast_retries, code):
        """The next attempt would send the same bytes."""
        mesh = _ScriptedMesh([_http_error(code)] * 10)
        assert (worker_mod._deliver_result(mesh, _PAYLOAD)
                == worker_mod.UNDELIVERABLE)
        assert len(mesh.calls) == 1

    @pytest.mark.parametrize("code", [408, 429])
    def test_the_two_4xx_that_mean_later_are_retried(self, fast_retries, code):
        mesh = _ScriptedMesh([_http_error(code)])
        assert worker_mod._deliver_result(mesh, _PAYLOAD) == worker_mod.DELIVERED
        assert len(mesh.calls) == 2

    def test_retrying_is_bounded(self, fast_retries):
        """A worker that retried forever would wedge itself with a queue behind it."""
        mesh = _UnreachableMesh()

        started = time.monotonic()
        outcome = worker_mod._deliver_result(mesh, _PAYLOAD)
        elapsed = time.monotonic() - started

        assert outcome == worker_mod.GAVE_UP
        assert elapsed < 30, f"the retry budget did not bound the loop ({elapsed:.1f}s)"

    def test_the_budget_stays_well_inside_the_lease(self):
        """Retrying past the lease means arguing with whoever redid the work.

        Asserted against the coordinator's own constant rather than a retyped
        number: the two drifting apart is how this stops being true.
        """
        from gpumesh.db import LEASE_SECONDS

        assert worker_mod.RESULT_RETRY_BUDGET < LEASE_SECONDS / 2

    def test_a_stop_event_already_set_shortens_the_budget(self, fast_retries):
        """Ctrl+C must not mean "stop in two minutes".

        The mesh here never succeeds, which is what makes the outcome a fact
        about the budget rather than a race — see ``_UnreachableMesh``. With a
        finite script of failures the loop could drain it and land on a success
        inside the shutdown budget, and the GAVE_UP assertion below would then
        fail while the code it is testing was working perfectly.

        The elapsed bound sits between the two budgets ``fast_retries``
        installs — 0.2 s while shutting down, 5 s otherwise — so it fails if
        the shutdown budget is ignored and passes with room to spare on a
        loaded runner.
        """
        mesh = _UnreachableMesh()
        stop = threading.Event()
        stop.set()

        started = time.monotonic()
        outcome = worker_mod._deliver_result(mesh, _PAYLOAD, stop)
        elapsed = time.monotonic() - started

        assert outcome == worker_mod.GAVE_UP
        assert (worker_mod.RESULT_RETRY_SHUTDOWN_BUDGET
                < 2 < worker_mod.RESULT_RETRY_BUDGET), (
            "the bound below no longer separates the two budgets"
        )
        assert elapsed < 2, (
            f"a stopping worker spent {elapsed:.1f}s on delivery — the "
            f"shutdown budget is not being applied"
        )

    def test_a_stop_arriving_mid_retry_is_noticed_at_once(self, monkeypatch):
        """The wait between attempts must be the event, not a sleep.

        The backoff here is longer than the whole budget on purpose: an
        implementation that slept it out instead of waiting on the stop event
        would sit there for the full budget, which is the shutdown regression
        this session already fixed once elsewhere. The budget is kept to 20s
        so a failure is a slow test rather than a hung one.
        """
        monkeypatch.setattr(worker_mod, "RESULT_RETRY_BASE", 30.0)
        monkeypatch.setattr(worker_mod, "RESULT_RETRY_MAX", 30.0)
        monkeypatch.setattr(worker_mod, "RESULT_RETRY_BUDGET", 20.0)
        monkeypatch.setattr(worker_mod, "RESULT_RETRY_SHUTDOWN_BUDGET", 0.1)

        mesh = _UnreachableMesh()
        stop = threading.Event()
        timer = threading.Timer(0.3, stop.set)
        timer.daemon = True
        timer.start()

        started = time.monotonic()
        try:
            outcome = worker_mod._deliver_result(mesh, _PAYLOAD, stop)
        finally:
            timer.cancel()
        elapsed = time.monotonic() - started

        assert outcome == worker_mod.GAVE_UP
        assert elapsed < 10, (
            f"the worker took {elapsed:.1f}s to notice the stop event — the "
            f"backoff is sleeping instead of waiting on it"
        )

    def test_an_unparseable_answer_is_treated_as_transient(self, fast_retries):
        """A coordinator having a bad moment is not a reason to bin the work."""
        mesh = _ScriptedMesh([json.JSONDecodeError("nope", "", 0)])
        assert worker_mod._deliver_result(mesh, _PAYLOAD) == worker_mod.DELIVERED
        assert len(mesh.calls) == 2


# ── end to end ──────────────────────────────────────────────────────────────


def test_a_worker_delivers_into_a_shutting_down_coordinator(coord, fast_retries):
    """All three fixes in one line of causation.

    The worker's own delivery path, against a real coordinator with its
    shutdown flag set, carrying an elapsed value — the exact combination that
    used to end with a 503, a warning, and the work gone.
    """
    worker_id, job_id, task_id = _lease_one(coord["client"])
    coord["handler"]._shutdown_event.set()

    outcome = worker_mod._deliver_result(coord["client"], {
        "task_id": task_id, "worker_id": worker_id,
        "ok": True, "result": {"answer": 42}, "elapsed": 0.75,
    }, threading.Event())

    assert outcome == worker_mod.DELIVERED
    task = _task_status(coord["db"], job_id, task_id)
    assert task["status"] == "done"
    assert task["result"] == {"answer": 42}


def test_a_worker_holds_a_result_across_a_coordinator_restart(tmp_path,
                                                              monkeypatch):
    """The resilience contract, applied to the one payload that matters.

    The worker already survives a coordinator that disappears and comes back —
    it re-registers and reconnects indefinitely. This proves a finished result
    survives the same outage instead of being dropped on the first refused
    POST. The second coordinator reopens the same database file, so the task
    row (still 'running', still leased to this worker) is waiting for it.
    """
    monkeypatch.setattr(worker_mod, "RESULT_RETRY_BASE", 0.05)
    monkeypatch.setattr(worker_mod, "RESULT_RETRY_MAX", 0.2)
    monkeypatch.setattr(worker_mod, "RESULT_RETRY_BUDGET", 60.0)

    db_path = tmp_path / "restart.db"
    httpd, port, thread = _start(db_path)
    client = MeshClient(f"http://127.0.0.1:{port}", TOKEN)
    worker_id, job_id, task_id = _lease_one(client)

    httpd.gpumesh_stop.set()
    httpd.shutdown()
    thread.join(timeout=15)

    outcome = {}

    def deliver():
        outcome["result"] = worker_mod._deliver_result(client, {
            "task_id": task_id, "worker_id": worker_id,
            "ok": True, "result": {"survived": True}, "elapsed": 2.0,
        }, threading.Event())

    delivering = threading.Thread(target=deliver, daemon=True)
    delivering.start()
    # Long enough that a worker which posts once and gives up has certainly
    # finished doing so. Not a half-second: a connect to a just-closed
    # loopback port can sit for around a second on Windows before it is
    # refused, which is long enough to hide the difference this test is for.
    time.sleep(3.0)
    assert delivering.is_alive(), (
        "the worker let go of a finished result while the coordinator was "
        "briefly away — nothing else has a copy of it"
    )

    httpd2, _, thread2 = _start(db_path, port=port)
    try:
        delivering.join(timeout=45)
        assert not delivering.is_alive(), "the delivery never finished"
        assert outcome["result"] == worker_mod.DELIVERED, (
            "the worker dropped a finished result because the coordinator was "
            "briefly away"
        )
        task = _task_status(httpd2.RequestHandlerClass.db, job_id, task_id)
        assert task["status"] == "done"
        assert task["result"] == {"survived": True}
    finally:
        httpd2.gpumesh_stop.set()
        httpd2.shutdown()
        thread2.join(timeout=15)
