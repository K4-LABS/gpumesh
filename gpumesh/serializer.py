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
import types


def serialize_function(func) -> str:
    """Serialize a Python function to a base64-encoded string.

    Primary: cloudpickle (handles closures, lambdas, nested functions)
    Fallback: inspect.getsource() (only works for simple top-level functions)

    The serialized form includes:
    1. The pickled function bytes (cloudpickle) or source code text
    2. A list of module names that need to be importable on the worker

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
        "func_name": getattr(func, "__name__", "unknown"),
    }

    metadata_bytes = json.dumps(metadata).encode("utf-8")
    length_prefix = len(metadata_bytes).to_bytes(4, byteorder="big")

    combined = length_prefix + metadata_bytes + func_bytes
    return base64.b64encode(combined).decode("ascii")


def _serialize_with_source(func) -> str:
    """Serialize using inspect.getsource (fallback)."""
    import json

    try:
        source = inspect.getsource(func)
    except (OSError, TypeError) as e:
        raise ValueError(
            f"Cannot serialize function {getattr(func, '__name__', '?')}: "
            f"{e}. Install cloudpickle for full serialization support: "
            f"pip install gpumesh[notebook]"
        )

    metadata = {
        "method": "source",
        "modules": _get_required_modules(func),
        "func_name": getattr(func, "__name__", "unknown"),
        "source": source,
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

    # Ensure required modules are importable
    for mod_name in metadata.get("modules", []):
        if mod_name not in sys.modules:
            try:
                importlib.import_module(mod_name)
            except ImportError:
                pass  # Module might not be critical; cloudpickle will handle it

    if method == "cloudpickle":
        try:
            import cloudpickle
        except ImportError:
            raise ImportError(
                "cloudpickle is required for deserialization. "
                "Install it with: pip install gpumesh[notebook]"
            )
        return cloudpickle.loads(rest)

    elif method == "source":
        # Reconstruct function from source code
        source = metadata["source"]
        func_name = metadata["func_name"]

        # Create a namespace and exec the source
        namespace = {}
        exec(compile(source, f"<{func_name}>", "exec"), namespace)

        if func_name not in namespace:
            raise ValueError(
                f"Function '{func_name}' not found in source code"
            )
        return namespace[func_name]

    else:
        raise ValueError(f"Unknown serialization method: {method}")


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
