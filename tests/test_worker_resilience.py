"""Tests for worker resilience: surviving coordinator outage and auto-reconnect.

These tests verify the Phase 1 reliability rework:
  1. Workers no longer have permanent-exit thresholds (the old health-check
     3-strike exit and the 120s coordinator-timeout exit are gone).
  2. Workers survive a coordinator outage (laptop sleep / WiFi drop /
     coordinator restart) and automatically re-register when it returns.
  3. Workers can run tasks again after reconnecting.

The suite is kept fast by measuring each outage in failed coordinator
round-trips rather than in seconds — see ``_OUTAGE_FAILURES``. No test here
sleeps out a fixed outage; the only sleeps left are the poll intervals inside
``_wait_until`` and ``_wait_for_worker``. An outage long enough to be
convincing as a sleep is also long enough to dominate the suite, and it still
only shows that time passed rather than that the worker spent it retrying.
"""

import json
import socket
import threading
import time
import urllib.error

import pytest

from gpumesh.server import serve
from gpumesh.worker import MeshClient, run_worker

TOKEN = "resilience-test-token"

SCRIPT = """
import json, sys
payload = json.load(sys.stdin)
print(json.dumps({"result": payload["x"] * 2}))
"""


@pytest.fixture
def coordinator(tmp_path):
    """Start a real loopback coordinator; yields a holder with .start()."""
    db_path = str(tmp_path / "resilience.db")
    holder = {"db_path": db_path, "port": None, "httpd": None}

    def start():
        httpd = serve("127.0.0.1", 0, db_path, TOKEN)
        holder["port"] = httpd.server_address[1]
        holder["httpd"] = httpd
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd

    holder["httpd"] = start()
    yield holder
    try:
        holder["httpd"].gpumesh_stop.set()
        holder["httpd"].shutdown()
    except Exception:
        pass


def _wait_for_worker(client, timeout=20.0):
    """Poll /api/workers until at least one alive worker appears."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = client.call("GET", "/api/workers")
            alive = [w for w in resp.get("workers", []) if w.get("alive")]
            if alive:
                return alive
        except Exception:
            pass
        time.sleep(0.2)
    return []


def _wait_until(predicate, timeout, interval=0.05):
    """Wait for *predicate* to become true. Returns whether it did.

    The same deadline-and-poll shape as ``_wait_for_worker`` above, for
    conditions that are not "a worker appeared". Every wait in this file goes
    through one of the two: a bare ``time.sleep`` here is either slower than
    it needs to be or, on a loaded runner, silently shorter than the thing it
    was standing in for.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _counting_client(counter):
    """The real MeshClient, plus a tally of calls that failed.

    Instrumentation, not a stub — every call runs the production
    implementation and the exception is re-raised untouched. It exists so an
    outage can be waited out by the *event* that matters (the worker actually
    failing to reach a dead coordinator, over and over, and carrying on
    anyway) instead of by a fixed number of seconds during which that was
    merely likely to have happened. Counting is also strictly stronger: a
    worker that had quietly stopped retrying would satisfy any sleep.
    """
    lock = threading.Lock()

    class _CountingMeshClient(MeshClient):
        def call(self, *args, **kwargs):
            try:
                return super().call(*args, **kwargs)
            except Exception:
                with lock:
                    counter["failures"] += 1
                raise

    return _CountingMeshClient


def _start_worker(url, state, monkeypatch=None, counter=None):
    """Start a real worker in a daemon thread.

    Pass *monkeypatch* and *counter* to have the worker's own MeshClient
    tally its failed coordinator calls; see ``_counting_client``.
    """
    if counter is not None:
        from gpumesh import worker as worker_mod

        monkeypatch.setattr(worker_mod, "MeshClient", _counting_client(counter))

    def worker_main():
        try:
            run_worker(url, TOKEN, persist_connection=False)
            state["exited"] = "returned"
        except Exception as exc:
            state["exited"] = f"raised: {exc}"

    thread = threading.Thread(target=worker_main, daemon=True)
    thread.start()
    return thread


