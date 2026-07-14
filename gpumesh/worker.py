"""Worker agent: joins a mesh and executes tasks until interrupted.

Loop: register -> heartbeat thread -> poll for a lease -> run task in a
sandboxed subprocess -> post the result -> repeat.
"""

import hmac
import json
import platform
import signal
import threading
import time
import urllib.error
import urllib.request

from . import capability, sandbox

HEARTBEAT_INTERVAL = 10.0
POLL_INTERVAL = 2.0
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


def _run_function_task(payload: dict, device: str, timeout: float) -> dict:
    """Execute a function-based task from the Python API.

    Runs the function in a thread with timeout protection.
    Strips internal keys (cost, _func, _task_index) from params.
    Includes _task_index in result for proper ordering.

    Note: GPUMESH_DEVICE env var is protected by _env_lock to avoid races
    with timed-out daemon threads that may still be running.
    """
    import os
    import threading
    from . import serializer

    func = serializer.deserialize_function(payload["_func"])
    params = {k: v for k, v in payload.get("_params", {}).items()
              if not k.startswith("_") and k != "cost"}
    task_index = payload.get("_task_index", 0)

    result_container = [None]
    error_container = [None]

    def _execute():
        try:
            result = func(**params)
            if not isinstance(result, dict):
                result = {"result": result}
            # Include task index for result ordering
            result["_task_index"] = task_index
            result_container[0] = result
        except Exception as e:
            error_container[0] = e

    # Set device env var for the function
    with _env_lock:
        old_device = os.environ.get("GPUMESH_DEVICE")
        os.environ["GPUMESH_DEVICE"] = device
    try:
        t = threading.Thread(target=_execute, daemon=True)
        t.start()
        t.join(timeout=timeout)

        if t.is_alive():
            # Thread is still running after timeout - log warning and wait longer
            # for cleanup (especially important for GPU operations that need release)
            t.join(timeout=min(timeout * 0.1, 5.0))  # Wait up to 10% of timeout or 5s
            if t.is_alive():
                raise sandbox.TaskError(
                    f"function timed out after {timeout}s"
                    f" (background thread still running — may hold GPU memory)"
                )
            else:
                raise sandbox.TaskError(f"function timed out after {timeout}s")
        if error_container[0] is not None:
            err = error_container[0]
            raise sandbox.TaskError(f"{type(err).__name__}: {err}")
        return result_container[0]
    finally:
        # Wait for any remaining cleanup in the thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        with _env_lock:
            if old_device is None:
                os.environ.pop("GPUMESH_DEVICE", None)
            else:
                os.environ["GPUMESH_DEVICE"] = old_device


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

    # Handle SIGTERM for graceful shutdown (Docker, systemd, kill)
    if threading.current_thread() is threading.main_thread():
        def _sigterm_handler(signum, frame):
            stop.set()
        if platform.system() != "Windows":
            signal.signal(signal.SIGTERM, _sigterm_handler)
        else:
            try:
                signal.signal(signal.SIGBREAK, _sigterm_handler)
            except (AttributeError, ValueError):
                pass

    def heartbeat():
        while not stop.is_set():
            try:
                mesh.call("POST", "/api/heartbeat", {"worker_id": worker_id})
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
        while True:
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
            try:
                # Check if this is a function-based task
                payload = task["payload"]
                if "_func" in payload and task.get("script") == "__gpumesh_function__":
                    result = _run_function_task(payload, info["device"], task_timeout)
                else:
                    result = sandbox.run_task(
                        task["script"], payload,
                        timeout=task_timeout, device=info["device"],
                    )
                elapsed = round(time.time() - started, 2)
                try:
                    mesh.call("POST", "/api/result", {
                        "task_id": task["task_id"], "worker_id": worker_id,
                        "ok": True, "result": result,
                    })
                except (urllib.error.URLError, OSError) as e:
                    print(f"[worker] WARNING: failed to submit result for "
                          f"{task['task_id']}: {e}")
                done += 1
                print(f"[worker] task {task['task_id']} done in {elapsed}s "
                      f"(total done={done})")
            except sandbox.TaskError as e:
                try:
                    mesh.call("POST", "/api/result", {
                        "task_id": task["task_id"], "worker_id": worker_id,
                        "ok": False, "error": str(e),
                    })
                except (urllib.error.URLError, OSError) as submit_err:
                    print(f"[worker] WARNING: failed to submit error for "
                          f"{task['task_id']}: {submit_err}")
                failed += 1
                print(f"[worker] task {task['task_id']} FAILED: {e}")
            except Exception as e:
                print(f"[worker] ERROR: unexpected error executing task: {type(e).__name__}: {e}")
                try:
                    mesh.call("POST", "/api/result", {
                        "task_id": task["task_id"],
                        "worker_id": worker_id,
                        "ok": False,
                        "error": f"Unexpected error: {type(e).__name__}: {e}",
                    })
                except Exception:
                    pass
                failed += 1
    except KeyboardInterrupt:
        print(f"\n[worker] leaving mesh (done={done}, failed={failed})")
    finally:
        stop.set()


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
