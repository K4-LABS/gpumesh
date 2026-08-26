"""Shared test configuration.

The most important fixture here isolates ``~/.gpumesh/config.json`` into a
temporary directory for EVERY test. Without this, tests that call
``run_worker``, ``get_connection`` or ``mesh.connect`` would read and
overwrite the real user config on disk (a real bug that clobbered the
developer's saved coordinator connection during development).
"""

import functools
import sys
import threading
import types

import pytest


@pytest.fixture(autouse=True)
def _stop_workers_started_by_tests(monkeypatch):
    """Shut down every worker a test starts, once that test finishes.

    ``run_worker`` is designed never to exit on its own, so a test that starts
    one in a daemon thread and walks away leaks a worker that polls for the
    rest of the session. That looked free — daemon threads do not block
    interpreter exit — but it is not: after ten minutes each leaked worker
    hits its periodic re-benchmark, so dozens of them wake up at once and
    print together. Daemon threads are killed wherever they happen to be
    during finalization, and one killed mid-``print`` leaves stdout's lock
    held, so the main thread hangs forever on its final flush. That is exactly
    how CI died: pytest reported its results at 10 minutes and the process
    then sat there until the 25-minute job timeout killed it. Windows only
    escaped because the suite finished before the ten-minute mark.

    Injecting a stop event per call and setting it at teardown keeps the leak
    from existing at all.
    """
    from gpumesh import worker as worker_module

    real_run_worker = worker_module.run_worker
    events = []

    @functools.wraps(real_run_worker)
    def tracked_run_worker(*args, **kwargs):
        if len(args) < 6 and kwargs.get("stop_event") is None:
            kwargs["stop_event"] = threading.Event()
            events.append(kwargs["stop_event"])
        return real_run_worker(*args, **kwargs)

    real_spawn = worker_module.spawn_local_worker

    @functools.wraps(real_spawn)
    def tracked_spawn(*args, **kwargs):
        thread = real_spawn(*args, **kwargs)
        events.append(thread.gpumesh_stop)
        return thread

    monkeypatch.setattr(worker_module, "spawn_local_worker", tracked_spawn)
    monkeypatch.setattr(worker_module, "run_worker", tracked_run_worker)
    # Test modules do ``from gpumesh.worker import run_worker``, which binds
    # the original function into their own namespace; patching the source
    # module alone would not reach those callers.
    for module in list(sys.modules.values()):
        # Some tests install mocks into sys.modules to stand in for optional
        # dependencies. getattr() on a mock invents the attribute rather than
        # raising, which would record a call the test never made, so only look
        # at real modules.
        if not isinstance(module, types.ModuleType):
            continue
        if getattr(module, "run_worker", None) is real_run_worker:
            monkeypatch.setattr(module, "run_worker", tracked_run_worker,
                                raising=False)
        if getattr(module, "spawn_local_worker", None) is real_spawn:
            monkeypatch.setattr(module, "spawn_local_worker", tracked_spawn,
                                raising=False)
    yield
    for event in events:
        event.set()


@pytest.fixture(autouse=True)
def _isolate_gpumesh_config(tmp_path, monkeypatch):
    """Point the connection manager at a throwaway config dir for every test."""
    from gpumesh import connection_manager

    config_dir = tmp_path / ".gpumesh"
    monkeypatch.setattr(connection_manager, "_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(connection_manager, "_CONFIG_PATH",
                        str(config_dir / "config.json"))
    yield


# Every GPUMESH_* variable cleared before each test. Named and module-level so
# a test can assert against it: the list has to keep up with the package, and
# the cost of it silently falling behind is measured in dozens of confusing
# failures, not one. See tests/test_claimer.py::TestGpumeshEnvIsolation, which
# scans the package for environment reads and checks every one appears here.
ISOLATED_GPUMESH_ENV_VARS = (
    "GPUMESH_TOKEN", "GPUMESH_URL", "GPUMESH_HOST", "GPUMESH_HOST_IP",
    "GPUMESH_CLAIM_HOST", "GPUMESH_LOCAL", "GPUMESH_VERBOSE", "GPUMESH_COLOR",
    "GPUMESH_DEVICE", "GPUMESH_PORT",
    # Security switches. Each of these changes behaviour the suite asserts on:
    # GPUMESH_STRICT_RESULTS makes decode_result refuse pickled results,
    # GPUMESH_AUTH_KDF / GPUMESH_AUTH_KDF_ITERATIONS change the hash format
    # and cost token hashing produces, and the TLS pair redirects or disarms
    # certificate verification.
    "GPUMESH_STRICT_RESULTS",
    "GPUMESH_AUTH_KDF", "GPUMESH_AUTH_KDF_ITERATIONS",
    "GPUMESH_TLS_CA", "GPUMESH_TLS_INSECURE",
)


@pytest.fixture(autouse=True)
def _isolate_gpumesh_env(monkeypatch):
    """Clear GPUMESH_* variables so the developer's shell cannot change results.

    Several of these are read as argparse defaults or consulted directly by
    the routing code, so a contributor who exports GPUMESH_TOKEN for their own
    mesh would otherwise see unrelated tests fail — the kind of failure that
    looks like a real bug and wastes an afternoon. Tests that need a value set
    it explicitly with monkeypatch.setenv.

    The two bind variables were the expensive omission. ``GPUMESH_HOST`` is
    read by ``_resolve_bind_host`` AND used as the ``--host`` argparse
    default, so exporting it flips the coordinator's bind out from under every
    test that asserts on the loopback default — 5 failures. ``GPUMESH_CLAIM_HOST``
    is worse: with it set, the claim server binds somewhere the tests cannot
    dial and 46 tests error out. Both are variables the README and
    CONTRIBUTING now actively suggest exporting, so this is not a hypothetical
    developer — it is the developer who followed the docs.

    Clearing here rather than in each test is what makes the opt-in shape
    work: this runs at setup, before the test body, so a test that genuinely
    wants a value just calls monkeypatch.setenv and wins.
    """
    for name in ISOLATED_GPUMESH_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    yield
