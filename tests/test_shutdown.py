"""Ctrl+C, SIGTERM and ``docker stop`` must actually stop gpumesh.

Both long-running commands used to park the main thread in a bare
``Thread.join()``. A signal arriving during that join is recorded by the
interpreter, but its Python-level handler only runs when the main thread next
executes bytecode — and inside an untimed join it never does. So the signal
was accepted and then silently never delivered:

  * ``gpumesh join`` ignored Ctrl+C completely, on every platform.
  * ``gpumesh serve`` ignored it whenever stdout was not a TTY, which is
    exactly the piped / redirected / ``docker run -d`` case.

Both had to be killed with taskkill or ``docker kill``. The tests below pin
the invariant that fixes it (never wait without a timeout), the cleanup that
follows a signal, and — end to end, in a real subprocess — that a real console
signal really does bring the coordinator down.
"""

import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from gpumesh import cli


# ── the invariant: never block where a signal cannot reach us ───────────────


class _RecordingThread:
    """Stands in for a Thread, remembering how each join() was called."""

    def __init__(self, alive_polls: int):
        self._remaining = alive_polls
        self.join_timeouts = []

    def is_alive(self):
        return self._remaining > 0

    def join(self, timeout=None):
        self.join_timeouts.append(timeout)
        self._remaining -= 1


def test_wait_interruptible_never_joins_without_a_timeout():
    """An untimed join is the bug itself — it must never happen."""
    fake = _RecordingThread(alive_polls=4)

    cli.wait_interruptible(fake, poll=0.001)

    assert fake.join_timeouts, "expected the thread to be waited on"
    assert all(t is not None for t in fake.join_timeouts), (
        f"wait_interruptible blocked without a timeout: {fake.join_timeouts}"
    )


def test_wait_interruptible_returns_once_the_thread_finishes():
    finished = threading.Event()

    def body():
        finished.wait(5.0)

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    finished.set()

    cli.wait_interruptible(thread, poll=0.01)

    assert not thread.is_alive()


def test_wait_interruptible_lets_keyboardinterrupt_through():
    """The whole point: a signal raised while waiting must propagate."""
    never_ends = threading.Event()
    thread = threading.Thread(target=lambda: never_ends.wait(30), daemon=True)
    thread.start()

    calls = {"n": 0}
    real_join = thread.join

    def interrupting_join(timeout=None):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise KeyboardInterrupt
        real_join(timeout=timeout)

    thread.join = interrupting_join
    try:
        with pytest.raises(KeyboardInterrupt):
            cli.wait_interruptible(thread, poll=0.01)
    finally:
        never_ends.set()


# ── coordinator shutdown ────────────────────────────────────────────────────


class _FakeServer:
    def __init__(self):
        self.gpumesh_stop = threading.Event()
        self.shutdown_called = False

    def shutdown(self):
        self.shutdown_called = True


class _FakeWorkerThread:
    def __init__(self):
        self.gpumesh_stop = threading.Event()
        self.joined = False
        self.join_timeouts = []

    def join(self, timeout=None):
        self.joined = True
        self.join_timeouts.append(timeout)


def test_stop_coordinator_stops_the_self_worker_too():
    """A leaked self-worker keeps polling and can hang interpreter exit."""
    httpd = _FakeServer()
    serve_thread = _RecordingThread(alive_polls=0)
    self_worker = _FakeWorkerThread()

    cli._stop_coordinator(httpd, serve_thread, self_worker)

    assert httpd.gpumesh_stop.is_set()
    assert httpd.shutdown_called
    assert self_worker.gpumesh_stop.is_set()
    assert self_worker.joined


def test_stop_coordinator_works_without_a_self_worker():
    httpd = _FakeServer()
    cli._stop_coordinator(httpd, _RecordingThread(alive_polls=0), None)
    assert httpd.shutdown_called


def test_second_interrupt_during_shutdown_exits_immediately(monkeypatch):
    """Ctrl+C twice means stop waiting for a clean shutdown."""
    exits = []
    monkeypatch.setattr(os, "_exit", lambda code: exits.append(code))

    class _StubbornServer(_FakeServer):
        def shutdown(self):
            raise KeyboardInterrupt

    cli._stop_coordinator(_StubbornServer(), _RecordingThread(0), None)

    assert exits == [1], "a second interrupt should force an immediate exit"


def test_self_worker_join_budget_covers_the_graceful_shutdown_timeout():
    """A join budget below the worker's own grace period cannot ever work.

    After the stop event is set, _run_function_task waits
    GRACEFUL_SHUTDOWN_TIMEOUT before killing a running task's subprocess. The
    old join(timeout=10) against a 30s grace period therefore *always* timed
    out with a task in flight, leaving the worker to daemon-thread teardown at
    interpreter exit — killed mid-print, holding stdout's lock, the exact hang
    _stop_coordinator's docstring says it prevents.
    """
    from gpumesh import worker as worker_mod

    httpd = _FakeServer()
    self_worker = _FakeWorkerThread()

    cli._stop_coordinator(httpd, _RecordingThread(alive_polls=0), self_worker)

    assert self_worker.join_timeouts, "the self-worker was never joined"
    budget = self_worker.join_timeouts[-1]
    assert budget is not None, "an untimed join would hang shutdown forever"
    assert budget >= worker_mod.GRACEFUL_SHUTDOWN_TIMEOUT, (
        f"join budget {budget}s is below the worker's own "
        f"{worker_mod.GRACEFUL_SHUTDOWN_TIMEOUT}s graceful-shutdown timeout"
    )
    # Derived from the constant, not re-typed: the two drifting apart is how
    # the bug happened in the first place.
    assert cli._SELF_WORKER_JOIN_TIMEOUT > worker_mod.GRACEFUL_SHUTDOWN_TIMEOUT