# Both waits below are expressed in failed coordinator round-trips rather than
# in seconds, because failures are what a reintroduced strike counter would
# count. The old `time.sleep(15)` did not even reach the "3 x 15s health-check
# window" its own comment claimed to be clearing, and it proved nothing about
# whether the worker was still trying during those fifteen seconds — only that
# they had passed.
#
# The survival test asks "does the worker die while the coordinator is gone?",
# so it waits out several complete retry cycles. Four consecutive failures is
# past the three-strike exit this file exists to keep deleted, and past any
# plausible small-N counter someone might reintroduce.
_OUTAGE_FAILURES = 4

# The repeated-outage test asks a different question — "does it come back,
# twice?" — and the coming back is asserted by _wait_for_worker on the other
# side. All this wait has to establish is that the worker really did notice
# the coordinator was gone before it was handed a new one; two failed
# round-trips settle that. Waiting longer here would only re-run the case
# above, at 8s a round.
_OUTAGE_NOTICED = 2


class TestWorkerSurvivesOutage:
    """Workers must never exit on their own due to a coordinator outage."""

    def test_worker_survives_outage_and_runs_tasks_after_reconnect(
        self, tmp_path, coordinator, monkeypatch
    ):
        """Full story: register, survive an outage, reconnect, run a task."""
        holder = coordinator
        url = f"http://127.0.0.1:{holder['port']}"
        client = MeshClient(url, TOKEN)

        state = {}
        counter = {"failures": 0}
        worker_thread = _start_worker(url, state, monkeypatch, counter)
        assert _wait_for_worker(client), "worker never registered"

        # ── Coordinator goes away (network drop / laptop sleep) ──
        holder["httpd"].gpumesh_stop.set()
        holder["httpd"].shutdown()
        baseline = counter["failures"]

        # The outage is measured in failed round-trips, not in seconds. See
        # _OUTAGE_FAILURES: waiting for the failures themselves proves the
        # worker is still trying, which is the thing an exit threshold would
        # break; a sleep only proved that time had passed.
        #
        # The wait ends early if the worker dies, because a dead worker IS the
        # regression and sitting out the rest of the deadline for it would
        # only make the failure slower to report.
        _wait_until(
            lambda: (counter["failures"] - baseline >= _OUTAGE_FAILURES
                     or not worker_thread.is_alive()),
            timeout=60,
        )

        # An exiting worker does not stop instantly — break, then the `finally`
        # that posts a shutdown heartbeat, then the thread ends — so asking
        # is_alive() the moment the fourth failure lands can win a race against
        # a worker that gave up on that very call. join() returns the instant
        # the thread ends, so this costs its timeout only when the worker is
        # healthy, which is the answer the test needs to be sure of.
        worker_thread.join(timeout=2.0)

        # The worker must STILL be alive and waiting.
        assert worker_thread.is_alive(), (
            f"worker permanently died during the outage after "
            f"{counter['failures'] - baseline} failed coordinator calls: "
            f"{state.get('exited')}"
        )
        assert counter["failures"] - baseline >= _OUTAGE_FAILURES, (
            f"worker made only {counter['failures'] - baseline} failed "
            f"coordinator calls in 60s — it has stopped retrying"
        )

        # ── Coordinator comes back on the same port ──
        httpd2 = serve("127.0.0.1", holder["port"], holder["db_path"], TOKEN)
        holder["httpd"] = httpd2
        threading.Thread(target=httpd2.serve_forever, daemon=True).start()
        client2 = MeshClient(url, TOKEN)

        # Worker re-registers and can run a real task.
        assert _wait_for_worker(client2), "worker did not re-register after reconnect"
        job = client2.call("POST", "/api/jobs", {
            "name": "reconnect-task",
            "script": SCRIPT,
            "payloads": [{"x": 21, "cost": 1}],
        })
        job_id = job["job_id"]

        deadline = time.time() + 30
        status = None
        while time.time() < deadline:
            status = client2.call("GET", f"/api/jobs/{job_id}")
            if status.get("finished"):
                break
            time.sleep(0.2)
        assert status and status["finished"], f"job did not finish: {status}"
        tasks = status.get("tasks", [])
        assert tasks and tasks[0]["status"] == "done", f"task failed: {tasks}"
        assert tasks[0]["result"]["result"] == 42

    def test_worker_gets_new_id_after_reconnect(self, coordinator):
        """A fresh coordinator DB means the worker re-registers with a new id."""
        holder = coordinator
        url = f"http://127.0.0.1:{holder['port']}"
        client = MeshClient(url, TOKEN)

        state = {}
        worker_thread = _start_worker(url, state)
        workers = _wait_for_worker(client)
        assert workers, "worker never registered"
        initial_id = workers[0]["id"]

        # Wipe the coordinator (fresh DB) on the same port.
        holder["httpd"].gpumesh_stop.set()
        holder["httpd"].shutdown()

        db_path2 = str(holder["db_path"]) + ".2"
        httpd2 = serve("127.0.0.1", holder["port"], db_path2, TOKEN)
        holder["httpd"] = httpd2
        threading.Thread(target=httpd2.serve_forever, daemon=True).start()
        client2 = MeshClient(url, TOKEN)

        workers = _wait_for_worker(client2, timeout=30)
        assert workers, "worker did not re-register after coordinator restart"
        assert workers[0]["id"] != initial_id, (
            "worker should re-register with a new id after the DB was wiped"
        )
        assert worker_thread.is_alive()

    def test_worker_survives_repeated_outages(self, tmp_path, coordinator,
                                              monkeypatch):
        """Two consecutive outages — the worker keeps coming back."""
        holder = coordinator
        url = f"http://127.0.0.1:{holder['port']}"
        client = MeshClient(url, TOKEN)

        state = {}
        counter = {"failures": 0}
        worker_thread = _start_worker(url, state, monkeypatch, counter)
        assert _wait_for_worker(client), "worker never registered"

        for round_no in range(2):
            # outage
            holder["httpd"].gpumesh_stop.set()
            holder["httpd"].shutdown()
            baseline = counter["failures"]
            _wait_until(
                lambda: (counter["failures"] - baseline >= _OUTAGE_NOTICED
                         or not worker_thread.is_alive()),
                timeout=60,
            )
            assert worker_thread.is_alive(), (
                f"worker died in outage round {round_no}: {state.get('exited')}"
            )
            assert counter["failures"] - baseline >= _OUTAGE_NOTICED, (
                f"worker stopped retrying in outage round {round_no} after "
                f"{counter['failures'] - baseline} failed calls"
            )
            # recovery on the same port
            httpd2 = serve("127.0.0.1", holder["port"], holder["db_path"], TOKEN)
            holder["httpd"] = httpd2
            threading.Thread(target=httpd2.serve_forever, daemon=True).start()
            assert _wait_for_worker(MeshClient(url, TOKEN)), (
                f"worker did not reconnect in round {round_no}"
            )
            # No settling sleep between rounds: _wait_for_worker has already
            # observed the coordinator report this worker as alive, which is
            # the state round two needs to start from.


