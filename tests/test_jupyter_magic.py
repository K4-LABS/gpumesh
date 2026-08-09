"""Tests for the Jupyter magic extension (``gpumesh.jupyter_magic``).

These guard against a past regression: ``from . import mesh`` inside the
magic module resolved to the ``mesh_fn`` *function* (exposed by the package
``__getattr__``) instead of the mesh module, which crashed ``%load_ext``.
They also cover the ``%%mesh`` cell magic: ``_transform_cell`` wraps
functions with ``@mesh``, and ``_mesh_cell`` runs the transformed cell.
"""

import ast

import pytest

import gpumesh  # noqa: F401  (ensures the package-level __getattr__ is active)


class _CellResult:
    """Minimal stand-in for IPython's ExecutionResult."""

    def __init__(self, error_in_exec=None, result=None):
        self.error_in_exec = error_in_exec
        self.result = result


class FakeShell:
    """Minimal stand-in for an IPython InteractiveShell."""

    def __init__(self):
        self.user_ns = {}
        self.magics = {"line": [], "cell": []}
        self._last_cell_result = None

    def register_magic_function(self, func, magic_kind="line"):
        self.magics.setdefault(magic_kind, []).append(func.__name__)

    def run_cell(self, code, store_history=True):
        """Execute ``code`` in user_ns, capturing the last expression value.

        Mirrors what IPython does for a cell (display + Out[] capture) so
        the ``%%mesh`` magic is testable without a real IPython install.
        """
        try:
            tree = ast.parse(code)
            if tree.body and isinstance(tree.body[-1], ast.Expr):
                last = tree.body[-1]
                assign = ast.Assign(
                    targets=[ast.Name(id="_mesh_test_result", ctx=ast.Store())],
                    value=last.value,
                )
                ast.copy_location(assign, last)
                ast.fix_missing_locations(assign)
                tree.body[-1] = assign
            exec(compile(tree, "<mesh-cell>", "exec"), self.user_ns)
            result = self.user_ns.pop("_mesh_test_result", None)
            self._last_cell_result = result
            return _CellResult(error_in_exec=None, result=result)
        except Exception as exc:
            return _CellResult(error_in_exec=exc)


class TestMeshFunctionsImport:
    """The magic module must import mesh module functions, not the decorator."""

    def test_mesh_functions_returns_callables(self):
        from gpumesh.jupyter_magic import _mesh_functions

        connect, devices, device_count, total_score, mesh_fn = _mesh_functions()
        assert callable(connect)
        assert callable(devices)
        assert callable(device_count)
        assert callable(total_score)
        assert callable(mesh_fn)

    def test_mesh_devices_runs_without_coordinator(self, capsys):
        """%mesh_devices degrades gracefully with no coordinator."""
        from gpumesh.jupyter_magic import _mesh_devices

        _mesh_devices("")
        out = capsys.readouterr().out
        assert "gpumesh" in out.lower()

    def test_mesh_status_runs_without_coordinator(self, capsys):
        """%mesh_status degrades gracefully with no coordinator."""
        from gpumesh.jupyter_magic import _mesh_status

        _mesh_status("")
        out = capsys.readouterr().out
        assert "gpumesh" in out.lower()


class TestLoadExtension:
    """%load_ext gpumesh must not crash and must inject @mesh."""

    def test_load_ipython_extension_injects_mesh(self):
        from gpumesh.jupyter_magic import load_ipython_extension

        shell = FakeShell()
        load_ipython_extension(shell)  # must not raise
        assert callable(shell.user_ns.get("mesh")), "@mesh was not injected"

    def test_hook_is_reachable_on_the_package(self):
        """``%load_ext gpumesh`` looks the hook up on the package itself.

        Importing it from gpumesh.jupyter_magic is not enough — IPython does
        ``getattr(gpumesh, "load_ipython_extension")``, so if only the
        submodule exposes it the documented magic fails with AttributeError.
        """
        import gpumesh

        assert callable(getattr(gpumesh, "load_ipython_extension", None))
        assert callable(getattr(gpumesh, "unload_ipython_extension", None))

    def test_package_level_hook_registers_every_magic(self):
        import gpumesh

        shell = FakeShell()
        gpumesh.load_ipython_extension(shell)

        assert "mesh" in shell.magics.get("cell", [])
        for name in ("mesh_devices", "mesh_status", "mesh_connect"):
            assert name in shell.magics.get("line", [])

    def test_unload_removes_mesh(self):
        from gpumesh.jupyter_magic import load_ipython_extension, unload_ipython_extension

        shell = FakeShell()
        load_ipython_extension(shell)
        unload_ipython_extension(shell)
        assert "mesh" not in shell.user_ns

    def test_cell_magic_registered(self):
        """%%mesh is registered as a CELL magic (not a line magic)."""
        from gpumesh.jupyter_magic import load_ipython_extension

        shell = FakeShell()
        load_ipython_extension(shell)
        assert "mesh" in shell.magics.get("cell", [])
        assert "mesh" not in shell.magics.get("line", [])


