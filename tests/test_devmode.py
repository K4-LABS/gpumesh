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


def _distribute_like_the_coordinator(function, params, timeout=300.0, **hints):
    """Stand-in for ``GPUMesh.distribute`` that models its argument contract.

    Not ``[function(**p) for p in params]``: the real distribute builds each
    payload as ``{"_params": {k: v for k, v in p.items() if k != "cost"}, ...}``
    (api.py) and the worker executes ``payload["_params"]`` and nothing else,
    so ``cost`` provably never reaches the function on the mesh path. A fake
    that forwarded the whole payload would model a mesh that does not exist,
    and would agree with the very bug these tests exist to catch.
    """
    return [function(**{k: v for k, v in p.items() if k != "cost"})
            for p in params]


class _FakeMesh:
    """A mesh with one live worker whose distribute() matches api.py."""

    def __init__(self):
        self.distributed = []

    def workers(self):
        return [{"alive": True}]

    def devices(self):
        return [{"device": "cpu"}]

    def distribute(self, function, params, timeout=300.0, **hints):
        self.distributed.append(params)
        return _distribute_like_the_coordinator(function, params,
                                                timeout=timeout, **hints)


@pytest.fixture
def no_coordinator(monkeypatch):
    """Force the ``_afn is None`` branch — the genuine no-coordinator path.

    ``_ensure_afn`` re-runs ``connect()`` on first use, and this machine may
    well have a real connection saved by ``gpumesh join``, which would quietly
    route these tests through AcceleratedFunction and test nothing. Marking the
    module already-connected with no mesh makes ``connect()`` a no-op that
    returns None, so ``_afn`` stays None the way it does on a laptop that never
    joined a mesh.
    """
    import importlib

    mesh_module = importlib.import_module("gpumesh.mesh")
    monkeypatch.setattr(mesh_module, "_connected", True)
    monkeypatch.setattr(mesh_module, "_mesh", None)
    return mesh_module


class TestCostAnnotatedPayloads:
    """A ``cost``-annotated payload must behave identically on both paths.

    Regression: ``MeshFunction.map``'s local branch was
    ``[self._bound_fn(**p) for p in params_list]``, so a payload annotated the
    way examples/payloads.json and the README teach raised ``TypeError:
    evaluate() got an unexpected keyword argument 'cost'`` the instant no
    coordinator was configured — i.e. exactly when the local fallback was
    supposed to be saving the run. ``GPUMesh.distribute`` has stripped it since
    1.1.0; this is the same defect in the ``@mesh`` front door.
    """

    @staticmethod
    def _tracked():
        """A user function that would raise if it were ever handed ``cost``."""
        seen = []

        def evaluate(lr):
            seen.append(lr)
            return {"lr": lr, "score": lr * 10}

        return evaluate, seen

    def test_map_local_fallback_accepts_cost(self, no_coordinator):
        """The bug, directly: no coordinator, cost-annotated payload."""
        from gpumesh.mesh import MeshFunction

        evaluate, seen = self._tracked()
        fn = MeshFunction(evaluate, None)
        assert fn._afn is None, "fixture must force the local branch"

        results = fn.map([{"lr": 0.1, "cost": 2.0}, {"lr": 0.2, "cost": 5.0}])

        assert results == [{"lr": 0.1, "score": 1.0}, {"lr": 0.2, "score": 2.0}]
        assert seen == [0.1, 0.2]

    def test_map_local_and_mesh_return_the_same_thing(self, no_coordinator):
        """Same payload, both paths, byte-identical results.

        This is the property the fallback is supposed to have and the one the
        bug broke: code that works with the mesh up keeps working with it down.
        """
        from gpumesh.mesh import MeshFunction

        payloads = [{"lr": 0.1, "cost": 2.0}, {"lr": 0.25, "cost": 1.0}]

        local_fn, local_seen = self._tracked()
        local = MeshFunction(local_fn, None)
        assert local._afn is None
        local_results = local.map(payloads)

        mesh_fn_, mesh_seen = self._tracked()
        fake = _FakeMesh()
        remote = MeshFunction(mesh_fn_, fake)
        assert remote._afn is not None, "mesh-configured branch expected"
        mesh_results = remote.map(payloads)

        assert local_results == mesh_results
        assert local_seen == mesh_seen == [0.1, 0.25]
        # And the hint really did travel to the scheduler rather than being
        # dropped on the floor — stripping it must not mean losing it.
        assert fake.distributed == [payloads]

    def test_user_function_never_receives_cost(self, no_coordinator):
        """Assert on the kwargs themselves, not just on not crashing.

        A function with ``**kwargs`` would swallow ``cost`` silently and pass
        the test above while still being handed a scheduler hint it has no
        business seeing.
        """
        from gpumesh.mesh import MeshFunction

        captured = []

        def greedy(**kwargs):
            captured.append(kwargs)
            return kwargs

        local = MeshFunction(greedy, None)
        assert local._afn is None
        local.map([{"lr": 0.1, "cost": 2.0}])

        remote = MeshFunction(greedy, _FakeMesh())
        remote.map([{"lr": 0.1, "cost": 2.0}])

        assert captured == [{"lr": 0.1}, {"lr": 0.1}]
        assert not any("cost" in kw for kw in captured)

    def test_map_falls_back_locally_when_distribute_fails(self, no_coordinator):
        """The other local branch: a mesh is configured but unreachable.

        ``AcceleratedFunction.map`` catches the failure and re-runs the batch
        here, which is the path a user actually hits when the coordinator dies
        mid-session — and it has to strip ``cost`` too.
        """
        import warnings

        from gpumesh.mesh import MeshFunction

        class DeadMesh(_FakeMesh):
            def distribute(self, function, params, timeout=300.0, **hints):
                raise ConnectionRefusedError("coordinator is gone")

        evaluate, seen = self._tracked()
        fn = MeshFunction(evaluate, DeadMesh())
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            results = fn.map([{"lr": 0.1, "cost": 2.0}])

        assert results == [{"lr": 0.1, "score": 1.0}]
        assert seen == [0.1]

    def test_map_falls_back_locally_when_no_workers_are_alive(self, no_coordinator):
        """And the third: a reachable coordinator with an empty pool."""
        import warnings

        from gpumesh.mesh import MeshFunction

        class EmptyMesh(_FakeMesh):
            def workers(self):
                return []

        evaluate, seen = self._tracked()
        fn = MeshFunction(evaluate, EmptyMesh())
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            results = fn.map([{"lr": 0.1, "cost": 2.0}])

        assert results == [{"lr": 0.1, "score": 1.0}]
        assert seen == [0.1]

    def test_map_of_empty_list_is_still_empty(self, no_coordinator):
        from gpumesh.mesh import MeshFunction

        evaluate, _ = self._tracked()
        assert MeshFunction(evaluate, None).map([]) == []


