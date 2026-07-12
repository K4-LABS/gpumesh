"""Worker agent: joins a mesh and executes tasks until interrupted.

Loop: register -> heartbeat thread -> poll for a lease -> run task in a
sandboxed subprocess -> post the result -> repeat.
"""

import json
import threading
import time
import urllib.error
import urllib.request

from . import capability, sandbox

HEARTBEAT_INTERVAL = 10.0
POLL_INTERVAL = 2.0


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
    old_device = os.environ.get("GPUMESH_DEVICE")
    os.environ["GPUMESH_DEVICE"] = device
    try:
        t = threading.Thread(target=_execute, daemon=True)
        t.start()
        t.join(timeout=timeout)

        if t.is_alive():
            raise sandbox.TaskError(f"function timed out after {timeout}s")
        if error_container[0] is not None:
            err = error_container[0]
            raise sandbox.TaskError(f"{type(err).__name__}: {err}")
        return result_container[0]
    finally:
        if old_device is None:
            os.environ.pop("GPUMESH_DEVICE", None)
        else:
            os.environ["GPUMESH_DEVICE"] = old_device


def run_worker(url: str, token: str, task_timeout: float = 240.0):
    mesh = MeshClient(url, token)
    info = capability.full_probe()
    print(f"[worker] device={info['device']} ({info['device_name']}) "
          f"score={info['score']} GFLOP/s")

    resp = mesh.call("POST", "/api/register", info)
    worker_id = resp["worker_id"]
    print(f"[worker] joined mesh as {worker_id}")

    stop = threading.Event()

    def heartbeat():
        while not stop.is_set():
            try:
                mesh.call("POST", "/api/heartbeat", {"worker_id": worker_id})
            except (urllib.error.URLError, OSError):
                pass  # transient network blip; the lease reaper covers us
            stop.wait(HEARTBEAT_INTERVAL)

    threading.Thread(target=heartbeat, daemon=True).start()

    done = failed = 0
    try:
        while True:
            try:
                task = mesh.call("POST", "/api/lease", {"worker_id": worker_id})
            except (urllib.error.URLError, OSError) as e:
                print(f"[worker] coordinator unreachable ({e}); retrying...")
                time.sleep(POLL_INTERVAL * 2)
                continue

            if task is None:
                time.sleep(POLL_INTERVAL)
                continue

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
                mesh.call("POST", "/api/result", {
                    "task_id": task["task_id"], "worker_id": worker_id,
                    "ok": True, "result": result,
                })
                done += 1
                print(f"[worker] task {task['task_id']} done in {elapsed}s "
                      f"(total done={done})")
            except sandbox.TaskError as e:
                mesh.call("POST", "/api/result", {
                    "task_id": task["task_id"], "worker_id": worker_id,
                    "ok": False, "error": str(e),
                })
                failed += 1
                print(f"[worker] task {task['task_id']} FAILED: {e}")
    except KeyboardInterrupt:
        print(f"\n[worker] leaving mesh (done={done}, failed={failed})")
    finally:
        stop.set()
