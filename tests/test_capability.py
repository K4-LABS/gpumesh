import sys
import types
from unittest.mock import patch

import pytest

from gpumesh.capability import full_probe, get_gpu_memory_info, probe_device

_GPU_KEYS = {"gpu_memory_total_mb", "gpu_memory_free_mb", "gpu_memory_used_mb"}


def _has_cuda():
    """True only on a machine with a working CUDA device.

    Used by ``skipif`` so that the handful of assertions which genuinely need
    real hardware are reported as *skipped* on a CPU-only box. The alternative
    — an ``if result is not None:`` that quietly asserts nothing — is how this
    file previously managed to look like coverage on every CI runner in
    existence while testing nothing at all there.
    """
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


_NEEDS_GPU = pytest.mark.skipif(not _has_cuda(), reason="requires a CUDA GPU")


def _torch_without_gpu():
    """A fake torch module reporting no CUDA and no MPS."""
    mod = types.ModuleType("torch")
    mod.cuda = types.SimpleNamespace(is_available=lambda: False)
    mod.backends = types.SimpleNamespace(mps=None)
    return mod


def test_probe_device_has_required_fields():
    info = probe_device()
    assert info["hostname"]
    assert info["device"] in ("cpu", "cuda", "mps")
    assert info["cpu_count"] >= 1


def test_probe_device_has_cpu_cores():
    info = probe_device()
    assert "cpu_cores" in info
    assert info["cpu_cores"] >= 1


def test_full_probe_score_positive():
    info = full_probe()
    assert info["score"] > 0


def test_get_gpu_memory_info_returns_none_when_cuda_is_absent():
    """Absence is reported as None — not as zeros, and not as a crash.

    The condition is forced rather than inherited from the host. The previous
    version of this test called ``get_gpu_memory_info(0)`` and only asserted
    inside ``if result is not None``, so on the CPU-only machines that run CI
    and that most contributors use, it executed no assertion whatsoever.
    Faking ``torch.cuda.is_available() -> False`` exercises the same branch
    identically on every machine.
    """
    with patch.dict(sys.modules, {"torch": _torch_without_gpu()}):
        assert get_gpu_memory_info(0) is None


def test_get_gpu_memory_info_returns_none_without_torch():
    """No torch at all is the other CPU-only shape, and must not raise.

    ``None`` in ``sys.modules`` is what the import system turns into
    ImportError, which is the case ``capability`` catches to fall back.
    """
    with patch.dict(sys.modules, {"torch": None}):
        assert get_gpu_memory_info(0) is None


def test_probe_device_reports_cpu_when_torch_sees_no_gpu():
    """The CPU fallback has to be complete, not just non-crashing.

    Every field the scheduler and the wizard read must still be there, and
    the GPU-only fields must be *absent* rather than present-and-zero — a
    zero would be indistinguishable from a real GPU with no free memory.
    """
    with patch.dict(sys.modules, {"torch": _torch_without_gpu()}):
        info = probe_device()
    assert info["device"] == "cpu"
    assert info["device_name"]
    assert info["hostname"]
    assert info["cpu_count"] >= 1
    assert not _GPU_KEYS & set(info)


def test_probe_device_falls_back_to_cpu_without_torch():
    with patch.dict(sys.modules, {"torch": None}):
        info = probe_device()
    assert info["device"] == "cpu"
    assert not _GPU_KEYS & set(info)


def test_full_probe_reports_gpu_memory_only_when_there_is_a_gpu():
    """The GPU fields are all-or-nothing, and tied to the reported device.

    Both branches assert. On CPU-only hardware — the machine this suite
    actually runs on nearly everywhere — the claim under test is that
    ``full_probe`` says so by *omitting* the three memory keys while still
    returning everything a caller needs to schedule work.
    """
    info = full_probe()
    present = _GPU_KEYS & set(info)
    if info["device"] == "cuda":
        assert present == _GPU_KEYS, f"partial GPU memory report: {present}"
        assert info["gpu_memory_total_mb"] > 0
        assert info["gpu_memory_free_mb"] >= 0
        assert info["gpu_memory_used_mb"] >= 0
    else:
        assert not present, (
            f"device is {info['device']!r} but full_probe reported {present} "
            f"— a CPU-only worker must not advertise GPU memory"
        )
    assert {"hostname", "device", "cpu_count", "cpu_cores", "score",
            "gflops", "bandwidth_gbps"} <= set(info)


@_NEEDS_GPU
def test_get_gpu_memory_info_describes_a_real_device():
    """The positive case, visibly skipped where it cannot run."""
    result = get_gpu_memory_info(0)
    assert result is not None
    assert result["device_index"] == 0
    assert result["device_name"]
    assert result["total_mb"] > 0
    assert result["free_mb"] >= 0
    assert result["used_mb"] >= 0
    assert result["free_mb"] <= result["total_mb"]


class _FakeProps:
    """Stand-in for torch._C._CudaDeviceProperties."""

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


def _fake_torch(props):
    """A fake torch module whose CUDA device has the given properties."""
    import types

    mod = types.ModuleType("torch")
    mod.cuda = types.SimpleNamespace(
        is_available=lambda: True,
        get_device_name=lambda i: "NVIDIA GeForce RTX 3060",
        get_device_properties=lambda i: props,
        mem_get_info=lambda i: (8 * 1024**3, 12 * 1024**3),
    )
    mod.backends = types.SimpleNamespace(mps=None)
    return mod


def test_probe_device_new_torch_memory_attribute():
    """torch >= 2.8 renamed total_mem -> total_memory."""
    import sys
    from unittest.mock import patch

    props = _FakeProps(total_memory=12 * 1024**3)
    with patch.dict(sys.modules, {"torch": _fake_torch(props)}):
        info = probe_device()
    assert info["device"] == "cuda"
    assert info["gpu_memory_total_mb"] == 12288.0


def test_probe_device_old_torch_memory_attribute():
    """Older torch exposes total_mem; the fallback must still work."""
    import sys
    from unittest.mock import patch

    props = _FakeProps(total_mem=12 * 1024**3)
    with patch.dict(sys.modules, {"torch": _fake_torch(props)}):
        info = probe_device()
    assert info["device"] == "cuda"
    assert info["gpu_memory_total_mb"] == 12288.0


def test_gpu_memory_info_new_torch_memory_attribute():
    """get_gpu_memory_info works with the renamed attribute too."""
    import sys
    from unittest.mock import patch

    props = _FakeProps(total_memory=12 * 1024**3)
    with patch.dict(sys.modules, {"torch": _fake_torch(props)}):
        result = get_gpu_memory_info(0)
    assert result is not None
    assert result["total_mb"] == 12288.0
    assert result["free_mb"] == 8192.0
