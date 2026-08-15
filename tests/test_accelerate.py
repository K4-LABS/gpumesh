"""Tests for the @accelerate decorator."""
from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from gpumesh.accelerate import (
    AcceleratedFunction,
    PlacementUnsupportedError,
    accelerate,
    install,
)


class TestAccelerateDecorator:
    """Tests for accelerate() decorator."""

    def test_returns_accelerated_function(self):
        mesh = MagicMock()

        @accelerate(mesh)
        def my_func(x):
            return x * 2

        assert isinstance(my_func, AcceleratedFunction)

    def test_preserves_name(self):
        mesh = MagicMock()

        @accelerate(mesh)
        def my_func(x):
            return x * 2

        assert my_func.__name__ == "my_func"

    def test_preserves_doc(self):
        mesh = MagicMock()

        @accelerate(mesh)
        def my_func(x):
            """My docstring."""
            return x * 2

        assert my_func.__doc__ == "My docstring."

    def test_preserves_wrapped(self):
        mesh = MagicMock()

        def original(x):
            return x * 2

        accelerated = accelerate(mesh)(original)
        assert accelerated.__wrapped__ is original

    def test_single_call_runs_locally(self):
        mesh = MagicMock()

        @accelerate(mesh)
        def add(a, b):
            return a + b

        result = add(1, 2)
        assert result == 3

    def test_single_call_with_kwargs(self):
        mesh = MagicMock()

        @accelerate(mesh)
        def train(lr, epochs):
            return {"lr": lr, "epochs": epochs}

        result = train(lr=0.01, epochs=100)
        assert result == {"lr": 0.01, "epochs": 100}

    def test_map_calls_distribute(self):
        mesh = MagicMock()
        mesh.distribute.return_value = [
            {"result": 1},
            {"result": 2},
        ]

        @accelerate(mesh)
        def process(x):
            return {"result": x}

        results = process.map([{"x": 1}, {"x": 2}])
        mesh.distribute.assert_called_once()
        assert len(results) == 2

    def test_local_env_var_forces_local(self):
        mesh = MagicMock()
        mesh.distribute.return_value = [{"result": 1}]

        @accelerate(mesh)
        def process(x):
            return {"result": x}

        with patch.dict(os.environ, {"GPUMESH_LOCAL": "1"}):
            result = process(x=1)
            mesh.distribute.assert_not_called()
            assert result == {"result": 1}

    def test_verbose_env_var(self):
        mesh = MagicMock()

        @accelerate(mesh)
        def add(a, b):
            return a + b

        with patch.dict(os.environ, {"GPUMESH_VERBOSE": "1"}):
            result = add(1, 2)
            assert result == 3

    def test_fallback_on_mesh_error(self):
        mesh = MagicMock()
        mesh.distribute.side_effect = Exception("Connection refused")

        @accelerate(mesh)
        def add(a, b):
            return a + b

        result = add(1, 2)
        assert result == 3

    def test_map_empty_list(self):
        mesh = MagicMock()

        @accelerate(mesh)
        def process(x):
            return x

        results = process.map([])
        assert results == []

    def test_map_fallback_on_mesh_error(self):
        mesh = MagicMock()
        mesh.distribute.side_effect = Exception("Connection refused")

        @accelerate(mesh)
        def process(x):
            return {"result": x}

        results = process.map([{"x": 1}, {"x": 2}])
        assert results == [{"result": 1}, {"result": 2}]

    def test_map_local_env_var(self):
        mesh = MagicMock()
        mesh.distribute.return_value = [{"result": 10}]

        @accelerate(mesh)
        def process(x):
            return {"result": x * 10}

        with patch.dict(os.environ, {"GPUMESH_LOCAL": "1"}):
            results = process.map([{"x": 1}])
            mesh.distribute.assert_not_called()
            assert results == [{"result": 10}]

    def test_single_call_with_positional_args(self):
        mesh = MagicMock()

        @accelerate(mesh)
        def add(a, b):
            return a + b

        result = add(3, 4)
        assert result == 7

    def test_single_call_with_mixed_args(self):
        mesh = MagicMock()

        @accelerate(mesh)
        def train(lr, epochs, verbose=False):
            return {"lr": lr, "epochs": epochs, "verbose": verbose}

        result = train(0.01, epochs=100, verbose=True)
        assert result == {"lr": 0.01, "epochs": 100, "verbose": True}


