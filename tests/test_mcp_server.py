"""MCP wrapper tests: schemas, clean failures, and parity with the CLI path.

Deliberately short. ``mcp_server`` wraps ``api.GPUMesh``, and the scheduler,
the sandbox and the serializer are all covered where they live. What is worth
asserting here is only what the wrapper itself adds: the tool schemas an
assistant reads, a transport failure arriving as a sentence rather than a
traceback, and a submit through MCP producing the same job a submit through
the CLI does.
"""

import asyncio
import contextlib
import io
import json
import threading
import time

import pytest

from gpumesh.server import serve
from gpumesh.worker import MeshClient

pytest.importorskip("mcp", reason="MCP support is the optional [mcp] extra")

# Imported after the guard above on purpose: the module imports the MCP SDK
# lazily, but a machine without the extra should skip this file rather than
# collect it.
from gpumesh import mcp_server

TOKEN = "mcp-test-token"

SCRIPT = """
import json, sys
payload = json.load(sys.stdin)
print(json.dumps({"square": payload["n"] ** 2}))
"""

PAYLOADS = [{"n": 2}, {"n": 3}]


@pytest.fixture
def coordinator(tmp_path):
    """A real coordinator on a random port, with no workers attached.

    No worker on purpose: every test here is about the client half, and a
    mesh that never completes a task is also the mesh that proves the tools
    report progress instead of hanging.
    """
    httpd = serve("127.0.0.1", 0, str(tmp_path / "mcp.db"), TOKEN)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    httpd.gpumesh_stop.set()
    httpd.shutdown()


def tools(url=None, token=None):
    """{name: Tool} as an MCP client would see it."""
    server = mcp_server.build_server(url, token)
    return {tool.name: tool for tool in asyncio.run(server.list_tools())}


def call(url, token, tool, **arguments):
    """Invoke one tool the way a client does, returning the parsed payload.

    ``call_tool`` returns (content, structured) in some FastMCP releases, the
    content list alone in others, and a ``CallToolResult`` in SDK 2.x.
    Normalising here keeps the assertions about gpumesh rather than about the
    SDK's return shape. One block is expected because every tool returns a
    dict. The note above ``workers`` in mcp_server.py says why that is
    deliberate.
    """
    server = mcp_server.build_server(url, token)
    outcome = asyncio.run(server.call_tool(tool, arguments))
    content = getattr(outcome, "content", None)
    if content is None:
        content = outcome[0] if isinstance(outcome, tuple) else outcome
    assert len(content) == 1, f"{tool} returned {len(content)} content blocks"
    return json.loads(content[0].text)


# -- schemas ---------------------------------------------------------------

# name -> (required arguments, all arguments)
EXPECTED_TOOLS = {
    "workers": ([], []),
    "mesh_status": ([], []),
    "submit_job": (["script", "payloads"], ["name", "payloads", "script"]),
    "job_status": (["job_id"], ["job_id"]),
    "job_result": (["job_id"], ["job_id"]),
    "cancel_job": (["job_id"], ["job_id"]),
    "radar_scan": ([], ["seconds"]),
}


def test_every_tool_is_registered_with_a_valid_schema():
    registered = tools()
    assert set(registered) == set(EXPECTED_TOOLS)
    for name, (required, properties) in EXPECTED_TOOLS.items():
        # SDK 1.x spells it inputSchema, 2.x input_schema.
        tool = registered[name]
        schema = getattr(tool, "inputSchema", None) or tool.input_schema
        assert schema["type"] == "object"
        assert sorted(schema.get("required", [])) == sorted(required), name
        assert sorted(schema.get("properties", {})) == sorted(properties), name
        # An assistant picks a tool by its description; an empty one makes the
        # tool unusable without the model guessing from the name alone.
        assert registered[name].description


def test_submit_job_documents_the_script_contract():
    """The stdin/stdout contract only exists in this docstring.

    A worker runs the script in a bare subprocess, so an assistant that does
    not know to read stdin and print JSON writes a script that fails on every
    task. That makes the wording load-bearing rather than decorative.
    """
    description = tools()["submit_job"].description
    assert "stdin" in description
    assert "stdout" in description


# -- failure modes ---------------------------------------------------------

def test_no_saved_connection_is_an_instruction_not_a_traceback():
    # conftest.py already points the config at an empty tmp dir.
    with pytest.raises(RuntimeError) as exc:
        mcp_server._resolve(None, None)
    message = str(exc.value)
    assert "gpumesh serve" in message and "gpumesh join" in message


