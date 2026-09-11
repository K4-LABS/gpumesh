"""Shared owner-only permission hardening for gpumesh's per-user state.

Several files gpumesh writes under ``~/.gpumesh`` hold something a stranger
on a shared machine should not be able to read: the saved mesh token
(``connection_manager.py``), the job database with its submitted code,
payloads and results (``db.py``), and the TLS private key ``--tls`` relies
on (``tls.py``). This module is the one place that knows how to lock a path
down on both platforms, so the three callers do not each reinvent it -- and
so a fix here (or a fourth caller, later) covers all of them.

A failure to tighten permissions is always a warning, never an exception:
the file is already on disk, and refusing to continue would be a worse
outcome than a readable file the user was told about.
"""

from __future__ import annotations

import os

from gpumesh.ansi import safe_print, yellow


def restrict_path(path: str, holds: str, remediation: str | None = None) -> None:
    """Restrict ``path`` to owner-only access, warning (never raising) on failure.

    A file is chmod'd ``0600``, a directory ``0700``. On Windows, ``chmod``
    only flips the read-only bit and does nothing about the inherited ACL
    that typically grants the ``Users`` group read access, so ``icacls`` is
    what actually restricts it there -- it runs in addition to, not instead
    of, the ``chmod`` above.

    ``holds`` is a short description of what ``path`` contains, folded into
    the warning so a reader knows which file to go fix by hand and why it
    matters: "permissions could not be tightened" without saying *what* is
    exposed is not something the reader can act on. ``remediation``
    overrides the generic closing sentence when a caller has something more
    specific to suggest than "restrict it by hand".
    """
    is_dir = os.path.isdir(path)
    mode, mode_label = (0o700, "0700") if is_dir else (0o600, "0600")
    try:
        os.chmod(path, mode)
    except OSError as exc:
        _warn(path, holds, f"could not set owner-only permissions ({mode_label}): {exc}", remediation)
    if os.name == "nt":
        _restrict_windows_acl(path, holds, remediation)


def _restrict_windows_acl(path: str, holds: str, remediation: str | None) -> None:
    user = os.environ.get("USERNAME", "")
    if not user:
        _warn(
            path, holds,
            "USERNAME is not set, so the Windows ACL could not be restricted with icacls",
            remediation,
        )
        return
    try:
        import subprocess
        proc = subprocess.run(
            ["icacls", path, "/inheritance:r", "/grant", f"{user}:(R,W)"],
            capture_output=True, timeout=5,
        )
        if proc.returncode != 0:
            detail = proc.stderr or proc.stdout or b""
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", errors="replace")
            detail = " ".join(detail.split()) or f"exit code {proc.returncode}"
            _warn(path, holds, f"icacls could not restrict the ACL: {detail}", remediation)
    except Exception as exc:
        _warn(path, holds, f"icacls could not be run: {exc}", remediation)


def _warn(path: str, holds: str, what_failed: str, remediation: str | None) -> None:
    closing = remediation or "Restrict it by hand if this machine is shared."
    safe_print(yellow(
        f"[gpumesh] WARNING: {path} holds {holds} and {what_failed}. Other "
        f"users on this machine may be able to read it. {closing}"
    ))