class TestAccelerateEnhanced:
    """Tests for enhanced @accelerate features."""

    def test_gpu_parameter(self):
        mesh = MagicMock()

        @accelerate(mesh, gpu="A100")
        def train(x):
            return x

        assert train._gpu == "A100"

    def test_cores_and_memory_parameters(self):
        mesh = MagicMock()

        @accelerate(mesh, cores=8, memory="16GB")
        def heavy(data):
            return data

        assert heavy._cores == 8
        assert heavy._memory == "16GB"

    def test_timeout_parameter(self):
        mesh = MagicMock()

        @accelerate(mesh, timeout=300)
        def train(x):
            return x

        assert train._timeout == 300

    def test_all_resource_params(self):
        mesh = MagicMock()

        @accelerate(mesh, gpu="RTX_3080", cores=4, memory="8GB", timeout=600)
        def train(x):
            return x

        assert train._gpu == "RTX_3080"
        assert train._cores == 4
        assert train._memory == "8GB"
        assert train._timeout == 600

    def test_to_method(self):
        mesh = MagicMock()

        @accelerate(mesh, cores=8, memory="16GB", timeout=300)
        def train(x):
            return x

        cuda_func = train.to("cuda")
        assert isinstance(cuda_func, AcceleratedFunction)
        assert cuda_func._gpu == "cuda"
        assert cuda_func._cores == 8
        assert cuda_func._memory == "16GB"
        assert cuda_func._timeout == 300

    def test_to_method_preserves_fn(self):
        mesh = MagicMock()

        def original(x):
            return x * 2

        acc = accelerate(mesh)(original)
        cuda_acc = acc.to("cuda")
        assert cuda_acc._fn is original

    def test_map_empty_list(self):
        mesh = MagicMock()

        @accelerate(mesh)
        def process(x):
            return x

        assert process.map([]) == []

    def test_map_with_timeout(self):
        mesh = MagicMock()
        mesh.distribute.return_value = [{"result": 1}]

        @accelerate(mesh, timeout=60)
        def process(x):
            return {"result": x}

        process.map([{"x": 1}])
        mesh.distribute.assert_called_once_with(
            function=process._fn,
            params=[{"x": 1}],
            timeout=60,
        )

    def _get_accel_module(self):
        """Get the actual accelerate module, not the function."""
        import sys
        return sys.modules['gpumesh.accelerate']

    def test_install_sets_global_mesh(self):
        mod = self._get_accel_module()
        old = mod._installed_mesh
        try:
            mesh = MagicMock()
            install(mesh)
            assert mod._installed_mesh is mesh
        finally:
            mod._installed_mesh = old

    def test_install_none_clears_mesh(self):
        mod = self._get_accel_module()
        old = mod._installed_mesh
        try:
            install(MagicMock())
            install(None)
            assert mod._installed_mesh is None
        finally:
            mod._installed_mesh = old

    def test_accelerate_no_args_uses_installed(self):
        mod = self._get_accel_module()
        old = mod._installed_mesh
        try:
            mesh = MagicMock()
            install(mesh)

            @accelerate
            def train(x):
                return x * 2

            assert isinstance(train, AcceleratedFunction)
            assert train._mesh is mesh
        finally:
            mod._installed_mesh = old

    def test_accelerate_no_args_no_install_raises(self):
        mod = self._get_accel_module()
        old = mod._installed_mesh
        try:
            mod._installed_mesh = None
            with pytest.raises(RuntimeError, match="No mesh provided"):
                @accelerate
                def train(x):
                    return x
        finally:
            mod._installed_mesh = old

    def test_auto_place_no_torch(self):
        mesh = MagicMock()

        @accelerate(mesh)
        def train(x):
            return x

        result = train(42)
        assert result == 42

    def test_auto_place_with_mock_torch(self):
        mesh = MagicMock()
        mock_torch = MagicMock()

        class FakeModule:
            pass

        mock_torch.nn.Module = FakeModule
        mock_model = FakeModule()
        mock_model.to = MagicMock(return_value=mock_model)

        mock_device = MagicMock()
        mock_torch.device.return_value = mock_device
        mock_torch.cuda.is_available.return_value = False
        mock_backends = MagicMock()
        mock_backends.mps.is_available.return_value = False
        mock_torch.backends = mock_backends

        @accelerate(mesh)
        def train(model, data):
            return model

        with patch.dict("sys.modules", {"torch": mock_torch}):
            result = train(mock_model, "data")
            mock_model.to.assert_called()

    def test_get_torch_device_with_gpu_preference(self):
        mesh = MagicMock()
        mock_torch = MagicMock()
        mock_torch.device.return_value = "cpu"
        mock_torch.cuda.is_available.return_value = False
        mock_backends = MagicMock()
        mock_backends.mps.is_available.return_value = False
        mock_torch.backends = mock_backends

        @accelerate(mesh, gpu="cpu")
        def train(x):
            return x

        with patch.dict("sys.modules", {"torch": mock_torch}):
            device = train._get_torch_device()
            assert device == "cpu"

    def test_get_torch_device_no_torch(self):
        mesh = MagicMock()

        @accelerate(mesh)
        def train(x):
            return x

        device = train._get_torch_device()
        assert device is None

    def test_to_method_returns_new_instance(self):
        mesh = MagicMock()

        @accelerate(mesh)
        def train(x):
            return x

        cuda = train.to("cuda")
        mps = train.to("mps")
        assert cuda is not mps
        assert cuda._gpu == "cuda"
        assert mps._gpu == "mps"


class TestParseMemoryMb:
    """Tests for _parse_memory_mb helper."""

    def test_parse_gb(self):
        from gpumesh.accelerate import _parse_memory_mb
        assert _parse_memory_mb("16GB") == 16384.0

    def test_parse_mb(self):
        from gpumesh.accelerate import _parse_memory_mb
        assert _parse_memory_mb("512MB") == 512.0

    def test_parse_bytes(self):
        from gpumesh.accelerate import _parse_memory_mb
        result = _parse_memory_mb("1048576B")
        assert abs(result - 1.0) < 0.01

    def test_parse_lowercase(self):
        from gpumesh.accelerate import _parse_memory_mb
        assert _parse_memory_mb("8gb") == 8192.0

    def test_parse_with_spaces(self):
        from gpumesh.accelerate import _parse_memory_mb
        assert _parse_memory_mb("  4GB  ") == 4096.0

    def test_parse_invalid_raises(self):
        from gpumesh.accelerate import _parse_memory_mb
        with pytest.raises(ValueError, match="Cannot parse"):
            _parse_memory_mb("16TB")


