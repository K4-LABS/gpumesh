"""Tests for gpumesh.serializer module."""

import pytest


class TestSerializeDeserialize:
    """Tests for function serialization round-trip."""

    def test_round_trip_simple_function(self):
        """Simple function survives serialize/deserialize."""
        from gpumesh.serializer import serialize_function, deserialize_function

        def add(a, b):
            return a + b

        data = serialize_function(add)
        func = deserialize_function(data)
        assert func(2, 3) == 5

    def test_round_trip_with_closures(self):
        """Closures are preserved via cloudpickle."""
        from gpumesh.serializer import serialize_function, deserialize_function

        try:
            import cloudpickle
        except ImportError:
            pytest.skip("cloudpickle not installed")

        multiplier = 3

        def multiply(x):
            return x * multiplier

        data = serialize_function(multiply)
        func = deserialize_function(data)
        assert func(5) == 15

    def test_round_trip_lambda(self):
        """Lambdas survive serialization."""
        from gpumesh.serializer import serialize_function, deserialize_function

        try:
            import cloudpickle
        except ImportError:
            pytest.skip("cloudpickle not installed")

        data = serialize_function(lambda x: x ** 2)
        func = deserialize_function(data)
        assert func(7) == 49

    def test_round_trip_returns_dict(self):
        """Function returning dict works correctly."""
        from gpumesh.serializer import serialize_function, deserialize_function

        def make_result(lr=0.1, epochs=100):
            return {"lr": lr, "epochs": epochs, "accuracy": 0.95}

        data = serialize_function(make_result)
        func = deserialize_function(data)
        result = func(lr=0.01, epochs=200)
        assert result["lr"] == 0.01
        assert result["accuracy"] == 0.95

    def test_round_trip_with_imports(self):
        """Function using imports works correctly."""
        from gpumesh.serializer import serialize_function, deserialize_function
        import math

        def compute(x):
            return {"result": math.sqrt(x)}

        data = serialize_function(compute)
        func = deserialize_function(data)
        result = func(16)
        assert result["result"] == 4.0

    def test_different_functions_different_output(self):
        """Different functions produce different serialized forms."""
        from gpumesh.serializer import serialize_function

        def func_a(x):
            return x + 1

        def func_b(x):
            return x * 2

        data_a = serialize_function(func_a)
        data_b = serialize_function(func_b)
        assert data_a != data_b


class TestGetRequiredModules:
    """Tests for module dependency detection."""

    def test_detects_os_module(self):
        """Detects os module when function references it."""
        from gpumesh.serializer import _get_required_modules
        import os as _os_module

        # Create a function that actually uses os in its globals
        ns = {"os": _os_module}
        exec("def uses_os(): return os.path.join('a', 'b')", ns)
        func = ns["uses_os"]

        modules = _get_required_modules(func)
        assert "os" in modules

    def test_builtin_not_in_modules(self):
        """Builtins are excluded from module list."""
        from gpumesh.serializer import _get_required_modules

        def simple(x):
            return x

        modules = _get_required_modules(simple)
        assert "builtins" not in modules


class TestErrorHandling:
    """Tests for error conditions."""

    def test_invalid_base64_raises(self):
        """Invalid base64 data raises error."""
        from gpumesh.serializer import deserialize_function

        with pytest.raises(Exception):
            deserialize_function("not-valid-base64!!!")

    def test_serialize_produces_string(self):
        """Serialization produces a non-empty base64 string."""
        from gpumesh.serializer import serialize_function

        def simple(x):
            return x

        data = serialize_function(simple)
        assert isinstance(data, str)
        assert len(data) > 0


class TestSourceFallback:
    """Tests for source code fallback."""

    def test_serialize_with_source_produces_data(self):
        """_serialize_with_source produces valid base64 data."""
        from gpumesh.serializer import _serialize_with_source

        def my_func(x):
            return x + 1

        data = _serialize_with_source(my_func)
        assert isinstance(data, str)
        assert len(data) > 0

    def test_serialize_with_source_metadata_contains_method(self):
        """Source serialization metadata contains 'method': 'source'."""
        import base64
        import json
        from gpumesh.serializer import _serialize_with_source

        def my_func(x):
            return x

        data = _serialize_with_source(my_func)
        combined = base64.b64decode(data)
        length_prefix = combined[:4]
        metadata_len = int.from_bytes(length_prefix, byteorder="big")
        metadata_bytes = combined[4:4 + metadata_len]
        metadata = json.loads(metadata_bytes)
        assert metadata["method"] == "source"
        assert "source" in metadata
        assert "my_func" in metadata["source"]
