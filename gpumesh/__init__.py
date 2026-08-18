from __future__ import annotations

"""gpumesh - distributed compute sharing over a mesh of volunteer machines."""

__version__ = "3.0.0"

# ── Wire protocol version ────────────────────────────────────────────────
#
# Deliberately NOT __version__. The coordinator and the workers are installed
# separately, on different machines, by different people, and they drift —
# that is the normal state of a mesh, not a misconfiguration. Tying the
# handshake to the package version would mean every patch release refused
# every worker that had not been upgraded that afternoon, so this is a small
# integer that only moves when the *wire* changes: a new required field, a
# changed meaning for an existing one, a new endpoint a worker must have.
#
# Why 2 and not 1: every gpumesh released up to and including 1.3.0 speaks a
# protocol that carries no version at all, and that unversioned protocol is a
# real thing this coordinator still has to talk to. It is named here,
# retroactively, as version 1 (UNVERSIONED_PROTOCOL below). Version 2 is the
# same protocol plus the handshake fields. Starting at 2 means the
# compatibility window is non-degenerate on the day it ships — the N-1 branch
# is exercised by every worker in the field right now, rather than being dead
# code that first runs whenever version 2 eventually lands.
PROTOCOL_VERSION = 2

# The compatibility window is exactly {N-1, N}: this coordinator accepts a
# worker one version behind, and a worker accepts a coordinator one version
# behind. One version of slack is what lets an operator upgrade the machines
# in a household or a lab one at a time instead of holding a flag day, which
# is the whole reason a mesh of borrowed laptops is workable. It is not wider
# because every supported version is a branch the code must actually keep
# implementing and CI must actually keep exercising — the compat workflow
# doubles in size per extra version — and "supported forever" is precisely how
# you arrive at the mystery misbehaviour this handshake exists to remove.
MIN_PROTOCOL_VERSION = PROTOCOL_VERSION - 1

# What an absent ``protocol_version`` means on the wire.
#
# This is a FIXED constant, not ``MIN_PROTOCOL_VERSION``. The distinction
# matters the moment PROTOCOL_VERSION becomes 3: a peer that sent no version
# is telling us it predates versioning entirely, which is evidence for "1",
# never for "whatever the oldest currently-supported version happens to be".
# Pinning it to the moving minimum would silently re-admit genuinely ancient
# peers forever and hand back exactly the undiagnosable failures this replaces.
#
# The serializer already learned the neighbouring half of this lesson the hard
# way (see the long comment on ``python_version`` in deserialize_function): a
# missing field means "unknown", and treating unknown as a mismatch broke real
# users. So absent is mapped to a concrete, defensible version and then run
# through the ordinary window check — it is not special-cased into a refusal,
# and it is not special-cased into an exemption either.
UNVERSIONED_PROTOCOL = 1


class ProtocolVersionMismatch(Exception):
    """A peer speaks a protocol version outside this one's support window.

    Deliberately not a bare ``ValueError``: this is raised on the registration
    path, where the worker's generic handler answers "failed to register" with
    troubleshooting advice about firewalls and tokens — all of it wrong for a
    version skew, and all of it sending the operator to look at the network
    when the fix is ``pip install -U gpumesh``. A named class is what lets that
    path route the failure to a message that names both versions instead.
    """

    def __init__(self, message: str, local: int = None, remote: int = None):
        super().__init__(message)
        #: Protocol version spoken by *this* process.
        self.local = local
        #: Protocol version spoken by the peer, after absent-value mapping.
        self.remote = remote


def is_supported_protocol(version: int) -> bool:
    """Is *version* inside this process's compatibility window?"""
    return MIN_PROTOCOL_VERSION <= version <= PROTOCOL_VERSION


from .api import GPUMesh, GPUMeshError  # noqa: F401,E402
from .accelerate import (  # noqa: F401
    accelerate,
    install as accelerate_install,
    PlacementUnsupportedError,
)
from .ansi import safe_print, esc, bold, cyan, green, dim

import importlib as _importlib
import os as _os
import sys as _sys

# THE declared public Python API. SemVer requires a project to say what its
# public API is, and until this list said so every internal rename was
# arguably a breaking change. What is listed here is supported and covered by
# the compatibility promises in docs/stability.md; everything else in the
# package — including every submodule not reachable through a name below — is
# internal and may change in any release. See docs/stability.md.
#
# ``mesh`` is in the list but is bound below rather than imported, for the
# reason in the long note under the binding: it must stay the decorator, not
# the submodule.
#
# Every exception a caller is expected to CATCH belongs here, and that is not
# a formality. ``PlacementUnsupportedError`` is what ``.map()`` raises instead
# of silently running a ``gpu=``-constrained batch on the local machine, so it
# is precisely the name a user has to write in an ``except`` clause to handle
# that case — and until it was listed, the one exception they could not avoid
# was, by this project's own written policy (docs/stability.md §1), not part of
# the public API. An exception that is raised at users but excluded from the
# supported surface is a promise nobody can rely on.
#
# ``_MeshUnavailable`` is deliberately NOT here: it is internal control flow
# raised and caught inside accelerate.py and never crosses the API boundary.
# ``UNVERSIONED_PROTOCOL`` and ``is_supported_protocol()`` are deliberately not
# here either — they are how this process interprets the wire, not something a
# caller needs; the wire itself is versioned separately (§3).
__all__ = [
    "GPUMesh",
    "GPUMeshError",
    "accelerate",
    "accelerate_install",
    "PlacementUnsupportedError",
    "mesh",
    "torch",
    "load_ipython_extension",
    "unload_ipython_extension",
    "ProtocolVersionMismatch",
    "PROTOCOL_VERSION",
    "MIN_PROTOCOL_VERSION",
    "__version__",
]

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
    # ``%load_ext gpumesh`` makes IPython look for these on the package
    # itself, not on the submodule, so they have to be reachable from here.
    if name in ("load_ipython_extension", "unload_ipython_extension"):
        magic = _importlib.import_module(".jupyter_magic", __name__)
        func = getattr(magic, name)
        globals()[name] = func
        return func
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