# ══════════════════════════════════════════════════════════════════════
#  STOPPING DURING AN OUTAGE
# ══════════════════════════════════════════════════════════════════════
#
# The tests above prove the worker keeps trying while the coordinator is away.
# This section proves the other half, which is the half that was broken: that
# it stops when it is asked to, *while* it is doing that trying.
#
# The main loop used to pace its outage retries with time.sleep(). A sleeping
# thread cannot see a stop event, so a worker told to shut down mid-outage went
# on running until the sleep ended on its own — up to the 60 s backoff cap.
# That is the root cause of run_worker threads outliving the teardown that set
# their stop event: conftest's autouse fixture sets the event, the test ends,
# and the worker wakes up a minute later into a session that has moved on,
# polling and printing over whatever is running by then.
#
# A coordinator is not needed for any of this, and using one would make the
# test worse: the point is which *wait* the worker is sitting in, and a real
# coordinator makes that a matter of timing rather than of construction. So the
# mesh client is replaced with one whose every answer is chosen by the test.


def _fake_mesh_client(answer):
    """A MeshClient class whose every call is answered by *answer*.

    ``answer(method, path, body)`` returns a response or raises. Replacing the
    class rather than the instance is what reaches the client ``run_worker``
    builds for itself on its first line.
    """

    class _FakeMeshClient:
        def __init__(self, base_url, token):
            self.base_url = base_url.rstrip("/")
            self.token = token

        def call(self, method, path, body=None, timeout=30.0):
            return answer(method, path, body)

    return _FakeMeshClient


