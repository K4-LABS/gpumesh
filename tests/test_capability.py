from gpumesh.capability import full_probe, probe_device


def test_probe_device_has_required_fields():
    info = probe_device()
    assert info["hostname"]
    assert info["device"] in ("cpu", "cuda", "mps")
    assert info["cpu_count"] >= 1


def test_full_probe_score_positive():
    info = full_probe()
    assert info["score"] > 0
