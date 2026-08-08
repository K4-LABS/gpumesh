"""``gpumesh.mesh`` — transparent distributed computing for normal Python.

Designed for the "connect once, code normally" experience:

  1. ``pip install gpumesh`` and connect a worker (once).
  2. In VS Code, Jupyter, or the terminal::

        from gpumesh import mesh

        @mesh
        def train(lr, epochs):
            return {"accuracy": model_training(lr, epochs)}

        result = train(lr=0.01, epochs=100)       # single call
        results = train.map([{"lr": 0.01}, ...])   # everywhere at once

  3. Everything else stays normal Python.

Lazy-imports ``api`` and ``accelerate`` on first use to avoid circular
imports from ``__init__.py``.
"""

from __future__ import annotations

import functools
import importlib
from typing import Any, Callable

from .ansi import safe_print, green, yellow, dim

# Lazy-import helpers (called at first use, not at module import time)
_GPUMesh = None
_AcceleratedFunction = None
_MeshUnavailable = None
_connection_manager = None


def _ensure_api():
    global _GPUMesh, _AcceleratedFunction, _MeshUnavailable, _connection_manager
    if _GPUMesh is None:
        accel = importlib.import_module(".accelerate", "gpumesh")
        _AcceleratedFunction = accel.AcceleratedFunction
        _MeshUnavailable = accel._MeshUnavailable
        _GPUMesh = importlib.import_module(".api", "gpumesh").GPUMesh
        _connection_manager = importlib.import_module(
            ".connection_manager", "gpumesh"
        )


# ── Auto-connect from saved config ───────────────────────────────────

_mesh: Any | None = None
_connected = False
_attempted = False  # only print the "no coordinator" hint once per process


def connect(url: str | None = None, token: str | None = None) -> Any | None:
    """Connect to a coordinator.

    If both ``url`` and ``token`` are omitted, uses the connection saved
    by ``gpumesh join`` or ``gpumesh quickjoin``.

    Returns the GPUMesh instance, or ``None`` if no connection is available.
    """
    global _mesh, _connected, _attempted

    if _connected:
        return _mesh

    _ensure_api()

    if url and token:
        _mesh = _GPUMesh(url, token)
        _connected = True
        safe_print(green("[gpumesh] connected to coordinator"))
        return _mesh

    # Try saved config
    saved = _connection_manager.load_connection()
    if saved and saved.get("url") and saved.get("token"):
        _mesh = _GPUMesh(saved["url"], saved["token"])
        _connected = True
        safe_print(green(f"[gpumesh] connected to coordinator at {saved['url']}"))
        return _mesh

    if not _attempted:
        _attempted = True
        safe_print(yellow("[gpumesh] no coordinator configured — @mesh functions will run locally"))
        safe_print(dim("  Run `gpumesh join URL --token TOKEN` first, or pass "))
        safe_print(dim("  `from gpumesh.mesh import connect; connect(url, token)` explicitly."))
    return None


# ── Decorator ────────────────────────────────────────────────────────


class MeshFunction:
    """A function that can execute on the mesh or the local machine."""

    def __init__(self, fn: Callable, mesh: Any | None,
                 gpu: str | None = None, cores: int | None = None,
                 memory: str | None = None, timeout: float | None = None):
        _ensure_api()
        self._bound_fn = fn
        self._mesh = mesh
        self._gpu = gpu
        self._cores = cores
        self._memory = memory
        self._timeout = timeout
        if mesh is not None:
            self._afn = _AcceleratedFunction(fn, mesh, gpu=gpu, cores=cores,
                                             memory=memory, timeout=timeout)
        else:
            self._afn = None
        functools.update_wrapper(self, fn)

    def _ensure_afn(self) -> None:
        """Resolve the mesh lazily so a later ``connect()`` takes effect.

        ``@mesh`` decorates at import time, which may run *before* any
        coordinator is configured. Resolving the mesh at decoration time
        would freeze those functions to local-only forever, even after the
        user calls ``mesh.connect(url, token)`` (the flow documented in
        examples/dev_mode.py). Re-check on first use: ``connect()`` is
        cheap and idempotent once a connection exists.
        """
        if self._afn is None:
            connect()
            if _mesh is not None:
                self._afn = _AcceleratedFunction(
                    self._bound_fn, _mesh, gpu=self._gpu, cores=self._cores,
                    memory=self._memory, timeout=self._timeout,
                )

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        # AcceleratedFunction already falls back to local execution internally
        # (mesh down / no workers), so plain delegation is sufficient.
        self._ensure_afn()
        if self._afn is not None:
            return self._afn(*args, **kwargs)
        return self._bound_fn(*args, **kwargs)

    def map(self, params_list: list[dict]) -> list[dict]:
        """Distribute across all connected laptops. Falls back to local."""
        if not params_list:
            return []
        self._ensure_afn()
        if self._afn is not None:
            # AcceleratedFunction.map falls back to local execution itself.
            return self._afn.map(params_list)
        return [self._bound_fn(**p) for p in params_list]


def mesh_fn(fn_or_args=None, *, gpu: str | None = None,
            cores: int | None = None, memory: str | None = None,
            timeout: float | None = None) -> Any:
    """Make a function mesh-aware.

    Usage::

        @mesh
        def train(lr, epochs): ...

        @mesh(gpu="A100", cores=8, memory="16GB", timeout=300)
        def heavy_work(data): ...
    """
    if fn_or_args is not None and callable(fn_or_args):
        connect()  # silent no-op if already connected
        return MeshFunction(fn_or_args, _mesh)

    def decorator(fn: Callable) -> MeshFunction:
        connect()  # silent no-op if already connected
        return MeshFunction(fn, _mesh, gpu=gpu, cores=cores,
                            memory=memory, timeout=timeout)

    return decorator


# ── Helpers ──────────────────────────────────────────────────────────

def devices() -> list[dict]:
    """List all devices in the mesh pool, or [] if not connected."""
    if not _connected:
        connect()
    if _mesh is not None:
        _ensure_api()
        try:
            return _mesh.devices()
        except Exception:
            pass
    return []


def device_count() -> int:
    """Total alive devices across the mesh."""
    if not _connected:
        connect()
    if _mesh is not None:
        _ensure_api()
        try:
            return _mesh.device_count()
        except Exception:
            pass
    return 1


def total_score() -> float:
    """Total compute score across the pool."""
    if not _connected:
        connect()
    if _mesh is not None:
        _ensure_api()
        try:
            return _mesh.total_score()
        except Exception:
            pass
    return 0.0


# Attach the helpers to the decorator itself so the ``mesh`` object exported
# by the package is a complete API: ``@mesh`` for decorating, plus
# ``mesh.connect(...)`` / ``mesh.devices()`` etc. This also keeps
# ``import gpumesh.mesh as mesh_mod`` working even when the package's
# ``__getattr__`` has already bound the ``mesh`` attribute to ``mesh_fn``.
mesh_fn.connect = connect
mesh_fn.devices = devices
mesh_fn.device_count = device_count
mesh_fn.total_score = total_score
mesh_fn.mesh_fn = mesh_fn