def _stub_probe(monkeypatch):
    """Skip the join-time benchmark; these tests are about the loop, not it."""
    from gpumesh import worker as worker_mod

    monkeypatch.setattr(worker_mod.capability, "full_probe", lambda: {
        "device": "cpu", "device_name": "stub-cpu", "score": 1.0,
        "hostname": "stub-host", "cpu_cores": 1,
    })


# Far above any wait the worker legitimately takes on its way out, and far
# above the 2 s the re-registration retry costs, so the two cannot be confused.
# A sleep of this length is what the regression looks like; an event wait of
# this length is indistinguishable from an instant one once the event is set.
_HUGE_POLL_INTERVAL = 45.0

# Generous enough that a slow runner cannot fail it, and still a third of the
# interval above, so a worker that slept its backoff out cannot pass it.
_STOP_DEADLINE = 15.0


class TestWorkerStopsDuringAnOutage:
    """A worker asked to stop mid-outage must stop, not finish its backoff."""

    def _run_until_lease_fails(self, monkeypatch, lease_error):
        """Register successfully, then break the lease call and wait for it.

        Reaching the outage backoff at all is the fiddly part, and it is why
        registration is allowed to succeed here: a worker that cannot register
        leaves through the registration-failure path instead and never enters
        the main loop, so a test that simply pointed a worker at a dead port
        would exercise nothing this class is about.

        Returns ``(thread, stop)`` with the worker parked in — or one or two
        statements away from entering — the wait that follows a failed lease.
        """
        from gpumesh import worker as worker_mod

        _stub_probe(monkeypatch)
        monkeypatch.setattr(worker_mod, "POLL_INTERVAL", _HUGE_POLL_INTERVAL)

        registered = threading.Event()
        lease_failed = threading.Event()
        outage = threading.Event()

        def answer(method, path, body):
            if path == "/api/register":
                registered.set()
                return {"worker_id": "w-outage"}
            if path == "/api/lease":
                if outage.is_set():
                    lease_failed.set()
                    raise lease_error
                return None
            # /api/workers (the reachability probe) and /api/heartbeat both
            # keep answering. A failing heartbeat would set need_reregister
            # from another thread and send the main loop down the
            # re-registration branch instead of the one under test.
            if path == "/api/heartbeat":
                return {"ok": True}
            return {"workers": []}

        monkeypatch.setattr(worker_mod, "MeshClient", _fake_mesh_client(answer))

        stop = threading.Event()
        thread = threading.Thread(
            target=worker_mod.run_worker,
            args=("http://127.0.0.1:1", "tok"),
            kwargs={"persist_connection": False, "stop_event": stop},
            daemon=True,
            name="test-outage-worker",
        )
        thread.start()

        assert registered.wait(30), "the worker never registered"
        outage.set()
        assert lease_failed.wait(30), "the worker never tried to lease again"
        return thread, stop

    # ids are explicit on purpose: pytest derives a parametrize id from a
    # value by probing ``getattr(value, "__name__", None)``, and
    # ``urllib.error.HTTPError`` inherits its ``__getattr__`` from
    # ``tempfile._TemporaryFileWrapper`` — ``addinfourl`` is an alias for it.
    # That ``__getattr__`` reads ``self.__dict__['file']``, which is absent
    # when the HTTPError was built with ``fp=None`` (the base is deliberately
    # not initialized then) and raises ``KeyError`` — a non-``AttributeError``,
    # so ``getattr``'s default does not catch it. Python 3.9 crashes the whole
    # collection with exit code 2 on that probe; 3.10+ happens to survive.
    @pytest.mark.parametrize("lease_error,branch", [
        (socket.timeout("coordinator gone"), "unreachable"),
        (urllib.error.HTTPError("http://127.0.0.1:1/api/lease", 500,
                                "server error", None, None), "answered 500"),
    ], ids=["unreachable", "answered-500"])
    def test_a_stop_mid_backoff_is_noticed_at_once(self, monkeypatch,
                                                   lease_error, branch):
        """Both failure branches wait on the event, not on the clock.

        The two are separate arms in the loop with separate waits — an
        unreachable coordinator waits out the growing network backoff, one that
        answered with a 5xx waits POLL_INTERVAL — and they were separate
        ``time.sleep`` calls. Fixing one and not the other would leave a worker
        that stops promptly from a WiFi drop and hangs for a minute from a
        coordinator restart, which is the same bug wearing a different hat.

        Both budgets are pinned to POLL_INTERVAL here, inflated to 45 s. A
        worker that waits on its stop event returns from that wait the instant
        the event is set; one that sleeps returns in 45 s. The join deadline
        sits between the two, so this cannot pass by being fast.
        """
        thread, stop = self._run_until_lease_fails(monkeypatch, lease_error)

        stop.set()
        thread.join(timeout=_STOP_DEADLINE)

        assert not thread.is_alive(), (
            f"a worker asked to stop while its coordinator was {branch} was "
            f"still running {_STOP_DEADLINE}s later — it is sleeping out its "
            f"{_HUGE_POLL_INTERVAL}s backoff instead of waiting on the stop "
            f"event, so its teardown cannot reclaim it"
        )


