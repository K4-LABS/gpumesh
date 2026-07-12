"""End-to-end: real coordinator over HTTP, simulated worker, full job cycle."""

import threading

import pytest

from gpumesh import sandbox
from gpumesh.server import serve
from gpumesh.worker import MeshClient

TOKEN = "test-token"

SCRIPT = """
import json, sys
payload = json.load(sys.stdin)
print(json.dumps({"square": payload["n"] ** 2}))
"""


@pytest.fixture
def mesh(tmp_path):
    httpd = serve("127.0.0.1", 0, str(tmp_path / "e2e.db"), TOKEN)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield MeshClient(f"http://127.0.0.1:{port}", TOKEN)
    httpd.gpumesh_stop.set()
    httpd.shutdown()


def test_full_job_lifecycle(mesh):
    # worker joins
    reg = mesh.call("POST", "/api/register",
                    {"hostname": "test", "device": "cpu", "score": 1.0})
    worker_id = reg["worker_id"]

    # client submits a 3-shard job
    job = mesh.call("POST", "/api/jobs", {
        "name": "squares",
        "script": SCRIPT,
        "payloads": [{"n": 2, "cost": 1}, {"n": 3, "cost": 2}, {"n": 4, "cost": 3}],
    })
    job_id = job["job_id"]

    # worker drains the queue
    while True:
        task = mesh.call("POST", "/api/lease", {"worker_id": worker_id})
        if task is None:
            break
        result = sandbox.run_task(task["script"], task["payload"], timeout=30)
        mesh.call("POST", "/api/result", {
            "task_id": task["task_id"], "worker_id": worker_id,
            "ok": True, "result": result,
        })

    status = mesh.call("GET", f"/api/jobs/{job_id}")
    assert status["finished"] is True
    squares = sorted(t["result"]["square"] for t in status["tasks"])
    assert squares == [4, 9, 16]


def test_auth_rejected(mesh):
    import urllib.error

    bad = MeshClient(mesh.base_url, "wrong-token")
    with pytest.raises(urllib.error.HTTPError) as exc:
        bad.call("GET", "/api/workers")
    assert exc.value.code == 401


def test_submit_requires_payloads(mesh):
    import urllib.error

    with pytest.raises(urllib.error.HTTPError) as exc:
        mesh.call("POST", "/api/jobs", {"name": "x", "script": "y", "payloads": []})
    assert exc.value.code == 400