class TestValidateResources:
    """Tests for resource validation in AcceleratedFunction."""

    def test_no_requirements_passes(self):
        mesh = MagicMock()
        mesh.devices.return_value = [
            {"device_name": "RTX 3080", "status": "alive", "cpu_cores": 8, "gpu_memory_free_mb": 10000}
        ]

        @accelerate(mesh)
        def train(x):
            return x

        result = train(42)
        assert result == 42

    def test_gpu_match_passes(self):
        mesh = MagicMock()
        mesh.devices.return_value = [
            {"device_name": "NVIDIA A100", "status": "alive", "cpu_cores": 8, "gpu_memory_free_mb": 40000}
        ]

        @accelerate(mesh, gpu="A100")
        def train(x):
            return x

        result = train(42)
        assert result == 42

    def test_gpu_no_match_raises(self):
        mesh = MagicMock()
        mesh.devices.return_value = [
            {"device_name": "RTX 3080", "status": "alive", "cpu_cores": 8, "gpu_memory_free_mb": 10000}
        ]

        @accelerate(mesh, gpu="A100")
        def train(x):
            return x

        with pytest.raises(ValueError, match="No worker can satisfy"):
            train(42)

    def test_cores_match_passes(self):
        mesh = MagicMock()
        mesh.devices.return_value = [
            {"device_name": "RTX 3080", "status": "alive", "cpu_cores": 16, "gpu_memory_free_mb": 10000}
        ]

        @accelerate(mesh, cores=8)
        def train(x):
            return x

        result = train(42)
        assert result == 42

    def test_cores_no_match_raises(self):
        mesh = MagicMock()
        mesh.devices.return_value = [
            {"device_name": "RTX 3080", "status": "alive", "cpu_cores": 4, "gpu_memory_free_mb": 10000}
        ]

        @accelerate(mesh, cores=8)
        def train(x):
            return x

        with pytest.raises(ValueError, match="No worker can satisfy"):
            train(42)

    def test_memory_match_passes(self):
        mesh = MagicMock()
        mesh.devices.return_value = [
            {"device_name": "RTX 3080", "status": "alive", "cpu_cores": 8, "gpu_memory_free_mb": 20000}
        ]

        @accelerate(mesh, memory="16GB")
        def train(x):
            return x

        result = train(42)
        assert result == 42

    def test_memory_no_match_raises(self):
        mesh = MagicMock()
        mesh.devices.return_value = [
            {"device_name": "RTX 3080", "status": "alive", "cpu_cores": 8, "gpu_memory_free_mb": 4000}
        ]

        @accelerate(mesh, memory="16GB")
        def train(x):
            return x

        with pytest.raises(ValueError, match="No worker can satisfy"):
            train(42)

    def test_dead_workers_ignored(self):
        mesh = MagicMock()
        mesh.devices.return_value = [
            {"device_name": "A100", "status": "dead", "cpu_cores": 64, "gpu_memory_free_mb": 80000}
        ]

        @accelerate(mesh, gpu="A100")
        def train(x):
            return x

        # Dead workers are filtered out; no alive workers means validation
        # is skipped (fail-open). The function runs normally.
        result = train(42)
        assert result == 42

    def test_validation_skipped_when_no_resources(self):
        mesh = MagicMock()
        mesh.devices.return_value = []

        @accelerate(mesh)
        def train(x):
            return x

        result = train(42)
        assert result == 42

    def test_validation_skipped_in_local_mode(self):
        mesh = MagicMock()
        mesh.devices.return_value = []

        @accelerate(mesh, gpu="A100")
        def train(x):
            return x

        with patch.dict(os.environ, {"GPUMESH_LOCAL": "1"}):
            result = train(42)
            assert result == 42

    def test_validate_uses_cpu_count_fallback(self):
        """Worker reports cpu_count but not cpu_cores; validation still works."""
        mesh = MagicMock()
        mesh.devices.return_value = [
            {"device_name": "RTX 3080", "status": "alive", "cpu_count": 16, "gpu_memory_free_mb": 10000}
        ]

        @accelerate(mesh, cores=8)
        def train(x):
            return x

        result = train(42)
        assert result == 42

    def test_total_memory_is_used_only_when_free_is_not_reported(self):
        """No free reading at all: total is the only measurement there is."""
        mesh = MagicMock()
        mesh.devices.return_value = [
            {"device": "cuda", "device_name": "RTX 3080", "status": "alive",
             "cpu_cores": 8, "gpu_memory_total_mb": 20000}
        ]

        @accelerate(mesh, memory="16GB")
        def train(x):
            return x

        assert train(42) == 42

    def test_reported_zero_free_does_not_fall_back_to_total(self):
        """A card that says it has 0 MB free has been measured, and has none.

        This used to fall through to ``gpu_memory_total_mb`` on the grounds
        that a worker before its first heartbeat also reports 0, which meant a
        fully occupied 24 GB card satisfied ``memory="8GB"`` and the work was
        routed to the one device with no room for it.
        """
        mesh = MagicMock()
        mesh.devices.return_value = [
            {"device": "cuda", "device_name": "RTX 3080", "status": "alive",
             "cpu_cores": 8, "gpu_memory_free_mb": 0, "gpu_memory_total_mb": 20000}
        ]

        @accelerate(mesh, memory="16GB")
        def train(x):
            return x

        with pytest.raises(ValueError, match="No worker can satisfy"):
            train(42)


def _device(device, device_name, **extra):
    """A worker record shaped like the coordinator's /api/devices entries."""
    d = {"device": device, "device_name": device_name, "status": "alive",
         "cpu_cores": 8, "gpu_memory_total_mb": 8000, "gpu_memory_free_mb": 8000}
    d.update(extra)
    return d