# ══════════════════════════════════════════════════════════════════════
#  A FAILED JOIN AND THE SAVED CONNECTION
# ══════════════════════════════════════════════════════════════════════
#
# ~/.gpumesh/config.json remembers the last coordinator that worked, and
# `gpumesh submit` reads it. A registration failure used to clear it
# unconditionally, which is right when the address that failed IS the saved one
# — a dead URL that persists silently breaks every later command — and wrong in
# every other case: one mistyped `gpumesh join` destroyed a perfectly good
# saved connection to a different coordinator, and the next `gpumesh submit`
# then failed for a reason with no visible connection to what the user did.
# Failing to reach one address says nothing about another.


@pytest.fixture
def failing_registration(monkeypatch):
    """Make ``run_worker`` fail registration immediately, without a network.

    ``_try_register`` is replaced rather than pointed at a dead port because
    the real one retries three times with 2 s sleeps between, and none of that
    is under test: the branch these tests are about starts the moment it gives
    up. The exception is the one a refused connection actually produces, so it
    lands in the URLError arm and not in the generic one below it.
    """
    from gpumesh import worker as worker_mod

    _stub_probe(monkeypatch)

    def _boom(mesh, info, retries=3, verbose=True):
        raise urllib.error.URLError(ConnectionRefusedError(
            "[WinError 10061] No connection could be made"))

    monkeypatch.setattr(worker_mod, "_try_register", _boom)


class TestFailedJoinAndTheSavedConnection:
    """Clear the saved connection only when it is the one that just failed."""

    def _join(self, url):
        from gpumesh.worker import run_worker

        run_worker(url, "tok", persist_connection=False,
                   stop_event=threading.Event())

    def test_a_failed_join_to_another_url_leaves_the_saved_one_alone(
            self, failing_registration):
        """The regression: a typo must not cost the user a working mesh."""
        from gpumesh import connection_manager

        connection_manager.save_connection("http://192.168.1.10:8000", "good-token")
        self._join("http://192.168.1.99:8000")  # the machine they meant to type

        saved = connection_manager.load_connection()
        assert saved is not None, (
            "a failed join to an unrelated address wiped the saved connection "
            "to a coordinator that is still running"
        )
        assert saved["url"] == "http://192.168.1.10:8000"
        assert saved["token"] == "good-token"

    def test_a_failed_join_to_the_saved_url_still_clears_it(
            self, failing_registration):
        """The behaviour worth keeping: a dead saved URL does not persist."""
        from gpumesh import connection_manager

        connection_manager.save_connection("http://192.168.1.10:8000", "good-token")
        self._join("http://192.168.1.10:8000")

        assert connection_manager.load_connection() is None, (
            "the saved connection survived a failed join to the very address "
            "it names — later commands will keep using a dead coordinator"
        )

    @pytest.mark.parametrize("saved_url,joined_url", [
        ("http://192.168.1.10:8000/", "http://192.168.1.10:8000"),
        ("http://192.168.1.10:8000", "http://192.168.1.10:8000/"),
        ("http://192.168.1.10:8000", "HTTP://192.168.1.10:8000"),
    ])
    def test_the_same_address_written_differently_still_counts_as_the_same(
            self, failing_registration, saved_url, joined_url):
        """A trailing slash is not a different coordinator.

        Comparing the two strings raw would make the useful half of this
        behaviour depend on how the user happened to type the URL, and the
        result — a dead connection left in place because it was saved with a
        slash — would look like the fix had simply not worked.
        """
        from gpumesh import connection_manager

        connection_manager.save_connection(saved_url, "good-token")
        self._join(joined_url)

        assert connection_manager.load_connection() is None

    def test_a_failed_join_with_nothing_saved_is_harmless(
            self, failing_registration):
        """The common case for a first-time user, and it must not raise."""
        from gpumesh import connection_manager

        assert connection_manager.load_connection() is None
        self._join("http://192.168.1.99:8000")
        assert connection_manager.load_connection() is None


