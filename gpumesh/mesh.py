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
_local_kwargs = None
_connection_manager = None


def _ensure_api():
    global _GPUMesh, _AcceleratedFunction, _MeshUnavailable, _local_kwargs
    global _connection_manager
    if _GPUMesh is None:
        accel = importlib.import_module(".accelerate", "gpumesh")
        _AcceleratedFunction = accel.AcceleratedFunction
        _MeshUnavailable = accel._MeshUnavailable
        # Borrowed rather than reimplemented on purpose. accelerate.map() and
        # this module's map() are the same fallback wearing two coats — both
        # answer "what kwargs does this payload become when it runs here?" —
        # and the answer drifting between them is precisely the bug this
        # import exists to close. See accelerate._local_kwargs for why only
        # ``cost`` is stripped.
        _local_kwargs = accel._local_kwargs
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
        #
        # Deliberately no ``cost`` strip on this path, unlike map() below.
        # There is no payload here: the arguments are the caller's own, written
        # out by hand at the call site, and on the mesh path they go into
        # ``_params`` verbatim (accelerate._remote_call builds the payload from
        # the merged kwargs). So ``train(lr=0.1, cost=2.0)`` can only mean the
        # user's function declares a ``cost`` parameter, and both paths already
        # deliver it. Stripping here would break that function on the local
        # path — the mirror image of the bug fixed in map(), and no better for
        # being symmetrical about it.
        self._ensure_afn()
        if self._afn is not None:
            return self._afn(*args, **kwargs)
        return self._bound_fn(*args, **kwargs)

    def map(self, params_list: list[dict]) -> list[dict]:
        """Distribute across all connected laptops. Falls back to local.

        The local branch strips ``cost`` for the same reason
        ``GPUMesh.distribute`` has since 1.1.0: it is a scheduler hint riding
        at the payload's top level, never an argument to the user's function.
        The mesh branch never showed it to the function, so a payload annotated
        the way examples/payloads.json teaches ran fine while the mesh was up
        and raised ``unexpected keyword argument 'cost'`` the moment it was
        not — i.e. exactly when the fallback was supposed to be saving you.

        Placement hints (``gpu``/``gpu_memory_mb``/``cpu_cores``) are NOT
        stripped and must not be: unlike ``cost``, they are written at the
        payload top level by ``distribute()`` from the decorator's own
        keywords, so they never originate in a user payload. A key of that
        name in a ``.map()`` payload is an ordinary parameter, lands in
        ``_params``, and reaches the function on the mesh path — dropping it
        locally would invent a fresh asymmetry rather than remove one.
        """
        if not params_list:
            return []
        self._ensure_afn()
        if self._afn is not None:
            # AcceleratedFunction.map falls back to local execution itself,
            # and applies the same strip on its own fallback branches.
            return self._afn.map(params_list)
        _ensure_api()
        return [self._bound_fn(**_local_kwargs(p)) for p in params_list]


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