class TestDeviceMatching:
    """Device kinds route on the worker's device type, models on its name."""

    @pytest.mark.parametrize("spec,dtype,name,expected", [
        ("cuda", "cuda", "NVIDIA GeForce RTX 4090", True),
        ("cuda", "cpu", "Intel i7", False),
        ("CUDA", "cuda", "NVIDIA A100", True),
        ("cuda:1", "cuda", "NVIDIA A100", True),
        ("cpu", "cpu", "Intel i7", True),
        ("cpu", "cuda", "NVIDIA A100", False),
        ("mps", "mps", "Apple Silicon GPU", True),
        ("gpu", "cuda", "NVIDIA A100", True),
        ("gpu", "mps", "Apple Silicon GPU", True),
        ("gpu", "cpu", "Intel i7", False),
        ("A100", "cuda", "NVIDIA A100-SXM4-40GB", True),
        ("a100", "cuda", "NVIDIA A100", True),
        ("A100", "cuda", "NVIDIA GeForce RTX 4090", False),
        ("RTX 4090", "cuda", "NVIDIA GeForce RTX 4090", True),
        (None, "cpu", "Intel i7", True),
    ])
    def test_device_matches(self, spec, dtype, name, expected):
        from gpumesh.accelerate import device_matches
        assert device_matches(spec, dtype, name) is expected

    def test_to_cuda_accepts_gpu_worker(self):
        mesh = MagicMock()
        mesh.devices.return_value = [_device("cuda", "NVIDIA GeForce RTX 4090")]

        @accelerate(mesh)
        def train(x):
            return x

        assert train.to("cuda")(42) == 42

    def test_to_cuda_rejects_cpu_only_mesh(self):
        mesh = MagicMock()
        mesh.devices.return_value = [_device("cpu", "Intel i7")]

        @accelerate(mesh)
        def train(x):
            return x

        with pytest.raises(ValueError, match="No worker can satisfy"):
            train.to("cuda")(42)

    def test_to_cpu_accepts_cpu_worker(self):
        mesh = MagicMock()
        mesh.devices.return_value = [_device("cpu", "Intel i7")]

        @accelerate(mesh)
        def train(x):
            return x

        assert train.to("cpu")(42) == 42

    def test_gpu_model_name_still_matches(self):
        mesh = MagicMock()
        mesh.devices.return_value = [_device("cuda", "NVIDIA A100-SXM4-40GB")]

        @accelerate(mesh, gpu="A100")
        def train(x):
            return x

        assert train(42) == 42


class TestRoutingHints:
    """gpu=/memory= reach the scheduler on both the single-call and map paths."""

    def _mesh_with_a100(self):
        mesh = MagicMock()
        mesh.workers.return_value = [{"alive": True}]
        mesh.devices.return_value = [
            _device("cuda", "NVIDIA A100", gpu_memory_free_mb=40000)
        ]
        mesh.submit.return_value = "job-1"
        mesh.status.return_value = {
            "finished": True,
            "tasks": [{"status": "done", "result": {"ok": True}}],
        }
        return mesh

    def test_single_call_payload_carries_hints(self):
        mesh = self._mesh_with_a100()

        @accelerate(mesh, gpu="A100", memory="16GB")
        def train(x):
            return {"ok": True}

        train(1)
        payload = mesh.submit.call_args.kwargs["payloads"][0]
        assert payload["gpu"] == "A100"
        assert payload["gpu_memory_mb"] == 16384.0

    def test_map_forwards_hints_to_distribute(self):
        mesh = self._mesh_with_a100()
        mesh.distribute.return_value = [{"ok": True}]

        @accelerate(mesh, gpu="A100", memory="16GB")
        def train(x):
            return {"ok": True}

        train.map([{"x": 1}])
        kwargs = mesh.distribute.call_args.kwargs
        assert kwargs["gpu"] == "A100"
        assert kwargs["gpu_memory_mb"] == 16384.0

    def test_map_without_hints_stays_unconstrained(self):
        mesh = MagicMock()
        mesh.workers.return_value = [{"alive": True}]
        mesh.distribute.return_value = [{"ok": True}]

        @accelerate(mesh)
        def train(x):
            return {"ok": True}

        train.map([{"x": 1}])
        kwargs = mesh.distribute.call_args.kwargs
        assert "gpu" not in kwargs
        assert "gpu_memory_mb" not in kwargs

    def test_unsatisfiable_hint_fails_fast_on_call(self):
        """An impossible requirement must raise, never queue a task nobody can lease."""
        mesh = self._mesh_with_a100()
        mesh.devices.return_value = [_device("cpu", "Intel i7")]

        @accelerate(mesh, gpu="A100")
        def train(x):
            return {"ok": True}

        with pytest.raises(ValueError, match="No worker can satisfy"):
            train(1)
        mesh.submit.assert_not_called()

    def test_unsatisfiable_hint_fails_fast_on_map(self):
        mesh = self._mesh_with_a100()
        mesh.devices.return_value = [_device("cpu", "Intel i7")]

        @accelerate(mesh, gpu="A100")
        def train(x):
            return {"ok": True}

        with pytest.raises(ValueError, match="No worker can satisfy"):
            train.map([{"x": 1}])
        mesh.distribute.assert_not_called()


class TestCoresHint:
    """``cores=`` was validated and then thrown away — it has to reach the wire."""

    def _mesh_with_64_cores(self):
        mesh = MagicMock()
        mesh.workers.return_value = [{"alive": True}]
        mesh.devices.return_value = [_device("cuda", "NVIDIA A100", cpu_cores=64)]
        mesh.submit.return_value = "job-1"
        mesh.status.return_value = {
            "finished": True,
            "tasks": [{"status": "done", "result": {"ok": True}}],
        }
        return mesh

    def test_single_call_payload_carries_cpu_cores(self):
        mesh = self._mesh_with_64_cores()

        @accelerate(mesh, cores=64)
        def train(x):
            return {"ok": True}

        train(1)
        payload = mesh.submit.call_args.kwargs["payloads"][0]
        assert payload["cpu_cores"] == 64

    def test_single_call_omits_cpu_cores_when_unset(self):
        mesh = self._mesh_with_64_cores()

        @accelerate(mesh)
        def train(x):
            return {"ok": True}

        train(1)
        payload = mesh.submit.call_args.kwargs["payloads"][0]
        assert "cpu_cores" not in payload

    def test_map_forwards_cores_to_distribute(self):
        mesh = self._mesh_with_64_cores()
        mesh.distribute.return_value = [{"ok": True}]

        @accelerate(mesh, cores=64)
        def train(x):
            return {"ok": True}

        train.map([{"x": 1}])
        assert mesh.distribute.call_args.kwargs["cores"] == 64

    def test_map_omits_cores_when_unset(self):
        mesh = self._mesh_with_64_cores()
        mesh.distribute.return_value = [{"ok": True}]

        @accelerate(mesh)
        def train(x):
            return {"ok": True}

        train.map([{"x": 1}])
        assert "cores" not in mesh.distribute.call_args.kwargs

    def test_impossible_core_count_still_fails_fast(self):
        mesh = self._mesh_with_64_cores()
        mesh.devices.return_value = [_device("cpu", "Intel i7", cpu_cores=2)]

        @accelerate(mesh, cores=64)
        def train(x):
            return {"ok": True}

        with pytest.raises(ValueError, match="No worker can satisfy"):
            train(1)
        mesh.submit.assert_not_called()