# ══════════════════════════════════════════════════════════════════════
#  CLAIM INTERCEPTION
# ══════════════════════════════════════════════════════════════════════

CLAIM_TOKEN = "broadcast-claim-token"


def _claim_request_bytes(body=b"", path="/api/claim", headers=None):
    hdrs = {"Host": "127.0.0.1", "Content-Type": "application/json"}
    if headers is not None:
        hdrs.update(headers)
    hdrs.setdefault("Content-Length", str(len(body)))
    head = f"POST {path} HTTP/1.1\r\n" + "".join(
        f"{k}: {v}\r\n" for k, v in hdrs.items() if v is not None
    ) + "\r\n"
    return head.encode() + body


def _raw_exchange(port, request_bytes, timeout=15.0):
    """Send raw bytes and read the whole reply until the server closes.

    Returns the complete byte stream so a test can count how many HTTP status
    lines came back — one response per request is the property under test.
    The socket timeout is what keeps a regression from hanging the suite.
    """
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        sock.sendall(request_bytes)
        chunks = []
        while True:
            try:
                chunk = sock.recv(65536)
            except OSError:
                break  # includes socket.timeout and a Windows RST
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        sock.close()


def _status_lines(raw):
    return [line for line in raw.split(b"\r\n") if line.startswith(b"HTTP/")]


def _claim_payload(token, coord_url):
    return json.dumps({
        "token": token,
        "coordinator_urls": [coord_url],
        "coordinator_token": TOKEN,
    }).encode()


@pytest.fixture
def broadcast_worker(tmp_path, monkeypatch):
    """Run the real ``run_worker_broadcast`` and hand back its claim port.

    The point of these tests is worker.py's own patched handler, so the flow
    is driven for real rather than re-implemented. Three things are stubbed,
    none of them the code under test: the UDP beacon (a unit test has no
    business broadcasting on the LAN, and a sandbox that blocks it would make
    run_worker_broadcast bail out before the claim server is reachable),
    ``run_worker`` (so the thread finishes after a successful claim instead
    of joining the mesh forever), and ``start_claim_server`` — wrapped, not
    replaced — purely to learn the ephemeral port it picked.
    """
    from gpumesh import claimer, discovery
    from gpumesh import worker as worker_mod
    from gpumesh.claimer import ClaimHandler

    # A claim is only acked once the worker has proved it can reach the
    # coordinator, so this needs one that genuinely answers.
    httpd = serve("127.0.0.1", 0, str(tmp_path / "broadcast.db"), TOKEN)
    coord_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    class _StubBeacon:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(discovery, "Beacon", _StubBeacon)

    joined = []
    monkeypatch.setattr(worker_mod, "run_worker",
                        lambda *args, **kwargs: joined.append(args))

    captured = {}
    _real_start = claimer.start_claim_server

    def _capturing_start(token, port=0):
        server, actual_port = _real_start(token, port)
        captured["httpd"] = server
        captured["port"] = actual_port
        return server, actual_port

    monkeypatch.setattr(claimer, "start_claim_server", _capturing_start)

    orig_do_POST = ClaimHandler.do_POST
    thread = threading.Thread(
        target=worker_mod.run_worker_broadcast,
        kwargs={"token": CLAIM_TOKEN},
        daemon=True,
        name="test-broadcast-worker",
    )
    thread.start()

    deadline = time.time() + 30
    while time.time() < deadline and "port" not in captured:
        time.sleep(0.05)
    assert "port" in captured, "claim server never started"

    # ...and wait until it is actually accepting, not merely bound.
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", captured["port"]),
                                     timeout=2).close()
            break
        except OSError:
            time.sleep(0.05)

    yield {"port": captured["port"], "coord_url": coord_url,
           "joined": joined, "thread": thread}

    # run_worker_broadcast blocks until it is claimed, so a test that did not
    # claim leaves it parked on that wait. Claim it now to let its own
    # cleanup (handler restore, claim-server shutdown) run normally.
    if thread.is_alive():
        try:
            _raw_exchange(captured["port"],
                          _claim_request_bytes(
                              _claim_payload(CLAIM_TOKEN, coord_url)))
        except OSError:
            pass
    thread.join(timeout=15)
    ClaimHandler.do_POST = orig_do_POST  # belt and braces
    with ClaimHandler._claim_lock:
        ClaimHandler._claimed = False
    try:
        captured["httpd"].server_close()
    except Exception:
        pass
    httpd.gpumesh_stop.set()
    httpd.shutdown()


