"""Hardware probing and benchmarking.

Detects the best compute device available and produces a single numeric
capability score used by the scheduler. torch and psutil are optional:
without them the probe falls back to CPU info and a pure-Python benchmark.
"""

# Required for the ``X | None`` annotations below: on Python 3.9 they would
# otherwise be evaluated at definition time and raise TypeError on import.
from __future__ import annotations

import os
import platform
import socket
import time


def probe_device() -> dict:
    info = {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count() or 1,
        "cpu_cores": os.cpu_count() or 1,
        "device": "cpu",
        "device_name": platform.processor() or "cpu",
    }
    try:
        import torch

        if torch.cuda.is_available():
            info["device"] = "cuda"
            info["device_name"] = torch.cuda.get_device_name(0)
            try:
                props = torch.cuda.get_device_properties(0)
                info["gpu_memory_total_mb"] = round(props.total_mem / (1024**2), 1)
            except (RuntimeError, OSError):
                pass
            try:
                free, total = torch.cuda.mem_get_info(0)
                info["gpu_memory_free_mb"] = round(free / (1024**2), 1)
            except (RuntimeError, OSError):
                pass
        elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            info["device"] = "mps"
            info["device_name"] = "Apple Silicon GPU"
    except (ImportError, RuntimeError, OSError):
        pass
    try:
        import psutil

        info["ram_gb"] = round(psutil.virtual_memory().total / 1e9, 1)
    except ImportError:
        pass
    return info


def get_gpu_memory_info(device_index: int = 0) -> dict | None:
    """Get GPU memory info for a specific device. Returns None if no GPU."""
    try:
        import torch

        if not torch.cuda.is_available():
            return None
        props = torch.cuda.get_device_properties(device_index)
        total = props.total_mem / (1024**2)
        free, total2 = torch.cuda.mem_get_info(device_index)
        return {
            "device_index": device_index,
            "device_name": torch.cuda.get_device_name(device_index),
            "total_mb": round(total, 1),
            "free_mb": round(free / (1024**2), 1),
            "used_mb": round((total2 - free) / (1024**2), 1),
        }
    except (ImportError, RuntimeError, OSError):
        return None


def get_gpu_memory_usage() -> dict:
    """Return current GPU memory and utilization stats.

    Returns gpu_memory_total_mb, gpu_memory_used_mb, gpu_memory_free_mb,
    and gpu_utilization_pct. Falls back to nvidia-smi parsing if torch
    is unavailable. Returns zeros if no GPU is present.
    """
    # Try torch first
    try:
        import torch
        if torch.cuda.is_available():
            free, total = torch.cuda.mem_get_info(0)
            total_mb = round(total / (1024**2), 1)
            free_mb = round(free / (1024**2), 1)
            used_mb = round((total - free) / (1024**2), 1)
            # GPU utilization via CUDA event timing (best-effort)
            util_pct = _get_gpu_utilization_torch()
            return {
                "gpu_memory_total_mb": total_mb,
                "gpu_memory_used_mb": used_mb,
                "gpu_memory_free_mb": free_mb,
                "gpu_utilization_pct": util_pct,
            }
    except (ImportError, RuntimeError, OSError):
        pass

    # Fallback: nvidia-smi parsing
    result = _get_gpu_utilization_nvidia_smi()
    if result is not None:
        return result

    return {
        "gpu_memory_total_mb": 0.0,
        "gpu_memory_used_mb": 0.0,
        "gpu_memory_free_mb": 0.0,
        "gpu_utilization_pct": 0.0,
    }


def _get_gpu_utilization_torch() -> float:
    """Estimate GPU compute utilization using torch.cuda.Event timing."""
    try:
        import torch
        if not torch.cuda.is_available():
            return 0.0
        start_event = torch.cuda.Event(enable_timing=True)
        end_event = torch.cuda.Event(enable_timing=True)
        # Quick busy-loop to measure how much of a short window is GPU-bound
        start_event.record()
        _ = torch.rand(64, 64, device="cuda") @ torch.rand(64, 64, device="cuda")
        end_event.record()
        torch.cuda.synchronize()
        elapsed_ms = start_event.elapsed_time(end_event)
        # Heuristic: if a small matmul takes >0.5ms the GPU is busy
        return min(round(elapsed_ms / 5.0 * 100, 1), 100.0)
    except (RuntimeError, OSError):
        return 0.0