class TestDirectCallKeepsItsArguments:
    """``__call__`` must NOT strip ``cost`` — the mirror-image bug.

    There is no payload on this path: the arguments are the caller's own, and
    ``_remote_call`` puts them into ``_params`` verbatim. So a ``cost`` keyword
    here can only mean the user's function declares a ``cost`` parameter, and
    dropping it locally would break that function exactly the way the payload
    bug broke the other one.
    """

    def test_call_delivers_a_declared_cost_argument_locally(self, no_coordinator):
        from gpumesh.mesh import MeshFunction

        def price(lr, cost):
            return {"lr": lr, "cost": cost}

        fn = MeshFunction(price, None)
        assert fn._afn is None
        assert fn(lr=0.1, cost=2.0) == {"lr": 0.1, "cost": 2.0}

    def test_call_delivers_a_declared_cost_argument_via_the_mesh(self, no_coordinator):
        """Same call, mesh configured, same answer.

        ``_FakeMesh`` has no ``jobs`` endpoint, so ``_remote_call`` raises
        ``_MeshUnavailable`` and lands on accelerate's own local fallback —
        which is the branch that would have differed had a strip been added
        there. The assertion is that both routes agree.
        """
        from gpumesh.mesh import MeshFunction

        def price(lr, cost):
            return {"lr": lr, "cost": cost}

        fn = MeshFunction(price, _FakeMesh())
        assert fn._afn is not None
        assert fn(lr=0.1, cost=2.0) == {"lr": 0.1, "cost": 2.0}

    def test_call_never_invents_a_cost_argument(self, no_coordinator):
        """A plain call reaches a plain signature untouched, both paths."""
        from gpumesh.mesh import MeshFunction

        captured = []

        def greedy(**kwargs):
            captured.append(kwargs)
            return kwargs

        MeshFunction(greedy, None)(lr=0.1)
        MeshFunction(greedy, _FakeMesh())(lr=0.1)
        assert captured == [{"lr": 0.1}, {"lr": 0.1}]


class TestPlacementHintsAreNotStripped:
    """``gpu``/``gpu_memory_mb``/``cpu_cores`` are not ``cost``.

    They ride at the same payload level, so it is tempting to strip them
    alongside it. They must not be: ``distribute()`` writes them there from
    the *decorator's* keywords (``@mesh(gpu="A100")``), never from a user
    payload, and a key of that name inside a ``.map()`` payload is an ordinary
    parameter that lands in ``_params`` and reaches the function on the mesh
    path. Dropping it locally would invent a new asymmetry rather than remove
    one, so both paths must deliver it.
    """

    @pytest.mark.parametrize("hint_name", ["gpu", "gpu_memory_mb", "cpu_cores"])
    def test_a_payload_key_named_like_a_hint_reaches_the_function(
        self, hint_name, no_coordinator
    ):
        from gpumesh.mesh import MeshFunction

        captured = []

        def takes_anything(**kwargs):
            captured.append(kwargs)
            return kwargs

        payload = [{"lr": 0.1, hint_name: "A100", "cost": 3.0}]

        MeshFunction(takes_anything, None).map(payload)
        MeshFunction(takes_anything, _FakeMesh()).map(payload)

        expected = {"lr": 0.1, hint_name: "A100"}
        assert captured == [expected, expected]

    def test_decorator_hints_never_appear_in_the_payload(self, no_coordinator):
        """``@mesh(gpu=...)`` is a scheduler instruction, not an argument.

        It is carried to distribute() as a keyword, so it cannot collide with
        the payload — which is why there is nothing to strip locally.
        """
        from gpumesh.mesh import MeshFunction

        captured = []

        def takes_anything(**kwargs):
            captured.append(kwargs)
            return kwargs

        fn = MeshFunction(takes_anything, None, gpu="A100", cores=8)
        fn.map([{"lr": 0.1, "cost": 2.0}])
        assert captured == [{"lr": 0.1}]


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