class TestClaimInterceptionAnswersOnce:
    """``run_worker_broadcast`` swaps its own do_POST onto ClaimHandler while
    it waits to be claimed. That replacement used to answer a malformed body
    by delegating to the original handler, which called ``_read_json`` again
    on a body that had already been consumed off the socket — an indefinite
    block, so one malformed claim permanently destroyed this worker's ability
    to be claimed. The delegation is gone; these tests pin the behaviour that
    replaced it: exactly one response, and the worker still claimable.
    """

    def test_malformed_claim_body_answers_exactly_once(self, broadcast_worker):
        from gpumesh.claimer import ClaimHandler

        raw = _raw_exchange(broadcast_worker["port"],
                            _claim_request_bytes(b"{bad json!!"))
        lines = _status_lines(raw)
        assert len(lines) == 1, f"expected one response, got {lines}"
        assert b"400" in lines[0]
        assert b"invalid JSON" in raw
        assert ClaimHandler._claimed is False

    def test_non_object_claim_body_answers_exactly_once(self, broadcast_worker):
        raw = _raw_exchange(broadcast_worker["port"],
                            _claim_request_bytes(b"[1,2,3]"))
        lines = _status_lines(raw)
        assert len(lines) == 1, f"expected one response, got {lines}"
        assert b"400" in lines[0]

    def test_oversized_claim_body_answers_exactly_once(self, broadcast_worker):
        # A concrete size, over the intended 16 KB cap, rather than
        # ``MAX_CONTENT_LENGTH + n``: a body sized from the constant scales
        # with it, so this test would stay green if the cap were raised back
        # to the 1 MB the claim server inherited from the coordinator and
        # never needed. See _OVER_16KB in tests/test_claimer.py, where the
        # trade-off between the absolute and relative forms is written out.
        body = b"z" * (20 * 1024)
        raw = _raw_exchange(broadcast_worker["port"],
                            _claim_request_bytes(body))
        lines = _status_lines(raw)
        assert len(lines) == 1, f"expected one response, got {lines}"
        assert b"413" in lines[0]

    def test_worker_is_still_claimable_after_a_malformed_body(self, broadcast_worker):
        """The regression this whole change exists for.

        The malformed body is answered, and then a perfectly ordinary claim
        on a new connection still works and still hands off to run_worker.
        Under the old fall-through the second request never got that far.
        """
        raw = _raw_exchange(broadcast_worker["port"],
                            _claim_request_bytes(b"{bad json!!"))
        assert b"400" in _status_lines(raw)[0]

        raw = _raw_exchange(
            broadcast_worker["port"],
            _claim_request_bytes(
                _claim_payload(CLAIM_TOKEN, broadcast_worker["coord_url"])),
        )
        lines = _status_lines(raw)
        assert len(lines) == 1, f"expected one response, got {lines}"
        assert b"200" in lines[0]
        assert b'"ok": true' in raw

        deadline = time.time() + 15
        while time.time() < deadline and not broadcast_worker["joined"]:
            time.sleep(0.1)
        assert broadcast_worker["joined"], "worker never handed off to run_worker"
        assert broadcast_worker["joined"][0][0] == broadcast_worker["coord_url"]
