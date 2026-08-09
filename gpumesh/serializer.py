"""Function serialization for the Python API.

Uses cloudpickle to serialize Python functions so they can be sent
over the network to worker machines for execution.

Requires: pip install gpumesh[notebook]  (installs cloudpickle + pandas)
"""

from __future__ import annotations

import base64
import importlib
import inspect
import sys
import textwrap
import types


def serialize_function(func) -> str:
    """Serialize a Python function to a base64-encoded string.

    Primary: cloudpickle (handles closures, lambdas, nested functions)
    Fallback: inspect.getsource() (only works for simple top-level functions)

    The serialized form includes:
    1. The pickled function bytes (cloudpickle) or source code text
    2. A list of module names that need to be importable on the worker
    3. Python version for cross-version compatibility detection

    Args:
        func: A callable Python function

    Returns:
        Base64-encoded string containing the serialized function

    Raises:
        ImportError: If cloudpickle is not installed
        ValueError: If function cannot be serialized
    """
    # Try cloudpickle first (preferred)
    try:
        import cloudpickle
        return _serialize_with_cloudpickle(func)
    except ImportError:
        pass

    # Fallback to inspect.getsource
    return _serialize_with_source(func)


def _serialize_with_cloudpickle(func) -> str:
    """Serialize using cloudpickle."""
    import cloudpickle
    import json

    func_bytes = cloudpickle.dumps(func)

    metadata = {
        "method": "cloudpickle",
        "modules": _get_required_modules(func),
        "module_globals": _get_module_globals(func),
        "func_name": _callable_name(func),
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
    }

    # Also try to include source as fallback for cross-version compatibility
    try:
        source = inspect.getsource(func)
        metadata["source"] = source
    except (OSError, TypeError):
        pass  # Source not available (e.g., interactive session)

    metadata_bytes = json.dumps(metadata).encode("utf-8")
    length_prefix = len(metadata_bytes).to_bytes(4, byteorder="big")

    combined = length_prefix + metadata_bytes + func_bytes
    return base64.b64encode(combined).decode("ascii")


def _serialize_with_source(func) -> str:
    """Serialize using inspect.getsource (fallback)."""
    import json
    import sys as _sys

    # Warn the user that we're using the fallback method
    try:
        if hasattr(_sys.stderr, 'isatty') and _sys.stderr.isatty():
            _sys.stderr.write("[gpumesh] NOTE: cloudpickle not installed — using source code fallback.\n")
            _sys.stderr.write("[gpumesh] Install cloudpickle for full serialization: pip install gpumesh[notebook]\n")
    except Exception:
        pass  # Don't let the warning break serialization

    try:
        source = inspect.getsource(func)
    except (OSError, TypeError) as e:
        raise ValueError(
            f"Cannot serialize function {_callable_name(func)}: "
            f"{e}. Install cloudpickle for full serialization support: "
            f"pip install gpumesh[notebook]"
        )

    metadata = {
        "method": "source",
        "modules": _get_required_modules(func),
        "module_globals": _get_module_globals(func),
        "func_name": _callable_name(func),
        "source": source,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
    }

    metadata_bytes = json.dumps(metadata).encode("utf-8")
    length_prefix = len(metadata_bytes).to_bytes(4, byteorder="big")

    combined = length_prefix + metadata_bytes
    return base64.b64encode(combined).decode("ascii")


def deserialize_function(data: str):
    """Deserialize a base64-encoded function string back to a callable.

    Args:
        data: Base64-encoded string from serialize_function()

    Returns:
        The deserialized callable function

    Raises:
        ImportError: If cloudpickle is not installed
        ValueError: If function cannot be deserialized
    """
    import json

    combined = base64.b64decode(data)

    # Extract metadata
    length_prefix = combined[:4]
    metadata_len = int.from_bytes(length_prefix, byteorder="big")
    metadata_bytes = combined[4:4 + metadata_len]
    rest = combined[4 + metadata_len:]

    metadata = json.loads(metadata_bytes)
    method = metadata.get("method", "cloudpickle")
    source_version = metadata.get("python_version")
    current_version = f"{sys.version_info.major}.{sys.version_info.minor}"

    # Check for Python version mismatch
    if source_version and source_version != current_version:
        # If we have source code, try that first
        if "source" in metadata:
            method = "source"

    # Ensure required modules are importable
    for mod_name in metadata.get("modules", []):
        if mod_name not in sys.modules:
            try:
                importlib.import_module(mod_name)
            except ImportError:
                pass  # Module might not be critical; cloudpickle will handle it

    if method == "cloudpickle":
        # Skip cloudpickle if we know it will fail due to version mismatch
        # and source is available as fallback
        if source_version and source_version != current_version and "source" in metadata:
            method = "source"  # Use source instead
        else:
            try:
                import cloudpickle
            except ImportError:
                if "source" in metadata:
                    method = "source"
                else:
                    raise ImportError(
                        "cloudpickle is required for deserialization. "
                        "Install it with: pip install gpumesh[notebook]"
                    )
            try:
                if method == "cloudpickle":
                    return cloudpickle.loads(rest)
            except Exception as e:
                # If cloudpickle fails (e.g., version mismatch), try source if available
                if "source" in metadata:
                    method = "source"
                else:
                    raise ValueError(
                        f"Failed to deserialize function: {e}. "
                        f"This may be due to Python version mismatch "
                        f"(serialized on {source_version}, running on {current_version}). "
                        f"Try using the same Python version on both machines."
                    )

    if method == "source":
        if "source" not in metadata:
            raise ValueError(
                "Function was serialized with cloudpickle but source code is not available. "
                f"This may be due to Python version mismatch "
                f"(serialized on {source_version}, running on {current_version}). "
                f"Try using the same Python version on both machines."
            )
        # Reconstruct function from source code
        source = textwrap.dedent(metadata["source"])
        func_name = metadata["func_name"]

        # Recreate imported module globals under the names used by the
        # original function, including aliases such as ``np`` for ``numpy``.
        namespace = {}
        for global_name, module_name in metadata.get("module_globals", {}).items():
            try:
                namespace[global_name] = importlib.import_module(module_name)
            except ImportError:
                pass

        # Older serialized data only recorded top-level module names. Bind
        # those canonical names as a best-effort compatibility fallback.
        for module_name in metadata.get("modules", []):
            global_name = module_name.split(".", 1)[0]
            if global_name not in namespace:
                try:
                    namespace[global_name] = importlib.import_module(global_name)
                except ImportError:
                    pass

        exec(compile(source, f"<{func_name}>", "exec"), namespace)

        if func_name not in namespace:
            raise ValueError(
                f"Function '{func_name}' not found in source code"
            )
        return namespace[func_name]

    else:
        raise ValueError(f"Unknown serialization method: {method}")


