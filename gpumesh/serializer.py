"""Function serialization for the Python API.

Uses cloudpickle to serialize Python functions so they can be sent
over the network to worker machines for execution.

Requires: pip install gpumesh[notebook]  (installs cloudpickle + pandas)
"""

from __future__ import annotations

import base64
import importlib
import inspect
import re
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

    # ``python_version`` was only added in v0.8.1 — v0.5.0 shipped a serializer
    # that omitted it, so a missing or null value means "unknown", not "same".
    # An unknown version is *not* evidence of a mismatch, though, and it must
    # not be treated as one: the overwhelmingly common case is an old client
    # talking to a worker on the very same Python, where cloudpickle is both
    # safe and strictly better than the source path.
    #
    # It is strictly better because the two paths do not restore the same
    # amount of context. cloudpickle carries the function's globals and closure
    # along with it, while the source path re-execs the text into a namespace
    # holding only the *module-valued* globals from ``module_globals``. A plain
    # module-level constant such as ``SCALE = 3``, or a closure variable, is
    # simply absent there, so the rebuilt function raises ``NameError: name
    # 'SCALE' is not defined`` when it is finally *called* inside the task
    # subprocess — past the exec-time NameError handler below, so the blame
    # lands on the user's own function body. Bound methods fare no better: the
    # source path refuses them outright, while cloudpickle ships them fine.
    #
    # So only a version we were actually told about, and that differs, may
    # divert to source or trigger the refusal. Anything the sender did not tell
    # us about keeps the pre-0.8.1 behaviour of trying cloudpickle — and if
    # those bytes really did come from a foreign interpreter, cloudpickle.loads
    # usually raises, and the handler further down still falls back to source.
    version_known = bool(source_version)
    version_mismatch = source_version != current_version

    if version_known and version_mismatch:
        if "source" in metadata:
            method = "source"
        else:
            # Cross-version cloudpickle is not safe here: loads() can
            # "succeed" and return a function whose bytecode (pickled under a
            # different Python version) crashes the interpreter when called —
            # a native segfault (0xC0000005 on Windows) inside the task
            # subprocess, with no traceback to diagnose. Without source to
            # rebuild from, refuse up front so the failure is a clean error
            # the user can act on instead of a worker crash.
            raise ValueError(
                f"Cannot run this task on a Python {current_version} worker: the "
                f"function was serialized on Python {source_version} with no source "
                f"fallback (it was likely defined in an interactive session or "
                f"heredoc, where inspect.getsource() cannot capture it). Define the "
                f"function in a .py file so its source can be shipped, or use the "
                f"same Python version on all machines."
            )

    # Ensure required modules are importable
    for mod_name in metadata.get("modules", []):
        if mod_name not in sys.modules:
            try:
                importlib.import_module(mod_name)
            except ImportError:
                pass  # Module might not be critical; cloudpickle will handle it

    if method == "cloudpickle":
        # Any version mismatch has already been resolved above: either it
        # switched us to the source path, or it raised. Reaching here means the
        # versions agree, or we were never told the sender's version.
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
            # Every path that selects the source method first checks that the
            # key is present, so this only fires on a hand-built or truncated
            # payload — nothing to do with Python versions.
            raise ValueError(
                "Serialized data selects the source method but carries no "
                "'source' key. The payload is malformed or truncated."
            )
        # Reconstruct function from source code
        source = textwrap.dedent(metadata["source"])
        func_name = metadata["func_name"]

        source = _strip_gpumesh_decorators(source, func_name)

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

        try:
            exec(compile(source, f"<{func_name}>", "exec"), namespace)
        except NameError as e:
            # Decorators other than gpumesh's own are kept (see
            # _strip_gpumesh_decorators), so one of them may not resolve here.
            # The bare NameError names the missing symbol but gives no hint
            # that a decorator is the reason, which sends users hunting inside
            # their function body.
            raise ValueError(
                f"Cannot rebuild '{func_name}' from source on this worker: {e}. "
                f"The function is wrapped in a decorator whose name is not "
                f"available here — install the package that provides it on the "
                f"worker, import it at module level in the same file as "
                f"'{func_name}', or run the same Python version on all machines "
                f"so the function ships as bytecode instead of source."
            )

        if func_name not in namespace:
            raise ValueError(
                f"Function '{func_name}' not found in source code"
            )
        rebuilt = namespace[func_name]

        # A callable object is serialized under its *class* name, so what the
        # source rebuilds is the class, not the instance. The worker calls
        # ``func(**params)``, which would construct an instance instead of
        # invoking __call__ — and return that instance as the task's result if
        # __init__ happens to accept the kwargs.
        #
        # Only a class is rejected here, not every non-FunctionType: a
        # preserved decorator such as @functools.lru_cache legitimately rebuilds
        # the function as a callable wrapper object, and that still invokes
        # correctly.
        if isinstance(rebuilt, type) or not callable(rebuilt):
            raise ValueError(
                f"'{func_name}' rebuilt from source as a "
                f"{type(rebuilt).__name__}, not a function. Callable objects "
                f"cannot be shipped via the source fallback (the worker would "
                f"construct one rather than call it). Use a module-level "
                f"function, or run the same Python version on all machines so "
                f"the object ships as bytecode instead of source."
            )

        # The worker's subprocess calls func(**params) and hands the return
        # value straight to the result encoder. A coroutine object is not
        # JSON-encodable and is never awaited, so the real failure surfaces
        # much later as an encoding error plus a "never awaited" warning.
        if inspect.iscoroutinefunction(rebuilt):
            raise ValueError(
                f"'{func_name}' is an async function. gpumesh runs tasks "
                f"synchronously and never awaits the result, so async "
                f"functions are not supported. Wrap the coroutine in a "
                f"synchronous function that calls asyncio.run() itself."
            )

        # ``inspect.getsource`` on a method returns the method body alone, and
        # dedent + exec at module level turns it into a plain function that
        # still declares ``self``. The worker calls func(**params) with no
        # instance, so this would fail inside the subprocess as a confusing
        # "missing 1 required positional argument: 'self'".
        try:
            first_param = next(iter(inspect.signature(rebuilt).parameters))
        except (StopIteration, TypeError, ValueError):
            first_param = None
        if first_param in ("self", "cls"):
            raise ValueError(
                f"'{func_name}' is a method (its first parameter is "
                f"'{first_param}'). Bound methods cannot be shipped via the "
                f"source fallback: the worker rebuilds the method body as a "
                f"bare function with no instance to bind to. Move the work into "
                f"a module-level function, or run the same Python version on "
                f"all machines so the method ships as bytecode instead."
            )

        return rebuilt

    else:
        raise ValueError(f"Unknown serialization method: {method}")


