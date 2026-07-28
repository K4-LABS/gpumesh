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
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request

from . import capability, sandbox

HEARTBEAT_INTERVAL = 10.0
POLL_INTERVAL = 2.0
REBENCHMARK_INTERVAL = 600.0  # re-benchmark every 10 minutes
GRACEFUL_SHUTDOWN_TIMEOUT = 30.0  # seconds to wait for current task on SIGTERM
_env_lock = threading.Lock()  # protects os.environ["GPUMESH_DEVICE"]


class MeshClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def call(self, method: str, path: str, body=None):
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
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status == 204:
                return None
            raw = resp.read()
            return json.loads(raw) if raw else None


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
            print("[worker] graceful shutdown timeout expired — killing subprocess")
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
        for line in reversed(stdout_text.strip().splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict) and "error" in parsed:
                    error_msg = parsed["error"]
                    tb = parsed.get("traceback", "")
                    break
            except (json.JSONDecodeError, ValueError):
                pass
        if error_msg is None:
            # Fallback: last few lines of stderr
            tail = stderr_text.strip().splitlines()[-5:]
            error_msg = " | ".join(tail) if tail else f"exit code {proc.returncode}"
        raise sandbox.TaskError(f"function subprocess failed: {error_msg}")

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

    # Inject task index for result ordering (subprocess doesn't know it)
    if isinstance(result, dict):
        result["_task_index"] = task_index

    return result


def _try_register(mesh: "MeshClient", info: dict, retries: int = 3):
    """Register with the coordinator, retrying transient connection errors.

    Many registration failures are transient (e.g. the coordinator was just
    started and the worker's first attempt races ahead of it). Retry a small
    number of times with a short sleep before giving up.

    Returns the worker_id on success, or raises the last underlying exception
    (urllib.error.URLError / OSError) if every attempt failed.
    """
    last_exc = None
    for attempt in range(retries):
        try:
            # Quick reachability probe so a connection failure produces a clear
            # message ("coordinator not reachable") instead of a cryptic one.
            try:
                mesh.call("GET", "/api/workers")
            except (urllib.error.URLError, OSError) as probe_exc:
                print(f"[worker] coordinator not reachable at "
                      f"{mesh.base_url}: {probe_exc}")
                if attempt < retries - 1:
                    print(f"[worker] retrying registration in 2s "
                          f"(attempt {attempt + 1}/{retries})...")
                    time.sleep(2)
                    continue
                raise
            resp = mesh.call("POST", "/api/register", info)
            return resp["worker_id"]
        except (urllib.error.URLError, OSError) as exc:
            last_exc = exc
            if attempt < retries - 1:
                print(f"[worker] registration attempt {attempt + 1}/{retries} "
                      f"failed: {exc} — retrying in 2s...")
                time.sleep(2)
                continue
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


