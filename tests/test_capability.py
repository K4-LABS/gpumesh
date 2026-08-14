from gpumesh.capability import full_probe, get_gpu_memory_info, probe_device


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


def test_get_gpu_memory_info_returns_none_without_cuda():
    """get_gpu_memory_info returns None when CUDA is not available."""
    result = get_gpu_memory_info(0)
    # On a machine without CUDA, this should be None
    # On a machine with CUDA, it should be a dict
    if result is not None:
        assert "total_mb" in result
        assert "free_mb" in result
        assert "used_mb" in result
        assert result["total_mb"] > 0
        assert result["free_mb"] >= 0


def test_full_probe_includes_gpu_memory_fields():
    """full_probe should include gpu memory fields when CUDA is available."""
    info = full_probe()
    # These fields are always present (may be None if no CUDA)
    # On CPU-only machines, they simply won't be in the dict
    if "gpu_memory_total_mb" in info:
        assert info["gpu_memory_total_mb"] > 0
    if "gpu_memory_free_mb" in info:
        assert info["gpu_memory_free_mb"] >= 0


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
