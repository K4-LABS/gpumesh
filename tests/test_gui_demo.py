"""Tests for the GUI payoff demo (examples/gui_demo_sweep.py) and its packaging.

The demo is the proof that the mesh path and the local path agree: the same
six tasks, the same answers, with each line naming the machine that ran it.
These tests pin the local half of that promise — the half that must hold on a
laptop with no coordinator at all.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "examples" / "gui_demo_sweep.py"
PYPROJECT = ROOT / "pyproject.toml"

_TASK_LINE = re.compile(r"^\[gui-demo\] task (\d+): .*worker=(\S+)", re.MULTILINE)


def _local_env(home: Path) -> dict:
    """An environment in which the demo cannot see any mesh.

    The demo subprocess does NOT inherit conftest's config isolation (that
    only patches the pytest process), so without this it would read the
    developer's real ~/.gpumesh/config.json and run against their live mesh —
    turning a fallback test into a test of whatever they happen to run. A
    throwaway HOME (USERPROFILE on Windows, where expanduser looks first)
    means "no saved connection", and clearing GPUMESH_* means no URL/token
    from the shell either. What remains is the genuine no-coordinator path.
    """
    env = dict(os.environ)
    for name in [n for n in env if n.startswith("GPUMESH_")]:
        del env[name]
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    return env


@pytest.fixture(scope="module")
def demo_run(tmp_path_factory):
    """Run the demo once, locally, and share the completed process."""
    home = tmp_path_factory.mktemp("gui-demo-home")
    return subprocess.run(
        [sys.executable, str(DEMO)],
        capture_output=True, text=True, timeout=120,
        env=_local_env(home), cwd=str(ROOT),
    )


class TestDemoRunsLocally:
    def test_exit_code_is_zero_with_no_coordinator(self, demo_run):
        assert demo_run.returncode == 0, demo_run.stderr[-2000:]

    def test_final_line_counts_the_machines(self, demo_run):
        assert "worked as one" in demo_run.stdout


class TestPerTaskAttribution:
    def test_all_six_tasks_name_their_worker(self, demo_run):
        found = dict(_TASK_LINE.findall(demo_run.stdout))
        assert sorted(found) == ["0", "1", "2", "3", "4", "5"]
        assert all(worker.strip() for worker in found.values())

    def test_local_run_attributes_everything_to_one_machine(self, demo_run):
        workers = {w for _, w in _TASK_LINE.findall(demo_run.stdout)}
        assert len(workers) == 1


class TestSubmitScriptPath:
    def test_piped_payload_runs_a_single_task(self, tmp_path):
        """The `gpumesh submit` contract: payload JSON on stdin, result JSON out.

        No coordinator needed — this is the shape the worker-side execution
        takes, so it is pinned here rather than against a live mesh.
        """
        payload = {"task_id": 2, "size": 5000}
        proc = subprocess.run(
            [sys.executable, str(DEMO)],
            input=json.dumps(payload),
            capture_output=True, text=True, timeout=120,
            env=_local_env(tmp_path), cwd=str(ROOT),
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        assert result["task_id"] == 2
        assert result["worker"]
        assert result["checksum"] == sum((i * i) % 10007 for i in range(5000))


class TestGuiExtraPackaging:
    def test_gui_extra_names_tray_deps(self):
        """`pip install gpumesh[gui]` must resolve to pystray + Pillow.

        Parsed as text, not built: building a wheel to assert on metadata
        would make this test about the build backend rather than the extra.
        """
        text = PYPROJECT.read_text(encoding="utf-8")
        section = text.split("[project.optional-dependencies]", 1)[1]
        section = section.split("\n[", 1)[0]
        match = re.search(r"^gui\s*=\s*\[(.*?)\]", section,
                          re.MULTILINE | re.DOTALL)
        assert match, "no `gui = [...]` extra under [project.optional-dependencies]"
        extra = match.group(1).lower()
        assert "pystray" in extra
        assert "pillow" in extra

    def test_gui_extra_has_floors_but_no_ceilings(self):
        """Floors, not caps: a library constrains, an application pins.

        This is the packaging rule stated at the top of
        [project.optional-dependencies] — `<` or `~=` in a new extra would
        break resolver freedom in someone else's environment.
        """
        text = PYPROJECT.read_text(encoding="utf-8")
        section = text.split("[project.optional-dependencies]", 1)[1]
        section = section.split("\n[", 1)[0]
        match = re.search(r"^gui\s*=\s*\[(.*?)\]", section,
                          re.MULTILINE | re.DOTALL)
        assert match
        value = match.group(1)
        assert ">=" in value
        assert "<" not in value.replace(">=", "")
        assert "~=" not in value
