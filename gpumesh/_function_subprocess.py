"""Subprocess helper for isolated function task execution.

Run by _run_function_task() in worker.py via subprocess.Popen.
Protocol (binary):
  1. Read 4-byte big-endian length N from stdin
  2. Read N bytes of cloudpickle data (func + params dict)
  3. Execute func(**params)
  4. Write JSON result line to stdout (same contract as sandbox tasks)
  5. Exit 0 on success, 1 on error

The result written in step 4 is a *result envelope* (see
``gpumesh/serializer.py``): JSON-encodable return values are embedded
directly, anything else (numpy arrays, torch tensors, DataFrames, ...) is
cloudpickled and base64'd so it survives the JSON-only hop through the
coordinator. This module cannot import gpumesh.serializer — it runs as a
standalone script with no package on its path — so the encoding half is
duplicated here. Keep ``_encode_result`` in sync with
``serializer.encode_result``.
"""

import base64
import json
import os
import sys
import traceback

RESULT_ENVELOPE_KEY = "__gpumesh_result__"


def _encode_result(value):
    """Wrap a return value in a JSON-safe envelope (see module docstring)."""
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


def _main():
    # --- read pickled input from stdin -----------------------------------
    raw_len = sys.stdin.buffer.read(4)
    if len(raw_len) < 4:
        print(json.dumps({"error": "helper: no input received"}))
        sys.exit(1)

    data_len = int.from_bytes(raw_len, byteorder="big")
    pickled_data = sys.stdin.buffer.read(data_len)
    if len(pickled_data) < data_len:
        print(json.dumps({"error": "helper: incomplete input"}))
        sys.exit(1)

    # --- deserialize -----------------------------------------------------
    try:
        import cloudpickle
        func, params = cloudpickle.loads(pickled_data)
    except Exception as exc:
        print(json.dumps({"error": f"helper: deserialization failed: {exc}"}))
        sys.exit(1)

    # --- execute ---------------------------------------------------------
    try:
        result = func(**params)
    except Exception as exc:
        tb = traceback.format_exc()
        print(json.dumps({
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": tb,
            "user_error": True,  # the task's own code raised — do not retry
        }))
        sys.exit(1)

    # --- write result as JSON on stdout ----------------------------------
    try:
        print(json.dumps(_encode_result(result)))
    except Exception as exc:
        # Nothing could encode the value — not even cloudpickle. Report the
        # type rather than the value: repr() of a huge array or a live handle
        # is useless in a log and can itself raise.
        print(json.dumps({
            "error": (
                f"cannot send result of type {type(result).__name__!r} back to "
                f"the coordinator: {type(exc).__name__}: {exc}. Return a "
                f"picklable value (open files, sockets, locks and live GPU "
                f"handles cannot cross process boundaries)."
            ),
            "user_error": True,  # a different return value is needed, not a retry
        }))
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    _main()
