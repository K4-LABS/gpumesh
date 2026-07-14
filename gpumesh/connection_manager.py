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

_STALE_WARN_SECONDS = 3600  # warn if a saved config is older than 1 hour
_STALE_CLEAR_SECONDS = 86400  # default max age for clear_if_stale (1 day)


_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".gpumesh")
_CONFIG_PATH = os.path.join(_CONFIG_DIR, "config.json")


def _ensure_dir():
    os.makedirs(_CONFIG_DIR, exist_ok=True)


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
    # Restrict file permissions
    try:
        os.chmod(_CONFIG_PATH, 0o600)
    except OSError:
        pass
    if os.name == "nt":
        try:
            import subprocess
            subprocess.run(
                ["icacls", _CONFIG_PATH, "/inheritance:r", "/grant", f"{os.environ['USERNAME']}:(R,W)"],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass


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
            return config
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
    return None


def clear_connection():
    """Remove saved connection config."""
    try:
        os.remove(_CONFIG_PATH)
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
        print(
            f"[gpumesh] NOTE: using a saved connection from {age_h}h ago "
            f"({config.get('url')}). If the coordinator moved networks or "
            f"changed IP/VPN, run 'gpumesh disconnect' and re-join."
        )


def get_connection(url: str | None, token: str | None) -> tuple[str, str]:
    """Resolve connection from args, env vars, or saved config.

    Priority: explicit args > env vars > saved config.
    Returns (url, token). Raises SystemExit if not resolved.
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

    if resolved_url and resolved_token:
        save_connection(resolved_url, resolved_token)

    return resolved_url, resolved_token
