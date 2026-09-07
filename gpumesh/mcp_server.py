from __future__ import annotations

"""MCP server: let an AI coding assistant drive a gpumesh mesh.

Every tool here is three lines over the public Python API in ``api.py``.
Resolve the connection, call the method, turn a transport failure into a
sentence. There is no scheduling, no retry policy and no result handling that
does not already exist elsewhere in the package, and there should never be.
An MCP tool that grows its own business logic is a second implementation of
the mesh that only agents can reach.

**Authority.** The agent gets the token the user already has, read from the
same places every other gpumesh command reads it: an explicit argument, then
``GPUMESH_URL``/``GPUMESH_TOKEN``, then ``~/.gpumesh/config.json``, all
through ``connection_manager.get_connection``. Nothing here writes that file
back. A tool call is a read, and ``persist=False`` is what stops an agent
overwriting the user's saved coordinator. No tool can reach a mesh the user
could not reach from their own shell.

Requires the ``mcp`` package, an optional extra::

    pip install "gpumesh[mcp]"
    gpumesh mcp-serve

See docs/mcp.md for the Claude Code and Cursor config snippets.
"""

import contextlib
import json
import sys
import time
import urllib.error

from . import client as _client
from . import connection_manager
from .api import GPUMesh, GPUMeshError

# An agent picks tool arguments from a docstring, so a scan window it can set
# freely is a tool call that blocks a conversation for as long as it likes.
MIN_SCAN_SECONDS = 0.5
MAX_SCAN_SECONDS = 30.0

_NO_CONNECTION = (
    "No gpumesh coordinator is configured on this machine. Start one with "
    "'gpumesh serve --token <TOKEN>', join an existing mesh with "
    "'gpumesh join <URL> --token <TOKEN>', or set the GPUMESH_URL and "
    "GPUMESH_TOKEN environment variables. gpumesh reads the same "
    "~/.gpumesh/config.json every other gpumesh command reads."
)


def _resolve(url: str | None, token: str | None) -> tuple:
    """Resolve (url, token) the way every other gpumesh entry point does.

    ``persist=False`` is the important half. Resolving is a read, and an
    agent calling a tool must not be able to rewrite the connection the user
    established with ``join`` or ``serve``.
    """
    resolved_url, resolved_token = connection_manager.get_connection(
        url or None, token or None, persist=False
    )
    if not resolved_url or not resolved_token:
        raise RuntimeError(_NO_CONNECTION)
    return resolved_url, resolved_token


def _explain(exc: Exception, url: str) -> str:
    """Turn a transport failure into a sentence an agent can act on.

    The coordinator already writes good refusals, so this relays an HTTPError
    body rather than replacing it. "Need script and non-empty payloads list"
    and the protocol-mismatch block both say more than anything invented here
    could. A second message would only let the mesh and the agent disagree
    about why a call failed.
    """
    if isinstance(exc, urllib.error.HTTPError):
        detail = ""
        try:
            body = json.loads(exc.read().decode("utf-8", errors="replace"))
            detail = body.get("error", "") if isinstance(body, dict) else ""
        except Exception:  # noqa: S110  - the detail is a bonus, never the message
            # Reading the body can fail for reasons that have nothing to do
            # with the failure being reported: a body already consumed, an
            # HTML error page from a proxy in the way, a truncated response.
            # None of them are worth replacing the status code the caller
            # actually needs, so the sentence below still gets sent with
            # whatever detail did survive.
            pass
        if exc.code == 401:
            return (
                f"Authentication failed at {url}: the coordinator rejected "
                f"this token. It is wrong, or the coordinator was restarted "
                f"with a different one. Check 'gpumesh show-connection'."
            )
        return (
            f"The coordinator at {url} refused the request (HTTP {exc.code})"
            + (f": {detail}" if detail else ".")
        )
    reason = getattr(exc, "reason", exc)
    return (
        f"The coordinator at {url} is unreachable: {reason}. It may not be "
        f"running, or this machine cannot route to that address."
    )