def test_auth_failure_names_the_token_not_the_stack(coordinator):
    with pytest.raises(Exception) as exc:
        call(coordinator, "wrong-token", "workers")
    message = str(exc.value)
    assert "Authentication failed" in message
    assert "token" in message
    # The urllib chain is what an assistant would otherwise read past.
    assert "Traceback" not in message
    assert "urllib" not in message


def test_unreachable_coordinator_degrades_to_a_sentence():
    # Port 1 is reserved and nothing listens on it, so this is refused fast on
    # every platform rather than waiting out a connect timeout.
    with pytest.raises(Exception) as exc:
        call("http://127.0.0.1:1", TOKEN, "workers")
    message = str(exc.value)
    assert "unreachable" in message
    assert "Traceback" not in message


def test_a_bad_submission_relays_the_coordinators_own_refusal(coordinator):
    with pytest.raises(Exception) as exc:
        call(coordinator, TOKEN, "submit_job", script=SCRIPT, payloads=[])
    # The coordinator's wording, not a second one invented in the wrapper.
    assert "non-empty payloads" in str(exc.value)


def test_missing_job_is_reported_as_missing(coordinator):
    with pytest.raises(Exception) as exc:
        call(coordinator, TOKEN, "job_status", job_id="nope")
    # The coordinator's own 404 body, relayed rather than reworded.
    assert "no such job" in str(exc.value)
    assert "Traceback" not in str(exc.value)


# -- parity with the CLI ---------------------------------------------------

def test_mcp_submit_produces_the_same_job_as_a_cli_submit(coordinator, tmp_path):
    """One job through each path; the coordinator must not be able to tell.

    This is the module's whole safety claim in one test. The MCP tool is a
    wrapper, so a job it creates has to be indistinguishable from one the CLI
    creates apart from its id.
    """
    from gpumesh import client as cli_client

    script_path = tmp_path / "task.py"
    script_path.write_text(SCRIPT, encoding="utf-8")
    payloads_path = tmp_path / "payloads.json"
    payloads_path.write_text(json.dumps(PAYLOADS), encoding="utf-8")

    cli_job_id = cli_client.submit_job(
        coordinator, TOKEN, str(script_path), str(payloads_path), name="parity"
    )
    mcp_job_id = call(
        coordinator, TOKEN, "submit_job",
        script=SCRIPT, payloads=PAYLOADS, name="parity",
    )["job_id"]
    assert mcp_job_id != cli_job_id

    mesh = MeshClient(coordinator, TOKEN)
    cli_job = mesh.call("GET", f"/api/jobs/{cli_job_id}")
    mcp_job = mesh.call("GET", f"/api/jobs/{mcp_job_id}")

    assert mcp_job["name"] == cli_job["name"] == "parity"
    assert mcp_job["counts"] == cli_job["counts"] == {"pending": len(PAYLOADS)}
    assert [t["cost"] for t in mcp_job["tasks"]] == [t["cost"] for t in cli_job["tasks"]]

    # Same script text and same payloads on the wire, task for task.
    worker_id = mesh.call("POST", "/api/register",
                          {"hostname": "t", "device": "cpu", "score": 1.0})["worker_id"]
    by_job = {}
    for _ in range(2 * len(PAYLOADS)):
        task = mesh.call("POST", "/api/lease", {"worker_id": worker_id})
        by_job.setdefault(task["job_id"], []).append(task)
    assert {t["script"] for t in by_job[mcp_job_id]} == {SCRIPT}
    assert ([t["payload"] for t in by_job[mcp_job_id]]
            == [t["payload"] for t in by_job[cli_job_id]])


