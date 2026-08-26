"""Worker agent: joins a mesh and executes tasks until interrupted.

Loop: register -> heartbeat thread -> poll for a lease -> run task in a
sandboxed subprocess -> post the result -> repeat.

Exo RunnerSupervisor pattern:
- Function tasks run in a separate OS process (not a thread) so a crash
  cannot take down the worker. The subprocess communicates results via
  stdin/stdout pipes using pickle serialization.
- After a task fails, diagnostics (error type, traceback, resource usage)
  are attached to the failure status.
- SIGTERM/SIGBREAK triggers graceful shutdown: stop accepting tasks,
  wait for the current task (with a grace period), report status, clean up.
"""

# Required for the ``X | None`` annotations below: on Python 3.9 they would
# otherwise be evaluated at definition time and raise TypeError on import.
from __future__ import annotations

import hmac
import json
import os
import platform
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
import traceback
import urllib.error
import urllib.request

from . import capability, sandbox
from . import (
    __version__,
    MIN_PROTOCOL_VERSION, PROTOCOL_VERSION, UNVERSIONED_PROTOCOL,
    ProtocolVersionMismatch, is_supported_protocol,
)
from gpumesh.ansi import safe_print, green, yellow, red, cyan, bold, dim, device_icon

HEARTBEAT_INTERVAL = 10.0
POLL_INTERVAL = 2.0
# Adaptive lease polling. A flat 2s poll put a two-second floor under every
# mesh call, so an interactive @mesh function felt slower than running it
# locally. After each task the worker polls rapidly for a short window (users
# almost always submit again immediately), then backs off to IDLE_POLL_INTERVAL
# so a worker left running overnight stays nearly free.
BUSY_POLL_INTERVAL = 0.1
IDLE_POLL_INTERVAL = 1.0
BUSY_POLL_WINDOW = 10.0  # seconds of fast polling after the last task
POLL_BACKOFF = 1.5
REBENCHMARK_INTERVAL = 600.0  # re-benchmark every 10 minutes
GRACEFUL_SHUTDOWN_TIMEOUT = 30.0  # seconds to wait for current task on SIGTERM

# --- result delivery ---------------------------------------------------
#
# A finished task's result is the only thing this loop produces that cannot be
# recreated for free: the compute is already spent. Everything else the worker
# sends (a heartbeat, a lease request) is cheap to repeat and worthless if
# late. So the result POST gets the retry the rest of the loop does not need.
#
# It used to be posted exactly once, with every failure caught by a warning
# arm — and because HTTPError subclasses URLError, that arm swallowed the 503
# from a shutting-down coordinator too. The worker printed a line, counted the
# task as done, and dropped the payload; the coordinator left the row 'running'
# and re-ran the whole task once LEASE_SECONDS expired.
#
# The budget is what stops the cure being worse: a worker that retried forever
# would sit on one task while the queue behind it went unserved, and would
# still be holding an answer nobody wants by the time the lease expired and
# somebody else redid the work. Two minutes is comfortably inside the 300s
# lease — long enough to ride out a coordinator restart, short enough that the
# worker is back in the poll loop well before its task is re-queued.
RESULT_POST_TIMEOUT = 30.0
RESULT_RETRY_BASE = 1.0
RESULT_RETRY_FACTOR = 2.0
RESULT_RETRY_MAX = 15.0
RESULT_RETRY_BUDGET = 120.0
# Once a shutdown has been requested the trade flips: the operator is watching
# a Ctrl+C that has not taken effect yet, and _stop_coordinator only waits
# GRACEFUL_SHUTDOWN_TIMEOUT + 10s for this thread. A few seconds still covers
# the case that actually happens on shutdown — a coordinator closing its socket
# a moment before we post — without turning "stop" into "stop in two minutes".
RESULT_RETRY_SHUTDOWN_BUDGET = 5.0

# Outcomes of a delivery attempt. Plain strings so a log line, a return value
# and a test assertion all read the same.
DELIVERED = "delivered"
OBSOLETE = "obsolete"            # the coordinator no longer wants it
AUTH_FAILED = "auth"             # wrong token; the worker has to stop
UNDELIVERABLE = "undeliverable"  # a refusal that retrying cannot fix
GAVE_UP = "gave_up"              # the retry budget ran out
# NOTE: there are deliberately NO "give up and exit" thresholds here.
# After a worker registers it never exits on its own: coordinator outages,
# laptop sleep and WiFi drops are survived by retrying with capped
# exponential backoff, and the worker re-registers when the coordinator
# returns (see run_worker).
_env_lock = threading.Lock()  # protects os.environ["GPUMESH_DEVICE"]
_task_id_lock = threading.Lock()  # protects _current_task_id[0] across heartbeat/main threads


class MeshClient:
    """Every gpumesh client request goes through here.

    That includes the Python API (``api.py``), the CLI's job commands
    (``client.py``) and ``@accelerate``. Keeping it the single chokepoint is
    what makes the TLS context below a one-place change rather than a hunt
    through every call site.
    """

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        # None for http:// URLs -- urlopen ignores the argument there, and
        # building a context for a plain-HTTP mesh would cost every client
        # an import of ssl for nothing.
        self._ssl_context = None
        if self.base_url.lower().startswith("https://"):
            from . import tls as tls_mod
            self._ssl_context = tls_mod.client_context(self.base_url)

    def call(self, method: str, path: str, body=None, timeout: float = 30.0):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "X-Auth-Token": self.token,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=self._ssl_context) as resp:
                if resp.status == 204:
                    return None
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.URLError as exc:
            # "certificate verify failed" is technically accurate and tells
            # nobody what to do about it. Re-raise the same class of error
            # with the two lines that actually fix it.
            from . import tls as tls_mod
            hint = tls_mod.explain_verify_failure(exc.reason if
                                                  hasattr(exc, "reason")
                                                  else exc)
            if hint:
                raise urllib.error.URLError(
                    str(exc.reason) + "\n\n" + hint) from exc
            raise


# Probing several candidate addresses must stay well inside the claim
# request's own timeout, so the coordinator never gives up while the worker
# is still deciding.
CLAIM_PROBE_TIMEOUT = 2.0


def find_reachable_coordinator(urls, token: str,
                               timeout: float = CLAIM_PROBE_TIMEOUT):
    """Return the first URL in ``urls`` that this machine can actually reach.

    Only the worker can answer this. Reachability is a property of the
    network *between* two machines, so a coordinator picking its own address
    from a local interface list can be wrong in ways it has no way to
    detect — the guess and the failure happen on opposite sides of the link.
    Probing here moves the decision to the only side that can observe it.

    Returns ``(url, None)`` on success, or ``(None, reason)`` where reason
    describes every attempt that failed.
    """
    failures = []
    for url in urls:
        try:
            MeshClient(url, token).call("GET", "/api/workers", timeout=timeout)
            return url, None
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                # Reachable, but the token was rejected. The network is fine,
                # so trying the remaining candidates cannot help.
                return None, f"{url}: authentication failed (wrong token)"
            failures.append(f"{url}: HTTP {exc.code}")
        except (urllib.error.URLError, OSError) as exc:
            failures.append(f"{url}: {getattr(exc, 'reason', exc)}")
    return None, "; ".join(failures) if failures else "no candidate URLs given"