class _OldMesh:
    """A pre-placement-hint mesh object: distribute() has no hint keywords."""

    def __init__(self, results=None):
        self._results = results if results is not None else [{"ok": "remote"}]
        self.calls = 0

    def workers(self):
        return [{"alive": True}]

    def devices(self):
        return [_device("cuda", "NVIDIA A100", gpu_memory_free_mb=40000, cpu_cores=64)]

    def distribute(self, function, params, timeout=300.0):
        self.calls += 1
        return self._results


class TestPlacementNeverDegradesToLocal:
    """A placement constraint must never quietly become a local run."""

    def _capable_mesh(self):
        mesh = MagicMock()
        mesh.workers.return_value = [{"alive": True}]
        mesh.devices.return_value = [
            _device("cuda", "NVIDIA A100", gpu_memory_free_mb=40000, cpu_cores=64)
        ]
        return mesh

    def test_map_refuses_when_mesh_cannot_carry_the_constraint(self):
        mesh = _OldMesh()

        @accelerate(mesh, gpu="A100")
        def train(x):
            return {"ok": "local"}

        with pytest.raises(PlacementUnsupportedError, match="cannot be honoured"):
            train.map([{"x": 1}])
        assert mesh.calls == 0

    def test_refusal_names_the_missing_keywords(self):
        mesh = _OldMesh()

        @accelerate(mesh, gpu="A100", cores=8, memory="16GB")
        def train(x):
            return {"ok": "local"}

        with pytest.raises(PlacementUnsupportedError) as exc:
            train.map([{"x": 1}])
        message = str(exc.value)
        assert "cores" in message
        assert "gpu" in message
        assert "gpu_memory_mb" in message

    def test_old_mesh_without_constraint_still_distributes(self):
        """No constraint means nothing to lose — the old signature is fine."""
        mesh = _OldMesh()

        @accelerate(mesh)
        def train(x):
            return {"ok": "local"}

        assert train.map([{"x": 1}]) == [{"ok": "remote"}]
        assert mesh.calls == 1

    def test_map_propagates_timeout_instead_of_running_locally(self):
        mesh = self._capable_mesh()
        mesh.distribute.side_effect = TimeoutError("Job job-1 timed out after 300.0s")

        @accelerate(mesh, gpu="A100")
        def train(x):
            return {"ok": "local"}

        with pytest.raises(TimeoutError):
            train.map([{"x": 1}])

    def test_map_still_falls_back_when_mesh_is_unreachable(self):
        """Mesh-down fallback is documented behaviour and stays."""
        mesh = self._capable_mesh()
        mesh.distribute.side_effect = OSError("Connection refused")

        @accelerate(mesh, gpu="A100")
        def train(x):
            return {"ok": "local"}

        with pytest.warns(UserWarning, match="Falling back to local"):
            assert train.map([{"x": 1}]) == [{"ok": "local"}]

    def test_user_type_error_is_not_mistaken_for_a_signature_mismatch(self):
        """The discriminator: a TypeError from user code must surface as itself.

        Pattern-matching exception text would have caught this one and
        reported an unsupported-keyword problem the mesh does not have.
        """
        mesh = self._capable_mesh()
        mesh.distribute.side_effect = OSError("Connection refused")

        @accelerate(mesh, gpu="A100")
        def train(x):
            raise TypeError("unexpected keyword argument 'gpu'")

        with pytest.warns(UserWarning):
            with pytest.raises(TypeError, match="unexpected keyword argument"):
                train.map([{"x": 1}])


class TestExceptionsUsersCatchAreDeclaredPublic:
    """An exception raised at users must be in the declared public API.

    docs/stability.md §1 says the public API is exactly ``gpumesh.__all__``.
    ``PlacementUnsupportedError`` was raised at callers — it is what ``.map()``
    throws instead of silently running a ``gpu=``-constrained batch locally —
    while being absent from that list, which made the one exception a user
    cannot avoid catching officially unsupported.
    """

    def test_placement_error_is_importable_from_the_package_root(self):
        import gpumesh

        assert gpumesh.PlacementUnsupportedError is PlacementUnsupportedError

    def test_placement_error_is_in_all(self):
        import gpumesh

        assert "PlacementUnsupportedError" in gpumesh.__all__

    def test_map_refusal_is_catchable_by_the_public_name(self):
        """The behaviour the export exists for, exercised end to end."""
        import gpumesh

        mesh = _OldMesh()

        @accelerate(mesh, gpu="A100")
        def train(x):
            return {"ok": "local"}

        with pytest.raises(gpumesh.PlacementUnsupportedError):
            train.map([{"x": 1}])

    def test_every_declared_name_resolves(self):
        """A name in __all__ that does not exist is a broken promise."""
        import gpumesh

        for name in gpumesh.__all__:
            assert getattr(gpumesh, name) is not None or name == "__version__"

    def test_internal_control_flow_stays_private(self):
        """_MeshUnavailable never crosses the API boundary, so it is not public."""
        import gpumesh
        from gpumesh.accelerate import _MeshUnavailable

        assert "_MeshUnavailable" not in gpumesh.__all__
        assert not hasattr(gpumesh, "_MeshUnavailable")
        assert issubclass(_MeshUnavailable, Exception)