def test_job_status_and_result_track_a_real_job(coordinator):
    """Submit, run the tasks by hand, and read both tools back."""
    from gpumesh import sandbox

    job_id = call(coordinator, TOKEN, "submit_job",
                  script=SCRIPT, payloads=PAYLOADS, name="squares")["job_id"]

    status = call(coordinator, TOKEN, "job_status", job_id=job_id)
    assert status["finished"] is False
    assert status["counts"] == {"pending": 2}
    # Progress must stay cheap: results belong to job_result, not to a poll.
    assert "tasks" not in status

    mesh = MeshClient(coordinator, TOKEN)
    worker_id = mesh.call("POST", "/api/register",
                          {"hostname": "t", "device": "cpu", "score": 1.0})["worker_id"]
    while True:
        task = mesh.call("POST", "/api/lease", {"worker_id": worker_id})
        if task is None:
            break
        mesh.call("POST", "/api/result", {
            "task_id": task["task_id"], "worker_id": worker_id, "ok": True,
            "result": sandbox.run_task(task["script"], task["payload"], timeout=30),
        })

    result = call(coordinator, TOKEN, "job_result", job_id=job_id)
    assert result["finished"] is True
    # Decoded back into real JSON, in submission order. Not an envelope, and
    # not a string of JSON.
    assert [t["result"]["square"] for t in result["tasks"]] == [4, 9]


def test_cancel_job_reports_what_it_cancelled(coordinator):
    job_id = call(coordinator, TOKEN, "submit_job",
                  script=SCRIPT, payloads=PAYLOADS)["job_id"]
    assert call(coordinator, TOKEN, "cancel_job", job_id=job_id) == {
        "pending": 2, "running": 0,
    }
    assert call(coordinator, TOKEN, "job_status",
                job_id=job_id)["counts"] == {"failed": 2}


def test_cancelling_an_unknown_job_says_so(coordinator):
    with pytest.raises(Exception) as exc:
        call(coordinator, TOKEN, "cancel_job", job_id="nope")
    assert "No job nope" in str(exc.value)


# -- stdout is the wire ----------------------------------------------------

def test_no_tool_writes_to_stdout(coordinator, monkeypatch):
    """stdout IS the MCP transport. Anything printed there corrupts a frame.

    The regression this pins down: ``connection_manager`` prints a NOTE
    whenever a saved connection is more than an hour old, which covers almost
    every real call, and ``safe_print`` resolves ``sys.stdout`` at call time.
    That line landed inside a JSON-RPC message and broke the session. The
    check asks for nothing at all from any tool, rather than for the absence
    of that one warning, because the next print somebody adds to the
    connection or client code will not arrive labelled as a wire-format bug.
    """
    from gpumesh import connection_manager

    connection_manager.save_connection(coordinator, TOKEN)
    aged = json.loads(open(connection_manager._CONFIG_PATH).read())
    aged["saved_at"] = time.time() - 10 * connection_manager._STALE_WARN_SECONDS
    with open(connection_manager._CONFIG_PATH, "w") as handle:
        json.dump(aged, handle)

    job_id = call(None, None, "submit_job", script=SCRIPT, payloads=PAYLOADS)["job_id"]

    for tool, arguments in [
        ("workers", {}),
        ("mesh_status", {}),
        ("submit_job", {"script": SCRIPT, "payloads": PAYLOADS}),
        ("job_status", {"job_id": job_id}),
        ("job_result", {"job_id": job_id}),
        ("cancel_job", {"job_id": job_id}),
        ("radar_scan", {"seconds": 0.5}),
    ]:
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            call(None, None, tool, **arguments)
        assert captured.getvalue() == "", f"{tool} wrote to stdout"


def test_a_failing_tool_does_not_write_to_stdout_either(coordinator):
    """The error path resolves the same config and must stay as quiet."""
    from gpumesh import connection_manager

    connection_manager.save_connection(coordinator, TOKEN)
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        with pytest.raises(Exception, match="no such job"):
            call(None, None, "job_status", job_id="nope")
    assert captured.getvalue() == ""


# -- connection resolution -------------------------------------------------

def test_saved_config_is_used_when_no_override_is_given(coordinator):
    """Zero-config: a mesh already joined needs nothing in the MCP config."""
    from gpumesh import connection_manager

    connection_manager.save_connection(coordinator, TOKEN)
    assert call(None, None, "workers") == {"workers": []}


def test_resolving_never_rewrites_the_saved_connection(coordinator):
    """An agent's --url must not be able to clobber the user's mesh.

    ``get_connection(persist=True)`` would save whatever was passed, so a
    single tool call against a second mesh would silently replace the
    connection the user established with join/serve.
    """
    from gpumesh import connection_manager

    connection_manager.save_connection(coordinator, TOKEN)
    mcp_server._resolve("http://192.0.2.1:9999", "other-token")
    saved = connection_manager.load_connection()
    assert saved["url"] == coordinator
    assert saved["token"] == TOKEN
