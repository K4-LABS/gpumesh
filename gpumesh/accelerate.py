"""Transparent GPU acceleration decorator for gpumesh.

API shape and scheduling strategy adapted from the projects below. No code was
taken from any of them; what was borrowed is the idea of what a good interface
looks like. The wording matters for burla in particular, which is licensed
FSL-1.1-Apache-2.0 — source-available, not OSI open source — so a docstring
saying this file "copies" from it would be a gift to anyone arguing the
opposite in bad faith. See the prior-art section of README.md.

- distry: @distry decorator for single function execution
- burla: remote_parallel_map for batch distribution, func_gpu for hardware
- clustrix: @cluster(cores=8, memory='16GB') for resource specs
- HF Accelerate: .to(device) for auto device placement
- cudf.pandas: install() for import hook
- ezpz: setup_torch() for auto-detect backend

Usage:
    from gpumesh import GPUMesh, accelerate

    mesh = GPUMesh("http://coordinator:8000", token="xxx")

    # Pattern 1: Basic (from distry @distry)
    @accelerate(mesh)
    def train(lr, epochs):
        return {"accuracy": 0.95}

    result = train(lr=0.01, epochs=100)  # Local best device

    # Pattern 2: Hardware selection (from burla func_gpu="A100")
    @accelerate(mesh, gpu="A100")
    def train(lr, epochs):
        return {"accuracy": 0.95}

    # Pattern 3: Resource specs (from clustrix @cluster(cores=8, memory='16GB'))
    @accelerate(mesh, cores=8, memory="16GB", timeout=300)
    def heavy_computation(data):
        return processed

    # Pattern 4: Batch call (from burla remote_parallel_map)
    results = train.map([
        {"lr": 0.01, "epochs": 100},
        {"lr": 0.05, "epochs": 200},
    ])

    # Pattern 5: Auto device placement (from HF Accelerate .to(device))
    @accelerate(mesh)
    def train_model(model, data):
        # model is automatically placed on best device
        return model(data)

    # Pattern 6: Import hook (from cudf.pandas.install())
    from gpumesh import accelerate
    accelerate.install(mesh)  # Now all @accelerate functions auto-use mesh
"""

from __future__ import annotations

import functools
import os
import threading
from typing import Any, Callable

from . import capability
from gpumesh.ansi import safe_print, green, yellow, red, bold, dim


# Adaptive polling: check often at first so short tasks return quickly, then
# back off so long-running ones don't hammer the coordinator.
POLL_MIN_INTERVAL = 0.05
POLL_MAX_INTERVAL = 1.0
POLL_BACKOFF = 1.5


class _MeshUnavailable(Exception):
    """Raised when the mesh cannot accept tasks (unreachable, no workers, etc).

    Deliberately private, and it stays private: this is internal control flow,
    not a failure mode a caller ever handles. ``_remote_call`` raises it and
    ``__call__`` catches it two frames later to take the local path — it never
    leaves this module, so there is nothing for a user to catch and no reason
    for it to appear in ``gpumesh.__all__``. The exceptions that *do* cross the
    API boundary (``PlacementUnsupportedError`` below, and the ``TimeoutError``
    ``map()`` re-raises) are listed there, because a name a user must catch and
    cannot find in the declared public API is a documentation bug.
    """
    pass


class PlacementUnsupportedError(RuntimeError):
    """Raised when the mesh object cannot carry a placement constraint.

    A ``gpu=``/``memory=``/``cores=`` requirement is a correctness statement,
    not a preference: the whole point of ``@accelerate(mesh, gpu="A100")`` is
    that the work does NOT run on the laptop that happens to be handy. When
    the mesh object in hand predates those keywords, running the batch locally
    behind a warning gives the caller the exact outcome they asked to avoid,
    so we refuse instead and name the mesh object that cannot honour it.
    """
    pass


# Device words that name a backend rather than a product. Everything else in a
# ``gpu=`` / ``.to()`` argument is treated as a hardware model name.
_GENERIC_DEVICE_KINDS = ("cuda", "cpu", "mps", "gpu")