def _run_function_task(payload: dict, device: str, timeout: float,
                       stop_event: threading.Event | None = None) -> dict:
    """Execute a function-based task in an isolated subprocess.

    Exo RunnerSupervisor pattern: the function runs in a separate OS
    process (not a thread), so a crashing task cannot take down the worker.

    Protocol:
      1. Serialize func + params with cloudpickle
      2. Spawn a subprocess that reads the pickled data from stdin
      3. Subprocess executes func(**params), writes JSON result to stdout
      4. On timeout, kill the entire process tree (no orphan threads)

    The GPUMESH_DEVICE env var is passed to the subprocess directly (no
    shared-state races).

    If stop_event is provided and gets set during execution (e.g. SIGTERM),
    the subprocess is killed after GRACEFUL_SHUTDOWN_TIMEOUT seconds.
    """
    from . import serializer

    try:
        func = serializer.deserialize_function(payload["_func"])
    except ValueError as e:
        # These ValueErrors are deliberate refusals (cross-version payload with
        # no usable source, a method or callable object the source fallback
        # cannot rebuild). A bare ValueError misses the TaskError handler in
        # the task loop and gets reported as "Unexpected error", labelling an
        # expected refusal as a surprise.
        #
        # Marked user_error so the coordinator fails it once. It used to be
        # left retryable on the theory that a retry might land on a worker
        # whose Python version matches — but BUSY_POLL_INTERVAL is 0.1s, so
        # this same incompatible worker re-leases the task long before any
        # other worker polls. All three attempts burned here, turning one
        # refusal into three near-instant failures and three diagnostics
        # blocks, and the user still had to read the same message to fix it.
        raise sandbox.TaskError(str(e), user_error=True)
    params = {k: v for k, v in payload.get("_params", {}).items()
              if not k.startswith("_")}
    task_index = payload.get("_task_index", 0)

    # Locate the helper module on disk
    helper_path = os.path.join(os.path.dirname(__file__), "_function_subprocess.py")
    if not os.path.isfile(helper_path):
        raise sandbox.TaskError(
            f"function subprocess helper not found: {helper_path}"
        )

    # Serialize (func, params) with cloudpickle — sent over stdin pipe
    try:
        import cloudpickle
    except ImportError:
        raise sandbox.TaskError(
            "cloudpickle is required for function-based tasks but is not installed. "
            "Install it with: pip install cloudpickle"
        )
    pickled_input = cloudpickle.dumps((func, params))

    # Build environment for the subprocess (inherit current env + device)
    env = os.environ.copy()
    env["GPUMESH_DEVICE"] = device
    # Same Windows fix as sandbox.run_task: the helper prints its result JSON
    # to stdout, and without PYTHONIOENCODING the child's stdout would use the
    # ANSI code page (cp1252), crashing on non-ASCII results. Force UTF-8.
    env["PYTHONIOENCODING"] = "utf-8"

    kwargs = {}
    if os.name == "posix":
        # start_new_session=True is os.setsid() performed by CPython between
        # fork and exec, without running Python code in the child. The
        # equivalent preexec_fn=os.setsid is documented as unsafe when the
        # parent has threads — and this parent always does (heartbeat thread,
        # and under a self-worker the coordinator's HTTP threads too). A child
        # forked while another thread holds an internal lock can block forever
        # trying to acquire it, hanging the task with no timeout able to help.
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(
        [sys.executable, helper_path],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        **kwargs,
    )

    # Graceful shutdown: if stop_event is set during execution, kill the
    # subprocess after GRACEFUL_SHUTDOWN_TIMEOUT seconds instead of waiting
    # the full task timeout (Bug fix: enforce the timeout constant).
    _shutdown_timer = [None]

    def _force_kill():
        if proc.poll() is None:
            safe_print(f"{bold(cyan('[worker]'))} {yellow('graceful shutdown timeout expired')} — killing subprocess")
            sandbox._kill_tree(proc)

    def _wait_for_stop_and_kill():
        # Poll instead of blocking on an untimed stop_event.wait(). The event
        # only fires on shutdown, so a watcher started for a task that ends
        # normally was never woken and never returned: one blocked daemon
        # thread leaked per function task, forever. A worker that ran 10,000
        # tasks held 10,000 of them. Waking twice a second lets the watcher
        # notice its subprocess is gone and exit, while still arming the kill
        # timer promptly when the stop event does fire mid-task.
        while not stop_event.wait(0.5):
            if proc.poll() is not None:
                return
        if proc.poll() is None:
            _shutdown_timer[0] = threading.Timer(GRACEFUL_SHUTDOWN_TIMEOUT, _force_kill)
            _shutdown_timer[0].daemon = True
            _shutdown_timer[0].start()

    if stop_event is not None:
        if stop_event.is_set():
            _shutdown_timer[0] = threading.Timer(GRACEFUL_SHUTDOWN_TIMEOUT, _force_kill)
            _shutdown_timer[0].daemon = True
            _shutdown_timer[0].start()
        else:
            threading.Thread(target=_wait_for_stop_and_kill, daemon=True).start()

    try:
        stdout_bytes, stderr_bytes = proc.communicate(
            input=len(pickled_input).to_bytes(4, "big") + pickled_input,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        sandbox._kill_tree(proc)
        # Reap the process to avoid zombies
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        raise sandbox.TaskError(
            f"function timed out after {timeout}s (subprocess killed)"
        )
    finally:
        if _shutdown_timer[0]:
            _shutdown_timer[0].cancel()

    if proc.returncode != 0:
        stderr_text = (stderr_bytes or b"").decode("utf-8", errors="replace")
        stdout_text = (stdout_bytes or b"").decode("utf-8", errors="replace")
        # Try to extract structured error from helper output (last JSON line)
        error_msg = None
        user_error = False
        for line in reversed(stdout_text.strip().splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict) and "error" in parsed:
                    error_msg = parsed["error"]
                    # The helper sets this when the task's own code is at
                    # fault, so the coordinator can skip pointless retries.
                    user_error = bool(parsed.get("user_error"))
                    tb = parsed.get("traceback", "")
                    break
            except (json.JSONDecodeError, ValueError):
                pass
        if error_msg is None:
            # Fallback: last few lines of stderr
            tail = stderr_text.strip().splitlines()[-5:]
            error_msg = " | ".join(tail) if tail else f"exit code {proc.returncode}"
        raise sandbox.TaskError(
            f"function subprocess failed: {error_msg}", user_error=user_error
        )

    # Parse result from stdout (last JSON line, same contract as sandbox)
    stdout_text = (stdout_bytes or b"").decode("utf-8", errors="replace")
    lines = [ln for ln in stdout_text.strip().splitlines() if ln.strip()]
    if not lines:
        raise sandbox.TaskError("function subprocess produced no output")

    try:
        result = json.loads(lines[-1])
    except json.JSONDecodeError:
        raise sandbox.TaskError(
            f"function subprocess output is not JSON: {lines[-1][:200]}"
        )

    # Inject task index for result ordering (subprocess doesn't know it).
    # This rides alongside the result envelope and is stripped by the client
    # before the envelope is unwrapped, so it never reaches user code.
    if isinstance(result, dict):
        result["_task_index"] = task_index

    return result


class _AuthError(Exception):
    """Raised when the coordinator rejects this worker's token (HTTP 401)."""


# HTTP 426 Upgrade Required — the coordinator refusing us on version grounds.
# Kept as a literal here rather than imported from server.py so a worker never
# pulls the coordinator module (and its Database import) into memory.
_PROTOCOL_MISMATCH_STATUS = 426


def _refusal_message(exc: "urllib.error.HTTPError") -> str:
    """Pull the coordinator's refusal prose out of a 426 body.

    The coordinator writes the message because only it knows its own version
    numbers and its own window. Relaying its text verbatim — instead of
    inventing a local one — is what keeps the two sides from disagreeing about
    why the join failed. A body we cannot read still has to produce something
    actionable, hence the fallback.
    """
    try:
        payload = json.loads(exc.read().decode("utf-8", errors="replace"))
        message = payload.get("error")
        if message:
            return str(message)
    except (OSError, ValueError, AttributeError):
        pass
    return (
        f"The coordinator refused this worker on protocol-version grounds "
        f"(HTTP {_PROTOCOL_MISMATCH_STATUS}) but sent no explanation. This "
        f"worker is gpumesh protocol {PROTOCOL_VERSION} (accepting "
        f"{MIN_PROTOCOL_VERSION}-{PROTOCOL_VERSION}). Run 'pip install -U "
        f"gpumesh' on both machines so they agree."
    )


def _check_coordinator_protocol(resp: dict) -> None:
    """Refuse a coordinator this worker cannot talk to.

    The coordinator's own gate only catches half the skew. A worker NEWER than
    the coordinator meets a coordinator with no gate at all — every gpumesh up
    to 1.3.0 accepts any registration and then behaves however the difference
    happens to make it behave. So the check has to exist on both ends, and this
    is the worker's end.

    An absent ``protocol_version`` in the response is not a failure: it means
    the coordinator predates the handshake, which is UNVERSIONED_PROTOCOL and
    is inside today's window. It stops being inside the window when
    PROTOCOL_VERSION reaches 3, and on that day this raises with a message
    naming both sides — which is the entire point.
    """
    raw = resp.get("protocol_version") if isinstance(resp, dict) else None
    if raw is None:
        coord_proto = UNVERSIONED_PROTOCOL
    else:
        try:
            coord_proto = int(raw)
        except (ValueError, TypeError):
            raise ProtocolVersionMismatch(
                f"The coordinator reported a protocol version this worker "
                f"cannot parse ({raw!r}). This worker is gpumesh protocol "
                f"{PROTOCOL_VERSION}. Check that the URL really points at a "
                f"gpumesh coordinator and not at another service.",
                local=PROTOCOL_VERSION,
            )
    if is_supported_protocol(coord_proto):
        return
    if coord_proto < MIN_PROTOCOL_VERSION:
        direction = (
            f"The coordinator speaks gpumesh wire protocol {coord_proto}, "
            f"which is older than the oldest version this worker supports "
            f"({MIN_PROTOCOL_VERSION})."
        )
        fix = ("Upgrade the COORDINATOR: run 'pip install -U gpumesh' on the "
               "coordinator machine and restart it.")
    else:
        direction = (
            f"The coordinator speaks gpumesh wire protocol {coord_proto}, "
            f"which is newer than this worker's ({PROTOCOL_VERSION})."
        )
        fix = ("Upgrade the WORKER: run 'pip install -U gpumesh' on this "
               "machine and rejoin.")
    raise ProtocolVersionMismatch(
        f"Refusing to join this mesh: incompatible gpumesh protocol version. "
        f"{direction} This worker is gpumesh {__version__}, speaking protocol "
        f"{PROTOCOL_VERSION} and accepting {MIN_PROTOCOL_VERSION}-"
        f"{PROTOCOL_VERSION}. {fix} Refusing now beats joining and then "
        f"failing on the first task in whatever way the difference produces.",
        local=PROTOCOL_VERSION,
        remote=coord_proto,
    )


def _try_register(mesh: "MeshClient", info: dict, retries: int = 3,
                   verbose: bool = True):
    """Register with the coordinator, retrying transient connection errors.

    Many registration failures are transient (e.g. the coordinator was just
    started and the worker's first attempt races ahead of it). Retry a small
    number of times with a short sleep before giving up.

    ``verbose=False`` silences the progress messages; used by the background
    auto-re-registration path so it doesn't spam the console during outages.

    Returns the worker_id on success, or raises the last underlying exception
    (urllib.error.URLError / OSError / json.JSONDecodeError) if every
    attempt failed. A rejected token (HTTP 401) raises ``_AuthError``
    immediately instead of retrying, and an incompatible protocol version
    (HTTP 426, or a coordinator reporting a version outside our window) raises
    ``ProtocolVersionMismatch`` — likewise without retrying, because neither
    machine's version changes while we sleep for two seconds.
    """
    def _print(msg):
        if verbose:
            safe_print(msg)

    last_exc = None
    for attempt in range(retries):
        try:
            # Quick reachability probe so a connection failure produces a clear
            # message ("coordinator not reachable") instead of a cryptic one.
            try:
                mesh.call("GET", "/api/workers")
            except urllib.error.HTTPError as probe_exc:
                # A 401 means the token is wrong — retrying cannot fix it, so
                # surface a clear error instead of "coordinator not reachable".
                if probe_exc.code == 401:
                    raise _AuthError() from probe_exc
                raise
            except (urllib.error.URLError, OSError) as probe_exc:
                _print(f"{bold(cyan('[worker]'))} {red('coordinator not reachable')} at "
                      f"{mesh.base_url}: {probe_exc}")
                if attempt < retries - 1:
                    _print(f"{bold(cyan('[worker]'))} {yellow('retrying registration')} in 2s "
                          f"(attempt {yellow(str(attempt + 1))}/{retries})...")
                    time.sleep(2)
                    continue
                raise
            # The version rides in the registration body itself. Older
            # coordinators ignore unknown keys, so sending it is safe against
            # every gpumesh ever released — which is what makes this handshake
            # deployable at all without a flag day.
            body = dict(info)
            body["protocol_version"] = PROTOCOL_VERSION
            try:
                resp = mesh.call("POST", "/api/register", body)
            except urllib.error.HTTPError as reg_exc:
                if reg_exc.code == _PROTOCOL_MISMATCH_STATUS:
                    raise ProtocolVersionMismatch(
                        _refusal_message(reg_exc), local=PROTOCOL_VERSION,
                    ) from reg_exc
                raise
            _check_coordinator_protocol(resp)
            return resp["worker_id"]
        except (_AuthError, ProtocolVersionMismatch):
            # Both are permanent refusals with a specific cause. Letting them
            # fall through to the retry/except-URLError arm below would spend
            # three attempts on a fact that cannot change, and then report the
            # result as a connection failure.
            raise
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            last_exc = exc
            if attempt < retries - 1:
                _print(f"{bold(cyan('[worker]'))} registration attempt {yellow(str(attempt + 1))}/{retries} "
                      f"{red('failed')}: {exc} — {yellow('retrying in 2s')}...")
                time.sleep(2)
                continue
            # If the coordinator answered the final attempt with a 401 the
            # token is wrong — surface a clear auth error, not a generic
            # connection failure.
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 401:
                raise _AuthError() from exc
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("registration failed with no captured exception")


def _snapshot_resources() -> dict:
    """Capture a snapshot of current resource usage for diagnostics."""
    snap = {
        "timestamp": time.time(),
        "process_id": os.getpid(),
    }
    try:
        import psutil
        proc = psutil.Process()
        mem = proc.memory_info()
        snap["rss_mb"] = round(mem.rss / (1024 ** 2), 1)
        snap["cpu_percent"] = proc.cpu_percent(interval=None)
    except (ImportError, OSError):
        pass
    gpu = capability.get_gpu_memory_info(0)
    if gpu:
        snap["gpu_free_mb"] = gpu["free_mb"]
        snap["gpu_used_mb"] = gpu["used_mb"]
    return snap


def _diagnostics_report(task_id: str, error: Exception,
                        started: float, start_resources: dict) -> dict:
    """Build a crash-recovery diagnostics report (Exo pattern).

    Attaches error type, traceback, resource usage at time of failure,
    and wall-clock duration to the failure status sent to the coordinator.
    """
    elapsed = round(time.time() - started, 2)
    end_resources = _snapshot_resources()
    gpu_delta = None
    if "gpu_used_mb" in start_resources and "gpu_used_mb" in end_resources:
        gpu_delta = round(
            end_resources["gpu_used_mb"] - start_resources["gpu_used_mb"], 1
        )
    report = {
        "error_type": type(error).__name__,
        "error_message": str(error),
        "elapsed_s": elapsed,
        "traceback": traceback.format_exc(),
        "resources_at_start": start_resources,
        "resources_at_end": end_resources,
    }
    if gpu_delta is not None:
        report["gpu_delta_mb"] = gpu_delta
    return report


def _classify_result_status(code: int):
    """What an HTTP status means for a result we are already holding.

    Returns ``None`` for "try again"; anything else is a final outcome.

    * **401** — the coordinator does not accept this token. A token does not
      change while we sleep, and the main loop needs to hear about it rather
      than watch a retry counter tick, so this is final and distinct.
    * **409** — ``complete_task`` matched no row: the lease expired and the
      task was re-leased and completed by somebody else, or the job was
      cancelled. Our copy really is obsolete. This is the one case where
      dropping a computed result is correct, and it is worth being precise
      about — retrying it would be holding an answer nobody is waiting for
      while the queue behind us goes unserved.
    * **408 / 429** — the two 4xx that explicitly mean "later". Retried.
    * **other 4xx** — the coordinator refuses these bytes, and the next
      attempt would send the same bytes. Retrying is a slower way to fail.
      Reported loudly instead, because a worker silently unable to deliver
      anything is the failure mode that hides worst.
    * **5xx**, including the 503 a shutting-down coordinator sends — a
      statement about the coordinator's moment, not about our request. It is
      expected back; that is precisely what the budget is for.
    """
    if code == 401:
        return AUTH_FAILED
    if code == 409:
        return OBSOLETE
    if code in (408, 429):
        return None
    if 400 <= code < 500:
        return UNDELIVERABLE
    return None


def _deliver_result(mesh: "MeshClient", payload: dict,
                    stop: "threading.Event | None" = None) -> str:
    """POST a task outcome, holding on to it until it lands or cannot.

    The worker is the right place for this. It is the side that owns the only
    copy, and it is already built to outlive coordinator outages — it
    re-registers and reconnects indefinitely and, by its own contract, never
    exits on its own. Giving up after a single POST was the one place that
    contract did not hold, and it was the place where giving up cost the most.

    *stop* stays responsive throughout: the wait between attempts is the event
    itself, so a shutdown wakes it immediately rather than at the end of a
    fifteen-second backoff.
    """
    if stop is None:
        stop = threading.Event()
    task_id = payload.get("task_id", "?")
    started = time.monotonic()
    delay = RESULT_RETRY_BASE
    attempts = 0
    warned = False
    while True:
        attempts += 1
        try:
            mesh.call("POST", "/api/result", payload,
                      timeout=RESULT_POST_TIMEOUT)
            if warned:
                safe_print(f"{bold(cyan('[worker]'))} result for "
                           f"{bold(str(task_id))} {green('delivered')} on "
                           f"attempt {yellow(str(attempts))}")
            return DELIVERED
        except urllib.error.HTTPError as exc:
            final = _classify_result_status(exc.code)
            if final == OBSOLETE:
                safe_print(f"{bold(cyan('[worker]'))} result for "
                           f"{bold(str(task_id))} {yellow('no longer wanted')} "
                           f"(HTTP 409) — the task was re-leased or cancelled; "
                           f"discarding it")
                return OBSOLETE
            if final == AUTH_FAILED:
                return AUTH_FAILED
            if final is not None:
                safe_print(f"{bold(cyan('[worker]'))} {red('WARNING')}: the "
                           f"coordinator refused the result for "
                           f"{bold(str(task_id))} with HTTP {exc.code} — "
                           f"retrying cannot change that, so the result is "
                           f"lost and the task will be re-run when its lease "
                           f"expires")
                return UNDELIVERABLE
            reason = f"HTTP {exc.code}"
        except (urllib.error.URLError, OSError) as exc:
            reason = str(getattr(exc, "reason", exc))
        except Exception as exc:
            # A coordinator that answered with something unparseable is still
            # a coordinator having a bad moment, and the result is still worth
            # more than our certainty about why the call failed.
            reason = f"{type(exc).__name__}: {exc}"

        # Re-read the event every pass: a shutdown can arrive mid-retry, and
        # from that moment the budget is the operator's patience, not the
        # coordinator's.
        shutting_down = stop.is_set()
        budget = (RESULT_RETRY_SHUTDOWN_BUDGET if shutting_down
                  else RESULT_RETRY_BUDGET)
        remaining = budget - (time.monotonic() - started)
        if remaining <= 0:
            safe_print(f"{bold(cyan('[worker]'))} {red('WARNING')}: gave up "
                       f"delivering the result for {bold(str(task_id))} after "
                       f"{yellow(str(attempts))} attempt(s) ({reason}) — the "
                       f"task will be re-run when its lease expires")
            return GAVE_UP
        if not warned:
            warned = True
            safe_print(f"{bold(cyan('[worker]'))} {yellow('could not submit')} "
                       f"the result for {bold(str(task_id))}: {reason} — "
                       f"{dim('holding it and retrying')}")
        wait = min(delay, remaining)
        if shutting_down:
            # stop.wait() returns instantly once the event is set, so it can no
            # longer pace this loop — and a hot loop would burn the whole
            # shutdown budget in milliseconds. The sleep is capped at a second
            # so the total stays bounded by the shutdown budget above, which is
            # itself short enough that nobody notices it.
            time.sleep(min(wait, 1.0))
        else:
            stop.wait(wait)
        delay = min(delay * RESULT_RETRY_FACTOR, RESULT_RETRY_MAX)


def run_worker(url: str, token: str, task_timeout: float = 240.0,
               safe_mode: bool = False, persist_connection: bool = True,
               stop_event: "threading.Event | None" = None):
    """Join a mesh as a worker and execute tasks until interrupted.

    Resilience contract: after a successful registration the worker NEVER
    exits on its own. A coordinator outage, laptop sleep, or WiFi drop only
    pauses it — it retries with capped exponential backoff and automatically
    re-registers when the coordinator returns. Only an explicit signal
    (Ctrl+C / SIGTERM / SIGBREAK), a 401 (the coordinator restarted with a
    different token), or *stop_event* being set stops the worker.

    Pass *stop_event* to shut a worker down from another thread. Signals only
    reach the main thread, so without it a worker started in a background
    thread — which is how :func:`start_worker_thread` and every embedded use
    runs one — could never be stopped at all.
    """
    mesh = MeshClient(url, token)
    info = capability.full_probe()
    safe_print(f"{bold(cyan('[worker]'))} device={device_icon(info['device'])} {bold(info['device_name'])} "
          f"score={yellow(str(info['score']))} GFLOP/s")

    try:
        # Retry transient connection errors (coordinator just started, etc.)
        worker_id = _try_register(mesh, info, retries=3)
        safe_print(f"{bold(cyan('[worker]'))} joined mesh as {bold(worker_id)}")
        if persist_connection:
            try:
                from . import connection_manager
                connection_manager.save_connection(url, token)
            except Exception:
                pass  # best-effort persistence; not critical
    except _AuthError:
        safe_print(f"{bold(cyan('[worker]'))} {red('authentication failed')} — the coordinator "
              f"rejected this token at {mesh.base_url}")
        safe_print("  The token is wrong, or the coordinator restarted with a different one.")
        safe_print(f"  Check your saved token: {cyan('gpumesh show-connection')}")
        safe_print(f"  Then rejoin with: {cyan(f'gpumesh join {url} --token <CORRECT_TOKEN>')}")
        return
    except ProtocolVersionMismatch as exc:
        # Deliberately ahead of the URLError branch below and of the generic
        # "failed to register" one. A version skew is not a network problem,
        # and the troubleshooting block those branches print — firewalls,
        # 10061 vs 10060, "is the coordinator running?" — is advice that sends
        # the operator to inspect a network that is working perfectly. The
        # saved connection is left alone for the same reason: the URL and the
        # token are both fine, so clearing them would turn one clear problem
        # into two.
        safe_print(f"{bold(cyan('[worker]'))} {red('protocol version mismatch')} — "
                   f"not joining {yellow(url)}")
        for line in textwrap.wrap(str(exc), width=76):
            safe_print(f"  {line}")
        safe_print(f"  This worker: gpumesh {bold(__version__)}, protocol "
                   f"{bold(str(PROTOCOL_VERSION))} "
                   f"(accepts {MIN_PROTOCOL_VERSION}-{PROTOCOL_VERSION})")
        # Built outside the f-string: an escaped quote inside an f-string
        # expression is a SyntaxError before Python 3.12, and this package
        # still supports 3.9.
        probe = "curl -H 'X-Auth-Token: <token>' " + url + "/api/health"
        safe_print(f"  Ask the coordinator for its numbers with: {dim(probe)}")
        return
    except (urllib.error.URLError, OSError) as exc:
        # Decode the specific underlying error for better guidance.
        #
        # Refused and timed out need OPPOSITE advice and must not be
        # conflated: refused means something answered and said no (the
        # coordinator is not listening there), while timed out means nothing
        # answered at all (the address is unroutable from here, or a firewall
        # is dropping the packets) — and in that case the coordinator may
        # well be running perfectly. Note WinError 10060 surfaces as
        # TimeoutError, so it must be classified after the refused check.
        cause = getattr(exc, "reason", exc)
        text = str(exc)
        is_refused = (
            isinstance(cause, ConnectionRefusedError)
            or "10061" in text
        )
        is_timeout = not is_refused and (
            isinstance(cause, (TimeoutError, socket.timeout))
            or "10060" in text
            or "timed out" in text.lower()
        )
        safe_print(f"{bold(cyan('[worker]'))} {red('failed to register')} with coordinator after retries: {exc}")
        # Clear the saved connection only when the address that just failed is
        # the saved one. The point is to stop a dead URL persisting and
        # silently breaking later commands — but this used to clear
        # unconditionally, so a single mistyped `gpumesh join` destroyed a
        # perfectly good saved connection to an entirely different
        # coordinator, and the next `gpumesh submit` (which reads that config)
        # failed for a reason the user had no way to connect to what they did.
        # Failing to reach one address says nothing about another.
        try:
            from . import connection_manager
            saved = connection_manager.load_connection() or {}
            saved_url = (saved.get("url") or "").rstrip("/").lower()
            if saved_url and saved_url == (url or "").rstrip("/").lower():
                connection_manager.clear_connection()
        except Exception:
            pass
        safe_print()
        safe_print(f"{bold(cyan('[worker]'))} {yellow('TROUBLESHOOTING:')}")
        if is_refused:
            safe_print(f"  {red('Connection REFUSED (10061) — something answered and said no.')}")
            safe_print(f"  {yellow('Most likely:')} the coordinator server is NOT running on that address,")
            safe_print("  OR it is bound to a different interface.")
            safe_print(f"  {cyan('- If testing on the same machine, use http://127.0.0.1:PORT')}")
            safe_print("    (127.0.0.1 is loopback-only and avoids interface/firewall issues).")
            safe_print(f"  {cyan('- On Windows, if binding to 0.0.0.0 fails, try running the')}")
            safe_print("    coordinator as Administrator so it can listen on all interfaces.")
        elif is_timeout:
            safe_print(f"  {red('Connection TIMED OUT (10060) — nothing answered at all.')}")
            safe_print(f"  {yellow('The coordinator may well be running')} — the packets never reached it.")
            safe_print(f"  {cyan('- The address may be unroutable from this machine.')} A coordinator")
            safe_print("    behind a VPN, or on a VirtualBox/VMware/Hyper-V/Docker adapter,")
            safe_print("    advertises an address only it can reach. Ask for the address that")
            safe_print("    matches YOUR network ('ipconfig' on Windows, 'ip addr' elsewhere).")
            safe_print(f"  {cyan('- Or a firewall is dropping the port silently.')} On the coordinator,")
            safe_print("    allow the inbound TCP port, or restart it as Administrator.")
            safe_print(f"  {cyan('- On different networks?')} Use Tailscale on both machines.")
        else:
            safe_print(f"  {red('The coordinator could not be reached (network or DNS error)')}.")
        safe_print(f"  {bold('1.')} Is the coordinator running? Start it with: {cyan('gpumesh serve --token YOUR_TOKEN')}")
        safe_print(f"  {bold('2.')} Is the URL correct? You tried: {yellow(url)}")
        safe_print(f"  {bold('3.')} Test it from THIS machine: {dim(f'curl {url}/api/workers')}")
        safe_print(f"     {dim('(HTTP 401 means reachable — then only the token is wrong)')}")
        safe_print(f"  {bold('4.')} Windows Firewall may be blocking the port — allow the port through")
        safe_print(f"  {bold('5.')} Are both machines on the same network? (or use Tailscale for remote)")
        safe_print()
        return
    except Exception as exc:
        safe_print(f"{bold(cyan('[worker]'))} {red('failed to register')} with coordinator: {exc}")
        return

    stop = stop_event if stop_event is not None else threading.Event()

    # --- Graceful shutdown handler (Exo pattern) ---
    # On SIGTERM/SIGBREAK: stop accepting new tasks immediately, let the
    # current task finish (with a grace period), report status, then exit.
    _shutdown_grace_start = [None]
    _current_task_id = [None]
    # Mutable connection state: worker_id changes when we auto re-register.
    _state = {"worker_id": worker_id, "need_reregister": False}
    _state_lock = threading.Lock()

    def _sigterm_handler(signum, frame):
        sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        safe_print(f"\n{bold(cyan('[worker]'))} received {bold(sig_name)} — {yellow('shutting down gracefully')}")
        _shutdown_grace_start[0] = time.time()
        stop.set()

    if threading.current_thread() is threading.main_thread():
        if platform.system() != "Windows":
            signal.signal(signal.SIGTERM, _sigterm_handler)
        else:
            try:
                signal.signal(signal.SIGBREAK, _sigterm_handler)
            except (AttributeError, ValueError):
                pass

    def heartbeat():
        last_benchmark = time.time()
        current_score = info["score"]
        while not stop.is_set():
            try:
                # Periodic re-benchmark to capture thermal throttling, etc.
                now = time.time()
                if now - last_benchmark >= REBENCHMARK_INTERVAL:
                    try:
                        bench = capability.run_benchmark(info["device"], force=True)
                        current_score = bench["score"]
                        safe_print(f"{bold(cyan('[worker]'))} re-benchmark: score={yellow(str(current_score))}"
                              f" (gflops={bench['gflops']},"
                              f" bw={dim(str(bench['bandwidth_gbps']))} GB/s)")
                    except Exception:
                        pass  # keep previous score on failure
                    last_benchmark = now
                with _task_id_lock:
                    tid = _current_task_id[0]
                with _state_lock:
                    wid = _state["worker_id"]
                try:
                    resp = mesh.call("POST", "/api/heartbeat", {
                        "worker_id": wid,
                        "score": current_score,
                        "gpu_memory_free_mb": capability.get_gpu_memory_usage().get("gpu_memory_free_mb", 0.0),
                        "task_id": tid,
                    })
                    # Coordinator answered but no longer knows us (row pruned
                    # by the TTL cleaner) → re-register on the main loop.
                    if resp is not None and resp.get("ok") is False:
                        with _state_lock:
                            _state["need_reregister"] = True
                except urllib.error.HTTPError as exc:
                    if exc.code == 401:
                        safe_print(f"{bold(cyan('[worker]'))} {red('authentication failed')} — the coordinator restarted with a different token. Exiting.")
                        stop.set()
                    else:
                        with _state_lock:
                            _state["need_reregister"] = True
                except Exception:
                    pass  # transient network blip; the main loop owns reconnection
            except Exception:
                pass
            stop.wait(HEARTBEAT_INTERVAL)

    threading.Thread(target=heartbeat, daemon=True).start()

    def submit(payload: dict) -> str:
        """Deliver one task outcome, and act on the one outcome that ends us.

        A 401 here means the same thing it means everywhere else in this file:
        the coordinator was restarted with a different token. The heartbeat
        thread and the lease loop both stop on it, and a worker that kept
        computing results it can never deliver would be worse than useless.
        """
        outcome = _deliver_result(mesh, payload, stop)
        if outcome == AUTH_FAILED:
            safe_print(f"{bold(cyan('[worker]'))} {red('authentication failed')} "
                       f"— the coordinator restarted with a different token. "
                       f"Exiting.")
            stop.set()
        return outcome

    done = failed = 0
    backoff = POLL_INTERVAL
    # Fast right after a task, slow when idle — see BUSY_POLL_INTERVAL above.
    lease_delay = BUSY_POLL_INTERVAL
    last_task_at = time.monotonic()
    unreachable_notice_shown = False
    try:
        while not stop.is_set():
            # Re-register once the coordinator is reachable again — we were
            # disconnected, or it restarted and forgot / pruned our row.
            if _state["need_reregister"]:
                try:
                    new_id = _try_register(mesh, info, retries=2, verbose=False)
                    with _state_lock:
                        _state["worker_id"] = new_id
                        _state["need_reregister"] = False
                    safe_print(f"{bold(cyan('[worker]'))} {green('reconnected')} to coordinator — joined mesh as {bold(new_id)}")
                    backoff = POLL_INTERVAL
                    unreachable_notice_shown = False
                except _AuthError:
                    # The coordinator restarted with a different token — retrying
                    # can never succeed, so stop with a clear message.
                    safe_print(f"{bold(cyan('[worker]'))} {red('authentication failed')} — the "
                          f"coordinator rejected this token after reconnection")
                    safe_print("  The coordinator was restarted with a different token. Exiting.")
                    stop.set()
                    break
                except ProtocolVersionMismatch as exc:
                    # The coordinator was restarted on a different gpumesh
                    # version while we were away — the one upgrade path an
                    # operator actually takes. Without this arm the mismatch
                    # would land in the generic `except Exception` below and
                    # the worker would retry every two seconds forever,
                    # silently, which is exactly the failure mode this whole
                    # handshake exists to delete.
                    safe_print(f"{bold(cyan('[worker]'))} {red('protocol version mismatch')} "
                               f"after reconnection — the coordinator was restarted "
                               f"on an incompatible gpumesh version")
                    for line in textwrap.wrap(str(exc), width=76):
                        safe_print(f"  {line}")
                    stop.set()
                    break
                except Exception:
                    # Coordinator still unreachable — wait and retry, never
                    # exit. stop.wait rather than time.sleep: a plain sleep
                    # here cannot see the stop event, so a worker asked to
                    # shut down kept sleeping out the full interval first.
                    stop.wait(2)
                    continue

            # Captured, not re-read at result time. complete_task matches the
            # worker_id recorded on the row when it was leased, so a worker
            # that re-registered in between must still report under the
            # identity that holds the lease — otherwise the write matches
            # nothing and a finished task looks abandoned.
            lease_worker_id = _state["worker_id"]
            try:
                task = mesh.call("POST", "/api/lease", {"worker_id": lease_worker_id})
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    safe_print(f"{bold(cyan('[worker]'))} {red('authentication failed')} — the coordinator restarted with a different token. Exiting.")
                    break
                # Coordinator answered but doesn't know us → re-register soon.
                with _state_lock:
                    _state["need_reregister"] = True
                stop.wait(POLL_INTERVAL)
                continue
            except Exception as exc:
                # Coordinator unreachable. NEVER exit: retry with capped backoff
                # until it comes back (laptop sleep / WiFi drop / restart safe).
                with _state_lock:
                    _state["need_reregister"] = True
                if not unreachable_notice_shown:
                    safe_print(f"{bold(cyan('[worker]'))} {yellow('coordinator unreachable')}: {exc}")
                    safe_print(f"{bold(cyan('[worker]'))} {dim('will keep retrying and auto-reconnect when it returns — no action needed')}")
                    unreachable_notice_shown = True
                # The worst of the three: backoff climbs to 60s, so a plain
                # sleep meant a worker told to stop during an outage kept
                # running for up to a minute — long enough for a test's
                # teardown to give up on it and leave the thread alive for the
                # rest of the process, and long enough for Ctrl+C to feel
                # broken.
                stop.wait(backoff)
                backoff = min(backoff * 2, 60.0)
                continue

            backoff = POLL_INTERVAL
            unreachable_notice_shown = False
            if task is None:
                # Nothing queued. Stay responsive for a short window after the
                # last task, then ease off so an idle worker costs nothing.
                if time.monotonic() - last_task_at > BUSY_POLL_WINDOW:
                    lease_delay = min(lease_delay * POLL_BACKOFF, IDLE_POLL_INTERVAL)
                stop.wait(lease_delay)
                continue
            # Got work: reset both the network backoff and the poll cadence.
            backoff = POLL_INTERVAL
            lease_delay = BUSY_POLL_INTERVAL
            last_task_at = time.monotonic()

            safe_print(f"{bold(cyan('[worker]'))} running task {bold(task['task_id'])} "
                  f"(cost={yellow(str(task['cost']))})")
            started = time.time()
            with _task_id_lock:
                _current_task_id[0] = task["task_id"]
            start_resources = _snapshot_resources()
            try:
                # Check if this is a function-based task
                payload = task["payload"]
                if safe_mode and "_func" in payload and task.get("script") == "__gpumesh_function__":
                    raise sandbox.TaskError(
                        "Safe mode: function distribution disabled on this coordinator. "
                        "Submit scripts instead."
                    )
                if not safe_mode and "_func" in payload and task.get("script") == "__gpumesh_function__":
                    result = _run_function_task(payload, info["device"], task_timeout, stop_event=stop)
                else:
                    result = sandbox.run_task(
                        task["script"], payload,
                        timeout=task_timeout, device=info["device"],
                    )
                elapsed = round(time.time() - started, 2)
                submit({
                    "task_id": task["task_id"], "worker_id": lease_worker_id,
                    "ok": True, "result": result, "elapsed": elapsed,
                })
                done += 1
                safe_print(f"{bold(cyan('[worker]'))} task {bold(task['task_id'])} {green('done')} in {dim(str(elapsed))}s "
                      f"(total done={yellow(str(done))})")
            except sandbox.TaskError as e:
                # Crash recovery: attach diagnostics to failure status
                diag = _diagnostics_report(task["task_id"], e, started, start_resources)
                safe_print(f"{bold(cyan('[worker]'))} task {bold(task['task_id'])} {red('FAILED')}: {e}")
                safe_print(f"{bold(cyan('[worker]'))} diagnostics: {red(diag['error_type'])} "
                      f"in {dim(str(diag['elapsed_s']))}s")
                if diag.get("gpu_delta_mb") is not None:
                    gpu_delta_val = diag['gpu_delta_mb']
                    safe_print(f"{bold(cyan('[worker]'))} GPU memory delta: {yellow(f'{gpu_delta_val:+.1f}')} MB")
                # Retried on the same terms as a success. A failure report is
                # not free either: lose it and the coordinator sees a silent
                # lease expiry, re-runs a task that is going to fail the same
                # way, and burns an attempt telling nobody anything.
                submit({
                    "task_id": task["task_id"], "worker_id": lease_worker_id,
                    "ok": False, "error": str(e),
                    # Deterministic failures (the task raised) are marked so
                    # the coordinator fails them at once instead of retrying.
                    "user_error": getattr(e, "user_error", False),
                    "diagnostics": diag,
                })
                failed += 1
            except Exception as e:
                diag = _diagnostics_report(task["task_id"], e, started, start_resources)
                safe_print(f"{bold(cyan('[worker]'))} {red('ERROR')}: unexpected error executing task: "
                      f"{type(e).__name__}: {e}")
                try:
                    submit({
                        "task_id": task["task_id"],
                        "worker_id": lease_worker_id,
                        "ok": False,
                        "error": f"Unexpected error: {type(e).__name__}: {e}",
                        "diagnostics": diag,
                    })
                except Exception:
                    # _deliver_result already contains every failure it can
                    # name; this arm only exists so an unexpected error in the
                    # reporting of an unexpected error cannot take the worker
                    # down with it.
                    pass
                failed += 1
            finally:
                with _task_id_lock:
                    _current_task_id[0] = None
                # Count the fast-poll window from when the task finished, so a
                # long task is still followed by a responsive stretch.
                last_task_at = time.monotonic()

        # Post-loop: graceful shutdown status report
        if _shutdown_grace_start[0] is not None:
            grace_elapsed = time.time() - _shutdown_grace_start[0]
            safe_print(f"{bold(cyan('[worker]'))} shutdown complete — ran for {dim(str(round(grace_elapsed, 1)))}s "
                  f"after signal (done={green(str(done))}, failed={red(str(failed))})")
    except KeyboardInterrupt:
        safe_print(f"\n{bold(cyan('[worker]'))} leaving mesh (done={green(str(done))}, failed={red(str(failed))})")
    finally:
        stop.set()
        # Best-effort status report to coordinator on exit. Kept on a short
        # timeout: this runs while the user is waiting for Ctrl+C to take
        # effect, and the coordinator is often the thing that just went away.
        try:
            mesh.call("POST", "/api/heartbeat", {
                "worker_id": _state["worker_id"],
                "status": "shutting_down",
                "done": done,
                "failed": failed,
            }, timeout=5.0)
        except Exception:
            pass
        # GPU cleanup: release any cached CUDA memory
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except (ImportError, RuntimeError):
            pass


def spawn_local_worker(url: str, token: str, task_timeout: float = 240.0,
                       safe_mode: bool = False, persist_connection: bool = False):
    """Start this machine as a worker of its OWN coordinator (self-worker).

    Used by ``gpumesh serve`` and ``GPUMesh.start_coordinator`` so the
    coordinator's own CPU/GPU automatically joins the pool. Runs in a daemon
    thread; the resilient :func:`run_worker` reconnects automatically.

    Returns the worker thread. Its ``gpumesh_stop`` attribute is the event
    that shuts the worker down — the same convention ``serve()`` uses for the
    coordinator — because a daemon thread that outlives its caller is not
    actually free: it keeps polling, and at interpreter shutdown it can be
    killed mid-print, leaving stdout's lock held and hanging the process.
    """
    stop = threading.Event()
    thread = threading.Thread(
        target=run_worker,
        args=(url, token, task_timeout, safe_mode, persist_connection, stop),
        daemon=True,
        name="gpumesh-self-worker",
    )
    thread.gpumesh_stop = stop
    thread.start()
    return thread


def run_worker_broadcast(token: str, claim_port: int = 0,
                         task_timeout: float = 240.0,
                         safe_mode: bool = False):
    """Start a worker that broadcasts its presence and waits to be claimed.

    Flow: broadcast UDP beacon → coordinator discovers us → coordinator
    sends claim to our HTTP claim server → verify token → register with
    coordinator → run as normal worker.
    """
    from . import capability, claimer, discovery

    # 1. Detect hardware
    info = capability.full_probe()
    safe_print(f"{bold(cyan('[worker]'))} device={device_icon(info['device'])} {bold(info['device_name'])} "
          f"score={yellow(str(info['score']))} GFLOP/s")

    # 2. Start claim server
    safe_print(f"{bold(cyan('[worker]'))} starting claim server on port {yellow(str(claim_port))}...")
    try:
        httpd, actual_port = claimer.start_claim_server(token, claim_port)
    except OSError as exc:
        safe_print(f"{bold(cyan('[worker]'))} {red('failed to start claim server')}: {exc}")
        return
    safe_print(f"{bold(cyan('[worker]'))} claim server listening on port {green(str(actual_port))}")

    # 3. Prepare claimed signal and storage for coordinator details
    claimed = threading.Event()
    coordinator_url = [None]
    coordinator_token = [None]

    # Patch the claim handler callback so it stores coordinator info and
    # sets the event instead of spawning its own run_worker thread.
    from .claimer import ClaimHandler
    with ClaimHandler._claim_lock:
        ClaimHandler._claimed = False  # reset in case of re-entry

    _orig_do_POST = ClaimHandler.do_POST

    def _intercepted_do_POST(self):
        """Intercept claim to store coordinator details, then let original
        handler respond."""
        if self.path != "/api/claim":
            return _orig_do_POST(self)

        # Answer these here rather than delegating to _orig_do_POST. The
        # delegation used to matter and then stopped: the original handler
        # calls _read_json() again, and the body it wants was already pulled
        # off the socket by the call above, so the second read blocked until
        # the connection died — one malformed claim body permanently cost this
        # worker its ability to be claimed. claimer._read_json is now
        # re-entry-safe and answers these cases itself, and claimer._send is
        # first-response-wins, which made the delegation a silent no-op. A
        # no-op that reads like a fallback is worse than no fallback, so it is
        # gone. What is left mirrors claimer.ClaimHandler.do_POST exactly:
        # the _send calls below are the ones _read_json already made, kept so
        # the two handlers stay legible as the same handler, and swallowed by
        # the first-response-wins guard.
        try:
            body = self._read_json()
        except UnicodeDecodeError:
            self._send(400, {"error": "request body must be UTF-8"})
            return
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid JSON"})
            return
        if body is None:
            # _read_json has already answered — rate limit, framing, size, or
            # shape (a non-dict JSON body is refused there, which is why there
            # is no isinstance check here).
            return

        # Validate token OUTSIDE the lock (read-only check, no lock needed)
        token_val = body.get("token", "")
        if not hmac.compare_digest(str(token_val), ClaimHandler._token):
            self._send(401, {"ok": False, "error": "wrong token"})
            return

        from .claimer import claim_candidates

        candidates = claim_candidates(body)
        tok = body.get("coordinator_token", "")
        if not candidates or not tok:
            self._send(400, {"ok": False,
                             "error": "need coordinator_url(s) and coordinator_token"})
            return

        # Use the class-level _claim_lock (consistent with claimer.py)
        # to prevent race conditions between dual-lock protection
        with ClaimHandler._claim_lock:
            if ClaimHandler._claimed:
                self._send(409, {"ok": False, "error": "already claimed"})
                return
            ClaimHandler._claimed = True

        # Same contract as claimer.ClaimHandler: the ack means "I reached
        # you", not "your token matched". Probing here is what lets the
        # coordinator report an unreachable address immediately, instead of
        # declaring success while this worker times out privately.
        url, reason = find_reachable_coordinator(candidates, tok)
        if url is None:
            with ClaimHandler._claim_lock:
                ClaimHandler._claimed = False  # allow a corrected retry
            safe_print(f"{bold(cyan('[worker]'))} {yellow('claim rejected')}: "
                       f"cannot reach coordinator ({reason})")
            self._send(502, {
                "ok": False,
                "error": f"cannot reach coordinator from this machine: {reason}",
                "tried": candidates,
            })
            return

        coordinator_url[0] = url
        coordinator_token[0] = tok

        # Send response and signal OUTSIDE the lock
        self._send(200, {"ok": True, "coordinator_url": url})
        claimed.set()

    ClaimHandler.do_POST = _intercepted_do_POST

    # Safe shutdown helper: BaseServer.shutdown() hangs forever if
    # serve_forever() was never called (it waits on __is_shut_down which
    # is only set in serve_forever's finally block).  Use server_close()
    # as a fallback when the server wasn't serving.
    def _safe_claim_shutdown():
        try:
            if hasattr(httpd, "_BaseServer__serving") and httpd._BaseServer__serving:
                httpd.shutdown()
            else:
                httpd.server_close()
        except OSError as exc:
            if getattr(exc, "winerror", None) == 10038:
                pass  # Windows: socket already closed during shutdown
            else:
                raise

    # 4. Start claim server in a daemon thread so it actually listens
    serve_thread = threading.Thread(
        target=httpd.serve_forever, daemon=True, name="gpumesh-claim-server"
    )
    serve_thread.start()

    # 5. Start UDP beacon
    safe_print(f"{bold(cyan('[worker]'))} broadcasting presence on local network (claim_port={yellow(str(actual_port))})...")
    beacon = None  # Initialize to ensure cleanup in finally block
    try:
        beacon = discovery.Beacon(
            device=info["device"],
            device_name=info["device_name"],
            score=info["score"],
            api_port=0,  # worker is a client, not a server
            claim_port=actual_port,
            hostname=info.get("hostname"),
        )
        beacon.start()
    except Exception as exc:
        safe_print(f"{bold(cyan('[worker]'))} {red('failed to start beacon')}: {exc}")
        ClaimHandler.do_POST = _orig_do_POST
        _safe_claim_shutdown()
        return

    # 5. Wait for claim
    safe_print(f"{bold(cyan('[worker]'))} waiting for coordinator to claim this worker...")
    safe_print(f"{bold(cyan('[worker]'))} {dim('(Ctrl+C to stop)')}")

    # Handle SIGTERM/SIGBREAK for graceful shutdown
    stop_broadcast = threading.Event()

    def _sigterm_handler(signum, frame):
        stop_broadcast.set()
        claimed.set()  # unblock wait

    if threading.current_thread() is threading.main_thread():
        if platform.system() != "Windows":
            signal.signal(signal.SIGTERM, _sigterm_handler)
        else:
            try:
                signal.signal(signal.SIGBREAK, _sigterm_handler)
            except (AttributeError, ValueError):
                pass

    try:
        # Wait for either claim or stop signal
        while not claimed.is_set():
            claimed.wait(timeout=1.0)
            if stop_broadcast.is_set():
                safe_print(f"\n{bold(cyan('[worker]'))} {yellow('stopped before being claimed')}")
                return
    except KeyboardInterrupt:
        safe_print(f"\n{bold(cyan('[worker]'))} {yellow('stopped before being claimed')}")
        return
    finally:
        # Restore original handler
        ClaimHandler.do_POST = _orig_do_POST
        if beacon is not None:
            try:
                beacon.stop()
            except Exception:
                pass  # best-effort cleanup
        _safe_claim_shutdown()
        with ClaimHandler._claim_lock:
            ClaimHandler._claimed = False

    if not coordinator_url[0]:
        return

    safe_print(f"{bold(cyan('[worker]'))} claimed by coordinator at {green(coordinator_url[0])}")
    safe_print(f"{bold(cyan('[worker]'))} {cyan('joining mesh')}...")

    # 6. Join the coordinator mesh
    try:
        run_worker(coordinator_url[0], coordinator_token[0], task_timeout, safe_mode)
    except Exception as exc:
        safe_print(f"{bold(cyan('[worker]'))} {red('ERROR')}: failed to join mesh: {exc}")