class TestUnknownCapacityIsNotZero:
    """A 1.3.0 coordinator reports no capacity keys; that is not a refusal."""

    def _mesh(self, device):
        mesh = MagicMock()
        mesh.devices.return_value = [device]
        return mesh

    def test_missing_cores_key_passes_validation(self):
        mesh = self._mesh(
            {"device": "cuda", "device_name": "NVIDIA A100", "status": "alive"}
        )

        @accelerate(mesh, cores=64)
        def train(x):
            return x

        train._validate_resources()  # must not raise

    def test_missing_memory_keys_pass_validation(self):
        mesh = self._mesh(
            {"device": "cuda", "device_name": "NVIDIA A100", "status": "alive"}
        )

        @accelerate(mesh, memory="80GB")
        def train(x):
            return x

        train._validate_resources()  # must not raise

    def test_reported_insufficient_cores_still_fails(self):
        mesh = self._mesh(_device("cpu", "Intel i7", cpu_cores=4))

        @accelerate(mesh, cores=8)
        def train(x):
            return x

        with pytest.raises(ValueError, match="No worker can satisfy"):
            train(42)

    def test_reported_zero_cores_still_fails(self):
        """A key present with value 0 keeps its old meaning."""
        mesh = self._mesh(
            {"device": "cpu", "device_name": "Intel i7", "status": "alive",
             "cpu_cores": 0, "cpu_count": 0}
        )

        @accelerate(mesh, cores=8)
        def train(x):
            return x

        with pytest.raises(ValueError, match="No worker can satisfy"):
            train(42)

    def test_reported_insufficient_memory_still_fails(self):
        mesh = self._mesh(
            {"device": "cuda", "device_name": "RTX 3080", "status": "alive",
             "gpu_memory_free_mb": 4000}
        )

        @accelerate(mesh, memory="16GB")
        def train(x):
            return x

        with pytest.raises(ValueError, match="No worker can satisfy"):
            train(42)

    def test_zero_free_memory_is_zero_not_unknown(self):
        """A reported 0 is a measurement; only an absent key is unknown."""
        mesh = self._mesh(
            {"device": "cuda", "device_name": "NVIDIA A100", "status": "alive",
             "gpu_memory_free_mb": 0, "gpu_memory_total_mb": 40000}
        )

        @accelerate(mesh, memory="16GB")
        def train(x):
            return x

        with pytest.raises(ValueError, match="No worker can satisfy"):
            train._validate_resources()

    def test_omitted_free_memory_key_still_passes(self):
        """The counterpart: silence about free VRAM must not become a refusal.

        These two tests are the whole distinction. A worker that omits the key
        (a 1.3.0 coordinator, mid-rollout) is unknown and passes; a worker that
        answers 0 has been measured and fails.
        """
        mesh = self._mesh(
            {"device": "cuda", "device_name": "NVIDIA A100", "status": "alive",
             "gpu_memory_total_mb": 40000}
        )

        @accelerate(mesh, memory="16GB")
        def train(x):
            return x

        train._validate_resources()  # must not raise

    def test_zero_free_refusal_says_the_card_is_occupied(self):
        """0 free with a big total now means one thing, so say that one thing.

        This used to assert the message offered two explanations — a full card
        *or* a worker that had registered moments earlier and not yet sent its
        first heartbeat — because the coordinator reported both as 0.0 and
        genuinely could not tell them apart. It can now: an unmeasured worker
        is reported as JSON null. Reaching this branch therefore means the card
        was measured and is full, and still offering the heartbeat explanation
        would send the operator off to wait for something that already arrived.
        """
        mesh = self._mesh(
            {"device": "cuda", "device_name": "NVIDIA A100", "status": "alive",
             "gpu_memory_free_mb": 0, "gpu_memory_total_mb": 40000}
        )

        @accelerate(mesh, memory="16GB")
        def train(x):
            return x

        with pytest.raises(ValueError, match="currently occupied") as excinfo:
            train._validate_resources()
        assert "heartbeat" not in str(excinfo.value), (
            "the refusal still offers the stale unmeasured-worker explanation"
        )

    def test_none_capacity_falls_through_to_the_next_key(self):
        """JSON null is how a coordinator could say "no reading"; 0 is not.

        A current coordinator sends exactly this: ``list_devices()`` reports
        null for a worker whose free VRAM nobody has measured yet, which is
        what lets both sides treat a *reported* zero strictly without
        punishing a worker whose first heartbeat has not landed. Older
        coordinators COALESCEd the same case to 0.0, so this path also has to
        keep working against them.
        """
        mesh = self._mesh(
            {"device": "cuda", "device_name": "NVIDIA A100", "status": "alive",
             "gpu_memory_free_mb": None, "gpu_memory_total_mb": 40000}
        )

        @accelerate(mesh, memory="16GB")
        def train(x):
            return x

        train._validate_resources()  # must not raise

    def test_reported_capacity_helper(self):
        from gpumesh.accelerate import _reported_capacity

        assert _reported_capacity({}, "cpu_cores", "cpu_count") is None
        assert _reported_capacity({"cpu_cores": None}, "cpu_cores", "cpu_count") is None
        assert _reported_capacity({"cpu_cores": 0}, "cpu_cores", "cpu_count") == 0
        # The first key that is actually present wins outright; a reported 0
        # does not fall through to the key behind it.
        assert _reported_capacity({"cpu_cores": 0, "cpu_count": 8},
                                  "cpu_cores", "cpu_count") == 0
        assert _reported_capacity({"cpu_cores": None, "cpu_count": 8},
                                  "cpu_cores", "cpu_count") == 8
        assert _reported_capacity({"cpu_cores": 4}, "cpu_cores", "cpu_count") == 4
        assert _reported_capacity({"gpu_memory_free_mb": 0,
                                   "gpu_memory_total_mb": 24000},
                                  "gpu_memory_free_mb", "gpu_memory_total_mb") == 0


def _fake_torch(device_count=2, names=("NVIDIA GeForce RTX 4090", "NVIDIA A100"),
                cuda=True):
    """A torch stand-in whose ``device()`` echoes the spec it was handed."""
    torch = MagicMock()
    torch.device.side_effect = lambda spec: f"device({spec})"
    torch.cuda.is_available.return_value = cuda
    torch.cuda.device_count.return_value = device_count
    torch.cuda.get_device_name.side_effect = lambda i: names[i]
    backends = MagicMock()
    backends.mps.is_available.return_value = False
    torch.backends = backends
    return torch