def device_matches(spec: str | None, device_type: str, device_name: str) -> bool:
    """Does a worker satisfy a ``gpu=`` / ``.to()`` device request?

    Two kinds of request have to work against the same worker record. A
    generic kind ("cuda") names a backend, and workers report that in their
    ``device`` field — matching it against ``device_name`` would compare it to
    a hardware string like "NVIDIA GeForce RTX 4090" and never hit. A model
    name ("A100", "RTX 4090") only ever appears inside ``device_name``, so it
    stays a substring match.

    The scheduler in db.py routes with this same function, so a requirement
    that passes validation is one some worker can actually be handed.
    """
    if not spec:
        return True
    want = spec.strip().lower()
    # "cuda:1" picks an ordinal within one machine; the mesh routes between
    # machines, so only the kind before the colon is meaningful here.
    kind = want.split(":", 1)[0]
    if kind in _GENERIC_DEVICE_KINDS:
        dtype = (device_type or "").strip().lower()
        if kind == "gpu":
            return dtype in ("cuda", "mps")
        return dtype == kind
    return want in (device_name or "").lower()


def _parse_memory_mb(mem_str: str) -> float:
    """Parse a memory string like '16GB' or '512MB' into MB."""
    s = mem_str.strip().upper()
    if s.endswith("GB"):
        try:
            return float(s[:-2]) * 1024.0
        except ValueError:
            pass
    if s.endswith("MB"):
        try:
            return float(s[:-2])
        except ValueError:
            pass
    if s.endswith("B"):
        try:
            return float(s[:-1]) / (1024**2)
        except ValueError:
            pass
    try:
        return float(s) / (1024**2)
    except ValueError:
        raise ValueError(
            f"Cannot parse memory string: {mem_str!r}. "
            f"Use formats like '16GB', '512MB', or a numeric byte value."
        )


def _local_kwargs(params: dict) -> dict:
    """The kwargs a payload turns into when ``.map()`` runs it on this machine.

    ``cost`` is a scheduler hint. It rides at the *payload's* top level, next
    to the placement hints, and it is read by the coordinator to weight the
    task — it is never an argument for the user's function. ``GPUMesh.distribute``
    has stripped it since 1.1.0 (``{k: v for k, v in p.items() if k != "cost"}``
    in api.py), so on the mesh path the function only ever sees its own
    parameters, and the worker reinforces that by executing ``payload["_params"]``
    and nothing else.

    The local fallbacks in ``map()`` used to hand the raw payload straight to
    the function, so a payload annotated the way examples/payloads.json and the
    docs teach worked on the mesh and raised ``unexpected keyword argument
    'cost'`` the instant the mesh was unavailable. That is exactly backwards:
    the fallback exists so code keeps working when the mesh is not there, and a
    fallback that only works when the thing it replaces is available is not a
    fallback. Strip it here so a mesh call and a local call take identical
    arguments — which is the property the fallback is supposed to have.

    Only ``cost`` is stripped. ``gpu``/``gpu_memory_mb``/``cpu_cores`` are
    written at the payload top level by distribute() *from the decorator's own
    keywords*, so they never come out of a user payload; a key of that name in
    a ``.map()`` payload lands in ``_params`` and reaches the function on the
    mesh path exactly as it does here. Dropping them locally would invent a new
    asymmetry rather than remove one.

    Not used by ``__call__``: there the payload is built from the caller's own
    arguments, so a function that genuinely declares a ``cost`` parameter must
    keep receiving it on both paths.
    """
    return {k: v for k, v in params.items() if k != "cost"}


