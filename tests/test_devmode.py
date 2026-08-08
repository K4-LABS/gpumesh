"""Tests for the ``gpumesh.mesh`` dev-mode module: @mesh decorator, auto-connect, helpers.

These tests verify Phase 3 of the gpumesh reliability rework:
  1. ``from gpumesh import mesh`` works without crashing
  2. ``@mesh`` decorator produces a callable function
  3. ``mesh.map()`` falls back to local execution when no coordinator
  4. ``mesh.devices()``, ``mesh.device_count()``, ``mesh.total_score()`` work
"""

import os
import sys
import threading
import time

import pytest

from gpumesh.server import serve
from gpumesh.worker import MeshClient, run_worker

TOKEN = "devmode-test-token"


class TestMeshImport:
    """``from gpumesh import mesh`` works."""

    def test_import_mesh_from_gpumesh(self):
        """``from gpumesh import mesh`` works."""
        from gpumesh import mesh

        assert callable(mesh), "mesh should be callable as a decorator"

    def test_mesh_export_has_helpers(self):
        """The package-level ``mesh`` is a complete API (decorator + helpers)."""
        from gpumesh import mesh

        # Regression: `from gpumesh import mesh` must expose the helper
        # functions too, so `mesh.connect(...)` / `mesh.devices()` work
        # regardless of whether ``gpumesh.mesh`` resolves to the module or
        # the decorator function.
        assert callable(mesh.connect)
        assert callable(mesh.devices)
        assert callable(mesh.device_count)
        assert callable(mesh.total_score)

    def test_import_mesh_module_after_package_attr(self):
        """``import gpumesh.mesh as mesh_mod`` still works after the package
        attribute has been bound to the decorator."""
        from gpumesh import mesh  # noqa: F401  (binds gpumesh.mesh attr)

        import gpumesh.mesh as mesh_mod

        # Both the module and the decorator expose the helpers.
        assert callable(mesh_mod.connect)
        assert callable(mesh_mod.mesh_fn)

    def test_import_mesh_directly(self):
        """``from gpumesh.mesh import mesh_fn`` works."""
        from gpumesh.mesh import mesh_fn

        assert callable(mesh_fn)

    def test_import_mesh_connect(self):
        """``from gpumesh.mesh import connect`` works."""
        from gpumesh.mesh import connect

        assert callable(connect)


class TestMeshDecorator:
    """@mesh decorator works correctly."""

    def test_mesh_decorator_callable(self):
        """@mesh produces a callable function."""
        from gpumesh.mesh import mesh_fn

        @mesh_fn
        def add(a, b):
            return a + b

        assert callable(add)
        result = add(2, 3)
        assert result == 5

    def test_mesh_decorator_preserves_name(self):
        """@mesh preserves the original function name."""
        from gpumesh.mesh import mesh_fn

        @mesh_fn
        def my_special_function():
            pass

        assert my_special_function.__name__ == "my_special_function"

    def test_mesh_decorator_with_args(self):
        """@mesh(gpu=\"A100\") syntax works."""
        from gpumesh.mesh import mesh_fn

        @mesh_fn(gpu="A100")
        def train(lr, epochs):
            return {"lr": lr, "epochs": epochs, "accuracy": 0.95}

        assert callable(train)
        result = train(lr=0.01, epochs=100)
        assert result["accuracy"] == 0.95

    def test_mesh_map_fallback_local(self):
        """.map() falls back to local execution when no coordinator."""
        from gpumesh.mesh import mesh_fn

        @mesh_fn
        def square(x):
            return {"x": x, "square": x ** 2}

        results = square.map([{"x": 2}, {"x": 3}])
        assert len(results) == 2
        assert results[0]["square"] == 4
        assert results[1]["square"] == 9

    def test_mesh_function_resolves_mesh_after_connect(self, monkeypatch):
        """@mesh decorated before any connection picks up the mesh once
        connected (the "decorate first, connect later" flow).

        Regression: MeshFunction captured the mesh at decoration time, so a
        function decorated before ``connect()`` stayed local-only forever
        even after the user connected (see examples/dev_mode.py).
        """
        # Use importlib, NOT ``import gpumesh.mesh as mesh_mod`` — the package
        # binds the ``mesh`` attribute to the decorator function, so ``import
        # ... as`` hands us the function instead of the module (same footgun
        # the existing coordinator test below documents).
        import importlib

        mesh_module = importlib.import_module("gpumesh.mesh")
        from gpumesh.mesh import MeshFunction

        class FakeMesh:
            def workers(self):
                return []

            def distribute(self, function, params, timeout=300.0):
                return [function(**p) for p in params]

        def add(a, b):
            return {"sum": a + b}

        # Decorate while no coordinator is configured:
        fn = MeshFunction(add, None)
        assert fn._afn is None

        # The user connects later:
        monkeypatch.setattr(mesh_module, "_connected", True)
        monkeypatch.setattr(mesh_module, "_mesh", FakeMesh())

        result = fn(2, 3)
        # The mesh was resolved lazily (no alive workers -> local fallback,
        # but the binding now happens instead of staying None forever).
        assert fn._afn is not None
        assert result == {"sum": 5}


class TestMeshHelpers:
    """mesh.devices() / device_count() / total_score() work."""

    def test_devices_returns_list(self):
        """devices() returns a list (empty when not connected)."""
        from gpumesh.mesh import devices

        devs = devices()
        assert isinstance(devs, list)

    def test_device_count_returns_int(self):
        """device_count() returns an int."""
        from gpumesh.mesh import device_count

        count = device_count()
        assert isinstance(count, int)

    def test_total_score_returns_float(self):
        """total_score() returns a float."""
        from gpumesh.mesh import total_score

        score = total_score()
        assert isinstance(score, (int, float))


class TestMeshWithCoordinator:
    """@mesh works with a real coordinator."""

    def test_mesh_functions_work_with_coordinator(self, tmp_path):
        """@mesh function works when a coordinator is available."""
        from gpumesh.mesh import mesh_fn, connect

        # Start a coordinator
        httpd = serve("127.0.0.1", 0, str(tmp_path / "devmode.db"), TOKEN)
        port = httpd.server_address[1]
        url = f"http://127.0.0.1:{port}"
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        time.sleep(0.2)

        # Start a worker
        worker_thread = threading.Thread(
            target=run_worker, args=(url, TOKEN), daemon=True
        )
        worker_thread.start()
        time.sleep(0.5)

        # Connect to the coordinator
        connect(url, TOKEN)

        @mesh_fn
        def double(x):
            return {"x": x, "doubled": x * 2}

        # Single call should work (runs on the pool)
        result = double(x=5)
        assert result["doubled"] == 10

        # Clean up saved connection (set by connect())
        from gpumesh import connection_manager

        connection_manager.clear_connection()
        httpd.gpumesh_stop.set()
        httpd.shutdown()

        # Reset the mesh module's process-global connection state so other
        # tests in this process are not affected by this one.
        # NOTE: use importlib, NOT ``import gpumesh.mesh as mesh_mod`` — the
        # package binds the ``mesh`` attribute to the decorator function, so
        # ``import ... as`` would hand us the function instead of the module
        # and the state reset below would silently no-op.
        import importlib

        mesh_mod = importlib.import_module("gpumesh.mesh")
        mesh_mod._connected = False
        mesh_mod._mesh = None
        mesh_mod._attempted = False