def test_stop_coordinator_joins_the_self_worker_even_if_shutdown_raises(capsys):
    """An OSError out of shutdown() must not leak the worker thread.

    Only (KeyboardInterrupt, SystemExit) used to be caught, so anything else
    escaped cmd_serve's finally: and skipped the join entirely.
    """
    class _ExplodingServer(_FakeServer):
        def shutdown(self):
            raise OSError("[WinError 10038] socket operation on non-socket")

    self_worker = _FakeWorkerThread()

    cli._stop_coordinator(_ExplodingServer(), _RecordingThread(0), self_worker)

    assert self_worker.gpumesh_stop.is_set()
    assert self_worker.joined, "the self-worker thread was leaked"
    assert "OSError" in capsys.readouterr().out, "the failure was swallowed silently"


# ── the coordinator must stop accepting before it closes its database ───────


def test_database_closes_only_after_the_accept_loop_has_stopped(tmp_path):
    """Closing the db first turned live requests into 500s and dropped results.

    The old order was: 503-gate, sleep(1.0), db.close(), stop the accept loop.
    A handler that passed the gate and took over a second hit
    ``sqlite3.ProgrammingError: Cannot operate on a closed database`` (returned
    as a 500), and every /api/result POST arriving in that window was answered
    503 — finished work, thrown away at the door.

    The listening socket is the observable proxy for "the accept loop has
    stopped": server_close() runs immediately before the close, so if the port
    still answers when the database closes, the order is wrong.
    """
    import socket as _socket

    from gpumesh import server

    httpd = server.serve("127.0.0.1", 0, str(tmp_path / "order.db"),
                         "order-token", discovery=False)
    port = httpd.server_address[1]
    db = httpd.RequestHandlerClass.db
    observed = {}
    real_close = db.close

    def recording_close():
        probe = _socket.socket()
        probe.settimeout(1.0)
        try:
            probe.connect(("127.0.0.1", port))
            observed["port_still_accepting"] = True
        except OSError:
            observed["port_still_accepting"] = False
        finally:
            probe.close()
        observed["closed"] = True
        real_close()

    db.close = recording_close
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        # Make sure the accept loop is genuinely running first: only an
        # answered request proves that, since the listening socket queues
        # connections in the kernel backlog whether or not anyone accepts.
        deadline = time.time() + 10.0
        while time.time() < deadline:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/workers", timeout=1.0
                ).close()
                break
            except urllib.error.HTTPError:
                break  # 401 for the missing token — it is serving
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("the coordinator never started serving")
        httpd.gpumesh_stop.set()
        httpd.shutdown()
    finally:
        t.join(timeout=15)

    assert observed.get("closed"), "the database was never closed"
    assert observed["port_still_accepting"] is False, (
        "the database was closed while the coordinator was still accepting "
        "connections — in-flight handlers can hit a closed database, and "
        "/api/result POSTs are answered 503 and their work discarded"
    )


# ── the function-task watcher must not outlive its task ─────────────────────


def _one_shot_function_payload(body):
    """A function payload the real subprocess helper can execute."""
    return {"_func": body, "_params": {}, "_task_index": 0}


def test_function_task_watcher_thread_exits_when_the_subprocess_finishes(
        monkeypatch):
    """One leaked daemon thread per function task, forever, was the bug.

    ``_wait_for_stop_and_kill`` blocked on an untimed ``stop_event.wait()``
    and was never woken by a task ending normally, so a worker that ran 10,000
    tasks held 10,000 blocked threads.
    """
    from gpumesh import serializer, worker as worker_mod

    monkeypatch.setattr(serializer, "deserialize_function",
                        lambda blob: blob, raising=False)

    stop = threading.Event()
    payload = _one_shot_function_payload(lambda: 21 * 2)

    before = set(threading.enumerate())
    for _ in range(3):
        result = worker_mod._run_function_task(payload, "cpu", 60.0,
                                               stop_event=stop)
        assert isinstance(result, dict)

    # The watcher wakes twice a second, so give it a moment to retire.
    deadline = time.time() + 10.0
    leaked = []
    while time.time() < deadline:
        leaked = [t for t in threading.enumerate()
                  if t not in before and t.is_alive()]
        if not leaked:
            break
        time.sleep(0.1)

    assert not leaked, (
        f"{len(leaked)} watcher thread(s) survived their finished tasks: "
        f"{[t.name for t in leaked]}"
    )
    assert not stop.is_set(), "the watcher must not touch the stop event"


