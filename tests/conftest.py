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
