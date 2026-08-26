"""Tests for gpumesh.serializer module."""

import pytest


def _force_source_path(data, python_version="9.9", drop_key=False):
    """Rewrite a serialized payload's python_version to force the source path.

    ``"9.9"`` can never match a real interpreter, so the deserializer takes the
    same branch a worker on a mismatched Python minor version would.
    ``drop_key=True`` removes the key entirely, as a pre-0.8.1 client would;
    ``python_version=None`` leaves it present but JSON null.
    """
    import base64
    import json

    combined = base64.b64decode(data)
    metadata_len = int.from_bytes(combined[:4], byteorder="big")
    metadata = json.loads(combined[4:4 + metadata_len])
    if drop_key:
        metadata.pop("python_version", None)
    else:
        metadata["python_version"] = python_version
    mb = json.dumps(metadata).encode("utf-8")
    return base64.b64encode(
        len(mb).to_bytes(4, byteorder="big") + mb + combined[4 + metadata_len:]
    ).decode("ascii")


def _payload_from_module_source(tmp_path, mod_name, source, attr,
                                python_version="9.9"):
    """Serialize ``attr`` from a real on-disk module, forcing the source path.

    inspect.getsource() needs a real file, so the function under test has to
    live in one. The module is removed from sys.modules afterwards: these tests
    reuse short names like ``decorated_mod``, and a stale entry would hand a
    later test the previous test's module instead of importing its own.
    """
    import sys
    from gpumesh.serializer import _serialize_with_cloudpickle

    (tmp_path / f"{mod_name}.py").write_text(source, encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    try:
        mod = __import__(mod_name)
        data = _serialize_with_cloudpickle(getattr(mod, attr))
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop(mod_name, None)

    return _force_source_path(data, python_version)


def _source_payload(func_name, source, **metadata_overrides):
    """Build a method='source' payload directly from source text.

    Some of the shapes under test (a callable object, a method) cannot be fed
    through inspect.getsource() at all, so the payload is assembled by hand
    exactly as _serialize_with_source() would.
    """
    import base64
    import json

    metadata = {
        "method": "source",
        "modules": [],
        "module_globals": {},
        "func_name": func_name,
        "source": source,
        "python_version": "9.9",
    }
    metadata.update(metadata_overrides)
    mb = json.dumps(metadata).encode("utf-8")
    return base64.b64encode(
        len(mb).to_bytes(4, byteorder="big") + mb
    ).decode("ascii")


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
        from gpumesh.serializer import deserialize_function

        forced = _payload_from_module_source(
            tmp_path,
            "decorated_mod",
            "from gpumesh import mesh\n\n"
            "@mesh\n"
            "def heavy(size=8):\n"
            "    return {'size': size, 'squared': size * size}\n",
            "heavy",
        )

        func = deserialize_function(forced)
        assert func(size=4) == {"size": 4, "squared": 16}

    def test_cross_version_without_source_raises_cleanly(self):
        """A version mismatch with no source fallback must fail loudly.

        Regression: a function defined in a heredoc/interactive session has
        no source file, so inspect.getsource() cannot capture it and the
        metadata carries no ``source`` key. On a worker running a different
        Python version, cloudpickle.loads() of foreign bytecode can "succeed"
        and return a function that hard-crashes the interpreter (0xC0000005
        on Windows) the moment it is called. The deserializer must refuse
        instead of handing the worker a bomb.
        """
        import base64
        import json
        import sys
        import cloudpickle
        from gpumesh.serializer import deserialize_function

        def original(x):
            return x * 2

        # Build a payload exactly like _serialize_with_cloudpickle but with a
        # fake, different python_version and NO source key (as if the function
        # had been defined in a heredoc where getsource fails).
        func_bytes = cloudpickle.dumps(original)
        metadata = {
            "method": "cloudpickle",
            "modules": [],
            "module_globals": {},
            "func_name": "original",
            "python_version": "9.9",  # guaranteed different from any real one
        }
        metadata_bytes = json.dumps(metadata).encode("utf-8")
        combined = (
            len(metadata_bytes).to_bytes(4, byteorder="big")
            + metadata_bytes
            + func_bytes
        )
        payload = base64.b64encode(combined).decode("ascii")

        with pytest.raises(ValueError, match="no source"):
            deserialize_function(payload)

        # Same payload with a matching version must still work (cloudpickle
        # path is fine when versions agree).
        metadata["python_version"] = f"{sys.version_info.major}.{sys.version_info.minor}"
        metadata_bytes = json.dumps(metadata).encode("utf-8")
        combined = (
            len(metadata_bytes).to_bytes(4, byteorder="big")
            + metadata_bytes
            + func_bytes
        )
        payload = base64.b64encode(combined).decode("ascii")
        assert deserialize_function(payload)(21) == 42


class TestDecoratorStripping:
    """Only gpumesh's own decorators may be removed from captured source."""

    def test_non_gpumesh_decorator_is_preserved(self):
        """Stripping every decorator silently changes what the task does.

        Regression: the source path dropped all lines before the ``def``, so a
        function carrying both ``@mesh`` and e.g. ``@torch.no_grad()`` ran with
        the second decorator applied on a same-version worker and without it on
        a cross-version one — different behaviour, no warning.
        """
        from gpumesh.serializer import deserialize_function

        payload = _source_payload(
            "heavy",
            "@mesh\n"
            "@functools.lru_cache\n"
            "def heavy(x=1):\n"
            "    return x + 1\n",
            module_globals={"functools": "functools"},
        )

        func = deserialize_function(payload)
        assert func(x=3) == 4
        # @functools.lru_cache survived; stripping it leaves a bare function.
        assert hasattr(func, "cache_info")

    def test_multiline_gpumesh_decorator_is_fully_removed(self):
        """A @mesh(...) call spanning lines must not leave a fragment behind."""
        from gpumesh.serializer import deserialize_function

        payload = _source_payload(
            "heavy",
            "@mesh(\n"
            '    gpu="A100",\n'
            '    note="a ( unbalanced paren in a string",\n'
            ")\n"
            "def heavy(x=1):\n"
            "    return x * 3\n",
        )

        assert deserialize_function(payload)(x=4) == 12

    @pytest.mark.parametrize("quotes", ['"""', "'''"])
    def test_triple_quoted_decorator_arg_does_not_end_the_span(self, quotes):
        """A multi-line string argument must not be read as real brackets.

        Regression: the bracket scan restarted from "no string is open" on
        every line, so the ``) ) )`` sitting in the note's *text* closed the
        decorator's parenthesis three lines early. The span ended there, and
        the leftover closing quotes, comma and paren were prepended to the def,
        so exec() raised SyntaxError before the task could run at all.
        """
        from gpumesh.serializer import deserialize_function

        payload = _source_payload(
            "train",
            "@accelerate(\n"
            "    mesh,\n"
            f"    note={quotes}\n"
            "    ) ) ) not real brackets\n"
            f"    {quotes},\n"
            ")\n"
            "def train(lr=0.1):\n"
            "    return lr * 2\n",
        )

        assert deserialize_function(payload)(lr=0.5) == 1.0

    def test_single_quoted_decorator_arg_with_bracket_is_removed(self):
        """The single-line case the scanner already handled must stay handled."""
        from gpumesh.serializer import deserialize_function

        payload = _source_payload(
            "train",
            "@mesh(\n"
            "    gpu='A100',\n"
            "    note=') unbalanced inside a single-quoted string',\n"
            ")\n"
            "def train(lr=0.1):\n"
            "    return lr * 4\n",
        )

        assert deserialize_function(payload)(lr=0.5) == 2.0

    def test_hash_inside_a_decorator_arg_is_not_a_comment(self):
        """``#`` inside a string must not stop the scan mid-line.

        If it did, the ``)`` after it would go uncounted and the span would run
        past the closing paren into the def.
        """
        from gpumesh.serializer import deserialize_function

        payload = _source_payload(
            "train",
            "@mesh(\n"
            '    note="# not a comment )",\n'
            '    gpu="A100",\n'
            ")\n"
            "def train(lr=1):\n"
            "    return lr + 1\n",
        )

        assert deserialize_function(payload)(lr=1) == 2

    @pytest.mark.parametrize("decorator", [
        "@mesh",
        '@mesh(gpu="A100")',
        "@accelerate",
        "@accelerate(mesh)",
        "@gpumesh.mesh",
    ])
    def test_gpumesh_decorator_forms_are_stripped(self, decorator):
        from gpumesh.serializer import deserialize_function

        payload = _source_payload(
            "heavy",
            f"{decorator}\ndef heavy(x=1):\n    return x + 10\n",
        )
        assert deserialize_function(payload)(x=1) == 11

    def test_unavailable_decorator_names_itself_in_the_error(self):
        """A preserved decorator that will not resolve must say so."""
        from gpumesh.serializer import deserialize_function

        payload = _source_payload(
            "heavy",
            "@mesh\n"
            "@some_missing_decorator\n"
            "def heavy(x=1):\n"
            "    return x\n",
        )

        with pytest.raises(ValueError, match="some_missing_decorator"):
            deserialize_function(payload)

    @pytest.mark.parametrize("def_line", [
        "def heavy (x=1):",
        "async  def heavy(x=1):",
        "def heavy[T](x=1):",
    ])
    def test_def_line_variants_are_recognized(self, def_line):
        """These are all legal ``def`` forms the literal startswith() missed.

        When the def line is not found the decorator lines stay put and exec()
        fails with the original ``NameError: name 'mesh' is not defined``.
        PEP 695 generics only parse on 3.12+, so this checks the stripping
        itself rather than executing the result.
        """
        from gpumesh.serializer import _strip_gpumesh_decorators

        stripped = _strip_gpumesh_decorators(
            f"@mesh\n{def_line}\n    return x\n", "heavy"
        )
        assert "@mesh" not in stripped
        assert stripped.startswith(def_line)


class TestSourceFallbackRefusals:
    """Shapes the source fallback cannot honestly rebuild must be refused."""

    def test_method_is_refused(self):
        """A method rebuilt at module level still declares ``self``.

        The worker calls func(**params) with no instance, so without this check
        the failure surfaces inside the subprocess as a confusing "missing 1
        required positional argument: 'self'".
        """
        from gpumesh.serializer import deserialize_function

        payload = _source_payload(
            "heavy",
            "    @mesh\n"
            "    def heavy(self, x):\n"
            "        return x * 2\n",
        )

        with pytest.raises(ValueError, match="method"):
            deserialize_function(payload)

    def test_classmethod_is_refused(self):
        from gpumesh.serializer import deserialize_function

        payload = _source_payload(
            "heavy",
            "    def heavy(cls, x):\n        return x\n",
        )
        with pytest.raises(ValueError, match="'cls'"):
            deserialize_function(payload)

    def test_callable_object_is_refused(self):
        """A callable instance is captured as its class, which would construct.

        The worker calls ``Heavy(**params)``; if __init__ accepted the kwargs
        the task would silently return an instance instead of a result.
        """
        from gpumesh.serializer import deserialize_function

        payload = _source_payload(
            "Heavy",
            "class Heavy:\n"
            "    def __init__(self, x=1):\n"
            "        self.x = x\n"
            "    def __call__(self, x=1):\n"
            "        return x * 2\n",
        )

        with pytest.raises(ValueError, match="not a function"):
            deserialize_function(payload)

    def test_async_function_is_refused(self):
        """The subprocess never awaits, so a coroutine cannot be a result."""
        from gpumesh.serializer import deserialize_function

        payload = _source_payload(
            "heavy",
            "@mesh\nasync def heavy(x=1):\n    return x * 2\n",
        )

        with pytest.raises(ValueError, match="async"):
            deserialize_function(payload)

    def test_malformed_source_payload_does_not_blame_the_python_version(self):
        """Dead wording: this guard cannot be reached via a version mismatch."""
        import base64
        import json
        from gpumesh.serializer import deserialize_function

        metadata = {
            "method": "source",
            "modules": [],
            "module_globals": {},
            "func_name": "heavy",
            "python_version": f"{__import__('sys').version_info.major}."
                              f"{__import__('sys').version_info.minor}",
        }
        mb = json.dumps(metadata).encode("utf-8")
        payload = base64.b64encode(
            len(mb).to_bytes(4, byteorder="big") + mb
        ).decode("ascii")

        with pytest.raises(ValueError, match="malformed or truncated"):
            deserialize_function(payload)


class TestUnknownPythonVersion:
    """A missing python_version means 'unknown' — which is not a mismatch.

    ``python_version`` only exists from v0.8.1 on, so an older client sends a
    payload without it. Nothing about that says the two machines disagree, and
    the overwhelmingly common case is that they agree, so an unknown version
    must not divert the payload away from cloudpickle. Only a version we were
    actually told about, and that differs, may do that.
    """

    @staticmethod
    def _marker_payload(tmp_path, mod_name, **force):
        """A working cloudpickle payload whose source rebuild is detectable.

        The pickled bytes are valid for this interpreter, so the deserializer
        succeeds either way and the only observable difference is which path
        ran: cloudpickle keeps the module global ``MARKER`` in the function's
        globals, the source rebuild exec's into a fresh namespace without it.
        """
        import sys
        from gpumesh.serializer import _serialize_with_cloudpickle

        (tmp_path / f"{mod_name}.py").write_text(
            "from gpumesh import mesh\n\n"
            "MARKER = 'original'\n\n"
            "@mesh\n"
            "def heavy(x=1):\n"
            "    return (x, MARKER)\n",
            encoding="utf-8",
        )
        sys.path.insert(0, str(tmp_path))
        try:
            mod = __import__(mod_name)
            data = _serialize_with_cloudpickle(mod.heavy)
        finally:
            sys.path.remove(str(tmp_path))
            sys.modules.pop(mod_name, None)

        return _force_source_path(data, **force)

    def test_absent_python_version_uses_cloudpickle(self, tmp_path):
        """Regression: an absent version forced the source path and lost globals.

        The payload carries a ``source`` key (every cloudpickle payload does,
        when getsource can see the file), so treating the unknown version as a
        mismatch sent it down the source path. That path only restores
        *module-valued* globals, so ``MARKER`` vanished and the rebuilt function
        raised ``NameError`` when it was called — inside the task subprocess,
        long after the deserializer had already declared success.
        """
        from gpumesh.serializer import deserialize_function

        payload = self._marker_payload(tmp_path, "absent_ver_mod", drop_key=True)
        assert deserialize_function(payload)(x=1) == (1, "original")

    def test_null_python_version_uses_cloudpickle(self, tmp_path):
        """An explicit JSON null must be treated like an absent key."""
        from gpumesh.serializer import deserialize_function

        payload = self._marker_payload(tmp_path, "null_ver_mod",
                                       python_version=None)
        assert deserialize_function(payload)(x=1) == (1, "original")

    def test_absent_python_version_keeps_bound_methods_working(self, tmp_path):
        """A pre-0.8.1 client's bound method must not hit the source refusal.

        cloudpickle ships a bound method with its instance attached and it
        simply works. The source path cannot: it rebuilds the method body as a
        bare function with no instance, so it refuses outright. Diverting a
        legacy payload to source therefore broke bound methods that had worked
        since v0.5.0.
        """
        import sys
        from gpumesh.serializer import (
            _serialize_with_cloudpickle, deserialize_function,
        )

        (tmp_path / "legacy_method_mod.py").write_text(
            "class Trainer:\n"
            "    def __init__(self, scale):\n"
            "        self.scale = scale\n\n"
            "    def work(self, x):\n"
            "        return x * self.scale\n",
            encoding="utf-8",
        )
        # Unlike the other helpers here, the module has to stay importable
        # across the deserialize call: cloudpickle records ``Trainer`` by
        # reference, so unpickling re-imports it. Tearing the module down first
        # would make cloudpickle.loads() fail and fall back to source, which is
        # the very path this test exists to prove we no longer take.
        sys.path.insert(0, str(tmp_path))
        try:
            mod = __import__("legacy_method_mod")
            data = _serialize_with_cloudpickle(mod.Trainer(3).work)
            payload = _force_source_path(data, drop_key=True)
            assert deserialize_function(payload)(4) == 12
        finally:
            sys.path.remove(str(tmp_path))
            sys.modules.pop("legacy_method_mod", None)

    def test_matching_version_still_uses_cloudpickle(self, tmp_path):
        """The same-version path must be untouched by the unknown-version rule."""
        import sys
        from gpumesh.serializer import deserialize_function

        payload = self._marker_payload(
            tmp_path, "same_ver_mod",
            python_version=f"{sys.version_info.major}.{sys.version_info.minor}",
        )
        assert deserialize_function(payload)(x=1) == (1, "original")

    def test_known_mismatch_with_source_still_prefers_source(self, tmp_path):
        """A version we were told about, and that differs, still diverts.

        Foreign bytecode is the one case where losing ``MARKER`` beats handing
        cloudpickle.loads() bytes that can crash the interpreter outright, so
        this half of the decision table must not move.
        """
        from gpumesh.serializer import deserialize_function

        payload = self._marker_payload(tmp_path, "known_diff_mod",
                                       python_version="9.9")
        func = deserialize_function(payload)
        with pytest.raises(NameError):
            func(x=1)

    def test_unknown_version_without_source_still_runs(self):
        """v0.5.0 sent neither key; refusing would break every task it submits."""
        import base64
        import json
        import cloudpickle
        from gpumesh.serializer import deserialize_function

        def original(x):
            return x * 3

        metadata = {
            "method": "cloudpickle",
            "modules": [],
            "module_globals": {},
            "func_name": "original",
        }
        mb = json.dumps(metadata).encode("utf-8")
        payload = base64.b64encode(
            len(mb).to_bytes(4, byteorder="big") + mb
            + cloudpickle.dumps(original)
        ).decode("ascii")

        assert deserialize_function(payload)(5) == 15


class TestWorkerErrorClassification:
    """A deliberate refusal must not be reported as an unexpected error."""

    def test_cross_version_refusal_becomes_a_task_error(self, monkeypatch):
        """Regression: a bare ValueError missed the TaskError branch.

        worker.py's task loop forwards ``user_error`` from TaskError and posts
        anything else as "Unexpected error: ...", which labelled this
        deliberate refusal a surprise.
        """
        from gpumesh import sandbox, serializer, worker

        message = (
            "Cannot run this task on a Python 3.11 worker: the function was "
            "serialized on Python 3.9 with no source fallback"
        )

        def boom(_data):
            raise ValueError(message)

        monkeypatch.setattr(serializer, "deserialize_function", boom)

        with pytest.raises(sandbox.TaskError) as excinfo:
            worker._run_function_task({"_func": "irrelevant"}, "cpu", 10)

        assert str(excinfo.value) == message
        # Failed once, not retried. The refusal is deterministic for a given
        # pair of Python versions, and the "a retry may land on a
        # matching-version worker" hope this used to rest on never actually
        # fires: BUSY_POLL_INTERVAL is 0.1s, so the same incompatible worker
        # re-leases the task long before any other worker polls. Retrying only
        # produced three identical failures and three diagnostics blocks.
        assert excinfo.value.user_error is True


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


class TestStrictResultMode:
    """Strict mode refuses to unpickle a worker's result (issue #15).

    decode_result() hands cloudpickle bytes the *worker* chose to
    cloudpickle.loads(), which is arbitrary code execution on whichever
    machine collects the result — normally the operator's own laptop, the one
    with the SSH keys on it. A valid token makes a worker trusted, not honest,
    so an operator has to be able to say "never execute what a worker sent".
    That is a real restriction rather than transparent hardening (a task
    returning a tensor stops working), so what these tests pin down is that
    the refusal is total, that JSON results keep working untouched, and that
    the error says which switch to reach for.
    """

    @staticmethod
    def _pickled_envelope():
        """A cloudpickle envelope, built and shipped the way a worker's is.

        The value is deliberately one JSON cannot express, so encode_result
        takes the pickled branch; the json round-trip mimics the hop through
        the coordinator's store, which is where a real client's envelope comes
        from.
        """
        pytest.importorskip("cloudpickle")
        np = pytest.importorskip("numpy")
        import json
        from gpumesh.serializer import encode_result, RESULT_ENVELOPE_KEY

        envelope = encode_result({"arr": np.arange(3)})
        assert envelope[RESULT_ENVELOPE_KEY]["encoding"] == "cloudpickle"
        return json.loads(json.dumps(envelope))

    def test_json_results_decode_identically_either_way(self):
        """Strict mode must cost nothing for results that were never pickled.

        This is the whole bargain: turning it on may only break tasks that
        return objects JSON cannot express. If a plain dict or an int decoded
        differently under strict mode, the flag would be unusable in practice.
        """
        from gpumesh.serializer import encode_result, decode_result

        for value in (5, None, "text", [1, 2], {"accuracy": 0.95}):
            envelope = encode_result(value)
            assert decode_result(envelope, strict=False) == value
            assert decode_result(envelope, strict=True) == value

    def test_pickled_result_decodes_when_strict_is_off(self):
        """The default stays permissive — nothing is refused unless asked."""
        from gpumesh.serializer import decode_result

        decoded = decode_result(self._pickled_envelope(), strict=False)
        assert list(decoded["arr"]) == [0, 1, 2]

    def test_pickled_result_is_refused_when_strict_is_on(self):
        """The point of the flag: the bytes never reach cloudpickle.loads()."""
        from gpumesh.serializer import decode_result, UntrustedResultError

        with pytest.raises(UntrustedResultError):
            decode_result(self._pickled_envelope(), strict=True)

    def test_environment_variable_turns_strict_on(self, monkeypatch):
        """GPUMESH_STRICT_RESULTS=1 reaches decode_result with no argument.

        The variable is how an operator turns this on for a whole shell or a
        systemd unit, so the default ``strict=None`` has to consult it rather
        than only honouring an explicit keyword at each call site.
        """
        from gpumesh.serializer import decode_result, UntrustedResultError

        monkeypatch.setenv("GPUMESH_STRICT_RESULTS", "1")
        with pytest.raises(UntrustedResultError):
            decode_result(self._pickled_envelope())

    def test_explicit_false_overrides_the_environment(self, monkeypatch):
        """A caller that says strict=False means it, exported variable or not.

        Precedence has to run argument-then-environment so a single call site
        — a notebook cell that knows its own worker — can opt out without the
        operator unsetting a variable for the whole session.
        """
        from gpumesh.serializer import decode_result

        monkeypatch.setenv("GPUMESH_STRICT_RESULTS", "1")
        decoded = decode_result(self._pickled_envelope(), strict=False)
        assert list(decoded["arr"]) == [0, 1, 2]

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes",
                                       "YES", "on", "On", " 1 "])
    def test_strict_results_enabled_accepts_truthy_spellings(self, value,
                                                             monkeypatch):
        """Operators type ``true`` and ``yes`` as readily as ``1``."""
        from gpumesh.serializer import strict_results_enabled

        monkeypatch.setenv("GPUMESH_STRICT_RESULTS", value)
        assert strict_results_enabled() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
    def test_strict_results_enabled_rejects_falsy_spellings(self, value,
                                                            monkeypatch):
        """Anything that is not an affirmative leaves strict mode off.

        ``0`` and ``false`` especially: a variable set to ``0`` reads as
        "explicitly disabled", and treating any non-empty value as true would
        silently break every task that returns a tensor.
        """
        from gpumesh.serializer import strict_results_enabled

        monkeypatch.setenv("GPUMESH_STRICT_RESULTS", value)
        assert strict_results_enabled() is False

    def test_strict_results_enabled_defaults_off(self, monkeypatch):
        """With nothing set at all, results decode as they always have."""
        from gpumesh.serializer import strict_results_enabled

        monkeypatch.delenv("GPUMESH_STRICT_RESULTS", raising=False)
        assert strict_results_enabled() is False
        assert strict_results_enabled(True) is True
        assert strict_results_enabled(False) is False

    def test_non_envelope_payloads_pass_through_under_strict(self, monkeypatch):
        """Script tasks emit bare JSON; strict mode has nothing to refuse there.

        Nothing was pickled, so nothing would be executed, and refusing these
        would turn strict mode into "script tasks stop reporting results".
        """
        from gpumesh.serializer import decode_result

        monkeypatch.setenv("GPUMESH_STRICT_RESULTS", "1")
        assert decode_result({"rows": 10}) == {"rows": 10}
        assert decode_result(None) is None
        assert decode_result(42) == 42
        assert decode_result([1, 2, 3]) == [1, 2, 3]
        # A dict whose envelope key holds something that is not an envelope is
        # still just a dict.
        assert decode_result({"__gpumesh_result__": "nope"}) == {
            "__gpumesh_result__": "nope"
        }

    def test_first_pickled_decode_warns_once(self):
        """Non-strict decoding says, once, that it is executing worker bytes.

        Once per process: a 500-task job that printed 500 identical lines
        would teach the reader to filter them out, which is the same as not
        warning at all. On the first decode rather than at import, so a mesh
        whose results are all JSON is never told it runs a risk it does not.
        """
        import warnings
        import gpumesh.serializer as serializer

        envelope = self._pickled_envelope()
        serializer._pickle_warning_shown = False
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            serializer.decode_result(envelope, strict=False)
            serializer.decode_result(envelope, strict=False)

        runtime = [w for w in caught if issubclass(w.category, RuntimeWarning)]
        assert len(runtime) == 1
        assert "unpickle" in str(runtime[0].message)
        assert "GPUMESH_STRICT_RESULTS" in str(runtime[0].message)

    def test_strict_mode_does_not_warn(self):
        """Nothing is unpickled, so there is no risk to announce."""
        import warnings
        import gpumesh.serializer as serializer
        from gpumesh.serializer import UntrustedResultError

        envelope = self._pickled_envelope()
        serializer._pickle_warning_shown = False
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                with pytest.raises(UntrustedResultError):
                    serializer.decode_result(envelope, strict=True)
            assert not [w for w in caught
                        if issubclass(w.category, RuntimeWarning)]
        finally:
            # The refusal left the one-time warning unspent; other tests in
            # this process should not inherit a primed flag from this one.
            serializer._pickle_warning_shown = True

    def test_refusal_names_both_switches(self):
        """An operator reading the error must be able to find the flag.

        The refusal is the only place a surprised user meets this feature, so
        it has to name both spellings — the variable they may have inherited
        from a unit file, and the CLI flag they can drop for a single run.
        """
        from gpumesh.serializer import decode_result, UntrustedResultError

        with pytest.raises(UntrustedResultError) as excinfo:
            decode_result(self._pickled_envelope(), strict=True)

        message = str(excinfo.value)
        assert "GPUMESH_STRICT_RESULTS" in message
        assert "--strict" in message

    def test_untrusted_result_error_is_a_runtime_error(self):
        """Callers that already catch RuntimeError keep catching this."""
        from gpumesh.serializer import UntrustedResultError

        assert issubclass(UntrustedResultError, RuntimeError)