def test_stop_event_still_kills_a_running_function_task(monkeypatch):
    """The behaviour the watcher exists for must survive the leak fix."""
    from gpumesh import sandbox, serializer, worker as worker_mod

    monkeypatch.setattr(serializer, "deserialize_function",
                        lambda blob: blob, raising=False)
    # Real value is 30s; the test only cares that the timer fires at all.
    monkeypatch.setattr(worker_mod, "GRACEFUL_SHUTDOWN_TIMEOUT", 0.5)

    stop = threading.Event()
    payload = _one_shot_function_payload(
        lambda: __import__("time").sleep(120)
    )
    threading.Timer(1.0, stop.set).start()

    started = time.time()
    with pytest.raises(sandbox.TaskError):
        # The task timeout is far beyond the graceful budget: if the stop
        # event were ignored the call would sit here for the full 120s.
        worker_mod._run_function_task(payload, "cpu", 120.0, stop_event=stop)
    assert time.time() - started < 60, "the subprocess was not killed on stop"


# ── worker shutdown ─────────────────────────────────────────────────────────


def test_worker_command_passes_a_stop_event(monkeypatch):
    """run_worker only sees signals on the main thread — and it is not there.

    Without an explicit stop event there is no way to shut the worker down
    short of killing the process.
    """
    captured = {}
    started = threading.Event()

    def fake_run_worker(url, token, task_timeout, safe_mode,
                        persist_connection, stop_event):
        captured["stop_event"] = stop_event
        started.set()
        # Must outlast _run_worker_with_report's own join(timeout=10) by a
        # wide margin. Waiting exactly 10s raced it: on a loaded machine the
        # fake worker occasionally finished first, which _run_worker_with_report
        # reads as "could not connect" — it returned early and this test failed
        # for a reason that had nothing to do with stop events.
        stop_event.wait(120)

    monkeypatch.setattr(cli.worker, "run_worker", fake_run_worker)
    monkeypatch.setattr(cli, "wait_interruptible",
                        lambda thread, poll=0.2: (_ for _ in ()).throw(KeyboardInterrupt))

    cli._run_worker_with_report("http://127.0.0.1:1", "tok", 1.0)

    assert started.is_set(), "worker thread never started"
    assert isinstance(captured.get("stop_event"), threading.Event)
    assert captured["stop_event"].is_set(), (
        "Ctrl+C must set the worker's stop event, not just abandon the thread"
    )


# ── end to end: a real process, a real console signal ───────────────────────


def _interrupt(proc):
    """Send the platform's Ctrl+C-equivalent to another process."""
    if os.name == "nt":
        # CTRL_C_EVENT cannot be targeted at another process group on
        # Windows; CTRL_BREAK_EVENT can, and travels the same signal path.
        proc.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        proc.send_signal(signal.SIGINT)


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_serve_subprocess_stops_on_a_console_signal(tmp_path):
    """The regression itself: a signalled coordinator must actually exit.

    Runs WITH the self-worker, which is the interesting half: the self-worker
    is the thread _stop_coordinator has to join, and the path that previously
    left it to daemon teardown. The old version of this test passed
    ``--no-self-worker``, so it only ever exercised the branch that already
    worked.

    Colour is left alone deliberately. stdout here is a pipe, so ``isatty()``
    is False and cmd_serve takes the ``wait_interruptible`` branch either way —
    forcing GPUMESH_COLOR=0 only made that look like a choice.
    """
    env = dict(os.environ)
    # Never touch the developer's real ~/.gpumesh/config.json.
    env["HOME"] = str(tmp_path)
    env["USERPROFILE"] = str(tmp_path)
    env.pop("GPUMESH_HOST", None)  # the loopback default is what we want here

    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "gpumesh", "serve",
         "--port", str(_free_port()), "--token", "shutdown-test-token",
         "--no-discovery",
         "--db", str(tmp_path / "shutdown.db")],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        cwd=str(tmp_path), env=env, **kwargs,
    )
    try:
        # Give it time to bind, print its banner, benchmark the self-worker
        # and reach the wait loop.
        time.sleep(20)
        if proc.poll() is not None:
            # A coordinator that dies during warmup is a failure, not a
            # reason to skip: skipping turned "crashes on startup" green.
            out = proc.communicate(timeout=10)[0] or b""
            pytest.fail(
                f"coordinator exited during warmup (rc={proc.returncode}) — "
                f"it never reached the wait loop this test is about.\n"
                f"{out.decode('utf-8', errors='replace')[-4000:]}"
            )

        _interrupt(proc)

        try:
            # The budget must clear the self-worker join (the worker's
            # graceful-shutdown timeout plus slack), or this fails for the
            # wrong reason.
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            pytest.fail(
                "coordinator ignored the console signal and had to be killed "
                "— Ctrl+C / docker stop cannot shut it down"
            )
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.communicate(timeout=10)
        except (subprocess.TimeoutExpired, ValueError):
            pass
