"""Shared test configuration.

The most important fixture here isolates ``~/.gpumesh/config.json`` into a
temporary directory for EVERY test. Without this, tests that call
``run_worker``, ``get_connection`` or ``mesh.connect`` would read and
overwrite the real user config on disk (a real bug that clobbered the
developer's saved coordinator connection during development).
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_gpumesh_config(tmp_path, monkeypatch):
    """Point the connection manager at a throwaway config dir for every test."""
    from gpumesh import connection_manager

    config_dir = tmp_path / ".gpumesh"
    monkeypatch.setattr(connection_manager, "_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(connection_manager, "_CONFIG_PATH",
                        str(config_dir / "config.json"))
    yield


@pytest.fixture(autouse=True)
def _isolate_gpumesh_env(monkeypatch):
    """Clear GPUMESH_* variables so the developer's shell cannot change results.

    Several of these are read as argparse defaults or consulted directly by
    the routing code, so a contributor who exports GPUMESH_TOKEN for their own
    mesh would otherwise see unrelated tests fail — the kind of failure that
    looks like a real bug and wastes an afternoon. Tests that need a value set
    it explicitly with monkeypatch.setenv.
    """
    for name in ("GPUMESH_TOKEN", "GPUMESH_URL", "GPUMESH_HOST_IP",
                 "GPUMESH_LOCAL", "GPUMESH_VERBOSE", "GPUMESH_COLOR",
                 "GPUMESH_PORT"):
        monkeypatch.delenv(name, raising=False)
    yield
