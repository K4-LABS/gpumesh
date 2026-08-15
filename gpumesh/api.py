"""GPUMesh Python API for Jupyter/Colab notebooks.

Usage:
    from gpumesh import GPUMesh

    # Connect to a coordinator
    mesh = GPUMesh("http://coordinator:8000", token="mysecret")

    # List connected workers
    mesh.workers()

    # Distribute a function across all workers
    results = mesh.distribute(
        function=train_model,
        params=[
            {"lr": 0.01, "epochs": 100},
            {"lr": 0.05, "epochs": 200},
        ]
    )

    # Convert to DataFrame (optional)
    df = mesh.results_to_dataframe(results)
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from . import capability, sandbox, serializer, utils
from gpumesh.ansi import safe_print, green, yellow, red, cyan, bold
from .accelerate import POLL_BACKOFF, POLL_MAX_INTERVAL, POLL_MIN_INTERVAL
from .db import _unusable_hints
from .worker import MeshClient


class GPUMeshError(Exception):
    """Raised when a GPUMesh operation fails."""
    pass


def _is_loopback_host(host: str) -> bool:
    """Is this bind address reachable only from this machine?

    ``0.0.0.0`` and ``::`` are the wildcards meaning "every interface", so
    they are emphatically not loopback even though they include it. Anything
    in 127.0.0.0/8, ``::1``, or the name ``localhost`` is.

    A hostname we cannot parse is reported as exposed. The asymmetry is
    deliberate: warning about a bind that turns out to be private costs a few
    lines of output, while staying quiet about one that turns out to be public
    costs the user a remote-code-execution surface they never agreed to.
    """
    import ipaddress

    h = (host or "").strip().lower()
    if h in ("localhost", "localhost.localdomain"):
        return True
    # Accept the bracketed IPv6 literal form, and drop any zone id
    # ("fe80::1%eth0") that ip_address() would choke on.
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    h = h.split("%", 1)[0]
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


class _CoordinatorHandle(str):
    """The token, carrying the handles needed to stop what returned it.

    ``start_coordinator`` is documented to return the token, and every caller
    in the wild writes ``token = GPUMesh.start_coordinator(...)`` and then
    compares it, prints it, or hands it to a worker. So the return value has to
    go on behaving in every way like the string it has always been — which is
    what subclassing ``str`` buys, and why this is not a small named tuple or a
    new "Coordinator" object. ``token == "mysecret"``, ``json.dumps(token)``,
    ``f"{token}"`` and ``isinstance(token, str)`` all keep their old answers.

    What it adds is the thing that was missing entirely: a way to shut the
    coordinator down. ``serve_forever`` and the reaper were started in threads
    nobody kept a reference to, so from a notebook the only way to stop a
    coordinator was to restart the kernel, and in the test suite it meant a
    listening socket and a reaper thread outliving the test that created them
    — which is not free, because the leaked threads keep waking up and add
    scheduler jitter to everything that runs afterwards.

    The attribute names are deliberately not new vocabulary. ``server.serve``
    already publishes ``httpd.gpumesh_stop`` (the reaper's stop event) and
    ``httpd.gpumesh_reaper``; ``worker.spawn_local_worker`` already publishes
    ``thread.gpumesh_stop``. Same prefix, same meanings, and the pieces are
    exposed individually as well as behind one call so that code which already
    knows the ``httpd.gpumesh_stop.set(); httpd.shutdown()`` sequence — every
    fixture in tests/ does — can keep using it unchanged.
    """

    # Set by start_coordinator immediately after construction. Declared here
    # so the attributes are documented in one place rather than discovered by
    # reading the factory.
    gpumesh_httpd = None          # the ThreadingHTTPServer that is listening
    gpumesh_stop = None           # the reaper's stop event (httpd.gpumesh_stop)
    gpumesh_serve_thread = None   # the thread running serve_forever
    gpumesh_worker = None         # the self-worker thread, or None

    def gpumesh_shutdown(self, timeout: float = 15.0) -> None:
        """Stop the coordinator, the reaper, and the self-worker.

        The same order ``cli._stop_coordinator`` uses, for the same reasons:
        the worker is told to stop first so it is not still polling a socket
        that is about to close, then the reaper, then the listener, and each
        thread is joined rather than left to daemon teardown — a daemon killed
        mid-``print`` at interpreter exit can leave stdout's lock held and hang
        the process.

        It is not a call *into* ``cli._stop_coordinator``, though, and the
        difference matters: that one narrates to the console and calls
        ``os._exit(1)`` on a second Ctrl+C, which is right for a foreground
        ``gpumesh serve`` and catastrophic in a Jupyter kernel, where it would
        take the user's session and their unsaved state with it.

        Safe to call more than once, and safe to call on a coordinator that
        never finished starting: everything here is either idempotent or
        wrapped. ``httpd.shutdown()`` blocks forever if ``serve_forever`` was
        never entered, but ``start_coordinator`` always starts that thread
        before it builds this object, and ``serve_forever`` sets its
        shut-down event from a ``finally``, so the wait ends even if it exits
        by raising.
        """
        worker = self.gpumesh_worker
        if worker is not None:
            worker_stop = getattr(worker, "gpumesh_stop", None)
            if worker_stop is not None:
                worker_stop.set()

        if self.gpumesh_stop is not None:
            self.gpumesh_stop.set()

        httpd = self.gpumesh_httpd
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except OSError as exc:
                # Windows raises 10038 ("not a socket") when the listener has
                # already been closed by a previous call. Anything else is
                # worth knowing about, but not worth losing the joins below.
                if getattr(exc, "winerror", None) != 10038:
                    safe_print(yellow(f"[mesh] closing the coordinator socket "
                                      f"raised {type(exc).__name__}: {exc}"))
            except Exception:
                pass

        if self.gpumesh_serve_thread is not None:
            self.gpumesh_serve_thread.join(timeout=timeout)
        if worker is not None:
            # The worker's own graceful-shutdown budget, matching
            # cli._SELF_WORKER_JOIN_TIMEOUT: a shorter join is guaranteed to
            # time out whenever a task is in flight, which would leak exactly
            # the thread this method exists to stop.
            from .worker import GRACEFUL_SHUTDOWN_TIMEOUT

            worker.join(timeout=GRACEFUL_SHUTDOWN_TIMEOUT + 10.0)


class GPUMesh:
    """Python API for distributing compute across a GPU mesh.
    
    Args:
        url: Coordinator URL (e.g., "http://192.168.1.10:8000")
        token: Authentication token
        
    Example:
        >>> mesh = GPUMesh("http://192.168.1.10:8000", token="mysecret")
        >>> mesh.workers()
        [{'id': 'w1', 'device': 'cuda', 'name': 'RTX 3080', 'score': 85.0}]
    """
    
    def __init__(self, url: str, token: str, safe_mode: bool = False):
        self.url = url.rstrip("/")
        self.token = token
        self.safe_mode = safe_mode
        self._client = MeshClient(self.url, self.token)
    
    # ── Static methods for setup ──────────────────────────────────────
    
    @staticmethod
    def start_coordinator(
        port: int = 8000,
        token: str | None = None,
        db_path: str = "gpumesh.db",
        tailscale: bool = False,
        safe_mode: bool = False,
        self_worker: bool = True,
        host: str = "127.0.0.1",
    ) -> str:
        """Start a coordinator server (non-blocking).

        Args:
            port: Port to listen on
            token: Auth token (generated if None)
            db_path: SQLite database path
            tailscale: Use Tailscale for network access
            host: Bind address. Defaults to loopback, so only this machine can
                connect. Pass "0.0.0.0" to let other machines join — see the
                warning that prints when you do.

        Returns:
            The token used. It is a string in every way that has ever
            mattered — compare it, print it, hand it to a worker — but it also
            carries the handles that stop what this call started:
            ``token.gpumesh_shutdown()`` stops the listener, the reaper and the
            self-worker, and ``token.gpumesh_httpd`` / ``token.gpumesh_stop``
            are the same two objects ``server.serve`` publishes, for callers
            that already drive that pair directly. See ``_CoordinatorHandle``.

        Example:
            >>> token = GPUMesh.start_coordinator(port=8000, token="mysecret")
            Coordinator running. Share this: http://127.0.0.1:8000
            >>> token.gpumesh_shutdown()
        """
        import secrets as _secrets
        from . import server, tunnel

        if token is None:
            token = _secrets.token_urlsafe(12)

        # Bind to loopback unless the caller deliberately opens it up.
        #
        # A worker executes arbitrary Python as this machine's OS user, so the
        # coordinator's port is a remote-code-execution surface whose only
        # guard is the token. Hard-coding "0.0.0.0" handed that surface to
        # every machine on the LAN — hotel and conference wifi included — for
        # anyone who started a coordinator without thinking about the network.
        # Exposure is now something you ask for, and are told about.
        #
        # The keyword call is deliberate: serve()'s parameter order has moved
        # before, and a positional call that silently binds the wrong argument
        # would be a security bug rather than a crash.
        httpd = server.serve(
            host=host, port=port, db_path=db_path, token=token,
            safe_mode=safe_mode,
        )

        # port=0 asks the OS to pick, so read back what actually got bound —
        # otherwise every line below advertises a port nobody can connect to.
        bound_port = httpd.server_address[1]

        loopback = _is_loopback_host(host)
        ip = utils.get_lan_ip()
        # A loopback coordinator is unreachable at the LAN IP, so the join
        # snippet has to name the address that will actually work.
        connect_host = "127.0.0.1" if loopback else ip

        safe_print(green(f"[mesh] coordinator listening on {host}:{bound_port}"))
        safe_print(green(f"[mesh] token: {token}"))

        if loopback:
            safe_print(yellow(f"[mesh] bound to loopback — only this machine can join."))
            safe_print(yellow(f"[mesh] http://{ip}:{bound_port} will NOT work from "
                              f"another machine while the bind address is {host}."))
            safe_print(yellow(f"[mesh] to open the mesh to your LAN, restart with:"))
            safe_print(yellow(f"       GPUMesh.start_coordinator(host=\"0.0.0.0\", "
                              f"port={bound_port}, token=\"{token}\")"))
        else:
            safe_print(red("=" * 64))
            safe_print(red(bold("[mesh] WARNING: this coordinator is exposed on the network")))
            safe_print(red(f"[mesh]   bind address : {host}"))
            safe_print(red(f"[mesh]   port         : {bound_port}"))
            safe_print(red("[mesh]"))
            safe_print(red("[mesh]   A worker executes arbitrary Python as this machine's"))
            safe_print(red("[mesh]   OS user. Any machine that can reach this port and"))
            safe_print(red("[mesh]   holds the token can run code as you — read your"))
            safe_print(red("[mesh]   files, reach your network, install what it likes."))
            safe_print(red("[mesh]"))
            safe_print(red("[mesh]   Only do this on a network you trust, and keep the"))
            safe_print(red("[mesh]   token secret. Bind host=\"127.0.0.1\" to close it."))
            safe_print(red("=" * 64))

        safe_print(green(f"[mesh] share this with workers:"))
        safe_print(green(f"       from gpumesh import GPUMesh"))
        safe_print(green(f"       GPUMesh.add_worker(\"http://{connect_host}:{bound_port}\", token=\"{token}\")"))

        # Handle tunnel mode
        if tailscale:
            tunnel.open_tunnel(port, mode="tailscale")
        
        # Run in background thread
        def _run():
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                pass
        
        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

        # This machine joins its own pool so its own CPU/GPU is used too.
        worker_thread = None
        if self_worker:
            try:
                from .worker import spawn_local_worker
                # The thread is kept now, where it used to be dropped on the
                # floor. spawn_local_worker has always returned it with a
                # gpumesh_stop event attached; nothing held it, so nothing
                # could ever set it.
                worker_thread = spawn_local_worker(
                    f"http://{connect_host}:{bound_port}", token,
                    safe_mode=safe_mode, persist_connection=False,
                )
                safe_print(green(f"[mesh] self-worker started — this machine's CPU/GPU joins the pool"))
            except Exception as exc:
                safe_print(yellow(f"[mesh] could not start self-worker: {exc}"))

        # Hand back the token *and* the means to stop this. Built here, at the
        # end, so `token` stayed an ordinary str for everything above — the
        # self-worker and the printed join snippet both get the plain string.
        handle = _CoordinatorHandle(token)
        handle.gpumesh_httpd = httpd
        # getattr, not attribute access: server.serve() sets gpumesh_stop, but
        # tests substitute a minimal stand-in for it, and a coordinator that
        # cannot be stopped is still better returned than raised over.
        handle.gpumesh_stop = getattr(httpd, "gpumesh_stop", None)
        handle.gpumesh_serve_thread = thread
        handle.gpumesh_worker = worker_thread
        return handle
    
    @staticmethod
    def add_worker(
        url: str,
        token: str,
        timeout: float = 240.0,
        safe_mode: bool = False,
    ) -> dict:
        """Join a mesh as a worker (non-blocking).
        
        Args:
            url: Coordinator URL
            token: Auth token
            timeout: Per-task timeout in seconds
            
        Returns:
            Worker info dict with device, score, etc.
            
        Example:
            >>> info = GPUMesh.add_worker("http://192.168.1.10:8000", token="mysecret")
            [worker] device=cuda (RTX 3080) score=85.234 GFLOP/s
        """
        from .worker import run_worker
        
        info = capability.full_probe()
        safe_print(green(f"[worker] device={info['device']} ({info['device_name']}) "
              f"score={info['score']} GFLOP/s"))
        
        # Run worker in background thread
        def _run():
            try:
                run_worker(url, token, task_timeout=timeout, safe_mode=safe_mode)
            except KeyboardInterrupt:
                pass
        
        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        
        return info
    
    # ── Instance methods for using the mesh ───────────────────────────
    
    def workers(self) -> list[dict]:
        """List connected workers.
        
        Returns:
            List of worker info dicts
            
        Example:
            >>> mesh.workers()
            [
                {'id': 'w1', 'device': 'cuda', 'name': 'RTX 3080', 'score': 85.0},
                {'id': 'w2', 'device': 'cuda', 'name': 'T4', 'score': 12.0},
            ]
        """
        try:
            resp = self._client.call("GET", "/api/workers")
        except (urllib.error.URLError, OSError) as exc:
            raise GPUMeshError(
                f"Failed to list workers: {exc}. "
                f"Check that the coordinator is running at {self.url}"
            ) from exc
        workers = resp.get("workers", [])
        
        return [
            {
                "id": w["id"],
                "device": w["device"],
                "device_name": w.get("device_name") or w["device"],
                "hostname": w["hostname"],
                "score": w["score"],
                "alive": True,
            }
            for w in workers
            if w.get("alive", True)
        ]
    
    def distribute(
        self,
        function: Callable,
        params: list[dict],
        name: str = "",
        timeout: float = 300.0,
        poll_interval: float | None = None,
        gpu: str | None = None,
        gpu_memory_mb: float | None = None,
        cores: int | None = None,
    ) -> list[dict]:
        """Distribute a function across all workers and collect results.

        Args:
            function: The function to execute
            params: List of parameter dicts (each becomes one task)
            name: Optional job name
            timeout: Total timeout for the job (seconds)
            poll_interval: Fixed seconds between status checks. Left unset,
                polling starts fast and backs off, so short jobs return
                promptly without long jobs hammering the coordinator.
            gpu: Restrict every task to workers matching this device kind
                ("cuda", "cpu", "mps", "gpu") or model name ("A100").
            gpu_memory_mb: Restrict every task to workers reporting at least
                this much GPU memory.
            cores: Restrict every task to workers reporting at least this many
                CPU cores. Travels in the payload as ``cpu_cores``, matching
                the name workers register their core count under.

        Returns:
            List of result dicts, one per param set
            
        Example:
            >>> def train(lr, epochs):
            ...     return {"accuracy": 0.95, "lr": lr}
            >>> results = mesh.distribute(
            ...     function=train,
            ...     params=[{"lr": 0.01}, {"lr": 0.05}]
            ... )
            [{'accuracy': 0.95, 'lr': 0.01}, {'accuracy': 0.95, 'lr': 0.05}]
        """
        # Handle empty params
        if not params:
            return []

        # Reject a placement hint whose *type* is impossible, before anything
        # is serialized and before a single byte goes over the wire.
        #
        # The coordinator already refuses these with a 400 that quotes the
        # offending value, so this is not about closing a hole — it is about
        # where the answer arrives. A caller who typed ``gpu=123`` instead of
        # ``gpu="cuda"`` gets a ValueError from the line they wrote, in the
        # same stack frame, instead of an HTTP error carrying a message about
        # "payload 0" from a machine on the other side of the room. The
        # function still has to be picklable, the coordinator still has to be
        # up, and neither is true of a typo.
        #
        # The rule is not re-stated here, it is *reused*: ``_unusable_hints``
        # is the exact predicate ``Database.create_job`` validates with. A
        # second implementation, however carefully mirrored, is a second thing
        # to keep in step, and the failure when it drifts is the worst kind —
        # either this side rejects a hint the mesh would have honoured, or it
        # waves through one the mesh will reject a round-trip later, which is
        # the situation this check exists to remove. Sharing the function makes
        # disagreement unrepresentable.
        hint_probe = {}
        if gpu:
            hint_probe["gpu"] = gpu
        if gpu_memory_mb is not None:
            hint_probe["gpu_memory_mb"] = gpu_memory_mb
        if cores is not None:
            hint_probe["cpu_cores"] = cores
        bad = _unusable_hints(hint_probe)
        if bad:
            # ``cores`` is the keyword the caller typed; ``cpu_cores`` is what
            # it is called in the payload. Echo theirs — a message naming a
            # parameter that does not appear in their code sends them looking
            # for it.
            raise ValueError(
                "distribute() got an unusable placement hint: %s. Placement "
                "hints must be a device name string (gpu='cuda', gpu='A100') "
                "and numbers (gpu_memory_mb=16384, cores=8)"
                % ("; ".join(b.replace("cpu_cores=", "cores=") for b in bad),)
            )

        # Serialize the function
        func_data = serializer.serialize_function(function)
        
        # Create tasks with serialized function + params.
        #
        # ``cost`` is a scheduler hint (it controls task weight and is kept
        # at the payload top level), NOT an argument for the function. Pass
        # the params through without it so functions with a fixed signature
        # don't fail with "unexpected keyword argument 'cost'" when users
        # annotate payloads with cost (see examples/payloads.json).
        #
        # Placement hints ride at the payload top level next to ``cost`` for
        # the same reason: the scheduler reads them, the function never sees
        # them. They are only written when set, since an absent hint is what
        # "no constraint" looks like to the scheduler.
        payloads = []
        for i, p in enumerate(params):
            payload = {
                "_func": func_data,
                "_params": {k: v for k, v in p.items() if k != "cost"},
                "_task_index": i,
                "cost": p.get("cost", 1.0),
            }
            if gpu:
                payload["gpu"] = gpu
            if gpu_memory_mb is not None:
                payload["gpu_memory_mb"] = gpu_memory_mb
            if cores is not None:
                payload["cpu_cores"] = cores
            payloads.append(payload)
        
        # Submit job
        callable_name = getattr(function, "__name__", type(function).__name__)
        try:
            resp = self._client.call("POST", "/api/jobs", {
                "name": name or f"distribute_{callable_name}",
                "script": "__gpumesh_function__",  # Marker for function-based job
                "payloads": payloads,
            })
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                raise GPUMeshError(
                    "Safe mode: function distribution disabled on this coordinator. "
                    "Submit scripts instead."
                ) from exc
            raise GPUMeshError(
                f"Failed to submit job to coordinator: {exc}. "
                f"Check that the coordinator is running at {self.url}"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise GPUMeshError(
                f"Failed to submit job to coordinator: {exc}. "
                f"Check that the coordinator is running at {self.url}"
            ) from exc
        job_id = resp["job_id"]
        
        # Poll for completion, starting fast and backing off unless the caller
        # pinned a fixed interval.
        tty = getattr(sys.stdout, "isatty", lambda: False)()
        start_time = time.time()
        fixed_interval = poll_interval is not None
        delay = poll_interval if fixed_interval else POLL_MIN_INTERVAL

        def _next_delay(current: float) -> float:
            if fixed_interval:
                return current
            return min(current * POLL_BACKOFF, POLL_MAX_INTERVAL)

        while True:
            try:
                job = self.job_status(job_id)
            except (urllib.error.URLError, OSError) as exc:
                elapsed = time.time() - start_time
                if elapsed > timeout:
                    raise TimeoutError(f"Job {job_id} timed out after {timeout}s") from exc
                time.sleep(delay)
                delay = _next_delay(delay)
                continue

            if job["finished"]:
                break

            elapsed = time.time() - start_time
            if elapsed > timeout:
                raise TimeoutError(f"Job {job_id} timed out after {timeout}s")

            # Show progress only on a real TTY so piped/redirected output
            # stays clean and machine-readable.
            if tty:
                counts = job["counts"]
                total = sum(counts.values())
                done = counts.get("done", 0) + counts.get("failed", 0)
                safe_print(f"\r[distribute] {done}/{total} tasks finished ({counts})", end="", flush=True)

            time.sleep(delay)
            delay = _next_delay(delay)

        if tty:
            safe_print()  # Newline after progress
        
        # Collect results.
        #
        # Tasks come back in database row order, which is submission order, so
        # results line up with ``params`` without re-sorting mixed success and
        # failure records. Each successful result is a serializer envelope:
        # strip the internal ordering key, then unwrap it so the caller gets
        # exactly the object the function returned.
        results = []
        for task in job["tasks"]:
            if task["status"] == "done":
                raw = task["result"]
                if raw is None:
                    results.append({})
                    continue
                if isinstance(raw, dict):
                    raw.pop("_task_index", None)
                results.append(serializer.decode_result(raw))
            elif task["status"] == "failed":
                results.append({"_error": task.get("error", "unknown")})
            else:
                results.append({"_error": "task did not complete"})

        return results
    
    def job_status(self, job_id: str) -> dict:
        """Get the status of a job.
        
        Args:
            job_id: The job ID
            
        Returns:
            Job status dict
        """
        return self._client.call("GET", f"/api/jobs/{job_id}")
    
    def submit(
        self,
        name: str = "",
        script: str = "",
        payloads: list[dict] | None = None,
    ) -> str:
        """Submit a job to the coordinator.

        Args:
            name: Job name
            script: Python script (or "__gpumesh_function__" for function tasks)
            payloads: List of payload dicts

        Returns:
            Job ID
        """
        resp = self._client.call("POST", "/api/jobs", {
            "name": name or "job",
            "script": script,
            "payloads": payloads or [],
        })
        return resp["job_id"]

    def status(self, job_id: str) -> dict:
        """Get the status of a job.

        Args:
            job_id: The job ID

        Returns:
            Job status dict with 'finished', 'counts', 'tasks' keys
        """
        return self._client.call("GET", f"/api/jobs/{job_id}")

    def submit_job(
        self,
        script: str,
        payloads: list[dict],
        name: str = "",
    ) -> str:
        """Submit a script-based job (legacy API).

        Args:
            script: Python script to execute
            payloads: List of payload dicts
            name: Optional job name

        Returns:
            Job ID
        """
        resp = self._client.call("POST", "/api/jobs", {
            "name": name or "script_job",
            "script": script,
            "payloads": payloads,
        })
        return resp["job_id"]
    
    @staticmethod
    def results_to_dataframe(results: list[dict]):
        """Convert results list to a pandas DataFrame.
        
        Args:
            results: List of result dicts from distribute()
            
        Returns:
            pandas DataFrame
            
        Example:
            >>> df = mesh.results_to_dataframe(results)
            >>> print(df)
               lr  epochs  accuracy
            0  0.01     100      0.82
            1  0.05     200      0.89
        """
        try:
            import pandas as pd
        except ImportError:
            raise ImportError(
                "pandas is required for results_to_dataframe(). "
                "Install it with: pip install pandas"
            )
        
        return pd.DataFrame(results)

    # ── Device access methods ─────────────────────────────────────────

    def devices(self) -> list[dict]:
        """List all compute devices (local + remote) as one unified pool.

        Returns:
            List of device dicts with index, hostname, device, device_name,
            score, and status.

        Example:
            >>> mesh.devices()
            [
                {'index': 0, 'hostname': 'laptop-a', 'device': 'cuda',
                 'device_name': 'RTX 3080', 'score': 85.2, 'status': 'alive'},
                {'index': 1, 'hostname': 'laptop-b', 'device': 'cuda',
                 'device_name': 'RTX 4090', 'score': 120.5, 'status': 'alive'},
            ]
        """
        try:
            resp = self._client.call("GET", "/api/devices")
            return resp.get("devices", [])
        except (urllib.error.URLError, OSError) as exc:
            raise GPUMeshError(f"Failed to list devices: {exc}") from exc

    def device_count(self) -> int:
        """Return the number of alive compute devices across all machines.

        Counts every machine contributing compute, GPU or CPU, so it always
        matches the length of the alive entries in :meth:`devices`. Use
        :meth:`gpu_count` when you specifically need GPUs.

        Example:
            >>> mesh.device_count()
            2
        """
        try:
            resp = self._client.call("GET", "/api/devices")
            return resp.get("alive_devices", 0)
        except (urllib.error.URLError, OSError) as exc:
            raise GPUMeshError(f"Failed to get device count: {exc}") from exc

    def gpu_count(self) -> int:
        """Return the number of alive GPUs (CUDA or MPS) across all machines.

        CPU-only workers are excluded, so this is 0 on a mesh of laptops
        without discrete GPUs even though :meth:`device_count` is not.

        Example:
            >>> mesh.gpu_count()
            1
        """
        try:
            resp = self._client.call("GET", "/api/devices")
            return resp.get("total_gpus", 0)
        except (urllib.error.URLError, OSError) as exc:
            raise GPUMeshError(f"Failed to get GPU count: {exc}") from exc

    def total_score(self) -> float:
        """Return the total compute score across all alive devices.

        Example:
            >>> mesh.total_score()
            205.7
        """
        try:
            resp = self._client.call("GET", "/api/devices")
            return resp.get("total_score", 0.0)
        except (urllib.error.URLError, OSError) as exc:
            raise GPUMeshError(f"Failed to get total score: {exc}") from exc

    def auto_device(self) -> dict | None:
        """Pick the most powerful alive device automatically.

        Returns:
            Device dict or None if no devices available.

        Example:
            >>> device = mesh.auto_device()
            >>> print(device['device_name'])
            RTX 4090
        """
        try:
            devices = self.devices()
            alive = [d for d in devices if d["status"] == "alive"]
            if not alive:
                return None
            # Pick device with highest score (most powerful)
            return max(alive, key=lambda d: d["score"])
        except GPUMeshError:
            raise
        except (urllib.error.URLError, OSError) as exc:
            raise GPUMeshError(f"Failed to auto-select device: {exc}") from exc


