import pytest

from gpumesh.sandbox import TaskError, run_task

ECHO = """
import json, sys
payload = json.load(sys.stdin)
print("some log line")
print(json.dumps({"echo": payload["x"] * 2}))
"""


def test_run_task_returns_last_json_line():
    result = run_task(ECHO, {"x": 21})
    assert result == {"echo": 42}


def test_run_task_timeout_kills_process():
    with pytest.raises(TaskError, match="timed out"):
        run_task("import time\ntime.sleep(60)", {}, timeout=1.0)


def test_run_task_nonzero_exit():
    with pytest.raises(TaskError, match="exited with code 1"):
        run_task("raise RuntimeError('kaput')", {})


def test_run_task_bad_output():
    with pytest.raises(TaskError, match="not JSON"):
        run_task("print('hello, not json')", {})


def test_run_task_no_output():
    with pytest.raises(TaskError, match="no output"):
        run_task("pass", {})


def test_device_env_passed_through():
    script = """
import json, os, sys
sys.stdin.read()
print(json.dumps({"device": os.environ["GPUMESH_DEVICE"]}))
"""
    assert run_task(script, {}, device="mps") == {"device": "mps"}
