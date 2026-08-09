"""Transparent GPU acceleration decorator for gpumesh.

Copies patterns from:
- distry: @distry decorator for single function execution
- burla: remote_parallel_map for batch distribution, func_gpu for hardware
- clustrix: @cluster(cores=8, memory='16GB') for resource specs
- HF Accelerate: .to(device) for auto device placement
- cudf.pandas: install() for import hook
- ezpz: setup_torch() for auto-detect backend

Usage:
    from gpumesh import GPUMesh, accelerate

    mesh = GPUMesh("http://coordinator:8000", token="xxx")

    # Pattern 1: Basic (from distry @distry)
    @accelerate(mesh)
    def train(lr, epochs):
        return {"accuracy": 0.95}

    result = train(lr=0.01, epochs=100)  # Local best device

    # Pattern 2: Hardware selection (from burla func_gpu="A100")
    @accelerate(mesh, gpu="A100")
    def train(lr, epochs):
        return {"accuracy": 0.95}

    # Pattern 3: Resource specs (from clustrix @cluster(cores=8, memory='16GB'))
    @accelerate(mesh, cores=8, memory="16GB", timeout=300)
    def heavy_computation(data):
        return processed

    # Pattern 4: Batch call (from burla remote_parallel_map)
    results = train.map([
        {"lr": 0.01, "epochs": 100},
        {"lr": 0.05, "epochs": 200},
    ])

    # Pattern 5: Auto device placement (from HF Accelerate .to(device))
    @accelerate(mesh)
    def train_model(model, data):
        # model is automatically placed on best device
        return model(data)

    # Pattern 6: Import hook (from cudf.pandas.install())
    from gpumesh import accelerate
    accelerate.install(mesh)  # Now all @accelerate functions auto-use mesh
"""

from __future__ import annotations

import functools
import os
import threading
from typing import Any, Callable

from . import capability
from gpumesh.ansi import safe_print, green, yellow, red, bold, dim


# Adaptive polling: check often at first so short tasks return quickly, then
# back off so long-running ones don't hammer the coordinator.
POLL_MIN_INTERVAL = 0.05
POLL_MAX_INTERVAL = 1.0
POLL_BACKOFF = 1.5


class _MeshUnavailable(Exception):
    """Raised when the mesh cannot accept tasks (unreachable, no workers, etc)."""
    pass


def _parse_memory_mb(mem_str: str) -> float:
    """Parse a memory string like '16GB' or '512MB' into MB."""
    s = mem_str.strip().upper()
    if s.endswith("GB"):
        try:
            return float(s[:-2]) * 1024.0
        except ValueError:
            pass
    if s.endswith("MB"):
        try:
            return float(s[:-2])
        except ValueError:
            pass
    if s.endswith("B"):
        try:
            return float(s[:-1]) / (1024**2)
        except ValueError:
            pass
    try:
        return float(s) / (1024**2)
    except ValueError:
        raise ValueError(
            f"Cannot parse memory string: {mem_str!r}. "
            f"Use formats like '16GB', '512MB', or a numeric byte value."
        )


