"""Local PyTorch convenience helpers for GPUMesh.

Usage:
    import gpumesh
    import torch

    mesh = gpumesh.GPUMesh("http://coordinator:8000", token="mysecret")
    model = gpumesh.torch.auto(mesh, MyModel())

``auto`` only places a model on devices attached to the current process. It
may use ``torch.nn.DataParallel`` across multiple local CUDA devices, but it
does not execute a model on remote GPUMesh workers. Use ``mesh.distribute``
for remote execution.

PyTorch is imported lazily when one of these helpers is called.
"""

from __future__ import annotations

import socket
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .api import GPUMesh


# Lazy imports — only available when torch is installed.
torch = None
nn = None
_init_lock = threading.Lock()


class RemoteDeviceError(RuntimeError):
    """Raised when a remote mesh device is requested as a torch.device."""


def _ensure_torch():
    """Import torch if available, raise a helpful error if not."""
    global torch, nn
    if torch is not None and nn is not None:
        return
    with _init_lock:
        if torch is not None and nn is not None:
            return
        try:
            import torch as _torch
            import torch.nn as _nn
        except ImportError:
            raise ImportError(
                "PyTorch is required for gpumesh.torch. "
                "Install with: pip install torch"
            )
        torch = _torch
        nn = _nn


def device(mesh: GPUMesh, index: int = 0):
    """Map a local mesh inventory entry to ``torch.device``.

    A ``torch.device`` can only address hardware visible to the current
    process. If ``index`` selects a remote worker, this function raises
    :class:`RemoteDeviceError`; use ``mesh.distribute`` for remote work.
    """
    _ensure_torch()
    alive = [d for d in mesh.devices() if d["status"] == "alive"]

    if not alive:
        return torch.device("cpu")

    if index >= len(alive):
        index = index % len(alive)

    target = alive[index]
    local_hostname = socket.gethostname()
    if target["hostname"] != local_hostname:
        raise RemoteDeviceError(
            f"Mesh device {index} ({target['hostname']}:{target['device']}) is remote; "
            "torch.device can only address local hardware. Use mesh.distribute() "
            "to execute work on remote workers."
        )

    if target["device"] == "cuda":
        if not torch.cuda.is_available():
            raise RuntimeError(
                "The selected mesh device is CUDA, but CUDA is not available "
                "to this PyTorch process."
            )
        local_cuda_idx = 0
        for candidate in alive:
            if candidate["hostname"] == local_hostname and candidate["device"] == "cuda":
                if candidate["id"] == target["id"]:
                    break
                local_cuda_idx += 1
        return torch.device(f"cuda:{local_cuda_idx}")

    if target["device"] == "mps":
        mps = getattr(torch.backends, "mps", None)
        if not mps or not mps.is_available():
            raise RuntimeError(
                "The selected mesh device is MPS, but MPS is not available "
                "to this PyTorch process."
            )
        return torch.device("mps")

    if target["device"] == "cpu":
        return torch.device("cpu")

    raise RuntimeError(f"Unsupported local mesh device type: {target['device']}")


def auto(mesh: GPUMesh, model):
    """Place a model on the best devices available to this local process.

    Multiple local CUDA devices use ``torch.nn.DataParallel``. One local CUDA
    device or local MPS moves the model to that device; otherwise the model is
    returned unchanged on CPU. Remote workers in ``mesh`` are not used by this
    helper. The ``mesh`` argument is retained for API compatibility and for a
    consistent ``gpumesh.torch`` interface.
    """
    _ensure_torch()

    local_cuda_count = torch.cuda.device_count() if torch.cuda.is_available() else 0
    mps = getattr(torch.backends, "mps", None)
    local_mps = bool(mps and mps.is_available())

    if local_cuda_count > 1:
        return nn.DataParallel(model.cuda())
    if local_cuda_count == 1:
        return model.cuda()
    if local_mps:
        return model.to("mps")
    return model


def device_count(mesh: GPUMesh) -> int:
    """Return the mesh's total alive GPU inventory count.

    This includes remote GPUs and therefore is not the number of devices that
    can be passed to local PyTorch APIs.
    """
    return mesh.device_count()


def setup_torch(mesh: GPUMesh = None, min_memory_mb: float = 0) -> str:
    """Auto-detect the best device and backend for PyTorch.

    Returns the device string ("cuda:0", "mps", "cpu").
    If mesh is provided, uses mesh device inventory for detection.
    If min_memory_mb is provided, filters out GPUs with less free VRAM.

    Usage:
        import gpumesh
        device = gpumesh.torch.setup_torch()
        model = model.to(device)
    """
    _ensure_torch()

    if mesh is not None:
        try:
            devices = mesh.devices()
            alive = [d for d in devices if d["status"] == "alive"]
            if alive:
                if min_memory_mb > 0:
                    alive = [
                        d for d in alive
                        if (d.get("gpu_memory_free_mb") or d.get("gpu_memory_total_mb") or 0) >= min_memory_mb
                    ]
                if alive:
                    best = max(alive, key=lambda d: d["score"])
                    local_hostname = socket.gethostname()
                    if best["hostname"] == local_hostname:
                        if best["device"] == "cuda" and torch.cuda.is_available():
                            return "cuda:0"
                        elif best["device"] == "mps":
                            mps = getattr(torch.backends, "mps", None)
                            if mps and mps.is_available():
                                return "mps"
        except Exception:
            pass

    if torch.cuda.is_available():
        if min_memory_mb > 0:
            try:
                free, _ = torch.cuda.mem_get_info(0)
                if free / (1024**2) < min_memory_mb:
                    mps = getattr(torch.backends, "mps", None)
                    if mps and mps.is_available():
                        return "mps"
                    return "cpu"
            except (RuntimeError, OSError):
                pass
        return "cuda:0"
    mps = getattr(torch.backends, "mps", None)
    if mps and mps.is_available():
        return "mps"
    return "cpu"


def get_device(mesh: GPUMesh = None) -> "torch.device":
    """Get the best ``torch.device`` for this process.

    Usage:
        import gpumesh.torch
        device = gpumesh.torch.get_device()
        model = model.to(device)
    """
    _ensure_torch()
    return torch.device(setup_torch(mesh))
