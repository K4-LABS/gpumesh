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

import hmac
import json
import os
import platform
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request

from . import capability, sandbox
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
# NOTE: there are deliberately NO "give up and exit" thresholds here.
# After a worker registers it never exits on its own: coordinator outages,
# laptop sleep and WiFi drops are survived by retrying with capped
# exponential backoff, and the worker re-registers when the coordinator
# returns (see run_worker).
_env_lock = threading.Lock()  # protects os.environ["GPUMESH_DEVICE"]
_task_id_lock = threading.Lock()  # protects _current_task_id[0] across heartbeat/main threads


class MeshClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 204:
                return None
            raw = resp.read()
            return json.loads(raw) if raw else None


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

    func = serializer.deserialize_function(payload["_func"])
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
        kwargs["preexec_fn"] = os.setsid  # new process group for clean kill

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
        stop_event.wait()
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
    immediately instead of retrying.
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
            resp = mesh.call("POST", "/api/register", info)
            return resp["worker_id"]
        except _AuthError:
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


def run_worker(url: str, token: str, task_timeout: float = 240.0,
               safe_mode: bool = False, persist_connection: bool = True):
    """Join a mesh as a worker and execute tasks until interrupted.

    Resilience contract: after a successful registration the worker NEVER
    exits on its own. A coordinator outage, laptop sleep, or WiFi drop only
    pauses it — it retries with capped exponential backoff and automatically
    re-registers when the coordinator returns. Only an explicit signal
    (Ctrl+C / SIGTERM / SIGBREAK) or a 401 (the coordinator restarted with a
    different token) stops the worker.
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
        # Best-effort: clear any possibly-stale saved connection so a bad
        # URL/token doesn't persist and silently break future runs.
        try:
            from . import connection_manager
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

    stop = threading.Event()

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
                except Exception:
                    # Coordinator still unreachable — wait and retry, never exit.
                    time.sleep(2)
                    continue

            try:
                task = mesh.call("POST", "/api/lease", {"worker_id": _state["worker_id"]})
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    safe_print(f"{bold(cyan('[worker]'))} {red('authentication failed')} — the coordinator restarted with a different token. Exiting.")
                    break
                # Coordinator answered but doesn't know us → re-register soon.
                with _state_lock:
                    _state["need_reregister"] = True
                time.sleep(POLL_INTERVAL)
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
                time.sleep(backoff)
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
                try:
                    mesh.call("POST", "/api/result", {
                        "task_id": task["task_id"], "worker_id": _state["worker_id"],
                        "ok": True, "result": result, "elapsed": elapsed,
                    })
                except (urllib.error.URLError, OSError) as e:
                    safe_print(f"{bold(cyan('[worker]'))} {yellow('WARNING')}: failed to submit result for "
                          f"{bold(task['task_id'])}: {e}")
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
                try:
                    mesh.call("POST", "/api/result", {
                        "task_id": task["task_id"], "worker_id": _state["worker_id"],
                        "ok": False, "error": str(e),
                        # Deterministic failures (the task raised) are marked so
                        # the coordinator fails them at once instead of retrying.
                        "user_error": getattr(e, "user_error", False),
                        "diagnostics": diag,
                    })
                except (urllib.error.URLError, OSError) as submit_err:
                    safe_print(f"{bold(cyan('[worker]'))} {yellow('WARNING')}: failed to submit error for "
                          f"{bold(task['task_id'])}: {submit_err}")
                failed += 1
            except Exception as e:
                diag = _diagnostics_report(task["task_id"], e, started, start_resources)
                safe_print(f"{bold(cyan('[worker]'))} {red('ERROR')}: unexpected error executing task: "
                      f"{type(e).__name__}: {e}")
                try:
                    mesh.call("POST", "/api/result", {
                        "task_id": task["task_id"],
                        "worker_id": _state["worker_id"],
                        "ok": False,
                        "error": f"Unexpected error: {type(e).__name__}: {e}",
                        "diagnostics": diag,
                    })
                except Exception:
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
        # Best-effort status report to coordinator on exit
        try:
            mesh.call("POST", "/api/heartbeat", {
                "worker_id": _state["worker_id"],
                "status": "shutting_down",
                "done": done,
                "failed": failed,
            })
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

    Returns the worker thread.
    """
    thread = threading.Thread(
        target=run_worker,
        args=(url, token, task_timeout, safe_mode, persist_connection),
        daemon=True,
        name="gpumesh-self-worker",
    )
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

        try:
            body = self._read_json()
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _orig_do_POST(self)
        if body is None or not isinstance(body, dict):
            return _orig_do_POST(self)

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
