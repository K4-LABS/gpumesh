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
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from . import capability, sandbox, serializer, utils
from .worker import MeshClient


class GPUMeshError(Exception):
    """Raised when a GPUMesh operation fails."""
    pass


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
    
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token
        self._client = MeshClient(self.url, self.token)
    
    # ── Static methods for setup ──────────────────────────────────────
    
    @staticmethod
    def start_coordinator(
        port: int = 8000,
        token: str | None = None,
        db_path: str = "gpumesh.db",
        tailscale: bool = False,
    ) -> str:
        """Start a coordinator server (non-blocking).
        
        Args:
            port: Port to listen on
            token: Auth token (generated if None)
            db_path: SQLite database path
            tailscale: Use Tailscale for network access
            
        Returns:
            The token used
            
        Example:
            >>> GPUMesh.start_coordinator(port=8000, token="mysecret")
            Coordinator running. Share this: http://192.168.1.10:8000
        """
        import secrets as _secrets
        from . import server, tunnel
        
        if token is None:
            token = _secrets.token_urlsafe(12)
        
        httpd = server.serve("0.0.0.0", port, db_path, token)
        
        # Print connection info
        ip = utils.get_lan_ip()
        print(f"[mesh] coordinator listening on 0.0.0.0:{port}")
        print(f"[mesh] token: {token}")
        print(f"[mesh] share this with workers:")
        print(f"       from gpumesh import GPUMesh")
        print(f"       GPUMesh.add_worker(\"http://{ip}:{port}\", token=\"{token}\")")
        
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
        
        return token
    
    @staticmethod
    def add_worker(
        url: str,
        token: str,
        timeout: float = 240.0,
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
        print(f"[worker] device={info['device']} ({info['device_name']}) "
              f"score={info['score']} GFLOP/s")
        
        # Run worker in background thread
        def _run():
            try:
                run_worker(url, token, task_timeout=timeout)
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
        poll_interval: float = 2.0,
    ) -> list[dict]:
        """Distribute a function across all workers and collect results.
        
        Args:
            function: The function to execute
            params: List of parameter dicts (each becomes one task)
            name: Optional job name
            timeout: Total timeout for the job (seconds)
            poll_interval: How often to check for results
            
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
        
        # Serialize the function
        func_data = serializer.serialize_function(function)
        
        # Create tasks with serialized function + params
        payloads = []
        for i, p in enumerate(params):
            payloads.append({
                "_func": func_data,
                "_params": p,
                "_task_index": i,
                "cost": p.get("cost", 1.0),
            })
        
        # Submit job
        callable_name = getattr(function, "__name__", type(function).__name__)
        try:
            resp = self._client.call("POST", "/api/jobs", {
                "name": name or f"distribute_{callable_name}",
                "script": "__gpumesh_function__",  # Marker for function-based job
                "payloads": payloads,
            })
        except (urllib.error.URLError, OSError) as exc:
            raise GPUMeshError(
                f"Failed to submit job to coordinator: {exc}. "
                f"Check that the coordinator is running at {self.url}"
            ) from exc
        job_id = resp["job_id"]
        
        # Poll for completion
        start_time = time.time()
        while True:
            try:
                job = self.job_status(job_id)
            except (urllib.error.URLError, OSError) as exc:
                elapsed = time.time() - start_time
                if elapsed > timeout:
                    raise TimeoutError(f"Job {job_id} timed out after {timeout}s") from exc
                time.sleep(poll_interval)
                continue
            
            if job["finished"]:
                break
            
            elapsed = time.time() - start_time
            if elapsed > timeout:
                raise TimeoutError(f"Job {job_id} timed out after {timeout}s")
            
            # Show progress
            counts = job["counts"]
            total = sum(counts.values())
            done = counts.get("done", 0) + counts.get("failed", 0)
            print(f"\r[distribute] {done}/{total} tasks finished ({counts})", end="", flush=True)
            
            time.sleep(poll_interval)
        
        print()  # Newline after progress
        
        # Collect results
        results = []
        for task in job["tasks"]:
            if task["status"] == "done":
                results.append(task["result"] if task["result"] is not None else {})
            elif task["status"] == "failed":
                results.append({"_error": task.get("error", "unknown")})
            else:
                results.append({"_error": "task did not complete"})

        # Tasks are returned in database row order, which is submission order.
        # Remove the worker's internal ordering key without re-sorting mixed
        # success and failure records.
        # Remove internal keys
        for r in results:
            r.pop("_task_index", None)
        
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
        """Return the total number of alive GPUs across all machines.

        Example:
            >>> mesh.device_count()
            2
        """
        try:
            resp = self._client.call("GET", "/api/devices")
            return resp.get("total_gpus", 0)
        except (urllib.error.URLError, OSError) as exc:
            raise GPUMeshError(f"Failed to get device count: {exc}") from exc

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