class TestTorchDeviceNormalisation:
    """``gpu=`` is user-typed text; torch.device() is case-sensitive."""

    def _fn(self, gpu):
        @accelerate(MagicMock(), gpu=gpu)
        def train(x):
            return x

        return train

    def test_uppercase_cuda_ordinal_is_normalised(self):
        fn = self._fn("CUDA:1")
        with patch.dict("sys.modules", {"torch": _fake_torch()}):
            assert fn._get_torch_device() == "device(cuda:1)"

    def test_uppercase_bare_kind_is_normalised(self):
        fn = self._fn("CUDA")
        with patch.dict("sys.modules", {"torch": _fake_torch()}):
            assert fn._get_torch_device() == "device(cuda:0)"

    def test_spec_with_surrounding_space_is_normalised(self):
        fn = self._fn("  Cuda:1 ")
        with patch.dict("sys.modules", {"torch": _fake_torch()}):
            assert fn._get_torch_device() == "device(cuda:1)"

    def test_out_of_range_ordinal_raises_here_not_at_dot_to(self):
        fn = self._fn("cuda:5")
        with patch.dict("sys.modules", {"torch": _fake_torch(device_count=1)}):
            with pytest.raises(ValueError, match="has 1 CUDA device"):
                fn._get_torch_device()

    def test_negative_ordinal_raises(self):
        fn = self._fn("cuda:-1")
        with patch.dict("sys.modules", {"torch": _fake_torch()}):
            with pytest.raises(ValueError, match="asks for CUDA device"):
                fn._get_torch_device()

    def test_unparseable_ordinal_raises(self):
        fn = self._fn("cuda:first")
        with patch.dict("sys.modules", {"torch": _fake_torch()}):
            with pytest.raises(ValueError, match="Cannot parse device ordinal"):
                fn._get_torch_device()

    def test_model_name_match_is_case_insensitive(self):
        fn = self._fn("a100")
        with patch.dict("sys.modules", {"torch": _fake_torch()}):
            assert fn._get_torch_device() == "device(cuda:1)"

    def test_gpu_kind_ignores_a_meaningless_ordinal(self):
        fn = self._fn("GPU:3")
        with patch.dict("sys.modules", {"torch": _fake_torch(device_count=1)}):
            assert fn._get_torch_device() == "device(cuda:0)"

    def test_uppercase_cpu_still_resolves(self):
        fn = self._fn("CPU")
        with patch.dict("sys.modules", {"torch": _fake_torch()}):
            assert fn._get_torch_device() == "device(cpu)"


class TestInstallOnDecorator:
    """The documented ``accelerate.install(mesh)`` entry point."""

    def test_install_is_reachable_from_the_decorator(self):
        from gpumesh import accelerate as accelerate_export

        assert accelerate_export.install is install

    def test_documented_install_snippet_runs(self):
        mod = sys.modules["gpumesh.accelerate"]
        old = mod._installed_mesh
        try:
            from gpumesh import accelerate as accelerate_export

            mesh = MagicMock()
            mesh.devices.return_value = []
            accelerate_export.install(mesh)

            @accelerate_export
            def train(lr, epochs):
                return {"accuracy": 0.95, "lr": lr, "epochs": epochs}

            assert train._mesh is mesh
            assert train(lr=0.01, epochs=100)["accuracy"] == 0.95
        finally:
            mod._installed_mesh = old

    def test_accelerate_install_alias_still_works(self):
        import gpumesh

        mod = sys.modules["gpumesh.accelerate"]
        old = mod._installed_mesh
        try:
            mesh = MagicMock()
            gpumesh.accelerate_install(mesh)
            assert mod._installed_mesh is mesh
        finally:
            mod._installed_mesh = old


class _LoopbackMesh:
    """A mesh that runs the batch here but through the real payload plumbing.

    This is the only way to compare the two paths honestly. A MagicMock says
    nothing about what the function would have *received* on the far side, and
    a real coordinator plus a real worker is not a unit test. So this mirrors
    exactly what the wire does, and nothing else:

      * ``GPUMesh.distribute`` builds one payload per params dict, putting
        ``{k: v for k, v in p.items() if k != "cost"}`` into ``_params`` and
        lifting ``cost`` to the payload's top level as the scheduler weight
        (api.py).
      * ``worker._run_function_task`` then calls the function with
        ``payload["_params"]`` and nothing else (worker.py).

    ``seen_costs`` records the weights so a test can also prove the hint still
    reaches the scheduler rather than being quietly dropped on the floor.
    """

    def __init__(self):
        self.seen_costs = []
        self.seen_params = []

    def workers(self):
        return [{"alive": True}]

    def devices(self):
        return [_device("cpu", "Intel i7")]

    def distribute(self, function, params, timeout=300.0, gpu=None,
                   gpu_memory_mb=None, cores=None):
        results = []
        for p in params:
            self.seen_costs.append(p.get("cost", 1.0))
            wire_params = {k: v for k, v in p.items() if k != "cost"}
            self.seen_params.append(wire_params)
            results.append(function(**wire_params))
        return results


