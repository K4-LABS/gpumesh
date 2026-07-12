"""Persistent connection manager for gpumesh.

Saves coordinator URL and token after first use so subsequent commands
don't require --url and --token flags.

Config file: ~/.gpumesh/config.json
"""

from __future__ import annotations

import json
import os
import time


_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".gpumesh")
_CONFIG_PATH = os.path.join(_CONFIG_DIR, "config.json")


def _ensure_dir():
    os.makedirs(_CONFIG_DIR, exist_ok=True)


def save_connection(url: str, token: str):
    """Save coordinator URL and token for future commands."""
    _ensure_dir()
    config = {
        "url": url.rstrip("/"),
        "token": token,
        "saved_at": time.time(),
    }
    with open(_CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def load_connection() -> dict | None:
    """Load saved connection. Returns {"url": ..., "token": ...} or None."""
    try:
        with open(_CONFIG_PATH) as f:
            config = json.load(f)
        if config.get("url") and config.get("token"):
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


def get_connection(url: str | None, token: str | None) -> tuple[str, str]:
    """Resolve connection from args, env vars, or saved config.

    Priority: explicit args > env vars > saved config.
    Returns (url, token). Raises SystemExit if not resolved.
    """
    # Try explicit args first
    if url and token:
        save_connection(url, token)
        return url, token

    # Try env vars
    env_url = url or os.environ.get("GPUMESH_URL", "")
    env_token = token or os.environ.get("GPUMESH_TOKEN", "")
    if env_url and env_token:
        save_connection(env_url, env_token)
        return env_url, env_token

    # Try saved config
    saved = load_connection()
    if saved:
        return saved["url"], saved["token"]

    # Nothing available
    return "", ""
