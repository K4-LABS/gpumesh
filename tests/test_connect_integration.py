"""Integration test reproducing the coordinator<->worker connect path.

This starts a REAL coordinator bound to 127.0.0.1 on an auto-assigned port
(port 0) and drives a REAL worker through ``run_worker()``. It proves the
same-machine happy path works end-to-end over loopback, which isolates the
production ``WinError 10061`` to environmental causes (Windows Firewall,
wrong IP, or stale saved config) rather than a code defect.

No real network, no firewall, and no LAN interface is touched: everything
stays on 127.0.0.1.
"""

import os
import shutil
import threading
import time

import pytest
import urllib.error

from gpumesh import connection_manager
from gpumesh.server import serve
from gpumesh.worker import MeshClient, run_worker

TOKEN = "integration-test-token"


@pytest.fixture
def coordinator(tmp_path):
    """Start a real loopback coordinator; yield (url, token, httpd).

    Teardown stops the server cleanly and restores the user's saved
    connection config so the test never pollutes ~/.gpumesh/config.json.
    """
    db_path = str(tmp_path / "connect_integration.db")
    httpd = serve("127.0.0.1", 0, db_path, TOKEN)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}"

    # run_worker() writes ~/.gpumesh/config.json; preserve any real config.
    cfg = connection_manager._CONFIG_PATH
    backup = None
    if os.path.exists(cfg):
        backup = cfg + ".bak"
        shutil.copy(cfg, backup)

    # Non-daemon thread so a missing shutdown would surface as a hard hang
    # rather than silently leaking.
    thread = threading.Thread(target=httpd.serve_forever, daemon=False)
    thread.start()
    # Give serve_forever a moment to enter its accept loop before we yield.
    # (We deliberately avoid inspecting private CPython internals such as
    # _BaseServer__serving, which is fragile across versions.)
    time.sleep(0.2)

    yield url, TOKEN, httpd

    # Teardown: same sequence the CLI uses on Ctrl+C / SIGTERM.
    httpd.gpumesh_stop.set()
    httpd.shutdown()
    thread.join(timeout=10)

    # Restore / clean the saved-connection config.
    connection_manager.clear_connection()
    if backup and os.path.exists(backup):
        shutil.move(backup, cfg)


def _wait_for_registration(client, timeout=15.0):
    """Poll /api/workers until at least one worker appears."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = client.call("GET", "/api/workers")
            if resp.get("workers"):
                return resp["workers"]
        except Exception:
            pass
        time.sleep(0.1)
    return []


def test_worker_registers_over_loopback(coordinator):
    """a) A real worker registers successfully against a loopback coordinator."""
    url, token, _ = coordinator
    client = MeshClient(url, token)

    # run_worker loops forever; run it in a daemon thread and verify it
    # registered, then let it be reaped at process exit.
    worker_thread = threading.Thread(
        target=run_worker, args=(url, token), daemon=True
    )
    worker_thread.start()

    workers = _wait_for_registration(client)
    assert workers, "worker never registered with the loopback coordinator"
    assert workers[0]["alive"] is True
    worker_id = workers[0]["id"]

    # Heartbeat must succeed for the registered worker.
    hb = client.call("POST", "/api/heartbeat", {"worker_id": worker_id})
    assert hb == {"ok": True}

    # Lease must respond cleanly (204 -> None) when no jobs are queued.
    lease = client.call("POST", "/api/lease", {"worker_id": worker_id})
    assert lease is None


def test_wrong_token_rejected_401(coordinator):
    """b) Auth works: a wrong token yields HTTP 401, not a connection."""
    url, _token, _ = coordinator
    bad = MeshClient(url, "definitely-the-wrong-token")
    with pytest.raises(urllib.error.HTTPError) as exc:
        bad.call("GET", "/api/workers")
    assert exc.value.code == 401


def test_unreachable_coordinator_fails_cleanly(coordinator):
    """c) A wrong/unreachable URL fails cleanly (URLError, no crash/hang)."""
    url, token, _ = coordinator  # coordinator is unused here on purpose

    # (1) A direct client call to a dead port raises URLError (the 10061 class)
    #     quickly, instead of hanging or crashing.
    dead_url = "http://127.0.0.1:1"
    rogue = MeshClient(dead_url, token)
    with pytest.raises(urllib.error.URLError):
        rogue.call("GET", "/api/workers")

    # (2) run_worker must return cleanly (no raise, no hang) when it cannot
    #     reach the coordinator, printing troubleshooting instead.
    run_worker(dead_url, token)