_DECORATOR_ROOT_RE = re.compile(r"^@\s*([A-Za-z_]\w*)")

# Decorators gpumesh itself defines. ``@gpumesh.mesh`` and friends arrive as a
# dotted form, so the root name is what we compare.
_GPUMESH_DECORATOR_ROOTS = frozenset({"mesh", "accelerate", "gpumesh"})


def _bracket_delta(line: str, quote: str | None = None) -> tuple[int, str | None]:
    """Net bracket nesting change contributed by ``line``, and its quote state.

    Bracket characters inside string literals and comments do not count, so a
    decorator argument like ``note="("`` does not throw off the span search.

    ``quote`` is the string delimiter that was still open when the previous
    line ended, and the delimiter still open when this one ends is handed back
    alongside the delta. A caller walking a multi-line construct has to thread
    that state from one line to the next: a triple-quoted decorator argument
    spans lines, and restarting each line as "no string is open" would count
    the brackets sitting in the string's body as real ones. A decorator holding
    a triple-quoted note whose text happens to contain ``) ) )`` used to end
    its span on that very line, leaving the closing quotes and paren of the
    decorator glued to the front of the def as a SyntaxError.
    """
    depth = 0
    i = 0
    while i < len(line):
        ch = line[i]
        if quote is not None:
            if ch == "\\":
                i += 2
                continue
            if line.startswith(quote, i):
                i += len(quote)
                quote = None
                continue
            i += 1
            continue
        if ch in "\"'":
            quote = ch * 3 if line.startswith(ch * 3, i) else ch
            i += len(quote)
            continue
        if ch == "#":
            break
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        i += 1
    return depth, quote


def _strip_gpumesh_decorators(source: str, func_name: str) -> str:
    """Remove gpumesh's own decorators from captured source, keeping the rest.

    ``@mesh`` and ``@accelerate`` cannot be reconstructed here — their names
    are not in the JSON metadata, so exec() would raise ``NameError: name
    'mesh' is not defined`` — and they are meaningless inside the worker's
    isolated subprocess anyway, where the function already runs bare.

    Every other decorator has to survive. Dropping ``@torch.no_grad()`` or a
    user's retry wrapper silently changes what the function does: the same task
    would run under no_grad on a same-version worker and with autograd enabled
    on a cross-version one, with no warning and a different memory profile.
    """
    lines = source.splitlines(keepends=True)
    def_re = re.compile(
        rf"^(?:async\s+)?def\s+{re.escape(func_name)}\s*[\(\[]"
    )
    def_idx = next(
        (i for i, line in enumerate(lines) if def_re.match(line.lstrip())),
        None,
    )
    if def_idx is None:
        return source

    kept = []
    i = 0
    while i < def_idx:
        line = lines[i]
        if not line.lstrip().startswith("@"):
            kept.append(line)
            i += 1
            continue

        # A decorator call may spread its arguments over several lines. Follow
        # the bracket nesting to the end of the call so that dropping the
        # decorator drops all of it, rather than leaving a dangling fragment
        # that turns into a SyntaxError. The open-quote state has to be carried
        # from each line into the next, because an argument may itself be a
        # triple-quoted string whose body holds brackets that are only text.
        end = i
        depth, quote = _bracket_delta(line)
        while depth > 0 and end + 1 < def_idx:
            end += 1
            delta, quote = _bracket_delta(lines[end], quote)
            depth += delta

        root = _DECORATOR_ROOT_RE.match(line.strip())
        if not (root and root.group(1) in _GPUMESH_DECORATOR_ROOTS):
            kept.extend(lines[i:end + 1])
        i = end + 1

    return "".join(kept + lines[def_idx:])


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
