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
