from __future__ import annotations

"""gpumesh - distributed compute sharing over a mesh of volunteer machines."""

__version__ = "1.1.0"

from .api import GPUMesh  # noqa: F401
from .accelerate import accelerate, install as accelerate_install  # noqa: F401
from .ansi import safe_print, esc, bold, cyan, green, dim

import importlib as _importlib
import os as _os
import sys as _sys

__all__ = ["GPUMesh", "accelerate", "accelerate_install", "mesh", "torch"]

# Bind the ``mesh`` singleton EAGERLY as a real attribute.
#
# NOTE: this must NOT go through __getattr__/lazy resolution. The import
# machinery pins the package attribute ``gpumesh.mesh`` to the submodule the
# first time anything does ``import gpumesh.mesh``, which would permanently
# shadow a lazy ``__getattr__`` and make ``from gpumesh import mesh`` return
# the module instead of the @mesh decorator — depending on import order.
# By importing the submodule here (so ``mesh`` is the documented decorator)
# and re-binding to ``mesh_fn`` immediately, both access styles are safe:
#   from gpumesh import mesh          -> the @mesh decorator (callable)
#   from gpumesh.mesh import connect  -> module functions (module import)
from . import mesh as _mesh_module  # noqa: F401
mesh = _mesh_module.mesh_fn


def __getattr__(name: str):
    """Lazily expose optional integrations without importing dependencies."""
    if name == "torch":
        module = _importlib.import_module(".torch", __name__)
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

_FLAG_PATH = _os.path.join(_os.path.expanduser("~"), ".gpumesh_welcomed")


def _is_interactive() -> bool:
    """Check if we're in an interactive terminal."""
    if _sys.stdout is None or not hasattr(_sys.stdout, "isatty"):
        return False
    if not _sys.stdout.isatty():
        return False
    if hasattr(_sys, "ps1"):
        return False
    if "pytest" in _sys.modules:
        return False
    return True


def _read_installed_version() -> str | None:
    """Read the version stored in the flag file."""
    try:
        with open(_FLAG_PATH) as f:
            return f.read().strip()
    except (FileNotFoundError, OSError):
        return None


def _write_flag():
    """Write the current version to the flag file."""
    try:
        with open(_FLAG_PATH, "w") as f:
            f.write(__version__)
    except OSError:
        pass


if _is_interactive():
    installed_version = _read_installed_version()

    if installed_version is None:
        # Fresh install — show setup instructions
        # (We can't auto-run the wizard here because __init__.py runs
        # during pip install BEFORE the package is fully installed.
        # The wizard is triggered by 'gpumesh setup' or 'gpumesh quickjoin'.)
        try:
            safe_print()
            safe_print(cyan("=" * 60))
            safe_print(bold("  gpumesh installed successfully!"))
            safe_print(dim(f"  version {__version__}"))
            safe_print(cyan("=" * 60))
            safe_print()
            safe_print(bold("  Get started in one command:"))
            safe_print()
            safe_print(green("    gpumesh setup"))
            safe_print()
            safe_print(dim("  This will detect your hardware and guide you"))
            safe_print(dim("  through choosing coordinator or worker role."))
            safe_print(dim("  Or start a coordinator directly:"))
            safe_print(green("    gpumesh serve --token your-secret-token"))
            safe_print()
            safe_print(cyan("=" * 60))
            safe_print()
        except Exception:
            pass
        _write_flag()

    elif installed_version != __version__:
        # Upgraded — show brief upgrade message
        try:
            safe_print()
            safe_print(cyan("=" * 60))
            safe_print(bold(f"  gpumesh upgraded: {installed_version} -> {__version__}"))
            safe_print(cyan("=" * 60))
            safe_print()
            safe_print(bold("  Run 'gpumesh setup' to reconfigure, or:"))
            safe_print(green("    gpumesh --help"))
            safe_print()
        except Exception:
            pass
        _write_flag()
    # else: same version already shown — do nothing
