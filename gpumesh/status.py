"""Coordinator keep-alive screen: shared log sink for mesh lines.

The coordinator's background threads (HTTP handlers, lease reaper,
discovery printer) emit mesh log lines while the CLI shows a live
ticker (workers online / jobs / uptime) on interactive terminals.

The ticker redraws its block in place with ANSI cursor tricks. If mesh
lines printed directly to stdout at the same time, the next redraw
would erase them (the classic "absorbed log line" problem). To prevent
that, the mesh sink buffers lines while the ticker is active; the
ticker drains the most recent ones into the same redraw region every
refresh, so nothing is ever overwritten silently.

In piped / Docker / non-ticker mode the sink is inactive and lines are
printed immediately, exactly as before.
"""

from __future__ import annotations

import threading

from .ansi import safe_print

_lock = threading.Lock()
_active = False
_log_lines: list[str] = []

LOG_VISIBLE = 6  # max mesh log lines shown inside the ticker region


def is_active() -> bool:
    """True while the ticker is buffering mesh lines."""
    with _lock:
        return _active


def set_active(active: bool) -> None:
    """Enable/disable buffered mode.

    Disabling also discards the buffer — after the ticker stops, lines
    print directly again and the region is gone.
    """
    global _active
    with _lock:
        _active = active
        if not active:
            _log_lines.clear()


def log(line: str) -> None:
    """Record one mesh log line (thread-safe).

    Buffered while the ticker is active; printed immediately otherwise
    (identical to the pre-ticker behavior).
    """
    with _lock:
        if _active:
            _log_lines.append(line)
            return
    safe_print(line)


def snapshot(limit: int = LOG_VISIBLE) -> list[str]:
    """Return the most recent ``limit`` buffered lines (a copy).

    This is the visible log window the ticker renders above its block.
    """
    with _lock:
        return list(_log_lines[-limit:])
