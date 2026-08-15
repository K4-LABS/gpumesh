"""Tests for ``gpumesh.torch`` — the optional PyTorch convenience layer.

torch is an *optional* dependency, so this module has two jobs and both are
tested here:

1. It must import and degrade sensibly on a machine with no torch at all.
   That is simulated by putting ``None`` in ``sys.modules["torch"]``, which is
   exactly what the import system turns into an ``ImportError``.

2. Its torch-present paths must be correct. Those are exercised against a
   *fake* torch module injected with ``monkeypatch.setitem``, rather than a
   real install. That keeps the file runnable on every platform (torch is not
   installed in CI) and, just as importantly, deterministic on a developer
   machine that *does* have torch and a real GPU — the fake shadows the real
   module for the duration of one test and is restored afterwards.

The module caches ``torch``/``nn`` in globals after the first successful
import, so every fixture here resets those globals; without that, whichever
test ran first would decide what all the others saw.
"""

import socket
import subprocess
import sys
import types

import pytest

import gpumesh.torch as gtorch


LOCAL_HOST = socket.gethostname()
REMOTE_HOST = "some-other-machine"


# ── fake torch ─────────────────────────────────────────────────────────────

class FakeDevice:
    """Stand-in for ``torch.device``; compares by the spec string."""

    def __init__(self, spec):
        self.spec = str(spec)

    def __eq__(self, other):
        return isinstance(other, FakeDevice) and other.spec == self.spec

    def __hash__(self):
        return hash(self.spec)

    def __repr__(self):
        return f"FakeDevice({self.spec!r})"


class FakeDataParallel:
    def __init__(self, module):
        self.module = module


class FakeModel:
    """Records the placement calls ``auto()`` makes on it."""

    def __init__(self):
        self.cuda_calls = 0
        self.moved_to = []

    def cuda(self):
        self.cuda_calls += 1
        return self

    def to(self, target):
        self.moved_to.append(target)
        return self


def _build_fake_torch(*, cuda_available=False, cuda_count=0,
                      mps_available=False, has_mps_attr=True,
                      mem_free_bytes=None, mem_error=None):
    torch_mod = types.ModuleType("torch")
    torch_mod.device = FakeDevice

    def mem_get_info(index=0):
        if mem_error is not None:
            raise mem_error
        if mem_free_bytes is None:
            raise RuntimeError("mem_get_info is not supported here")
        return (mem_free_bytes, mem_free_bytes)

    torch_mod.cuda = types.SimpleNamespace(
        is_available=lambda: cuda_available,
        device_count=lambda: cuda_count,
        mem_get_info=mem_get_info,
    )

    backends = types.SimpleNamespace()
    if has_mps_attr:
        backends.mps = types.SimpleNamespace(is_available=lambda: mps_available)
    torch_mod.backends = backends

    nn_mod = types.ModuleType("torch.nn")
    nn_mod.DataParallel = FakeDataParallel
    torch_mod.nn = nn_mod
    return torch_mod, nn_mod


@pytest.fixture
def install_torch(monkeypatch):
    """Return a callable that installs a fake torch and primes the module."""

    def _install(**kwargs):
        torch_mod, nn_mod = _build_fake_torch(**kwargs)
        monkeypatch.setitem(sys.modules, "torch", torch_mod)
        monkeypatch.setitem(sys.modules, "torch.nn", nn_mod)
        # Clear the cached globals so _ensure_torch actually re-imports, and
        # let monkeypatch put them back at teardown.
        monkeypatch.setattr(gtorch, "torch", None)
        monkeypatch.setattr(gtorch, "nn", None)
        gtorch._ensure_torch()
        return torch_mod

    return _install


@pytest.fixture
def no_torch(monkeypatch):
    """Make this process look like one where torch was never installed."""
    monkeypatch.setattr(gtorch, "torch", None)
    monkeypatch.setattr(gtorch, "nn", None)
    # A None entry in sys.modules makes ``import torch`` raise ImportError —
    # the same exception a genuinely missing package raises.
    monkeypatch.setitem(sys.modules, "torch", None)
    monkeypatch.setitem(sys.modules, "torch.nn", None)