# -- task results ------------------------------------------------------------
#
# A task's return value travels: worker subprocess -> worker -> coordinator
# (stored as JSON in SQLite) -> client. Only the middle hop constrains us: the
# coordinator persists results as JSON, so whatever crosses it must be
# JSON-encodable.
#
# Plain JSON is not enough. Real workloads return numpy scalars, arrays, torch
# tensors and DataFrames, none of which json.dumps() accepts, and a bare
# json.dumps() also cannot express "this function returned the integer 5"
# without wrapping it in a dict and changing the value the caller sees.
#
# So every function result is wrapped in an envelope: JSON-encodable values
# pass through verbatim, everything else is cloudpickled and base64'd. The
# client unwraps it, so a function returns exactly the same object whether it
# ran on the mesh or locally.
#
# NOTE: _function_subprocess.py deliberately duplicates the *encoding* half of
# this. That helper runs as a standalone script with no gpumesh package on its
# path, so it cannot import this module. Keep the two in sync.

RESULT_ENVELOPE_KEY = "__gpumesh_result__"


def encode_result(value) -> dict:
    """Wrap a task's return value in a JSON-safe envelope.

    JSON-encodable values are stored as-is; anything else is cloudpickled.
    See :func:`decode_result` for the inverse.
    """
    import json

    try:
        json.dumps(value)
    except (TypeError, ValueError):
        pass
    else:
        return {RESULT_ENVELOPE_KEY: {"encoding": "json", "value": value}}

    import cloudpickle

    return {
        RESULT_ENVELOPE_KEY: {
            "encoding": "cloudpickle",
            "value": base64.b64encode(cloudpickle.dumps(value)).decode("ascii"),
        }
    }


def decode_result(payload):
    """Unwrap a result envelope back into the original return value.

    Anything that is not an envelope is returned unchanged, so script-based
    tasks (which emit plain JSON) and results produced by older workers keep
    working untouched.
    """
    if not isinstance(payload, dict):
        return payload
    envelope = payload.get(RESULT_ENVELOPE_KEY)
    if not isinstance(envelope, dict):
        return payload

    if envelope.get("encoding") == "cloudpickle":
        try:
            import cloudpickle
        except ImportError:
            raise ImportError(
                "This task returned a non-JSON object (e.g. a numpy array or "
                "torch tensor), which requires cloudpickle to read back. "
                "Install it with: pip install cloudpickle"
            )
        return cloudpickle.loads(base64.b64decode(envelope["value"]))

    return envelope.get("value")


def is_result_envelope(payload) -> bool:
    """Return True if ``payload`` is a result envelope produced by a worker."""
    return (
        isinstance(payload, dict)
        and isinstance(payload.get(RESULT_ENVELOPE_KEY), dict)
    )


def _callable_name(func) -> str:
    """Return a useful stable name for functions and callable objects."""
    return getattr(func, "__name__", type(func).__name__)


def _get_module_globals(func) -> dict[str, str]:
    """Map module-valued global names to their importable module names."""
    module_globals = {}
    for name, value in getattr(func, "__globals__", {}).items():
        if isinstance(value, types.ModuleType):
            module_name = getattr(value, "__name__", None)
            if module_name:
                module_globals[name] = module_name
    return module_globals


def _get_required_modules(func) -> list[str]:
    """Extract module names that a function depends on.

    This is a best-effort heuristic based on the function's globals.
    """
    modules = set()

    # Check function's module
    func_module = getattr(func, "__module__", None)
    if func_module and func_module != "__main__":
        # Get top-level module name
        top_module = func_module.split(".")[0]
        if top_module not in ("builtins",):
            modules.add(top_module)

    # Check global variables for imported modules
    globals_dict = getattr(func, "__globals__", {})
    for name, value in globals_dict.items():
        if isinstance(value, types.ModuleType):
            mod_name = getattr(value, "__name__", None)
            if mod_name:
                top = mod_name.split(".")[0]
                if top not in ("builtins",):
                    modules.add(top)

    return sorted(modules)