def _get_gpu_utilization_nvidia_smi() -> dict | None:
    """Parse nvidia-smi for memory and utilization. Returns None on failure."""
    import subprocess
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.total,memory.used,memory.free,utilization.gpu",
             "--format=csv,noheader,nounits"],
            timeout=5, text=True, stderr=subprocess.DEVNULL,
        )
        parts = out.strip().split(",")
        if len(parts) >= 4:
            return {
                "gpu_memory_total_mb": float(parts[0].strip()),
                "gpu_memory_used_mb": float(parts[1].strip()),
                "gpu_memory_free_mb": float(parts[2].strip()),
                "gpu_utilization_pct": float(parts[3].strip()),
            }
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, OSError):
        pass
    return None


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


def _bench_memory_bandwidth_torch(device: str) -> float:
    """Allocate 256 MB, copy it, measure bandwidth in GB/s."""
    import torch

    size_mb = 256
    num_bytes = size_mb * 1024 * 1024
    n_elements = num_bytes // 4  # float32 = 4 bytes
    src = torch.rand(n_elements, device=device, dtype=torch.float32)
    # warmup
    dst = src.clone()
    del dst
    torch.cuda.synchronize() if device == "cuda" else None
    iters = 5
    start = time.perf_counter()
    for _ in range(iters):
        dst = src.clone()
        if device == "cuda":
            torch.cuda.synchronize()
    elapsed = time.perf_counter() - start
    total_bytes = num_bytes * iters
    return total_bytes / elapsed / 1e9  # GB/s


def _bench_memory_bandwidth_python() -> float:
    """Rough CPU memory bandwidth proxy: copy a 256 MB bytearray."""
    size_mb = 256
    src = bytearray(size_mb * 1024 * 1024)
    iters = 2
    start = time.perf_counter()
    for _ in range(iters):
        dst = bytearray(src)
    elapsed = time.perf_counter() - start
    total_bytes = len(src) * iters
    return total_bytes / elapsed / 1e9


def benchmark(device: str) -> float:
    try:
        return round(_bench_torch(device), 3)
    except (ImportError, RuntimeError, OSError):
        return round(_bench_python(), 3)


import threading

_benchmark_cache: dict = {}
_benchmark_lock = threading.Lock()  # protects _benchmark_cache dict mutations


def run_benchmark(device: str | None = None, force: bool = False) -> dict:
    """Run composite benchmark (GFLOPS + memory bandwidth).

    Returns {"gflops": float, "bandwidth_gbps": float, "score": float}.
    Result is cached so the benchmark only runs once per process
    (unless force=True for periodic re-benchmarking).
    """
    cache_key = device or "default"
    if not force:
        with _benchmark_lock:
            if cache_key in _benchmark_cache:
                return _benchmark_cache[cache_key]

    if device is None:
        info = probe_device()
        device = info["device"]

    try:
        gflops = _bench_torch(device)
    except (ImportError, RuntimeError, OSError):
        gflops = _bench_python()

    try:
        if device in ("cuda", "mps"):
            bw = _bench_memory_bandwidth_torch(device)
        else:
            bw = _bench_memory_bandwidth_python()
    except (ImportError, RuntimeError, OSError):
        bw = _bench_memory_bandwidth_python()

    score = round(gflops * 0.7 + bw * 0.3, 3)
    result = {
        "gflops": round(gflops, 3),
        "bandwidth_gbps": round(bw, 3),
        "score": score,
    }
    with _benchmark_lock:
        _benchmark_cache[cache_key] = result
    return result


def full_probe() -> dict:
    info = probe_device()
    bench = run_benchmark(info["device"])
    info["score"] = bench["score"]
    info["gflops"] = bench["gflops"]
    info["bandwidth_gbps"] = bench["bandwidth_gbps"]
    gpu_info = get_gpu_memory_info(0)
    if gpu_info:
        info["gpu_memory_total_mb"] = gpu_info["total_mb"]
        info["gpu_memory_free_mb"] = gpu_info["free_mb"]
        info["gpu_memory_used_mb"] = gpu_info["used_mb"]
    return info