@contextlib.contextmanager
def _session(url: str | None, token: str | None):
    """Resolve the connection, keep stdout clean, and report failures as prose.

    All three live in one context manager because all three have to hold for
    the whole of every tool body. A tool that opens two of them is a tool that
    will one day open one.

    **The stdout redirect protects the transport.** An MCP stdio server's
    stdout is the JSON-RPC channel, and gpumesh's own ``safe_print`` resolves
    ``sys.stdout`` at call time. So the entirely reasonable NOTE that
    ``connection_manager`` prints when a saved connection is over an hour old,
    which covers almost every real call, lands in the middle of a protocol
    frame and breaks the session. This guards every path a tool can reach
    rather than that one known caller. The next warning somebody adds to the
    connection or client code will not arrive labelled as a wire-format bug.

    Redirecting is safe because ``mcp.server.stdio`` wraps
    ``sys.stdout.buffer`` once at startup and writes to that object forever
    after, so rebinding the ``sys.stdout`` name cannot reach it. Stderr is
    where these lines belong anyway, since an MCP client shows it as server
    logs.

    ``from None`` on the raise drops the chained urllib traceback. It tells an
    agent nothing it can act on and costs it a screen of context to read.
    HTTPError subclasses URLError, so both arrive here.
    """
    with contextlib.redirect_stdout(sys.stderr):
        mesh_url, mesh_token = _resolve(url, token)
        try:
            yield mesh_url, mesh_token
        except (urllib.error.URLError, OSError, GPUMeshError) as exc:
            # ``GPUMesh`` catches URLError itself and re-raises GPUMeshError
            # ``from`` it, and HTTPError is a URLError. So a 401 arriving here
            # through a GPUMesh method wears a GPUMeshError whose text says
            # "check that the coordinator is running", which is exactly the
            # wrong advice for a rejected token. The original sits on
            # __cause__. Prefer it, so every tool reports the status code it
            # actually got instead of only the ones that bypass api.py.
            cause = exc.__cause__
            if isinstance(cause, urllib.error.HTTPError):
                exc = cause
            raise RuntimeError(_explain(exc, mesh_url)) from None


def _decoded(raw):
    """Unwrap one task result into something an agent can read.

    ``client._format_result`` already owns this. It strips the ordering key,
    unwraps the serializer envelope, honours strict mode, and falls back to
    ``repr`` for values JSON cannot hold, such as a tensor or an array. It
    hands back a string. Parsing that string once more turns an ordinary
    result back into real JSON structure for the agent, while a tensor's repr
    stays a string.
    """
    if raw is None:
        return None
    text = _client._format_result(raw)
    try:
        return json.loads(text)
    except ValueError:
        return text


