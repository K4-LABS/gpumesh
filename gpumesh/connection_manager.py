"""Persistent connection manager for gpumesh.

Saves coordinator URL and token after first use so subsequent commands
don't require --url and --token flags.

Config file: ~/.gpumesh/config.json
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse

from gpumesh.ansi import safe_print, green, yellow, red, bold
from gpumesh._file_perms import restrict_path

_STALE_WARN_SECONDS = 3600  # warn if a saved config is older than 1 hour
_STALE_CLEAR_SECONDS = 86400  # default max age for clear_if_stale (1 day)


_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".gpumesh")
_CONFIG_PATH = os.path.join(_CONFIG_DIR, "config.json")

# The coordinator's job database lives next to the saved connection, not in
# the process's working directory. The old default -- the relative path
# "gpumesh.db" -- meant the queue was wherever `gpumesh serve` happened to be
# run from: restart the coordinator from another folder and it opened a fresh,
# empty database, silently discarding every queued job. Jobs are the whole
# point of the coordinator, so the database gets the same stable home as the
# token.
_DB_PATH = os.path.join(_CONFIG_DIR, "gpumesh.db")


def _ensure_dir():
    # Restricted only on the call that creates it, not every call: this
    # runs on most command invocations, and a persistent chmod/icacls
    # failure would otherwise print the same warning every time rather than
    # once -- the exact "reads as a glitch and gets tuned out" problem
    # _warn_if_world_readable (below) is written to avoid for the token file.
    created = not os.path.isdir(_CONFIG_DIR)
    os.makedirs(_CONFIG_DIR, exist_ok=True)
    if created:
        restrict_path(_CONFIG_DIR, "the saved token, job database and TLS key")


def default_db_path() -> str:
    """The stable default location for the coordinator's job database.

    ``~/.gpumesh/gpumesh.db``, next to ``config.json``: a path that does not
    change with the working directory, so a coordinator restarted from
    anywhere reopens the same queue instead of a fresh empty one.
    """
    _ensure_dir()
    return _DB_PATH


def _normalize_url(url: str) -> str:
    """Lowercase scheme/host and strip trailing slashes for stable storage."""
    url = (url or "").strip()
    if not url:
        return url
    url = url.rstrip("/")
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    return urllib.parse.urlunsplit(
        (scheme, netloc, parts.path, parts.query, parts.fragment)
    )


_TOKEN_REMEDIATION = (
    "That token grants code execution across the mesh. Restrict the file "
    "by hand, or run 'gpumesh disconnect' and rotate the token if this "
    "machine is shared."
)


def save_connection(url: str, token: str):
    """Save coordinator URL and token for future commands.

    The URL is normalized (lowercased scheme/host, trailing slashes
    stripped) so stale/dead variants don't accumulate.

    On Unix, sets file permissions to 0o600 (owner-only read/write)
    so the token is not readable by other users on the system.
    """
    _ensure_dir()
    config = {
        "url": _normalize_url(url),
        "token": token,
        "saved_at": time.time(),
    }
    import tempfile
    tmp_fd, tmp_path = tempfile.mkstemp(dir=_CONFIG_DIR, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(config, f, indent=2)
        os.replace(tmp_path, _CONFIG_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    safe_print(green(f"[gpumesh] config saved to {_CONFIG_PATH}"))
    # A failure here is a warning, never an error. The file is already on
    # disk and refusing the whole save would leave the user with no saved
    # connection at all — a worse outcome than a readable one. But it must
    # not be *silent*: this file holds the mesh token in plaintext, the
    # token is the only thing standing between a stranger on the LAN and
    # arbitrary code execution on every node in the mesh, and the user's
    # mental model after seeing "config saved" is that gpumesh locked it
    # down. Swallowing the failure meant the one case where that model is
    # wrong was also the one case nobody was told about.
    restrict_path(_CONFIG_PATH, "your mesh token in plaintext", _TOKEN_REMEDIATION)


_warned_world_readable = False


def _warn_if_world_readable():
    """Warn once per process if the saved token file is group/other readable.

    save_connection tightens permissions, but it is not the only way this
    file comes into existence: it survives upgrades from versions that never
    chmod'd it, it can be restored from a backup or copied between machines
    with a permissive umask, and the tightening itself can fail (see
    _warn_permissions). Checking on read is what catches all of those, since
    every command that resolves a connection goes through here.

    Only meaningful on POSIX. On Windows st_mode carries no real ACL
    information — a file with a wide-open ACL still reports 0o666 — so
    inspecting it there would produce a warning that is simultaneously
    always-on and evidence-free. Windows protection comes from the icacls
    call in save_connection instead.

    Warns once, not once per call: several CLI commands resolve the
    connection more than once in a single run, and a security warning
    repeated three times in a row reads as a glitch and gets tuned out.
    """
    global _warned_world_readable
    if _warned_world_readable or os.name == "nt":
        return
    try:
        mode = os.stat(_CONFIG_PATH).st_mode
    except OSError:
        return
    if mode & 0o077:
        _warned_world_readable = True
        safe_print(yellow(
            f"[gpumesh] WARNING: {_CONFIG_PATH} is readable by other users "
            f"(mode {oct(mode & 0o777)}) and it holds your mesh token in "
            f"plaintext. Run 'chmod 600 {_CONFIG_PATH}' to fix it."
        ))


def load_connection() -> dict | None:
    """Load saved connection.

    Returns {"url": ..., "token": ..., "saved_at": ...} (saved_at is a
    Unix timestamp, or None if missing) or None if no valid config exists.
    """
    try:
        with open(_CONFIG_PATH) as f:
            config = json.load(f)
        if config.get("url") and config.get("token"):
            config.setdefault("saved_at", None)
            _warn_if_world_readable()
            return config
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
    return None


def clear_connection():
    """Remove saved connection config."""
    try:
        os.remove(_CONFIG_PATH)
        safe_print(yellow(f"[gpumesh] config cleared: {_CONFIG_PATH}"))
    except FileNotFoundError:
        pass


def clear_if_stale(max_age_seconds: int = _STALE_CLEAR_SECONDS) -> bool:
    """Remove the saved connection if it is older than ``max_age_seconds``.

    Returns True if a stale config was removed, False otherwise. Use this
    after a connection failure so a dead saved URL (e.g. an old VPN/network
    IP) doesn't keep getting silently reused.
    """
    config = load_connection()
    if not config:
        return False
    saved_at = config.get("saved_at")
    if saved_at is None or (time.time() - saved_at) > max_age_seconds:
        clear_connection()
        return True
    return False


def warn_stale(config: dict, threshold_seconds: int = _STALE_WARN_SECONDS):
    """Print a note if a saved config being used is older than threshold."""
    saved_at = config.get("saved_at")
    if saved_at is None:
        return
    age = time.time() - saved_at
    if age > threshold_seconds:
        age_h = int(age // 3600)
        safe_print(yellow(
            f"[gpumesh] NOTE: using a saved connection from {age_h}h ago "
            f"({config.get('url')}). If the coordinator moved networks or "
            f"changed IP/VPN, run 'gpumesh disconnect' and re-join."
        ))


def get_connection(url: str | None, token: str | None,
                   persist: bool = False) -> tuple[str, str]:
    """Resolve connection from args, env vars, or saved config.

    Priority: explicit args > env vars > saved config.
    Returns (url, token), or ("", "") when nothing can be resolved.

    ``persist`` controls whether the resolved values overwrite the saved
    config. It defaults to False: resolving a connection is a read, and a
    one-off ``--url``/``--token`` (or a typo in one) must not destroy the
    connection the user established with ``join``/``serve``. Only commands
    that deliberately establish a connection pass ``persist=True``.
    """
    saved = load_connection() or {}
    resolved_url = (
        url
        or os.environ.get("GPUMESH_URL", "")
        or saved.get("url", "")
    )
    resolved_token = (
        token
        or os.environ.get("GPUMESH_TOKEN", "")
        or saved.get("token", "")
    )

    # If we fell back to a saved config (no explicit url/token/env), warn
    # the user if that config looks stale so a dead URL is easier to spot.
    if (not url and not token
            and not os.environ.get("GPUMESH_URL")
            and not os.environ.get("GPUMESH_TOKEN")
            and saved.get("url")):
        warn_stale(saved)

    if persist and resolved_url and resolved_token:
        save_connection(resolved_url, resolved_token)

    return resolved_url, resolved_token
