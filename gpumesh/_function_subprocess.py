"""Subprocess helper for isolated function task execution.

Run by _run_function_task() in worker.py via subprocess.Popen.
Protocol (binary):
  1. Read 4-byte big-endian length N from stdin
  2. Read N bytes of cloudpickle data (func + params dict)
  3. Execute func(**params)
  4. Write JSON result line to stdout (same contract as sandbox tasks)
  5. Exit 0 on success, 1 on error
"""

import json
import os
import sys
import traceback


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
        if not isinstance(result, dict):
            result = {"result": result}
    except Exception as exc:
        tb = traceback.format_exc()
        print(json.dumps({
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": tb,
        }))
        sys.exit(1)

    # --- write result as JSON on stdout ----------------------------------
    try:
        print(json.dumps(result))
    except (TypeError, ValueError):
        print(json.dumps({
            "error": f"helper: result not JSON-serializable: {type(result).__name__}: {result}",
        }))
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    _main()