def _reported_capacity(device: dict, *keys: str) -> float | None:
    """The capacity a device record actually reports, or None if it reports none.

    ``/api/devices`` on a 1.3.0 coordinator carries none of the capacity keys
    at all. Reading them with ``d.get(...) or 0`` collapsed that silence into
    a hard zero, so every ``memory=``/``cores=`` requirement became
    unsatisfiable the moment a new client met an old coordinator — which is
    the normal state of the world halfway through a rollout. An absent key
    means "unknown", and unknown capacity must not fail validation: the
    scheduler is the real enforcement point, and refusing here only stops the
    task from ever reaching it.

    A key that IS present is a measurement, and the first one present wins
    outright — there is no falling through to the next key behind it. The old
    truthiness chain read a reported ``gpu_memory_free_mb: 0`` as if the worker
    had said nothing and answered with ``gpu_memory_total_mb``, so a fully
    occupied 24 GB card satisfied ``memory="8GB"`` and the work was routed to
    the one device in the mesh with no room for it. ``gpumesh/torch.py`` fixed
    exactly this in ``_reported_free_memory_mb``.

    The asymmetry that used to justify keeping the chain here — accelerate
    *validates a request* and must not reject a mesh it cannot measure, while
    torch *chooses a device* and must not pick one it cannot verify — is sound,
    and it is preserved: an absent key still returns None and still passes.
    But it covers the absent case and only the absent case. A worker that
    explicitly answers "0 MB free" has been measured, and it has none; reading
    that as unknown is not caution, it is ignoring the measurement.

    A present-but-``None`` value counts as absent, and falls through to the
    next key. JSON ``null`` is the only way a coordinator can ever say "the
    key exists in my response shape but I have no reading for it" — and today
    it cannot say that at all: ``db.list_devices()`` wraps free VRAM in
    ``COALESCE(s.gpu_memory_free_mb, 0.0)``, so a worker that has registered
    but not yet sent its first heartbeat is reported as 0 free rather than as
    unknown. Honouring ``None`` costs nothing against a current coordinator
    (which never sends one) and is what a coordinator that learns to
    distinguish the two cases will send.
    """
    for key in keys:
        if key not in device:
            continue
        value = device[key]
        if value is not None:
            return value
    return None