class AcceleratedFunction:
    """Wrapper that transparently routes execution to local best device
    or distributes across the mesh."""

    def __init__(self, fn: Callable, mesh: Any, gpu: str | None = None,
                 cores: int | None = None, memory: str | None = None,
                 timeout: float | None = None):
        functools.update_wrapper(self, fn)
        self._fn = fn
        self._mesh = mesh
        self._gpu = gpu
        self._cores = cores
        self._memory = memory
        self._timeout = timeout
        self.__wrapped__ = fn

    def _validate_resources(self) -> None:
        """Validate that at least one mesh worker can satisfy resource requirements."""
        if self._gpu is None and self._cores is None and self._memory is None:
            return
        if self._mesh is None:
            return

        try:
            devices = self._mesh.devices()
        except Exception as exc:
            import warnings
            warnings.warn(
                f"[accelerate] could not validate resources: {exc}",
                stacklevel=2,
            )
            return

        alive = [d for d in devices if d.get("status") == "alive"]
        if not alive:
            return

        required_memory_mb = _parse_memory_mb(self._memory) if self._memory else None

        for d in alive:
            gpu_match = True
            if self._gpu:
                gpu_match = self._gpu.upper() in d.get("device_name", "").upper()

            mem_match = True
            if required_memory_mb is not None:
                worker_free = d.get("gpu_memory_free_mb") or d.get("gpu_memory_total_mb") or 0
                mem_match = worker_free >= required_memory_mb

            cores_match = True
            if self._cores is not None:
                worker_cores = d.get("cpu_cores") or d.get("cpu_count") or 0
                cores_match = worker_cores >= self._cores

            if gpu_match and mem_match and cores_match:
                return

        parts = []
        if self._gpu:
            parts.append(f"gpu={self._gpu!r}")
        if self._cores is not None:
            parts.append(f"cores={self._cores}")
        if self._memory is not None:
            parts.append(f"memory={self._memory!r}")
        req = ", ".join(parts)
        raise ValueError(
            f"No worker can satisfy resource requirements: {req}. "
            f"Available workers: {[d.get('device_name', d.get('device', '?')) for d in alive]}. "
            f"Try removing the gpu/cores/memory constraint, or add a worker with matching resources."
        )

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Execute on the mesh (remote if workers available) or locally.

        Routing logic (inspired by Exo/PartaGPU):
        - GPUMESH_LOCAL=1 → force local
        - Mesh has workers → submit as a single-task job, poll for result
        - Mesh unreachable or no workers → fall back to local best device
        """
        if os.environ.get("GPUMESH_LOCAL") == "1":
            return self._fn(*args, **kwargs)

        self._validate_resources()

        # Try remote execution if mesh is available
        if self._mesh is not None and os.environ.get("GPUMESH_LOCAL") != "1":
            try:
                return self._remote_call(args, kwargs)
            except _MeshUnavailable:
                pass  # fall through to local

        # Local fallback
        args, kwargs = self._auto_place(args, kwargs)

        device = None
        try:
            device = capability.probe_device()
        except Exception:
            pass

        if os.environ.get("GPUMESH_VERBOSE") == "1":
            device_name = device["device_name"] if device else "unknown"
            target = self._gpu or "auto"
            safe_print(yellow(f"[accelerate] running locally on device: {device_name} (target: {target})"))

        return self._fn(*args, **kwargs)

    def _remote_call(self, args: tuple, kwargs: dict) -> Any:
        """Submit a single task to the mesh and wait for the result.

        Uses the same job submission path as .map() but with a single task.
        Falls back to local if mesh is unreachable or has no workers.
        """
        import json as _json
        import time as _time
        import urllib.error

        # Check if mesh has any alive workers
        try:
            workers = self._mesh.workers()
        except Exception:
            raise _MeshUnavailable("cannot reach mesh")

        alive = [w for w in workers if w.get("alive", False)]
        if not alive:
            raise _MeshUnavailable("no alive workers")

        # Merge args into kwargs using function signature
        call_kwargs = self._merge_args_kwargs(args, kwargs)

        # Serialize and submit
        try:
            from . import serializer
            func_bytes = serializer.serialize_function(self._fn)
        except Exception:
            raise _MeshUnavailable("cannot serialize function")

        payload = {
            "_func": func_bytes,
            "_params": call_kwargs,
            "_task_index": 0,
        }
        if self._gpu:
            payload["gpu"] = self._gpu

        try:
            job_id = self._mesh.submit(
                name=f"accelerate:{self._fn.__name__}",
                script="__gpumesh_function__",
                payloads=[payload],
            )
        except Exception:
            raise _MeshUnavailable("cannot submit job")

        if os.environ.get("GPUMESH_VERBOSE") == "1":
            safe_print(green(f"[accelerate] submitted to mesh: job={job_id}, "
                  f"workers={len(alive)}"))

        # Poll for the result, starting fast and backing off.
        #
        # A fixed one-second poll put a ~2s floor under every mesh call, which
        # dwarfs the runtime of a short function and made the mesh feel slower
        # than running locally. Short functions now return in well under a
        # second, while long ones settle to a slow poll that costs the
        # coordinator almost nothing.
        timeout = self._timeout or 300.0
        start = _time.time()
        delay = POLL_MIN_INTERVAL
        while _time.time() - start < timeout:
            try:
                status = self._mesh.status(job_id)
            except Exception:
                _time.sleep(delay)
                delay = min(delay * POLL_BACKOFF, POLL_MAX_INTERVAL)
                continue

            if status.get("finished"):
                tasks = status.get("tasks", [])
                if tasks:
                    t = tasks[0]
                    if t["status"] == "done":
                        from . import serializer

                        raw = t.get("result")
                        if isinstance(raw, dict):
                            # Internal ordering key; never reaches user code.
                            raw.pop("_task_index", None)
                        # Unwrap the envelope so a mesh call returns exactly
                        # what a local call would have returned.
                        result = {} if raw is None else serializer.decode_result(raw)
                        if os.environ.get("GPUMESH_VERBOSE") == "1":
                            safe_print(green(f"[accelerate] mesh result: {result}"))
                        return result
                    raise RuntimeError(
                        f"mesh task failed: {t.get('error') or t['status']}"
                    )
                # A finished job with no tasks means the job record was lost
                # (coordinator restarted with a fresh database, say). Report
                # that rather than blocking until the timeout expires.
                raise RuntimeError(
                    f"mesh job {job_id} finished with no tasks — the "
                    f"coordinator may have restarted with a fresh database"
                )

            _time.sleep(delay)
            delay = min(delay * POLL_BACKOFF, POLL_MAX_INTERVAL)

        raise TimeoutError(
            f"mesh task did not complete within {timeout}s"
        )

    def _merge_args_kwargs(self, args: tuple, kwargs: dict) -> dict:
        """Merge positional args into kwargs using function signature."""
        import inspect
        sig = inspect.signature(self._fn)
        params = list(sig.parameters.keys())
        merged = dict(kwargs)
        for i, arg in enumerate(args):
            if i < len(params):
                merged[params[i]] = arg
            else:
                merged[f"_positional_{i}"] = arg
        return merged

    def _auto_place(self, args: tuple, kwargs: dict) -> tuple:
        """Auto-place PyTorch models on best device (HF Accelerate pattern)."""
        try:
            import torch
        except ImportError:
            return args, kwargs

        device = self._get_torch_device()
        if device is None:
            return args, kwargs

        new_args = []
        for arg in args:
            if isinstance(arg, torch.nn.Module):
                arg = arg.to(device)
            new_args.append(arg)

        new_kwargs = {}
        for k, v in kwargs.items():
            if isinstance(v, torch.nn.Module):
                v = v.to(device)
            new_kwargs[k] = v

        return tuple(new_args), new_kwargs

    def _get_torch_device(self):
        """Get the best torch device (from ezpz setup_torch pattern)."""
        try:
            import torch
        except ImportError:
            return None

        if self._gpu:
            if self._gpu.upper() == "CPU":
                return torch.device("cpu")
            if torch.cuda.is_available():
                for i in range(torch.cuda.device_count()):
                    name = torch.cuda.get_device_name(i)
                    if self._gpu.upper() in name.upper():
                        return torch.device(f"cuda:{i}")
                return torch.device("cuda:0")
            return torch.device("cpu")

        if torch.cuda.is_available():
            return torch.device("cuda:0")
        mps = getattr(torch.backends, "mps", None)
        if mps and mps.is_available():
            return torch.device("mps")
        return torch.device("cpu")

    def map(self, params_list: list[dict]) -> list[dict]:
        """Distribute across all mesh devices (burla remote_parallel_map pattern).

        Args:
            params_list: List of dicts, each dict becomes one task's kwargs.

        Returns:
            List of result dicts, one per parameter set.
        """
        if not params_list:
            return []

        if os.environ.get("GPUMESH_LOCAL") == "1":
            return [self._fn(**p) for p in params_list]

        self._validate_resources()

        # Fast-fail: detect when no real workers are connected so we warn
        # early instead of hanging through distribute's timeout.
        # Only applies when workers() returns a real list we can inspect;
        # test mocks bypass this check.
        if self._mesh is not None:
            try:
                _workers = self._mesh.workers()
                if isinstance(_workers, list):
                    alive = [w for w in _workers if w.get("alive", False)]
                    if not alive:
                        import warnings
                        warnings.warn(
                            "[accelerate] No alive workers — falling back to local execution. "
                            "Connect a worker with `gpumesh join URL --token TOKEN` to use the mesh.",
                            stacklevel=2,
                        )
                        return [self._fn(**p) for p in params_list]
            except Exception:
                pass  # mesh down — fall through to distribute which will also fallback

        try:
            return self._mesh.distribute(
                function=self._fn,
                params=params_list,
                timeout=self._timeout or 300.0,
            )
        except Exception as exc:
            import warnings
            warnings.warn(
                f"[accelerate] mesh.distribute() failed: {exc}. "
                f"Falling back to local execution.",
                stacklevel=2,
            )
            return [self._fn(**p) for p in params_list]

    def to(self, device: str) -> AcceleratedFunction:
        """Send function to specific device (Runhouse .to(gpu) pattern).

        Args:
            device: Device target ("cuda", "cpu", "mps", or specific like "cuda:1")

        Returns:
            New AcceleratedFunction bound to that device.
        """
        return AcceleratedFunction(
            self._fn, self._mesh,
            gpu=device, cores=self._cores,
            memory=self._memory, timeout=self._timeout,
        )


# -- Global install() (cudf.pandas pattern) ---------------------------------

_installed_mesh = None
_install_lock = threading.Lock()


def install(mesh: Any = None) -> None:
    """Install gpumesh as a transparent import hook (cudf.pandas pattern).

    After calling install(), all @accelerate-decorated functions will
    automatically use the mesh for distribution.

    Usage:
        from gpumesh import accelerate
        accelerate.install(mesh)

        @accelerate  # No mesh argument needed!
        def train(lr, epochs):
            return {"accuracy": 0.95}
    """
    global _installed_mesh
    with _install_lock:
        _installed_mesh = mesh


def accelerate(mesh_or_fn: Any = None, *, gpu: str | None = None,
               cores: int | None = None, memory: str | None = None,
               timeout: float | None = None) -> Any:
    """Decorator that makes mesh resources transparent to user code.

    Can be used as:
        @accelerate(mesh)  # With mesh argument
        def train(...): ...

        @accelerate(mesh, gpu="A100", cores=8, memory="16GB", timeout=300)
        def train(...): ...

        # Or after install():
        accelerate.install(mesh)
        @accelerate  # Without mesh argument
        def train(...): ...
    """
    # @accelerate (no arguments, no parentheses) -- use installed mesh
    if mesh_or_fn is not None and callable(mesh_or_fn) and not hasattr(mesh_or_fn, 'distribute'):
        fn = mesh_or_fn
        if _installed_mesh is None:
            raise RuntimeError(
                "No mesh provided. Either pass mesh to @accelerate(mesh) "
                "or call accelerate.install(mesh) first."
            )
        return AcceleratedFunction(fn, _installed_mesh)

    # @accelerate(mesh) or @accelerate(mesh, gpu="A100", cores=8)
    mesh = mesh_or_fn

    def decorator(fn: Callable) -> AcceleratedFunction:
        return AcceleratedFunction(fn, mesh, gpu=gpu, cores=cores,
                                   memory=memory, timeout=timeout)

    return decorator
