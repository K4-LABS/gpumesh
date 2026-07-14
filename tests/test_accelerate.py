"""Tests for the @accelerate decorator."""
from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from gpumesh.accelerate import AcceleratedFunction, accelerate, install


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
