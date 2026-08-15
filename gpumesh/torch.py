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
        except ImportError as exc:
            # Chain the original, and quote it. An ImportError out of
            # ``import torch`` does not always mean torch is missing: a broken
            # CUDA install raises one too, from a failed DLL load deep inside
            # the package. Swallowing it made the two look identical, and sent
            # people off to reinstall a package they already had.
            raise ImportError(
                "PyTorch is required for gpumesh.torch. "
                f"Install with: pip install torch (import failed with: {exc})"
            ) from exc
        torch = _torch
        nn = _nn


def _local_cuda_index(alive, target, local_hostname) -> int:
    """Position of ``target`` among this machine's CUDA rows in ``alive``.

    The mesh inventory is not ``torch.cuda``'s device list. It is rows a
    coordinator collected, in the order it collected them, and mapping the Nth
    local CUDA row to ``cuda:N`` is an assumption nothing enforces. A stale row
    — a card that has since been removed, or an inventory predating a reboot
    with fewer GPUs — makes it produce an index this process cannot address,
    which torch would report much later as an opaque invalid-device error.
    Check it against torch's own count and name both numbers.

    ``alive`` must be the *unfiltered* alive list: the index counts positions
    within the inventory, so computing it from a filtered subset shifts it.
    """
    idx = 0
    for candidate in alive:
        if candidate["hostname"] == local_hostname and candidate["device"] == "cuda":
            if candidate["id"] == target["id"]:
                break
            idx += 1
    count = torch.cuda.device_count()
    if idx >= count:
        raise RuntimeError(
            f"The mesh inventory places this device at cuda:{idx}, but this "
            f"process can see only {count} CUDA device(s). The inventory is "
            f"probably stale — restart the worker on this machine so it "
            f"re-registers its current hardware."
        )
    return idx


def _reported_free_memory_mb(entry: dict):
    """Free VRAM a mesh row reports, or ``None`` when it reports none.

    Same distinction as ``accelerate._reported_capacity``: an *absent* key
    means unknown, and unknown must not be read as zero. The chain here is
    ``is not None`` rather than truthiness, though, and that difference is
    deliberate. ``free or total or 0`` treated a GPU with 0 MB free as if it
    had said nothing and fell back to its *total*, so a fully occupied 24 GB
    card sailed through ``min_memory_mb=8000`` and the task landed on the one
    device with no room for it.

    ``accelerate._reported_capacity`` now uses the same ``is not None`` rule,
    and the coordinator reports JSON ``null`` — not ``0.0`` — when nobody has
    measured a worker's free VRAM yet, so "unknown" and "measured empty" are
    finally distinct values on the wire. That is what lets both sides be strict
    about a reported zero without punishing a worker whose first heartbeat has
    not landed. The two functions still differ on the *absent* case, and
    deliberately: accelerate is validating a request and lets an unmeasured
    device through for the scheduler to judge, whereas here we are *choosing* a
    device, so an unmeasured card is skipped in favour of one that certainly
    has room.
    """
    for key in ("gpu_memory_free_mb", "gpu_memory_total_mb"):
        value = entry.get(key)
        if value is not None:
            return value
    return None


def _has_room(entry: dict, min_memory_mb: float) -> bool:
    """True when a mesh row reports enough free VRAM, or reports none at all."""
    free = _reported_free_memory_mb(entry)
    return free is None or free >= min_memory_mb


def device(mesh: GPUMesh, index: int = 0):
    """Map a local mesh inventory entry to ``torch.device``.

    A ``torch.device`` can only address hardware visible to the current
    process. If ``index`` selects a remote worker, this function raises
    :class:`RemoteDeviceError`; use ``mesh.distribute`` for remote work.

    ``index`` must be non-negative. An index past the end of the inventory
    wraps, so round-robin callers can keep counting up.
    """
    _ensure_torch()
    # Negative indices used to fall straight through the wrap below into
    # Python's own negative indexing and quietly return the *last* entry. That
    # is not a selection anybody asked for: an index that reached -1 by
    # arithmetic is a bug in the caller, and silently handing back a different
    # device (possibly a remote one) hides it.
    if index < 0:
        raise ValueError(
            f"device index must be >= 0, got {index}. Indices name mesh "
            f"inventory entries and are wrapped, not counted from the end."
        )
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
        return torch.device(f"cuda:{_local_cuda_index(alive, target, local_hostname)}")

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
    can be passed to local PyTorch APIs. CPU-only workers are not counted —
    use ``mesh.device_count()`` for every contributing machine.
    """
    return mesh.gpu_count()


def setup_torch(mesh: GPUMesh = None, min_memory_mb: float = 0) -> str:
    """Auto-detect the best device and backend for PyTorch.

    Returns the device string ("cuda:N", "mps", "cpu").
    If mesh is provided, uses mesh device inventory for detection — including
    which local CUDA index the chosen entry corresponds to, so this agrees with
    ``device()`` on a machine with more than one GPU.
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
            inventory = [d for d in devices if d["status"] == "alive"]
            if inventory:
                # The memory filter narrows what we may *choose*; the local
                # CUDA index is still a position within the whole inventory, so
                # the unfiltered list is kept for _local_cuda_index.
                alive = inventory
                if min_memory_mb > 0:
                    alive = [d for d in alive if _has_room(d, min_memory_mb)]
                if alive:
                    best = max(alive, key=lambda d: d["score"])
                    local_hostname = socket.gethostname()
                    if best["hostname"] == local_hostname:
                        if best["device"] == "cuda" and torch.cuda.is_available():
                            # Name the card the mesh entry actually refers to.
                            # This returned "cuda:0" unconditionally, so on a
                            # multi-GPU box it and device() disagreed about the
                            # same inventory row — one of them was always wrong.
                            return f"cuda:{_local_cuda_index(inventory, best, local_hostname)}"
                        elif best["device"] == "mps":
                            mps = getattr(torch.backends, "mps", None)
                            if mps and mps.is_available():
                                return "mps"
        # Everything above is a best-effort hint. A coordinator that is down, or
        # an inventory stale enough that _local_cuda_index refuses it, falls
        # through to plain local detection rather than failing the caller —
        # unlike device(), which is asked for one specific entry and says so.
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