class TestCostIsASchedulerHintNotAnArgument:
    """A ``cost``-annotated payload must behave the same with or without a mesh.

    ``cost`` is documented in examples/payloads.json and the docs as a payload
    annotation, and ``GPUMesh.distribute`` has stripped it out of the function
    arguments since 1.1.0. ``.map()``'s three local fallbacks never got that
    fix, so annotating payloads worked on the mesh and raised "unexpected
    keyword argument 'cost'" the moment the mesh was unavailable — the exact
    inverse of what a fallback is for.
    """

    PAYLOADS = [
        {"lr": 0.01, "epochs": 100, "cost": 1},
        {"lr": 0.05, "epochs": 200, "cost": 5},
    ]
    EXPECTED = [
        {"lr": 0.01, "epochs": 100},
        {"lr": 0.05, "epochs": 200},
    ]

    @staticmethod
    def _fn():
        def evaluate(lr, epochs):
            return {"lr": lr, "epochs": epochs}
        return evaluate

    def test_mesh_path_strips_cost(self):
        """The reference behaviour every local path has to match."""
        mesh = _LoopbackMesh()
        train = accelerate(mesh)(self._fn())

        assert train.map(self.PAYLOADS) == self.EXPECTED
        # ...and the hint still reached the scheduler; stripping it from the
        # function arguments must not mean discarding it.
        assert mesh.seen_costs == [1, 5]

    def test_local_path_gpumesh_local_env_var(self):
        mesh = _LoopbackMesh()
        train = accelerate(mesh)(self._fn())

        with patch.dict(os.environ, {"GPUMESH_LOCAL": "1"}):
            assert train.map(self.PAYLOADS) == self.EXPECTED

    def test_local_path_no_alive_workers(self):
        mesh = MagicMock()
        mesh.workers.return_value = [{"alive": False}]
        train = accelerate(mesh)(self._fn())

        with pytest.warns(UserWarning, match="No alive workers"):
            assert train.map(self.PAYLOADS) == self.EXPECTED
        mesh.distribute.assert_not_called()

    def test_local_path_mesh_down(self):
        mesh = MagicMock()
        mesh.workers.return_value = [{"alive": True}]
        mesh.distribute.side_effect = ConnectionRefusedError("coordinator down")
        train = accelerate(mesh)(self._fn())

        with pytest.warns(UserWarning, match="Falling back to local execution"):
            assert train.map(self.PAYLOADS) == self.EXPECTED

    def test_every_path_produces_the_same_answer(self):
        """The property that actually matters, asserted as one comparison."""
        mesh_result = accelerate(_LoopbackMesh())(self._fn()).map(self.PAYLOADS)

        with patch.dict(os.environ, {"GPUMESH_LOCAL": "1"}):
            forced_local = accelerate(_LoopbackMesh())(self._fn()).map(self.PAYLOADS)

        no_workers_mesh = MagicMock()
        no_workers_mesh.workers.return_value = []
        with pytest.warns(UserWarning):
            no_workers = accelerate(no_workers_mesh)(self._fn()).map(self.PAYLOADS)

        down_mesh = MagicMock()
        down_mesh.workers.return_value = [{"alive": True}]
        down_mesh.distribute.side_effect = OSError("connection refused")
        with pytest.warns(UserWarning):
            mesh_down = accelerate(down_mesh)(self._fn()).map(self.PAYLOADS)

        assert mesh_result == forced_local == no_workers == mesh_down

    def test_a_single_call_still_passes_a_real_cost_argument(self):
        """``__call__`` must NOT strip: there the payload is the caller's kwargs.

        A function that genuinely declares a ``cost`` parameter is called
        directly, not annotated — and on the mesh path those kwargs go into
        ``_params`` untouched, so stripping here would break the symmetry
        rather than restore it.
        """
        mesh = MagicMock()
        mesh.workers.return_value = []

        @accelerate(mesh)
        def price(units, cost):
            return units * cost

        assert price(units=3, cost=4) == 12

    def test_a_function_that_declares_cost_is_refused_by_map_on_both_paths(self):
        """The one deliberate casualty, and it fails identically on both paths.

        ``cost`` is reserved at the payload's top level, so a ``.map()``
        payload can never deliver it as an argument. The mesh path has always
        raised a missing-argument TypeError here; the local path now raises
        the same one instead of accidentally succeeding.
        """
        @accelerate(_LoopbackMesh())
        def price(units, cost):
            return units * cost

        with pytest.raises(TypeError, match="cost"):
            price.map([{"units": 3, "cost": 4}])

        with patch.dict(os.environ, {"GPUMESH_LOCAL": "1"}):
            with pytest.raises(TypeError, match="cost"):
                price.map([{"units": 3, "cost": 4}])


class TestPlacementHintsInAPayloadAreNotStripped:
    """``gpu``/``gpu_memory_mb``/``cpu_cores`` are not payload annotations.

    They ride at the payload's top level like ``cost``, but distribute() writes
    them there from the *decorator's* keywords — they never come out of a user
    payload. A key of that name inside a ``.map()`` dict therefore lands in
    ``_params`` and is handed to the function on the mesh path, so the local
    fallback must hand it over too. Stripping them would have invented a second
    asymmetry while fixing the first.
    """

    def test_a_gpu_key_reaches_the_function_on_both_paths(self):
        def render(scene, gpu):
            return {"scene": scene, "gpu": gpu}

        payloads = [{"scene": "a", "gpu": "A100"}]
        expected = [{"scene": "a", "gpu": "A100"}]

        assert accelerate(_LoopbackMesh())(render).map(payloads) == expected

        with patch.dict(os.environ, {"GPUMESH_LOCAL": "1"}):
            assert accelerate(_LoopbackMesh())(render).map(payloads) == expected

    def test_cpu_cores_and_gpu_memory_mb_reach_the_function_on_both_paths(self):
        def plan(cpu_cores, gpu_memory_mb):
            return {"cores": cpu_cores, "vram": gpu_memory_mb}

        payloads = [{"cpu_cores": 8, "gpu_memory_mb": 16384}]
        expected = [{"cores": 8, "vram": 16384}]

        assert accelerate(_LoopbackMesh())(plan).map(payloads) == expected

        with patch.dict(os.environ, {"GPUMESH_LOCAL": "1"}):
            assert accelerate(_LoopbackMesh())(plan).map(payloads) == expected

    def test_decorator_hints_still_bypass_the_function_entirely(self):
        """The hints the decorator sets go to distribute(), never to _params."""
        mesh = _LoopbackMesh()

        @accelerate(mesh, cores=1, memory="1MB")
        def train(lr):
            return {"lr": lr}

        assert train.map([{"lr": 0.1, "cost": 2}]) == [{"lr": 0.1}]
        assert mesh.seen_params == [{"lr": 0.1}]
