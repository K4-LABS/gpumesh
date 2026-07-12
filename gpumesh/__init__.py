"""gpumesh - distributed compute sharing over a mesh of volunteer machines."""

__version__ = "0.5.0"

from .api import GPUMesh  # noqa: F401

import os as _os
import sys as _sys

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
            print()
            print("\033[36m" + "=" * 60 + "\033[0m")
            print("\033[1m  gpumesh installed successfully!\033[0m")
            print("\033[36m" + "=" * 60 + "\033[0m")
            print()
            print("\033[1m  Get started in one command:\033[0m")
            print()
            print("\033[32m    gpumesh setup\033[0m")
            print()
            print("\033[2m  This will detect your hardware and guide you\033[0m")
            print("\033[2m  through choosing coordinator or worker role.\033[0m")
            print()
            print("\033[36m" + " " * 40 + "\033[0m")
            print()
        except Exception:
            pass
        _write_flag()

    elif installed_version != __version__:
        # Upgraded — show brief upgrade message
        try:
            print()
            print("\033[36m" + "=" * 60 + "\033[0m")
            print(f"\033[1m  gpumesh upgraded: {installed_version} -> {__version__}\033[0m")
            print("\033[36m" + "=" * 60 + "\033[0m")
            print()
            print("\033[1m  Run 'gpumesh setup' to reconfigure, or:\033[0m")
            print("\033[32m    gpumesh --help\033[0m")
            print()
        except Exception:
            pass
        _write_flag()
    # else: same version already shown — do nothing