# ── fake mesh ──────────────────────────────────────────────────────────────

class FakeMesh:
    def __init__(self, devices=(), gpu_count=0, devices_error=None):
        self._devices = list(devices)
        self._gpu_count = gpu_count
        self._devices_error = devices_error
        self.devices_calls = 0
        self.gpu_count_calls = 0

    def devices(self):
        self.devices_calls += 1
        if self._devices_error is not None:
            raise self._devices_error
        return list(self._devices)

    def gpu_count(self):
        self.gpu_count_calls += 1
        return self._gpu_count


def _entry(**kwargs):
    """A mesh inventory row with sane defaults."""
    row = {
        "id": 1,
        "hostname": LOCAL_HOST,
        "device": "cpu",
        "status": "alive",
        "score": 1.0,
    }
    row.update(kwargs)
    return row


# ── torch missing ──────────────────────────────────────────────────────────

class TestWithoutTorch:
    """Every entry point must fail with a helpful ImportError, not a crash."""

    def test_module_import_does_not_import_torch(self):
        """Importing gpumesh.torch must not pull torch in.

        Run in a subprocess so the assertion is about a pristine interpreter
        rather than whatever the rest of this session already imported.
        """
        code = (
            "import gpumesh.torch as t;"
            "print(t.torch is None, t.nn is None)"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.split() == ["True", "True"]

    def test_ensure_torch_raises_install_hint(self, no_torch):
        with pytest.raises(ImportError, match="pip install torch"):
            gtorch._ensure_torch()

    def test_the_original_import_error_is_preserved(self, no_torch):
        """A broken CUDA install raises ImportError from a failed DLL load.

        Reported as a bare "PyTorch is required ... pip install torch" it looks
        identical to torch not being installed, and sends the user off to
        reinstall a package they already have. Both the chain and the original
        text have to survive.
        """
        with pytest.raises(ImportError) as excinfo:
            gtorch._ensure_torch()
        assert excinfo.value.__cause__ is not None
        assert isinstance(excinfo.value.__cause__, ImportError)
        # The cause's own message is quoted, because that is what a user reads
        # in a one-line CLI error without a traceback.
        assert str(excinfo.value.__cause__) in str(excinfo.value)

    def test_device_requires_torch(self, no_torch):
        with pytest.raises(ImportError, match="PyTorch is required"):
            gtorch.device(FakeMesh())

    def test_auto_requires_torch(self, no_torch):
        with pytest.raises(ImportError, match="PyTorch is required"):
            gtorch.auto(FakeMesh(), FakeModel())

    def test_setup_torch_requires_torch(self, no_torch):
        with pytest.raises(ImportError, match="PyTorch is required"):
            gtorch.setup_torch()

    def test_get_device_requires_torch(self, no_torch):
        with pytest.raises(ImportError, match="PyTorch is required"):
            gtorch.get_device()

    def test_device_count_works_without_torch(self, no_torch):
        """device_count only asks the mesh, so it must not need torch."""
        mesh = FakeMesh(gpu_count=7)
        assert gtorch.device_count(mesh) == 7
        assert mesh.gpu_count_calls == 1


class TestPublicSurface:
    def test_remote_device_error_is_a_runtime_error(self):
        assert issubclass(gtorch.RemoteDeviceError, RuntimeError)

    def test_exposed_lazily_from_the_package(self):
        """``gpumesh.torch`` is documented; the lazy __getattr__ must serve it."""
        import gpumesh

        assert getattr(gpumesh, "torch") is gtorch

    def test_ensure_torch_caches_the_import(self, install_torch, monkeypatch):
        install_torch()
        # Break importing torch entirely. A second _ensure_torch must not care,
        # because it short-circuits on the cached globals.
        monkeypatch.setitem(sys.modules, "torch", None)
        gtorch._ensure_torch()
        assert gtorch.torch is not None


# ── device() ───────────────────────────────────────────────────────────────

class TestDevice:
    def test_empty_mesh_falls_back_to_cpu(self, install_torch):
        install_torch()
        assert gtorch.device(FakeMesh()) == FakeDevice("cpu")

    def test_dead_devices_are_ignored(self, install_torch):
        install_torch()
        mesh = FakeMesh([_entry(status="dead", device="cuda")])
        assert gtorch.device(mesh) == FakeDevice("cpu")

    def test_local_cpu_entry(self, install_torch):
        install_torch()
        mesh = FakeMesh([_entry(device="cpu")])
        assert gtorch.device(mesh) == FakeDevice("cpu")

    def test_remote_entry_raises_remote_device_error(self, install_torch):
        install_torch()
        mesh = FakeMesh([_entry(hostname=REMOTE_HOST, device="cuda")])
        with pytest.raises(gtorch.RemoteDeviceError, match="is remote"):
            gtorch.device(mesh)

    def test_remote_error_names_mesh_distribute(self, install_torch):
        """The error has to point at the thing that *does* work remotely."""
        install_torch()
        mesh = FakeMesh([_entry(hostname=REMOTE_HOST, device="cuda")])
        with pytest.raises(gtorch.RemoteDeviceError) as excinfo:
            gtorch.device(mesh)
        assert "mesh.distribute()" in str(excinfo.value)

    def test_out_of_range_index_wraps(self, install_torch):
        install_torch()
        mesh = FakeMesh([
            _entry(id=1, device="cpu"),
            _entry(id=2, hostname=REMOTE_HOST, device="cpu"),
        ])
        # index 2 wraps to 0 (the local entry), index 3 wraps to 1 (remote).
        assert gtorch.device(mesh, 2) == FakeDevice("cpu")
        with pytest.raises(gtorch.RemoteDeviceError):
            gtorch.device(mesh, 3)

    def test_negative_index_is_rejected(self, install_torch):
        """-1 used to skip the wrap and index backwards.

        ``index >= len(alive)`` is false for a negative index, so Python's own
        negative indexing took over and quietly returned the *last* entry —
        a different device from the one the caller named, and on this mesh a
        remote one. An index that went negative by arithmetic is a caller bug;
        say so instead of picking something.
        """
        install_torch()
        mesh = FakeMesh([
            _entry(id=1, device="cpu"),
            _entry(id=2, hostname=REMOTE_HOST, device="cpu"),
        ])
        with pytest.raises(ValueError, match="must be >= 0"):
            gtorch.device(mesh, -1)

    def test_negative_index_is_rejected_before_the_mesh_is_asked(self, install_torch):
        """The argument is wrong whatever the inventory happens to contain."""
        install_torch()
        mesh = FakeMesh([_entry(device="cpu")])
        with pytest.raises(ValueError):
            gtorch.device(mesh, -3)
        assert mesh.devices_calls == 0

    def test_stale_inventory_index_is_refused(self, install_torch):
        """cuda:N with N past torch's own count must not reach torch.

        The local CUDA index is derived from the inventory's *ordering*, which
        nothing ties to torch.cuda's enumeration. A row left over from a
        machine that has since lost a card yields an index this process cannot
        address; fail with both numbers rather than letting torch raise an
        opaque invalid-device error later.
        """
        install_torch(cuda_available=True, cuda_count=1)
        mesh = FakeMesh([
            _entry(id=1, device="cuda"),
            _entry(id=2, device="cuda"),   # stale: this box has one GPU now
        ])
        with pytest.raises(RuntimeError) as excinfo:
            gtorch.device(mesh, 1)
        message = str(excinfo.value)
        assert "cuda:1" in message      # what the inventory asked for
        assert "1 CUDA device" in message   # what actually exists

    def test_local_cuda_without_cuda_runtime_raises(self, install_torch):
        install_torch(cuda_available=False)
        mesh = FakeMesh([_entry(device="cuda")])
        with pytest.raises(RuntimeError, match="CUDA is not available"):
            gtorch.device(mesh)

    def test_local_cuda_maps_to_cuda_zero(self, install_torch):
        install_torch(cuda_available=True, cuda_count=1)
        mesh = FakeMesh([_entry(id=1, device="cuda")])
        assert gtorch.device(mesh) == FakeDevice("cuda:0")

    def test_second_local_cuda_maps_to_cuda_one(self, install_torch):
        install_torch(cuda_available=True, cuda_count=2)
        mesh = FakeMesh([
            _entry(id=1, device="cuda"),
            _entry(id=2, device="cuda"),
        ])
        assert gtorch.device(mesh, 1) == FakeDevice("cuda:1")

    def test_remote_cuda_entries_do_not_shift_the_local_index(self, install_torch):
        """A remote GPU sitting between two local ones must not be counted."""
        install_torch(cuda_available=True, cuda_count=2)
        mesh = FakeMesh([
            _entry(id=1, hostname=REMOTE_HOST, device="cuda"),
            _entry(id=2, device="cuda"),
            _entry(id=3, device="cuda"),
        ])
        assert gtorch.device(mesh, 2) == FakeDevice("cuda:1")

    def test_local_mps(self, install_torch):
        install_torch(mps_available=True)
        mesh = FakeMesh([_entry(device="mps")])
        assert gtorch.device(mesh) == FakeDevice("mps")

    def test_local_mps_when_unavailable_raises(self, install_torch):
        install_torch(mps_available=False)
        mesh = FakeMesh([_entry(device="mps")])
        with pytest.raises(RuntimeError, match="MPS is not available"):
            gtorch.device(mesh)

    def test_local_mps_on_a_torch_without_mps_backend_raises(self, install_torch):
        """Old torch builds have no ``torch.backends.mps`` attribute at all."""
        install_torch(has_mps_attr=False)
        mesh = FakeMesh([_entry(device="mps")])
        with pytest.raises(RuntimeError, match="MPS is not available"):
            gtorch.device(mesh)

    def test_unknown_device_type_raises(self, install_torch):
        install_torch()
        mesh = FakeMesh([_entry(device="tpu")])
        with pytest.raises(RuntimeError, match="Unsupported local mesh device type: tpu"):
            gtorch.device(mesh)


# ── auto() ─────────────────────────────────────────────────────────────────

class TestAuto:
    def test_multi_gpu_uses_data_parallel(self, install_torch):
        install_torch(cuda_available=True, cuda_count=2)
        model = FakeModel()
        wrapped = gtorch.auto(FakeMesh(), model)
        assert isinstance(wrapped, FakeDataParallel)
        assert wrapped.module is model
        assert model.cuda_calls == 1

    def test_single_gpu_moves_the_model(self, install_torch):
        install_torch(cuda_available=True, cuda_count=1)
        model = FakeModel()
        assert gtorch.auto(FakeMesh(), model) is model
        assert model.cuda_calls == 1

    def test_mps_moves_the_model(self, install_torch):
        install_torch(cuda_available=False, mps_available=True)
        model = FakeModel()
        assert gtorch.auto(FakeMesh(), model) is model
        assert model.moved_to == ["mps"]
        assert model.cuda_calls == 0

    def test_cpu_only_returns_the_model_untouched(self, install_torch):
        install_torch()
        model = FakeModel()
        assert gtorch.auto(FakeMesh(), model) is model
        assert model.cuda_calls == 0
        assert model.moved_to == []

    def test_cuda_available_but_zero_devices_falls_through(self, install_torch):
        """is_available() True with device_count() 0 must not call .cuda()."""
        install_torch(cuda_available=True, cuda_count=0)
        model = FakeModel()
        assert gtorch.auto(FakeMesh(), model) is model
        assert model.cuda_calls == 0

    def test_remote_mesh_devices_are_not_used(self, install_torch):
        """auto() is local-only; a mesh full of remote GPUs changes nothing."""
        install_torch()
        mesh = FakeMesh([_entry(hostname=REMOTE_HOST, device="cuda")])
        model = FakeModel()
        assert gtorch.auto(mesh, model) is model
        assert mesh.devices_calls == 0


# ── device_count() ─────────────────────────────────────────────────────────

class TestDeviceCount:
    def test_delegates_to_mesh_gpu_count(self, install_torch):
        install_torch()
        mesh = FakeMesh(gpu_count=3)
        assert gtorch.device_count(mesh) == 3

    def test_zero_gpu_mesh(self, install_torch):
        install_torch()
        assert gtorch.device_count(FakeMesh(gpu_count=0)) == 0


# ── setup_torch() ──────────────────────────────────────────────────────────

class TestSetupTorchWithoutMesh:
    def test_prefers_cuda(self, install_torch):
        install_torch(cuda_available=True, cuda_count=1)
        assert gtorch.setup_torch() == "cuda:0"

    def test_falls_back_to_mps(self, install_torch):
        install_torch(cuda_available=False, mps_available=True)
        assert gtorch.setup_torch() == "mps"

    def test_falls_back_to_cpu(self, install_torch):
        install_torch()
        assert gtorch.setup_torch() == "cpu"

    def test_cpu_when_torch_has_no_mps_backend(self, install_torch):
        install_torch(has_mps_attr=False)
        assert gtorch.setup_torch() == "cpu"

    def test_min_memory_satisfied_keeps_cuda(self, install_torch):
        install_torch(cuda_available=True, mem_free_bytes=8 * 1024 ** 3)
        assert gtorch.setup_torch(min_memory_mb=2000) == "cuda:0"

    def test_min_memory_unsatisfied_drops_to_cpu(self, install_torch):
        install_torch(cuda_available=True, mem_free_bytes=512 * 1024 ** 2)
        assert gtorch.setup_torch(min_memory_mb=2000) == "cpu"

    def test_min_memory_unsatisfied_prefers_mps_over_cpu(self, install_torch):
        install_torch(cuda_available=True, mps_available=True,
                      mem_free_bytes=512 * 1024 ** 2)
        assert gtorch.setup_torch(min_memory_mb=2000) == "mps"

    def test_mem_get_info_failure_keeps_cuda(self, install_torch):
        """An unqueryable free-VRAM figure must not demote a working GPU."""
        install_torch(cuda_available=True,
                      mem_error=RuntimeError("not supported on this build"))
        assert gtorch.setup_torch(min_memory_mb=2000) == "cuda:0"

    def test_mem_get_info_oserror_keeps_cuda(self, install_torch):
        install_torch(cuda_available=True, mem_error=OSError("driver gone"))
        assert gtorch.setup_torch(min_memory_mb=2000) == "cuda:0"


class TestSetupTorchWithMesh:
    def test_best_local_cuda_wins(self, install_torch):
        install_torch(cuda_available=True, cuda_count=1)
        mesh = FakeMesh([
            _entry(id=1, device="cpu", score=1.0),
            _entry(id=2, device="cuda", score=90.0),
        ])
        assert gtorch.setup_torch(mesh) == "cuda:0"

    def test_best_local_mps_wins(self, install_torch):
        install_torch(mps_available=True)
        mesh = FakeMesh([_entry(device="mps", score=50.0)])
        assert gtorch.setup_torch(mesh) == "mps"

    def test_best_remote_entry_falls_back_to_local_detection(self, install_torch):
        """A remote GPU cannot be handed to a local torch call."""
        install_torch(cuda_available=False)
        mesh = FakeMesh([_entry(hostname=REMOTE_HOST, device="cuda", score=99.0)])
        assert gtorch.setup_torch(mesh) == "cpu"

    def test_mesh_errors_are_swallowed(self, install_torch):
        install_torch(cuda_available=False)
        mesh = FakeMesh(devices_error=ConnectionError("coordinator down"))
        assert gtorch.setup_torch(mesh) == "cpu"

    def test_mesh_with_no_alive_devices_falls_through(self, install_torch):
        install_torch(cuda_available=True)
        mesh = FakeMesh([_entry(status="dead", device="cuda")])
        assert gtorch.setup_torch(mesh) == "cuda:0"

    def test_min_memory_filters_mesh_entries(self, install_torch):
        """A local GPU below the floor loses to a roomier one, despite scoring
        higher. The runner-up has to be a *different* device type, or the
        fallback path below the mesh block would return the same answer and
        the filter would not actually be under test."""
        install_torch(cuda_available=True, cuda_count=1, mps_available=True)
        mesh = FakeMesh([
            _entry(id=1, device="cuda", score=90.0, gpu_memory_free_mb=100,
                   gpu_memory_total_mb=100),
            _entry(id=2, device="mps", score=50.0, gpu_memory_free_mb=16000,
                   gpu_memory_total_mb=16000),
        ])
        assert gtorch.setup_torch(mesh, min_memory_mb=8000) == "mps"

    def test_min_memory_filtering_everything_falls_through(self, install_torch):
        install_torch(cuda_available=True, cuda_count=1)
        mesh = FakeMesh([
            _entry(device="cuda", score=90.0, gpu_memory_free_mb=100,
                   gpu_memory_total_mb=100),
        ])
        assert gtorch.setup_torch(mesh, min_memory_mb=8000) == "cuda:0"

    def test_min_memory_keeps_a_roomy_mesh_entry(self, install_torch):
        install_torch(cuda_available=True, cuda_count=1)
        mesh = FakeMesh([
            _entry(device="cuda", score=90.0, gpu_memory_free_mb=12000,
                   gpu_memory_total_mb=24000),
        ])
        assert gtorch.setup_torch(mesh, min_memory_mb=8000) == "cuda:0"

    def test_full_gpu_is_rejected_by_the_memory_filter(self, install_torch):
        """0 MB free is a reported zero, not a missing figure.

        ``free or total or 0`` read a fully occupied card's 0 as "nothing
        reported" and fell back to its *total*, so a 24 GB GPU with not one
        byte free satisfied min_memory_mb=8000 and the task was placed on the
        single device with no room for it.
        """
        install_torch(cuda_available=True, cuda_count=1, mps_available=True)
        mesh = FakeMesh([
            # 0 MB free: every byte of this GPU is already in use.
            _entry(id=1, device="cuda", score=90.0, gpu_memory_free_mb=0,
                   gpu_memory_total_mb=24000),
            _entry(id=2, device="mps", score=50.0, gpu_memory_free_mb=16000,
                   gpu_memory_total_mb=16000),
        ])
        assert gtorch.setup_torch(mesh, min_memory_mb=8000) == "mps"

    def test_unreported_memory_is_unknown_not_zero(self, install_torch):
        """A coordinator that sends no capacity keys must not fail the filter.

        Pre-1.4 coordinators carry none of them, which is the normal state of
        the world halfway through a rollout — the same reasoning
        accelerate._reported_capacity is built on. Unknown passes; the
        scheduler is the real enforcement point.
        """
        install_torch(cuda_available=True, cuda_count=1)
        mesh = FakeMesh([_entry(device="cuda", score=90.0)])
        assert gtorch.setup_torch(mesh, min_memory_mb=8000) == "cuda:0"

    def test_explicit_null_memory_is_also_unknown(self, install_torch):
        install_torch(cuda_available=True, cuda_count=1)
        mesh = FakeMesh([
            _entry(device="cuda", score=90.0, gpu_memory_free_mb=None,
                   gpu_memory_total_mb=None),
        ])
        assert gtorch.setup_torch(mesh, min_memory_mb=8000) == "cuda:0"

    def test_free_memory_alone_is_enough_to_reject(self, install_torch):
        """No total reported, free below the floor: still filtered out."""
        install_torch(cuda_available=True, cuda_count=1, mps_available=True)
        mesh = FakeMesh([
            _entry(id=1, device="cuda", score=90.0, gpu_memory_free_mb=100),
            _entry(id=2, device="mps", score=50.0, gpu_memory_free_mb=16000),
        ])
        assert gtorch.setup_torch(mesh, min_memory_mb=8000) == "mps"

    def test_second_local_gpu_is_named_by_its_own_index(self, install_torch):
        """setup_torch returned "cuda:0" whatever entry it picked.

        device() computes the local index; setup_torch hard-coded zero, so on a
        multi-GPU box the two public entry points named different cards for the
        same inventory row and one of them was always wrong.
        """
        install_torch(cuda_available=True, cuda_count=2)
        mesh = FakeMesh([
            _entry(id=1, device="cuda", score=10.0),
            _entry(id=2, device="cuda", score=90.0),   # the better card
        ])
        assert gtorch.setup_torch(mesh) == "cuda:1"

    def test_setup_torch_and_device_agree(self, install_torch):
        """The two entry points must name the same card for the same entry."""
        install_torch(cuda_available=True, cuda_count=2)
        mesh = FakeMesh([
            _entry(id=1, device="cuda", score=10.0),
            _entry(id=2, device="cuda", score=90.0),
        ])
        assert gtorch.setup_torch(mesh) == str(gtorch.device(mesh, 1).spec)

    def test_remote_entries_do_not_shift_the_index(self, install_torch):
        install_torch(cuda_available=True, cuda_count=1)
        mesh = FakeMesh([
            _entry(id=1, hostname=REMOTE_HOST, device="cuda", score=10.0),
            _entry(id=2, device="cuda", score=90.0),
        ])
        assert gtorch.setup_torch(mesh) == "cuda:0"

    def test_a_filtered_out_gpu_still_occupies_its_index(self, install_torch):
        """The memory filter must not renumber the surviving devices.

        cuda:0 is skipped for being full, so the answer is cuda:1 — computing
        the index from the filtered list instead would have called it cuda:0
        and sent the work straight back to the full card.
        """
        install_torch(cuda_available=True, cuda_count=2)
        mesh = FakeMesh([
            _entry(id=1, device="cuda", score=99.0, gpu_memory_free_mb=0,
                   gpu_memory_total_mb=24000),
            _entry(id=2, device="cuda", score=90.0, gpu_memory_free_mb=20000,
                   gpu_memory_total_mb=24000),
        ])
        assert gtorch.setup_torch(mesh, min_memory_mb=8000) == "cuda:1"

    def test_stale_inventory_falls_back_to_local_detection(self, install_torch):
        """setup_torch is a best-effort hint, so it degrades instead of raising.

        device() is asked for one specific entry and refuses a stale index;
        setup_torch is asked "what should I use?" and answering from plain
        local detection is a better answer than an exception.
        """
        install_torch(cuda_available=True, cuda_count=1)
        mesh = FakeMesh([
            _entry(id=1, device="cuda", score=10.0),
            _entry(id=2, device="cuda", score=90.0),   # stale second card
        ])
        assert gtorch.setup_torch(mesh) == "cuda:0"


# ── get_device() ───────────────────────────────────────────────────────────

class TestGetDevice:
    def test_wraps_setup_torch_result(self, install_torch):
        install_torch(cuda_available=True, cuda_count=1)
        assert gtorch.get_device() == FakeDevice("cuda:0")

    def test_cpu_device(self, install_torch):
        install_torch()
        assert gtorch.get_device() == FakeDevice("cpu")

    def test_passes_the_mesh_through(self, install_torch):
        install_torch(mps_available=True)
        mesh = FakeMesh([_entry(device="mps", score=50.0)])
        assert gtorch.get_device(mesh) == FakeDevice("mps")
        assert mesh.devices_calls == 1