def run_worker(url: str, token: str, task_timeout: float = 240.0):
    mesh = MeshClient(url, token)
    info = capability.full_probe()
    print(f"[worker] device={info['device']} ({info['device_name']}) "
          f"score={info['score']} GFLOP/s")

    try:
        # Retry transient connection errors (coordinator just started, etc.)
        worker_id = _try_register(mesh, info, retries=3)
        print(f"[worker] joined mesh as {worker_id}")
        try:
            from . import connection_manager
            connection_manager.save_connection(url, token)
        except Exception:
            pass  # best-effort persistence; not critical
    except (urllib.error.URLError, OSError) as exc:
        # Decode the specific underlying error for better guidance.
        cause = getattr(exc, "reason", exc)
        is_refused = (
            isinstance(cause, ConnectionRefusedError)
            or isinstance(cause, TimeoutError)
            or "10061" in str(exc)
        )
        print(f"[worker] failed to register with coordinator after retries: {exc}")
        # Best-effort: clear any possibly-stale saved connection so a bad
        # URL/token doesn't persist and silently break future runs.
        try:
            from . import connection_manager
            connection_manager.clear_connection()
        except Exception:
            pass
        print()
        print("[worker] TROUBLESHOOTING:")
        if is_refused:
            print("  This error (10061 / ConnectionRefused) means the coordinator")
            print("  actively refused the connection.")
            print("  Most likely: the coordinator server is NOT running on that address,")
            print("  OR it is bound to a different interface.")
            print("  - If testing on the same machine, use http://127.0.0.1:PORT")
            print("    (127.0.0.1 is loopback-only and avoids interface/firewall issues).")
            print("  - On Windows, if binding to 0.0.0.0 fails, try running the")
            print("    coordinator as Administrator so it can listen on all interfaces.")
        else:
            print("  The coordinator could not be reached (timeout / network error).")
        print("  1. Is the coordinator running? Start it with: gpumesh serve --token YOUR_TOKEN")
        print(f"  2. Is the URL correct? You tried: {url}")
        print("  3. Is the port open? Try: curl http://127.0.0.1:PORT/api/workers")
        print("  4. Windows Firewall may be blocking the port — allow the port through")
        print("  5. Are both machines on the same network? (or use Tailscale for remote)")
        print()
        return
    except Exception as exc:
        print(f"[worker] failed to register with coordinator: {exc}")
        return

    stop = threading.Event()

    # --- Graceful shutdown handler (Exo pattern) ---
    # On SIGTERM/SIGBREAK: stop accepting new tasks immediately, let the
    # current task finish (with a grace period), report status, then exit.
    _shutdown_grace_start = [None]
    _current_task_id = [None]

    def _sigterm_handler(signum, frame):
        sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        print(f"\n[worker] received {sig_name} — shutting down gracefully")
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
                        print(f"[worker] re-benchmark: score={current_score}"
                              f" (gflops={bench['gflops']},"
                              f" bw={bench['bandwidth_gbps']} GB/s)")
                    except Exception:
                        pass  # keep previous score on failure
                    last_benchmark = now
                mesh.call("POST", "/api/heartbeat", {
                    "worker_id": worker_id,
                    "score": current_score,
                    "gpu_memory_free_mb": capability.get_gpu_memory_usage().get("gpu_memory_free_mb", 0.0),
                    "task_id": _current_task_id[0],
                })
            except (urllib.error.URLError, OSError, json.JSONDecodeError):
                pass  # transient network blip; the lease reaper covers us
            stop.wait(HEARTBEAT_INTERVAL)

    threading.Thread(target=heartbeat, daemon=True).start()

    done = failed = 0
    backoff = POLL_INTERVAL
    # Synapse-style liveness tracking: timestamp-based (not counter-based).
    # Tracks last successful coordinator interaction; exits if unreachable
    # for COORDINATOR_TIMEOUT seconds (like synapse's _reap_stale_peers).
    last_successful_call = time.time()
    COORDINATOR_TIMEOUT = 120.0  # seconds — exit if no success for 2 min
    try:
        while not stop.is_set():
            try:
                task = mesh.call("POST", "/api/lease", {"worker_id": worker_id})
            except (urllib.error.URLError, OSError) as e:
                # Check if we've been unreachable too long (synapse pattern)
                elapsed = time.time() - last_successful_call
                if elapsed > COORDINATOR_TIMEOUT:
                    print(f"[worker] coordinator unreachable for {elapsed:.0f}s "
                          f"(>{COORDINATOR_TIMEOUT}s). Exiting.")
                    break
                wait = min(backoff, 60.0)
                print(f"[worker] WARNING: coordinator unreachable "
                      f"({elapsed:.0f}s since last success): {e}")
                time.sleep(wait)
                backoff = min(backoff * 2, 60.0)
                continue

            # Successful call — update timestamp and reset backoff
            last_successful_call = time.time()
            backoff = POLL_INTERVAL
            if task is None:
                time.sleep(POLL_INTERVAL)
                continue
            # Only reset backoff when we actually get work (not on empty polls)
            backoff = POLL_INTERVAL

            print(f"[worker] running task {task['task_id']} "
                  f"(cost={task['cost']})")
            started = time.time()
            _current_task_id[0] = task["task_id"]
            start_resources = _snapshot_resources()
            try:
                # Check if this is a function-based task
                payload = task["payload"]
                if "_func" in payload and task.get("script") == "__gpumesh_function__":
                    result = _run_function_task(payload, info["device"], task_timeout, stop_event=stop)
                else:
                    result = sandbox.run_task(
                        task["script"], payload,
                        timeout=task_timeout, device=info["device"],
                    )
                elapsed = round(time.time() - started, 2)
                try:
                    mesh.call("POST", "/api/result", {
                        "task_id": task["task_id"], "worker_id": worker_id,
                        "ok": True, "result": result, "elapsed": elapsed,
                    })
                except (urllib.error.URLError, OSError) as e:
                    print(f"[worker] WARNING: failed to submit result for "
                          f"{task['task_id']}: {e}")
                done += 1
                print(f"[worker] task {task['task_id']} done in {elapsed}s "
                      f"(total done={done})")
            except sandbox.TaskError as e:
                # Crash recovery: attach diagnostics to failure status
                diag = _diagnostics_report(task["task_id"], e, started, start_resources)
                print(f"[worker] task {task['task_id']} FAILED: {e}")
                print(f"[worker] diagnostics: {diag['error_type']} "
                      f"in {diag['elapsed_s']}s")
                if diag.get("gpu_delta_mb") is not None:
                    print(f"[worker] GPU memory delta: {diag['gpu_delta_mb']:+.1f} MB")
                try:
                    mesh.call("POST", "/api/result", {
                        "task_id": task["task_id"], "worker_id": worker_id,
                        "ok": False, "error": str(e),
                        "diagnostics": diag,
                    })
                except (urllib.error.URLError, OSError) as submit_err:
                    print(f"[worker] WARNING: failed to submit error for "
                          f"{task['task_id']}: {submit_err}")
                failed += 1
            except Exception as e:
                diag = _diagnostics_report(task["task_id"], e, started, start_resources)
                print(f"[worker] ERROR: unexpected error executing task: "
                      f"{type(e).__name__}: {e}")
                try:
                    mesh.call("POST", "/api/result", {
                        "task_id": task["task_id"],
                        "worker_id": worker_id,
                        "ok": False,
                        "error": f"Unexpected error: {type(e).__name__}: {e}",
                        "diagnostics": diag,
                    })
                except Exception:
                    pass
                failed += 1
            finally:
                _current_task_id[0] = None

        # Post-loop: graceful shutdown status report
        if _shutdown_grace_start[0] is not None:
            grace_elapsed = time.time() - _shutdown_grace_start[0]
            print(f"[worker] shutdown complete — ran for {grace_elapsed:.1f}s "
                  f"after signal (done={done}, failed={failed})")
    except KeyboardInterrupt:
        print(f"\n[worker] leaving mesh (done={done}, failed={failed})")
    finally:
        stop.set()
        # Best-effort status report to coordinator on exit
        try:
            mesh.call("POST", "/api/heartbeat", {
                "worker_id": worker_id,
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


def run_worker_broadcast(token: str, claim_port: int = 0,
                         task_timeout: float = 240.0):
    """Start a worker that broadcasts its presence and waits to be claimed.

    Flow: broadcast UDP beacon → coordinator discovers us → coordinator
    sends claim to our HTTP claim server → verify token → register with
    coordinator → run as normal worker.
    """
    from . import capability, claimer, discovery

    # 1. Detect hardware
    info = capability.full_probe()
    print(f"[worker] device={info['device']} ({info['device_name']}) "
          f"score={info['score']} GFLOP/s")

    # 2. Start claim server
    print(f"[worker] starting claim server on port {claim_port}...")
    try:
        httpd, actual_port = claimer.start_claim_server(token, claim_port)
    except OSError as exc:
        print(f"[worker] failed to start claim server: {exc}")
        return
    print(f"[worker] claim server listening on port {actual_port}")

    # 3. Prepare claimed signal and storage for coordinator details
    claimed = threading.Event()
    coordinator_url = [None]
    coordinator_token = [None]
    claim_lock = threading.Lock()

    # Patch the claim handler callback so it stores coordinator info and
    # sets the event instead of spawning its own run_worker thread.
    from .claimer import ClaimHandler
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

        url = body.get("coordinator_url", "")
        tok = body.get("coordinator_token", "")
        if not url or not tok:
            self._send(400, {"ok": False,
                             "error": "need coordinator_url and coordinator_token"})
            return

        # Atomically check and set _claimed under lock
        with claim_lock:
            if ClaimHandler._claimed:
                self._send(409, {"ok": False, "error": "already claimed"})
                return
            ClaimHandler._claimed = True
            coordinator_url[0] = url
            coordinator_token[0] = tok

        # Send response and signal OUTSIDE the lock
        self._send(200, {"ok": True})
        claimed.set()

    ClaimHandler.do_POST = _intercepted_do_POST

    # Safe shutdown helper: BaseServer.shutdown() hangs forever if
    # serve_forever() was never called (it waits on __is_shut_down which
    # is only set in serve_forever's finally block).  Use server_close()
    # as a fallback when the server wasn't serving.
    def _safe_claim_shutdown():
        if hasattr(httpd, "_BaseServer__serving") and httpd._BaseServer__serving:
            httpd.shutdown()
        else:
            httpd.server_close()

    # 4. Start claim server in a daemon thread so it actually listens
    serve_thread = threading.Thread(
        target=httpd.serve_forever, daemon=True, name="gpumesh-claim-server"
    )
    serve_thread.start()

    # 5. Start UDP beacon
    print(f"[worker] broadcasting presence on local network (claim_port={actual_port})...")
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
        print(f"[worker] failed to start beacon: {exc}")
        ClaimHandler.do_POST = _orig_do_POST
        _safe_claim_shutdown()
        return

    # 5. Wait for claim
    print("[worker] waiting for coordinator to claim this worker...")
    print("[worker] (Ctrl+C to stop)")

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
                print("\n[worker] stopped before being claimed")
                return
    except KeyboardInterrupt:
        print("\n[worker] stopped before being claimed")
        return
    finally:
        # Restore original handler
        ClaimHandler.do_POST = _orig_do_POST
        ClaimHandler._claimed = False
        beacon.stop()
        _safe_claim_shutdown()

    if not coordinator_url[0]:
        return

    print(f"[worker] claimed by coordinator at {coordinator_url[0]}")
    print("[worker] joining mesh...")

    # 6. Join the coordinator mesh
    try:
        run_worker(coordinator_url[0], coordinator_token[0], task_timeout)
    except Exception as exc:
        print(f"[worker] ERROR: failed to join mesh: {exc}")