class AcceleratedFunction:
    """Wrapper that transparently routes execution to local best device
    or distributes across the mesh."""

    def __init__(self, fn: Callable, mesh: Any, gpu: str | None = None,
                 cores: int | None = None, memory: str | None = None,
                 timeout: float | None = None):
        functools.update_wrapper(self, fn)
        self._fn = fn
        self._mesh = mesh
        self._gpu = gpu
        self._cores = cores
        self._memory = memory
        self._timeout = timeout
        self.__wrapped__ = fn

    def _memory_hint_mb(self) -> float | None:
        """The ``memory=`` requirement in MB, or None when it was not given."""
        return _parse_memory_mb(self._memory) if self._memory else None

    def _validate_resources(self) -> None:
        """Validate that at least one mesh worker can satisfy resource requirements."""
        if self._gpu is None and self._cores is None and self._memory is None:
            return
        if self._mesh is None:
            return

        try:
            devices = self._mesh.devices()
        except Exception as exc:
            import warnings
            warnings.warn(
                f"[accelerate] could not validate resources: {exc}",
                stacklevel=2,
            )
            return

        alive = [d for d in devices if d.get("status") == "alive"]
        if not alive:
            return

        required_memory_mb = self._memory_hint_mb()

        for d in alive:
            gpu_match = device_matches(
                self._gpu, d.get("device", ""), d.get("device_name", "")
            )

            # An unreported capacity is unknown, not zero — see
            # _reported_capacity. Unknown passes validation and leaves the
            # decision to the scheduler.
            #
            # This check must never be more permissive than db.py's
            # ``_worker_can_run``, or a task passes validation here and is then
            # skipped by every worker forever — the shape of bug that produced
            # ``fail_unsatisfiable_tasks()``. It is not: the scheduler reads
            # free-or-total (``if not my_free_mb: my_free_mb = my_total_mb``),
            # so its effective capacity is always >= the value read here, and
            # anything that passes here is leasable there. The one place the
            # two now differ is a reported zero, where this side refuses and
            # the scheduler would still fall back to total. Refusing at submit
            # time with a message naming the requirement beats leasing the task
            # onto a card with no room for it and failing inside CUDA.
            mem_match = True
            if required_memory_mb is not None:
                worker_free = _reported_capacity(
                    d, "gpu_memory_free_mb", "gpu_memory_total_mb"
                )
                mem_match = worker_free is None or worker_free >= required_memory_mb

            cores_match = True
            if self._cores is not None:
                worker_cores = _reported_capacity(d, "cpu_cores", "cpu_count")
                cores_match = worker_cores is None or worker_cores >= self._cores

            if gpu_match and mem_match and cores_match:
                return

        parts = []
        if self._gpu:
            parts.append(f"gpu={self._gpu!r}")
        if self._cores is not None:
            parts.append(f"cores={self._cores}")
        if self._memory is not None:
            parts.append(f"memory={self._memory!r}")
        req = ", ".join(parts)

        # A worker in the seconds between registering and its first heartbeat
        # reports free VRAM as 0 (the coordinator's SQL COALESCEs the missing
        # worker_stats row to 0.0) while reporting a real total, and a card
        # that is genuinely full reports exactly the same pair. The record
        # cannot tell them apart, and taking the strict reading is right —
        # a full card must not be handed the work — but one of those two
        # situations fixes itself in a few seconds and the operator has no way
        # to guess which one they are in from "no worker can satisfy". Say so.
        hint = ""
        if required_memory_mb is not None:
            unmeasured = [
                d for d in alive
                if d.get("gpu_memory_free_mb") == 0
                and (d.get("gpu_memory_total_mb") or 0) >= required_memory_mb
            ]
            if unmeasured:
                # A coordinator now reports null, not 0.0, for a worker whose
                # free VRAM nobody has measured yet, so reaching here means the
                # card was measured and is genuinely full. The older wording
                # also offered "they may have just registered" as an
                # explanation; against a current coordinator that is no longer
                # possible, and offering it sends the operator away to wait for
                # a heartbeat that has already arrived.
                hint = (
                    f" Note: {len(unmeasured)} worker(s) report 0 MB free VRAM "
                    f"but a total large enough for this request, so those GPUs "
                    f"are currently occupied rather than too small. The request "
                    f"should succeed once whatever is using them finishes."
                )

        raise ValueError(
            f"No worker can satisfy resource requirements: {req}. "
            f"Available workers: {[d.get('device_name', d.get('device', '?')) for d in alive]}. "
            f"Try removing the gpu/cores/memory constraint, or add a worker with matching resources."
            f"{hint}"
        )

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Execute on the mesh (remote if workers available) or locally.

        Routing logic (inspired by Exo/PartaGPU):
        - GPUMESH_LOCAL=1 → force local
        - Mesh has workers → submit as a single-task job, poll for result
        - Mesh unreachable or no workers → fall back to local best device
        """
        if os.environ.get("GPUMESH_LOCAL") == "1":
            return self._fn(*args, **kwargs)

        self._validate_resources()

        # Try remote execution if mesh is available
        if self._mesh is not None and os.environ.get("GPUMESH_LOCAL") != "1":
            try:
                return self._remote_call(args, kwargs)
            except _MeshUnavailable:
                pass  # fall through to local

        # Local fallback
        args, kwargs = self._auto_place(args, kwargs)

        device = None
        try:
            device = capability.probe_device()
        except Exception:
            pass

        if os.environ.get("GPUMESH_VERBOSE") == "1":
            device_name = device["device_name"] if device else "unknown"
            target = self._gpu or "auto"
            safe_print(yellow(f"[accelerate] running locally on device: {device_name} (target: {target})"))

        return self._fn(*args, **kwargs)

    def _remote_call(self, args: tuple, kwargs: dict) -> Any:
        """Submit a single task to the mesh and wait for the result.

        Uses the same job submission path as .map() but with a single task.
        Falls back to local if mesh is unreachable or has no workers.
        """
        import json as _json
        import time as _time
        import urllib.error

        # Check if mesh has any alive workers
        try:
            workers = self._mesh.workers()
        except Exception:
            raise _MeshUnavailable("cannot reach mesh")

        alive = [w for w in workers if w.get("alive", False)]
        if not alive:
            raise _MeshUnavailable("no alive workers")

        # Merge args into kwargs using function signature
        call_kwargs = self._merge_args_kwargs(args, kwargs)

        # Serialize and submit
        try:
            from . import serializer
            func_bytes = serializer.serialize_function(self._fn)
        except Exception:
            raise _MeshUnavailable("cannot serialize function")

        payload = {
            "_func": func_bytes,
            "_params": call_kwargs,
            "_task_index": 0,
        }
        # Scheduler hints: the coordinator only leases this task to a worker
        # that matches them, so gpu="A100" really does keep the task off the
        # CPU laptop that polls first.
        if self._gpu:
            payload["gpu"] = self._gpu
        memory_mb = self._memory_hint_mb()
        if memory_mb is not None:
            payload["gpu_memory_mb"] = memory_mb
        # ``cores=`` was validated but never written, so the coordinator never
        # saw it and a cores=64 task leased happily to a two-core laptop. The
        # scheduler filters on the key name ``cpu_cores`` (the same name the
        # worker registers its capacity under), not on ``cores``.
        if self._cores is not None:
            payload["cpu_cores"] = self._cores

        try:
            job_id = self._mesh.submit(
                name=f"accelerate:{self._fn.__name__}",
                script="__gpumesh_function__",
                payloads=[payload],
            )
        except Exception:
            raise _MeshUnavailable("cannot submit job")

        if os.environ.get("GPUMESH_VERBOSE") == "1":
            safe_print(green(f"[accelerate] submitted to mesh: job={job_id}, "
                  f"workers={len(alive)}"))

        # Poll for the result, starting fast and backing off.
        #
        # A fixed one-second poll put a ~2s floor under every mesh call, which
        # dwarfs the runtime of a short function and made the mesh feel slower
        # than running locally. Short functions now return in well under a
        # second, while long ones settle to a slow poll that costs the
        # coordinator almost nothing.
        timeout = self._timeout or 300.0
        start = _time.time()
        delay = POLL_MIN_INTERVAL
        # Remember why the polls stopped answering.
        #
        # A coordinator that goes away mid-task is, from the timeout message
        # alone, indistinguishable from a task that is merely slow — and it is
        # a real case rather than a hypothetical one. A coordinator shutting
        # down answers 503 and then stops listening, so a client polling
        # through that window learns nothing for the rest of its timeout and is
        # then told "mesh task did not complete", which blames the task and
        # sends the reader to look at their own function. Swallowing the poll
        # errors is still right — a single dropped packet must not fail a
        # five-minute job — but discarding them entirely is what makes the
        # ending unreadable.
        poll_failures = 0
        last_poll_error = None
        while _time.time() - start < timeout:
            try:
                status = self._mesh.status(job_id)
            except Exception as exc:
                poll_failures += 1
                last_poll_error = exc
                _time.sleep(delay)
                delay = min(delay * POLL_BACKOFF, POLL_MAX_INTERVAL)
                continue
            poll_failures = 0
            last_poll_error = None

            if status.get("finished"):
                tasks = status.get("tasks", [])
                if tasks:
                    t = tasks[0]
                    if t["status"] == "done":
                        from . import serializer

                        raw = t.get("result")
                        if isinstance(raw, dict):
                            # Internal ordering key; never reaches user code.
                            raw.pop("_task_index", None)
                        # Unwrap the envelope so a mesh call returns exactly
                        # what a local call would have returned.
                        #
                        # Under strict mode this raises UntrustedResultError
                        # out of the decorated call, on purpose. A decorated
                        # function is supposed to be indistinguishable from
                        # the local one, so the honest failure is an exception
                        # at the call site rather than a placeholder object
                        # that the caller's next line would silently compute
                        # with.
                        result = {} if raw is None else serializer.decode_result(raw)
                        if os.environ.get("GPUMESH_VERBOSE") == "1":
                            safe_print(green(f"[accelerate] mesh result: {result}"))
                        return result
                    raise RuntimeError(
                        f"mesh task failed: {t.get('error') or t['status']}"
                    )
                # A finished job with no tasks means the job record was lost
                # (coordinator restarted with a fresh database, say). Report
                # that rather than blocking until the timeout expires.
                raise RuntimeError(
                    f"mesh job {job_id} finished with no tasks — the "
                    f"coordinator may have restarted with a fresh database"
                )

            _time.sleep(delay)
            delay = min(delay * POLL_BACKOFF, POLL_MAX_INTERVAL)

        if poll_failures:
            raise TimeoutError(
                f"mesh task did not complete within {timeout}s, and the last "
                f"{poll_failures} status poll(s) failed with "
                f"{type(last_poll_error).__name__}: {last_poll_error}. The "
                f"coordinator became unreachable while the task was "
                f"outstanding, so this is most likely a coordinator that went "
                f"away (shut down, restarted, network dropped) rather than a "
                f"slow task."
            ) from last_poll_error

        raise TimeoutError(
            f"mesh task did not complete within {timeout}s"
        )

    def _merge_args_kwargs(self, args: tuple, kwargs: dict) -> dict:
        """Merge positional args into kwargs using function signature."""
        import inspect
        sig = inspect.signature(self._fn)
        params = list(sig.parameters.keys())
        merged = dict(kwargs)
        for i, arg in enumerate(args):
            if i < len(params):
                merged[params[i]] = arg
            else:
                merged[f"_positional_{i}"] = arg
        return merged

    def _auto_place(self, args: tuple, kwargs: dict) -> tuple:
        """Auto-place PyTorch models on best device (HF Accelerate pattern)."""
        try:
            import torch
        except ImportError:
            return args, kwargs

        device = self._get_torch_device()
        if device is None:
            return args, kwargs

        new_args = []
        for arg in args:
            if isinstance(arg, torch.nn.Module):
                arg = arg.to(device)
            new_args.append(arg)

        new_kwargs = {}
        for k, v in kwargs.items():
            if isinstance(v, torch.nn.Module):
                v = v.to(device)
            new_kwargs[k] = v

        return tuple(new_args), new_kwargs

    @staticmethod
    def _cuda_ordinal(torch, spec: str) -> int:
        """The CUDA index in a normalised "cuda:N" spec, checked against reality.

        torch.device("cuda:5") is happy to build a device object for a GPU
        that does not exist; the failure only surfaces much later, at the
        first ``.to()``, as an invalid-device-ordinal error a long way from
        the ``gpu="cuda:5"`` that caused it. Checking here turns that into a
        message naming the request and the machine's actual GPU count.
        """
        raw = spec.split(":", 1)[1]
        try:
            index = int(raw)
        except ValueError:
            raise ValueError(
                f"Cannot parse device ordinal in gpu={spec!r}. "
                f"Use a form like 'cuda:0'."
            )
        count = torch.cuda.device_count()
        if index < 0 or index >= count:
            raise ValueError(
                f"gpu={spec!r} asks for CUDA device {index}, but this machine "
                f"has {count} CUDA device(s) (valid: cuda:0 to cuda:{count - 1}). "
                f"Use a valid ordinal, or plain 'cuda' to let gpumesh choose."
            )
        return index

    def _get_torch_device(self):
        """Get the best torch device (from ezpz setup_torch pattern)."""
        try:
            import torch
        except ImportError:
            return None

        if self._gpu:
            # Normalise once, up front. ``gpu=`` is user-facing text and
            # "CUDA:1" is as reasonable a thing to type as "cuda:1", but
            # torch.device() is case-sensitive and rejects the former outright.
            # Everything below works from the normalised ``spec``; the raw
            # ``self._gpu`` only ever appears in error messages, where echoing
            # what the user actually wrote is the useful thing.
            spec = self._gpu.strip().lower()
            # A generic kind asks for a backend, so honour it directly instead
            # of hunting for it in the hardware name (no GPU is called "cuda").
            kind = spec.split(":", 1)[0]
            if kind == "cpu":
                return torch.device("cpu")
            mps = getattr(torch.backends, "mps", None)
            if kind == "mps":
                return torch.device("mps") if mps and mps.is_available() else torch.device("cpu")
            if kind == "gpu" and not torch.cuda.is_available() and mps and mps.is_available():
                return torch.device("mps")
            if torch.cuda.is_available():
                if kind in _GENERIC_DEVICE_KINDS:
                    # Only "cuda:N" carries a meaningful ordinal — "gpu:1" and
                    # "mps:1" name no real device, so they collapse to the
                    # default like any other bare kind.
                    if kind == "cuda" and ":" in spec:
                        return torch.device(f"cuda:{self._cuda_ordinal(torch, spec)}")
                    return torch.device("cuda:0")
                for i in range(torch.cuda.device_count()):
                    name = torch.cuda.get_device_name(i)
                    if spec in (name or "").lower():
                        return torch.device(f"cuda:{i}")
                return torch.device("cuda:0")
            return torch.device("cpu")

        if torch.cuda.is_available():
            return torch.device("cuda:0")
        mps = getattr(torch.backends, "mps", None)
        if mps and mps.is_available():
            return torch.device("mps")
        return torch.device("cpu")

    def map(self, params_list: list[dict]) -> list[dict]:
        """Distribute across all mesh devices (burla remote_parallel_map pattern).

        Args:
            params_list: List of dicts, each dict becomes one task's kwargs.

        Returns:
            List of result dicts, one per parameter set.
        """
        if not params_list:
            return []

        if os.environ.get("GPUMESH_LOCAL") == "1":
            return [self._fn(**_local_kwargs(p)) for p in params_list]

        self._validate_resources()

        # Fast-fail: detect when no real workers are connected so we warn
        # early instead of hanging through distribute's timeout.
        # Only applies when workers() returns a real list we can inspect;
        # test mocks bypass this check.
        if self._mesh is not None:
            try:
                _workers = self._mesh.workers()
                if isinstance(_workers, list):
                    alive = [w for w in _workers if w.get("alive", False)]
                    if not alive:
                        import warnings
                        warnings.warn(
                            "[accelerate] No alive workers — falling back to local execution. "
                            "Connect a worker with `gpumesh join URL --token TOKEN` to use the mesh.",
                            stacklevel=2,
                        )
                        return [self._fn(**_local_kwargs(p)) for p in params_list]
            except Exception:
                pass  # mesh down — fall through to distribute which will also fallback

        # Carry the same routing hints a single call sends, so a mapped job is
        # spread only over workers that match gpu=/cores=/memory=. They are
        # omitted when unset, because an absent hint is what "no constraint"
        # looks like to the scheduler.
        hints = {}
        if self._gpu:
            hints["gpu"] = self._gpu
        memory_mb = self._memory_hint_mb()
        if memory_mb is not None:
            hints["gpu_memory_mb"] = memory_mb
        if self._cores is not None:
            hints["cores"] = self._cores

        # Check the mesh object's signature BEFORE calling it.
        #
        # The old code sent the hints optimistically and let a blanket
        # ``except Exception`` mop up. Against an older mesh object that
        # raised ``TypeError: distribute() got an unexpected keyword argument
        # 'gpu'``, which the handler read as "mesh trouble" and answered by
        # running the whole batch locally behind a warning — so
        # ``@accelerate(mesh, gpu="A100").map(...)`` *guaranteed* the work
        # never touched an A100. Pattern-matching the exception text is not a
        # fix either: a TypeError raised inside the user's own function during
        # the local fallback is indistinguishable from a signature mismatch,
        # and mistaking one for the other is how this class of bug returns.
        # Asking the object what it accepts leaves nothing to guess.
        unsupported = self._unsupported_hint_kwargs(hints)
        if unsupported:
            raise PlacementUnsupportedError(
                f"{type(self._mesh).__name__}.distribute() does not accept "
                f"{', '.join(sorted(unsupported))}, so the placement "
                f"constraint on {self._fn.__name__!r} cannot be honoured. "
                f"Refusing to run locally instead — that is the outcome the "
                f"constraint exists to prevent. Upgrade the mesh client, or "
                f"drop the gpu/cores/memory constraint to run anywhere."
            )

        try:
            return self._mesh.distribute(
                function=self._fn,
                params=params_list,
                timeout=self._timeout or 300.0,
                **hints,
            )
        except TimeoutError:
            # A job that no worker could take is not a job to quietly re-run
            # on this laptop after five minutes. The caller waited out the
            # whole timeout precisely because they wanted mesh execution;
            # swallowing it here hides an unschedulable job behind a warning
            # and a very slow "success".
            #
            # The line the contract is actually drawn on is *acceptance*, and
            # it is not arbitrary. ``distribute()`` raises TimeoutError only
            # from its own poll loop, which it never reaches unless
            # ``POST /api/jobs`` already succeeded — every pre-acceptance
            # network failure comes out as GPUMeshError instead, and falls
            # back below. So by the time this fires the coordinator owns a job
            # that may still be pending, may be running on a worker right now,
            # and may finish after we return. Running the batch locally on top
            # of that executes the same work twice, which for anything with a
            # side effect is worse than any error. Documented in
            # docs/stability.md §1 under "the fallback contract".
            raise
        except Exception as exc:
            # Everything else — an unreachable coordinator, a refused
            # connection, a job the coordinator never accepted — keeps the
            # documented behaviour of falling back to local execution, which
            # is what makes a mesh optional.
            import warnings
            warnings.warn(
                f"[accelerate] mesh.distribute() failed: {exc}. "
                f"Falling back to local execution.",
                stacklevel=2,
            )
            return [self._fn(**_local_kwargs(p)) for p in params_list]

    def _unsupported_hint_kwargs(self, hints: dict) -> list[str]:
        """Which placement keywords this mesh object's distribute() can't take.

        Returns an empty list whenever we cannot prove a keyword is missing:
        a ``**kwargs`` catch-all, a callable with no introspectable signature
        (mocks, C-level callables, some proxies), or no mesh at all. Guessing
        "unsupported" on an object we cannot read would break working setups,
        whereas guessing "supported" only lands us back at the call, where a
        real TypeError still surfaces.
        """
        if not hints or self._mesh is None:
            return []

        import inspect
        distribute = getattr(self._mesh, "distribute", None)
        if distribute is None:
            return []
        try:
            sig = inspect.signature(distribute)
        except (TypeError, ValueError):
            return []

        params = list(sig.parameters.values())
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
            return []
        accepted = {
            p.name for p in params
            if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                          inspect.Parameter.KEYWORD_ONLY)
        }
        return [k for k in hints if k not in accepted]

    def to(self, device: str) -> AcceleratedFunction:
        """Send function to specific device (Runhouse .to(gpu) pattern).

        Args:
            device: Device target ("cuda", "cpu", "mps", or specific like "cuda:1")

        Returns:
            New AcceleratedFunction bound to that device.
        """
        return AcceleratedFunction(
            self._fn, self._mesh,
            gpu=device, cores=self._cores,
            memory=self._memory, timeout=self._timeout,
        )


# -- Global install() (cudf.pandas pattern) ---------------------------------

_installed_mesh = None
_install_lock = threading.Lock()


def install(mesh: Any = None) -> None:
    """Install gpumesh as a transparent import hook (cudf.pandas pattern).

    After calling install(), all @accelerate-decorated functions will
    automatically use the mesh for distribution.

    Usage:
        from gpumesh import accelerate
        accelerate.install(mesh)

        @accelerate  # No mesh argument needed!
        def train(lr, epochs):
            return {"accuracy": 0.95}
    """
    global _installed_mesh
    with _install_lock:
        _installed_mesh = mesh


def accelerate(mesh_or_fn: Any = None, *, gpu: str | None = None,
               cores: int | None = None, memory: str | None = None,
               timeout: float | None = None) -> Any:
    """Decorator that makes mesh resources transparent to user code.

    Can be used as:
        @accelerate(mesh)  # With mesh argument
        def train(...): ...

        @accelerate(mesh, gpu="A100", cores=8, memory="16GB", timeout=300)
        def train(...): ...

        # Or after install():
        accelerate.install(mesh)
        @accelerate  # Without mesh argument
        def train(...): ...
    """
    # @accelerate (no arguments, no parentheses) -- use installed mesh
    if mesh_or_fn is not None and callable(mesh_or_fn) and not hasattr(mesh_or_fn, 'distribute'):
        fn = mesh_or_fn
        if _installed_mesh is None:
            raise RuntimeError(
                "No mesh provided. Either pass mesh to @accelerate(mesh) "
                "or call accelerate.install(mesh) first."
            )
        return AcceleratedFunction(fn, _installed_mesh)

    # @accelerate(mesh) or @accelerate(mesh, gpu="A100", cores=8)
    mesh = mesh_or_fn

    def decorator(fn: Callable) -> AcceleratedFunction:
        return AcceleratedFunction(fn, mesh, gpu=gpu, cores=cores,
                                   memory=memory, timeout=timeout)

    return decorator


# ``from gpumesh import accelerate`` binds the decorator function, not this
# module, so the documented ``accelerate.install(mesh)`` has to reach the
# helper through the function. Same trick mesh.py uses for mesh_fn.connect.
accelerate.install = install
