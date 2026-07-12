"""Hardware probing and benchmarking.

Detects the best compute device available and produces a single numeric
capability score used by the scheduler. torch and psutil are optional:
without them the probe falls back to CPU info and a pure-Python benchmark.
"""

import os
import platform
import socket
import time


def probe_device() -> dict:
    info = {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count() or 1,
        "device": "cpu",
        "device_name": platform.processor() or "cpu",
    }
    try:
        import torch

        if torch.cuda.is_available():
            info["device"] = "cuda"
            info["device_name"] = torch.cuda.get_device_name(0)
        elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            info["device"] = "mps"
            info["device_name"] = "Apple Silicon GPU"
    except ImportError:
        pass
    try:
        import psutil

        info["ram_gb"] = round(psutil.virtual_memory().total / 1e9, 1)
    except ImportError:
        pass
    return info


def _bench_torch(device: str) -> float:
    """Time a matmul on the given torch device; return ops/sec score."""
    import torch

    n = 1024
    a = torch.rand(n, n, device=device)
    b = torch.rand(n, n, device=device)
    # warmup
    (a @ b).sum().item()
    start = time.perf_counter()
    iters = 10
    for _ in range(iters):
        c = a @ b
    c.sum().item()  # force sync on cuda/mps
    elapsed = time.perf_counter() - start
    flops = 2 * n**3 * iters
    return flops / elapsed / 1e9  # GFLOP/s


def _bench_python() -> float:
    """Pure-Python matmul fallback. Slow by design; scores are comparable
    across machines because everyone runs the same loop."""
    n = 48
    a = [[(i * j) % 7 / 7.0 for j in range(n)] for i in range(n)]
    b = [[(i + j) % 5 / 5.0 for j in range(n)] for i in range(n)]
    start = time.perf_counter()
    iters = 5
    for _ in range(iters):
        [[sum(a[i][k] * b[k][j] for k in range(n)) for j in range(n)] for i in range(n)]
    elapsed = time.perf_counter() - start
    flops = 2 * n**3 * iters
    return flops / elapsed / 1e9


def benchmark(device: str) -> float:
    try:
        return round(_bench_torch(device), 3)
    except ImportError:
        return round(_bench_python(), 3)


def full_probe() -> dict:
    info = probe_device()
    info["score"] = benchmark(info["device"])
    return info