def build_server(url: str | None = None, token: str | None = None):
    """Build the MCP server. ``url``/``token`` override the saved config."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError:
        # SDK 2.x renamed FastMCP to MCPServer and moved it up a level. The
        # constructor, the ``@server.tool()`` decorator and ``run()`` are
        # unchanged, so nothing below this line cares which one it got.
        from mcp.server import MCPServer as FastMCP

    server = FastMCP("gpumesh")

    # _session resolves per call rather than once at startup. The user may
    # start the coordinator after the agent has already connected, and a
    # server that cached "no mesh" at boot would stay wrong until restarted.

    # Every tool returns a dict rather than a bare list on purpose. FastMCP
    # serialises a returned list into one content block PER ELEMENT, so a
    # ten-worker mesh arrives as ten unlabelled blocks and an empty mesh as no
    # blocks at all, which without reading the schema looks the same as a tool
    # that returned nothing. One dict is one block with a name on it.
    @server.tool()
    def workers() -> dict:
        """List the machines currently in the mesh.

        Each entry has the hostname, the device kind (cuda/mps/cpu), the
        device name, and a benchmark score, where higher is faster. The
        scheduler gives heavier tasks to higher scores. An empty list means no
        worker is alive. The coordinator is up but nothing has joined it.
        """
        with _session(url, token) as (mesh_url, mesh_token):
            return {"workers": GPUMesh(mesh_url, mesh_token).workers()}

    @server.tool()
    def mesh_status() -> dict:
        """Overall mesh health: gpumesh version, uptime, live and dead worker
        counts, total compute score, and pending/running/done/failed job
        counts across the whole coordinator.
        """
        from .worker import MeshClient

        with _session(url, token) as (mesh_url, mesh_token):
            return MeshClient(mesh_url, mesh_token).call("GET", "/api/health")

    @server.tool()
    def submit_job(script: str, payloads: list, name: str = "") -> dict:
        """Run a Python script across the mesh, once per payload, and return
        the job id immediately. This does not wait. Poll with job_status, then
        read job_result.

        ``script`` is a standalone Python program. Each task runs it in a
        fresh subprocess on some worker. The task's payload arrives as JSON on
        stdin, and the script must print its result as JSON on the last line
        of stdout. Nothing from the submitting machine reaches it beyond what
        the payload carries.

            import json, sys
            p = json.load(sys.stdin)
            print(json.dumps({"lr": p["lr"], "score": p["lr"] * 2}))

        ``payloads`` is one dict per task, the parameter sets of a sweep. The
        scheduler reads four of its keys itself instead of passing them to the
        script: ``cost`` weights the task and defaults to 1.0, ``gpu`` names a
        device such as "cuda" or "A100", and ``cpu_cores`` and
        ``gpu_memory_mb`` set the least capacity a worker must report to be
        handed this task.
        """
        with _session(url, token) as (mesh_url, mesh_token):
            job_id = GPUMesh(mesh_url, mesh_token).submit_job(
                script=script, payloads=payloads, name=name
            )
            return {"job_id": job_id}

    @server.tool()
    def job_status(job_id: str) -> dict:
        """Progress of one job: whether it has finished, and how many of its
        tasks are pending, running, done and failed.

        This leaves results out on purpose. A large sweep's results dwarf its
        progress, and a poll loop should not pay for them. Read job_result
        once ``finished`` is true.
        """
        # An unknown job is a 404 from the coordinator, which _session relays
        # verbatim ("no such job"). No None check here, because job_status
        # returns the parsed body or raises, never None.
        with _session(url, token) as (mesh_url, mesh_token):
            job = GPUMesh(mesh_url, mesh_token).job_status(job_id)
            return {
                "job_id": job["id"],
                "name": job["name"],
                "finished": job["finished"],
                "counts": job["counts"],
            }

    @server.tool()
    def job_result(job_id: str) -> dict:
        """Results of a job, one entry per task in submission order.

        Safe to call before the job has finished. Unfinished tasks come back
        with their current status and a null result, and a task that failed
        carries its error message instead.
        """
        with _session(url, token) as (mesh_url, mesh_token):
            job = GPUMesh(mesh_url, mesh_token).job_status(job_id)
            return {
                "job_id": job["id"],
                "finished": job["finished"],
                "counts": job["counts"],
                "tasks": [
                    {
                        "task_id": task["id"],
                        "status": task["status"],
                        "result": _decoded(task["result"]),
                        "error": task["error"],
                    }
                    for task in job["tasks"]
                ],
            }

    @server.tool()
    def cancel_job(job_id: str) -> dict:
        """Cancel a job. Every pending and running task of it fails.

        Returns how many tasks were cancelled in each state. Tasks that had
        already finished keep their results.
        """
        with _session(url, token) as (mesh_url, mesh_token):
            result = _client.cancel_job(mesh_url, mesh_token, job_id)
            if result is None:
                raise RuntimeError(
                    f"No job {job_id} on the coordinator at {mesh_url}."
                )
            return result

    @server.tool()
    def radar_scan(seconds: float = 3.0) -> dict:
        """Listen for nearby gpumesh machines broadcasting on the local
        network, and report what answered within ``seconds``.

        This is LAN discovery rather than the mesh roster. It finds machines
        that could join, including ones that have not, and needs no token and
        no coordinator. Nothing answers across subnets or over a VPN. Use
        'gpumesh join <URL> --token <TOKEN>' there.
        """
        from .discovery import Listener

        window = max(MIN_SCAN_SECONDS, min(float(seconds), MAX_SCAN_SECONDS))
        listener = Listener()
        try:
            listener.start()
        except OSError as exc:
            raise RuntimeError(
                f"Could not open the discovery listener: {exc}. Another "
                f"gpumesh radar or coordinator may already hold that port."
            ) from None
        try:
            time.sleep(window)
            peers = listener.peers()
        finally:
            listener.stop()
        return {"peers": [
            {
                "hostname": peer.hostname,
                "role": peer.role,
                "ip": peer.ip,
                "device": peer.device,
                "device_name": peer.display_name,
                "score": peer.score,
                "api_port": peer.api_port,
                "alive": peer.alive,
            }
            for peer in peers
        ]}

    return server