class TestTransformCell:
    """_transform_cell wraps top-level functions with @mesh."""

    def test_wraps_top_level_functions(self):
        from gpumesh.jupyter_magic import _transform_cell

        src = "def train(lr):\n    return lr\n\ndef preprocess(x):\n    return x\n"
        out, count = _transform_cell(src)
        assert count == 2
        assert out == (
            "@mesh\ndef train(lr):\n    return lr\n\n"
            "@mesh\ndef preprocess(x):\n    return x\n"
        )

    def test_preserves_comments_and_formatting(self):
        from gpumesh.jupyter_magic import _transform_cell

        src = "# my comment\ndef f():\n    pass\n"
        out, count = _transform_cell(src)
        assert count == 1
        assert out.startswith("# my comment\n@mesh\ndef f():")

    def test_skips_async_and_nested_functions(self):
        from gpumesh.jupyter_magic import _transform_cell

        src = (
            "async def fetch():\n    pass\n\n"
            "class A:\n    def method(self):\n        pass\n\n"
            "def top():\n    def inner():\n        pass\n    return inner\n"
        )
        out, count = _transform_cell(src)
        assert count == 1
        assert out.count("@mesh") == 1
        assert "@mesh\ndef top():" in out
        assert "async def fetch" in out  # untouched
        assert "def method" in out  # class method untouched

    def test_no_functions_returns_unchanged(self):
        from gpumesh.jupyter_magic import _transform_cell

        src = "x = 1 + 2\nprint(x)\n"
        out, count = _transform_cell(src)
        assert count == 0
        assert out == src

    def test_existing_decorator_gets_mesh_on_top(self):
        from gpumesh.jupyter_magic import _transform_cell

        src = "@decorator\ndef f():\n    pass\n"
        out, count = _transform_cell(src)
        assert count == 1
        assert out == "@mesh\n@decorator\ndef f():\n    pass\n"

    def test_multiline_decorator_gets_mesh_on_top(self):
        from gpumesh.jupyter_magic import _transform_cell

        src = "@decorator(\n    arg\n)\ndef f():\n    pass\n"
        out, count = _transform_cell(src)
        assert count == 1
        assert out.startswith("@mesh\n@decorator(")


class TestMeshCell:
    """%%mesh executes the transformed cell in the notebook namespace."""

    def test_cell_functions_defined_and_mesh_aware(self, capsys):
        from gpumesh.jupyter_magic import _mesh_cell

        shell = FakeShell()
        cell = (
            "def square(x):\n"
            "    return {'x': x, 'square': x * x}\n"
            "results = square.map([{'x': 2}, {'x': 3}])\n"
        )
        _mesh_cell("", cell, ip=shell)

        assert callable(shell.user_ns.get("square"))
        assert hasattr(shell.user_ns["square"], "map"), "function must be mesh-aware"
        # No coordinator configured -> falls back to local execution.
        assert shell.user_ns.get("results") == [
            {"x": 2, "square": 4},
            {"x": 3, "square": 9},
        ]

    def test_cell_last_expression_value_captured(self, capsys):
        from gpumesh.jupyter_magic import _mesh_cell

        shell = FakeShell()
        cell = "def double(x):\n    return {'v': x * 2}\n\ndouble(21)\n"
        _mesh_cell("", cell, ip=shell)
        # Single call runs locally when disconnected; the FakeShell captures
        # the cell's last expression value (mirroring IPython's Out[]).
        assert callable(shell.user_ns.get("double"))
        assert shell._last_cell_result == {"v": 42}

    def test_syntax_error_reports_gracefully(self, capsys):
        from gpumesh.jupyter_magic import _mesh_cell

        shell = FakeShell()
        result = _mesh_cell("", "def broken(:\n", ip=shell)
        assert result is None
        out = capsys.readouterr().out
        assert "could not parse" in out

    def test_no_functions_warns_and_runs_as_is(self, capsys):
        from gpumesh.jupyter_magic import _mesh_cell

        shell = FakeShell()
        _mesh_cell("", "x = 42\n", ip=shell)
        out = capsys.readouterr().out
        assert "no functions" in out
        assert shell.user_ns.get("x") == 42

    def test_mesh_injected_into_namespace(self, capsys):
        from gpumesh.jupyter_magic import _mesh_cell

        shell = FakeShell()
        cell = "def f():\n    return 1\n"
        _mesh_cell("", cell, ip=shell)
        # The @mesh decorator (a callable) must exist in the notebook ns.
        assert callable(shell.user_ns.get("mesh")), "@mesh decorator must exist"

    def test_already_meshed_function_not_double_wrapped(self, capsys):
        """A function the user already decorated with @mesh is left alone
        (wrapping twice would produce mesh(mesh(f)) and break remote runs)."""
        from gpumesh.jupyter_magic import _transform_cell, _mesh_cell

        src = "@mesh\ndef f():\n    return 1\n"
        out, count = _transform_cell(src)
        assert count == 0
        assert out == src, "existing @mesh must not be duplicated"

        shell = FakeShell()
        cell = "@mesh\ndef g():\n    return 2\ng()\n"
        _mesh_cell("", cell, ip=shell)
        assert callable(shell.user_ns.get("g"))

    def test_magic_line_arguments_rejected(self, capsys):
        """%%mesh takes no options; a trailing argument must be rejected,
        not silently run a transformed cell the user didn't intend."""
        from gpumesh.jupyter_magic import _mesh_cell

        shell = FakeShell()
        result = _mesh_cell("-h", "def f():\n    pass\n", ip=shell)
        assert result is None
        out = capsys.readouterr().out
        assert "takes no arguments" in out
        assert "f" not in shell.user_ns, "cell must not run when args rejected"
