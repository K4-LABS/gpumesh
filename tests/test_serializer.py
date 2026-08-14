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

    def test_decorated_function_survives_source_fallback(self, tmp_path):
        """@mesh-decorated functions work when the source path is used.

        Regression: with mismatched Python versions the worker reconstructs
        the function from its source, which includes the ``@mesh`` decorator
        line. exec() of that source used to raise ``NameError: name 'mesh'
        is not defined`` because decorator names are not in the metadata.
        """
        import base64
        import json
        import sys
        from gpumesh.serializer import _serialize_with_cloudpickle, \
            deserialize_function

        # Define the function in a real module so inspect.getsource works.
        mod = tmp_path / "decorated_mod.py"
        mod.write_text(
            "from gpumesh import mesh\n\n"
            "@mesh\n"
            "def heavy(size=8):\n"
            "    return {'size': size, 'squared': size * size}\n",
            encoding="utf-8",
        )
        sys.path.insert(0, str(tmp_path))
        try:
            import decorated_mod
            data = _serialize_with_cloudpickle(decorated_mod.heavy)
        finally:
            sys.path.remove(str(tmp_path))

        # Force the worker's source path by faking a Python version mismatch.
        combined = base64.b64decode(data)
        metadata_len = int.from_bytes(combined[:4], byteorder="big")
        metadata = json.loads(combined[4:4 + metadata_len])
        metadata["python_version"] = "9.9"
        mb = json.dumps(metadata).encode("utf-8")
        forced = base64.b64encode(
            len(mb).to_bytes(4, byteorder="big") + mb + combined[4 + metadata_len:]
        ).decode("ascii")

        func = deserialize_function(forced)
        assert func(size=4) == {"size": 4, "squared": 16}


class TestResultEnvelope:
    """Tests for the task-result envelope.

    Results cross the coordinator as JSON, so the envelope has to carry both
    plain JSON values and objects JSON cannot express, and unwrap to exactly
    what the function returned.
    """

    @pytest.mark.parametrize("value", [
        5,
        0,
        None,
        True,
        False,
        "text",
        3.5,
        [1, 2, 3],
        [],
        {},
        {"accuracy": 0.95},
        {"nested": {"a": [1, {"b": 2}]}},
    ])
    def test_json_values_round_trip_unchanged(self, value):
        from gpumesh.serializer import encode_result, decode_result

        assert decode_result(encode_result(value)) == value

    def test_envelope_is_json_encodable(self):
        """The coordinator stores results as JSON, so the envelope must be."""
        import json
        from gpumesh.serializer import encode_result

        json.dumps(encode_result({"accuracy": 0.95}))
        json.dumps(encode_result(5))

    def test_non_json_value_round_trips_via_cloudpickle(self):
        pytest.importorskip("cloudpickle")
        np = pytest.importorskip("numpy")
        import json
        from gpumesh.serializer import encode_result, decode_result

        value = {"loss": np.float32(1.5), "arr": np.arange(4)}
        envelope = encode_result(value)

        # Must survive the JSON hop through the coordinator.
        envelope = json.loads(json.dumps(envelope))
        decoded = decode_result(envelope)

        assert decoded["loss"] == np.float32(1.5)
        assert list(decoded["arr"]) == [0, 1, 2, 3]

    def test_decode_passes_through_plain_dicts(self):
        """Script tasks and older workers emit bare dicts; leave them alone."""
        from gpumesh.serializer import decode_result

        assert decode_result({"rows": 10}) == {"rows": 10}
        assert decode_result(None) is None
        assert decode_result(42) == 42

    def test_is_result_envelope(self):
        from gpumesh.serializer import encode_result, is_result_envelope

        assert is_result_envelope(encode_result({"a": 1}))
        assert not is_result_envelope({"a": 1})
        assert not is_result_envelope(None)

    def test_subprocess_helper_encoding_matches_serializer(self):
        """_function_subprocess duplicates encode_result; keep them in step."""
        import importlib.util
        import os
        import gpumesh
        from gpumesh.serializer import decode_result, RESULT_ENVELOPE_KEY

        helper_path = os.path.join(
            os.path.dirname(gpumesh.__file__), "_function_subprocess.py"
        )
        spec = importlib.util.spec_from_file_location("_gpumesh_helper", helper_path)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)

        assert helper.RESULT_ENVELOPE_KEY == RESULT_ENVELOPE_KEY
        for value in (5, None, {"a": 1}, [1, 2]):
            assert decode_result(helper._encode_result(value)) == value
