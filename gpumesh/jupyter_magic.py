"""Jupyter / IPython magic extension for gpumesh.

Usage in Jupyter or IPython::

    %load_ext gpumesh

    @mesh                       # mesh decorator injected into the notebook
    def train(lr, epochs):
        return {"accuracy": 0.95}

    train(lr=0.01, epochs=100)                      # one worker (local if none)
    results = train.map([{"lr": 0.01}, {"lr": 0.05}])  # everywhere at once

    # %%mesh — a whole cell, like %%time: every function defined in the
    # cell is automatically wrapped with @mesh, then the cell runs. Calls
    # to those functions (single or .map()) go to the pool automatically.
    %%mesh
    def preprocess(chunk_id, rows):
        return {"chunk": chunk_id, "rows": rows * rows}

    result = preprocess.map([{"chunk_id": i, "rows": 100 + i} for i in range(6)])

    %mesh_devices               # show the pool
    %mesh_status                # show connection status
    %mesh_connect URL TOKEN     # connect to a coordinator
"""

from __future__ import annotations

import ast


def _mesh_functions():
    """Import the mesh module functions directly.

    NOTE: we must NOT use ``from . import mesh`` here — the gpumesh package
    binds the ``mesh`` attribute to the decorator function (``mesh_fn``), so
    that import would give us the decorator instead of the module. Importing
    the module functions explicitly avoids that footgun.
    """
    from gpumesh.mesh import (  # noqa: F401
        connect,
        devices,
        device_count,
        total_score,
        mesh_fn,
    )

    return connect, devices, device_count, total_score, mesh_fn


def _mesh_devices(args: str):
    """%mesh_devices — list all devices in the pool."""
    _connect, _devices, _device_count, _total_score, _ = _mesh_functions()
    _connect()

    devs = _devices()
    if not devs:
        print("[gpumesh] No workers connected. Your machine runs locally.")
        return

    alive = [d for d in devs if d.get("status") == "alive"]
    print(f"[gpumesh] Pool: {len(alive)} device(s) alive")
    for d in alive:
        print(f"  {d.get('device_name', d.get('device', '?'))} "
              f"({d.get('hostname', '?')})  score={d.get('score', 0):.1f}")

    print(f"  Total compute: {_total_score():.1f} GFLOP/s")


def _mesh_status(args: str):
    """%mesh_status — show connection status."""
    from gpumesh import connection_manager

    _connect, _devices, _device_count, _total_score, _ = _mesh_functions()
    _connect()

    saved = connection_manager.load_connection()
    if saved:
        print(f"[gpumesh] Connected to: {saved.get('url', '?')}")
        print(f"  Token: {saved.get('token', '?')[:12]}...")
        print(f"  Total devices (including you): {_device_count()}")
    else:
        print("[gpumesh] Not connected to any coordinator.")
        print("  Run: gpumesh join URL --token YOUR_TOKEN")
        print("  Or: gpumesh quickjoin URL --token YOUR_TOKEN")


def _transform_cell(source: str) -> tuple[str, int]:
    """Return ``(transformed_source, wrapped_count)`` for the ``%%mesh`` magic.

    Inserts ``@mesh`` before every top-level ``def`` so each function in
    the cell is automatically mesh-aware. Line-based insertion (instead of
    ``ast.unparse``) preserves the user's comments and formatting.

    - Only direct module-level functions are wrapped (functions nested in
      ``if``/``class``/other functions are left alone).
    - ``async def`` is skipped — coroutines can't be executed by the remote
      subprocess helper, so wrapping them would break at call time.
    - Functions that already have decorators get ``@mesh`` placed ABOVE
      them (outermost), so ``@mesh`` wraps the final decorated callable.
    """
    tree = ast.parse(source)
    insert_lines: set[int] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            # Skip functions the user already decorated with @mesh — wrapping
            # them again would produce mesh(mesh(f)), which breaks remote runs.
            if any(isinstance(d, ast.Name) and d.id == "mesh"
                   for d in node.decorator_list):
                continue
            if node.decorator_list:
                insert_lines.add(node.decorator_list[0].lineno)
            else:
                insert_lines.add(node.lineno)
    if not insert_lines:
        return source, 0

    lines = source.splitlines(keepends=True)
    out = []
    for i, line in enumerate(lines, start=1):
        if i in insert_lines:
            out.append("@mesh\n")
        out.append(line)
    return "".join(out), len(insert_lines)


def _mesh_cell(line: str, cell: str, ip=None):
    """Core of the ``%%mesh`` cell magic.

    Wraps every top-level function in the cell with ``@mesh``, then runs the
    cell through ``ip.run_cell`` so its own output displays exactly like
    ``%%time``. ``ip`` may be injected for testing; when ``None`` it is
    resolved lazily so this module stays importable without IPython.
    """
    if ip is None:
        try:
            from IPython import get_ipython
        except ImportError:
            print("[gpumesh] %%mesh requires IPython/Jupyter.")
            return None
        ip = get_ipython()
        if ip is None:
            print("[gpumesh] %%mesh requires IPython/Jupyter.")
            return None

    if line.strip():
        print(f"[gpumesh] %%mesh takes no arguments — got: {line.strip()!r}")
        return None

    _connect, _devices, _device_count, _total_score, _mesh_fn = _mesh_functions()
    _connect()

    # Make sure the @mesh decorator exists in the notebook namespace.
    if "mesh" not in ip.user_ns:
        ip.user_ns["mesh"] = _mesh_fn

    try:
        code, wrapped = _transform_cell(cell)
    except SyntaxError as exc:
        print(f"[gpumesh] %%mesh: could not parse cell: {exc}")
        return None

    if wrapped == 0:
        print("[gpumesh] %%mesh: no functions found in cell — ran it as-is.")

    # Execute the transformed cell; its own last-expression output is
    # displayed by run_cell (same behavior as %%time).
    result = ip.run_cell(code, store_history=False)
    if result is not None and getattr(result, "error_in_exec", None) is not None:
        return None
    return None


def load_ipython_extension(ip):
    """Called when ``%load_ext gpumesh`` is executed in IPython/Jupyter."""
    _connect, _devices, _device_count, _total_score, _mesh_fn = _mesh_functions()

    def mesh_devices(line):
        _mesh_devices(line)
    mesh_devices.__name__ = "mesh_devices"

    def mesh_status(line):
        _mesh_status(line)
    mesh_status.__name__ = "mesh_status"

    def mesh_connect(line):
        """%mesh_connect URL TOKEN — connect to a coordinator."""
        parts = line.strip().split()
        if len(parts) >= 2:
            _connect(parts[0], parts[1])
        else:
            print("Usage: %mesh_connect URL TOKEN")
    mesh_connect.__name__ = "mesh_connect"

    # ``register_magic_function`` is the public InteractiveShell API; it works
    # with a real IPython instance and keeps this testable with a fake shell.
    ip.register_magic_function(mesh_devices, magic_kind="line")
    ip.register_magic_function(mesh_status, magic_kind="line")
    ip.register_magic_function(mesh_connect, magic_kind="line")

    def mesh_cell(line, cell):
        """%%mesh — run this cell's functions across the pool."""
        return _mesh_cell(line, cell, ip=ip)

    mesh_cell.__name__ = "mesh"
    ip.register_magic_function(mesh_cell, magic_kind="cell")

    # Inject the @mesh decorator into the notebook namespace
    _connect()
    ip.user_ns["mesh"] = _mesh_fn


def unload_ipython_extension(ip):
    """Clean up when the extension is unloaded."""
    if "mesh" in ip.user_ns:
        del ip.user_ns["mesh"]
