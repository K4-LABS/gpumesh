"""Live terminal radar for gpumesh peer discovery.

Displays a refreshing list of nearby devices (workers or coordinators)
using ANSI escape codes.  Falls back to plain text when stdout is not
a TTY.
"""

from __future__ import annotations

import shutil
import sys
import time

from .discovery import Listener, Peer


from .ansi import (_SUPPORTS_COLOR, esc as _esc, bold as _bold,
                   cyan as _cyan, green as _green, yellow as _yellow,
                   dim as _dim, erase_line as _erase_line,
                   move_up as _move_up, move_down as _move_down,
                   clear_lines as _clear_lines)


def _safe_write(text: str):
    """Write to stdout, ignoring broken pipe / closed TTY errors."""
    try:
        sys.stdout.write(text)
    except (OSError, ValueError):
        pass


def _safe_flush():
    try:
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


# -- radar display --------------------------------------------------------

def _format_peer_line(peer: Peer, index: int) -> str:
    """Format a single peer as a radar line, truncated to terminal width."""
    columns = shutil.get_terminal_size((80, 24)).columns
    icon = _green("+")
    name = _bold(peer.hostname)
    device = _cyan(peer.display_name)
    score = f"{peer.score:.1f} GFLOP/s"
    ip = _dim(peer.ip)

    line = f"  {icon} {name}  {device}  {score}  {ip}"
    # Truncate to terminal width (leave 1 char margin)
    max_len = columns - 1
    if len(line) > max_len:
        line = line[:max_len - 3] + "..."
    return line


def print_radar_header(mode: str = "coordinator"):
    """Print the radar header."""
    columns = shutil.get_terminal_size((80, 24)).columns
    if mode == "coordinator":
        title = "RADAR — Nearby Workers"
    else:
        title = "RADAR — Nearby Coordinators"

    width = max(50, len(title) + 8)
    # Clamp to terminal width
    width = min(width, columns - 1)
    print()
    print(_cyan("=" * width))
    print(_bold(f"  {title}"))
    print(_cyan("=" * width))
    print()
    print(_dim("  (scanning every 2s, press Ctrl+C to stop)"))
    print()


def print_radar_peers(peers: list[Peer], prev_count: int = 0) -> int:
    """Print/overwrite the peer list. Returns the number of lines printed."""
    lines = []

    if not peers:
        lines.append(_dim("  No devices found yet..."))
    else:
        for i, peer in enumerate(sorted(peers,
                                        key=lambda p: (p.hostname, p.ip))):
            lines.append(_format_peer_line(peer, i))

    # Clear previous output (move UP and erase)
    if prev_count > 0:
        _clear_lines(prev_count)

    for line in lines:
        _erase_line()
        _safe_write(f"{line}\n")
    _safe_flush()

    return len(lines)


def print_radar_footer():
    """Print footer after radar stops."""
    print()
    print(_dim("  Radar stopped."))


# -- interactive radar loop ------------------------------------------------

def select_peer(peers: list[Peer]) -> Peer | None:
    """Display a numbered list of peers and let the user pick one.

    Returns the selected Peer or None on cancel.
    """
    if not peers:
        print(_yellow("  No devices found."))
        return None

    print()
    for i, peer in enumerate(peers, 1):
        print(f"  {_cyan(str(i))}) {_bold(peer.hostname)} — "
              f"{peer.display_name} ({peer.score:.1f} GFLOP/s) — {peer.ip}")
    print()
    print(_dim("  Enter number to connect, or Ctrl+C to cancel"))
    print()

    try:
        choice = input(_bold("  Pick device: ")).strip()
        idx = int(choice) - 1
        if 0 <= idx < len(peers):
            return peers[idx]
        print(_yellow(f"  Invalid choice: {choice}"))
        return None
    except (ValueError, EOFError, KeyboardInterrupt):
        print()
        if isinstance(sys.exc_info()[1], EOFError):
            print(_yellow("  Cannot prompt in non-interactive mode."))
        return None


def select_worker_for_claim(peers: list[Peer]) -> tuple[Peer, str] | tuple[None, None]:
    """Display a numbered list of workers and let user pick one + enter token.

    Returns (peer, token) on success, or (None, None) on cancel.
    """
    if not peers:
        print(_yellow("  No workers found."))
        return None, None

    print()
    for i, peer in enumerate(peers, 1):
        print(f"  {_cyan(str(i))}) {_bold(peer.hostname)} — "
              f"{peer.display_name} ({peer.score:.1f} GFLOP/s) — {peer.ip}")
    print()
    print(_dim("  Enter number to claim, or Ctrl+C to cancel"))
    print()

    try:
        choice = input(_bold("  Pick worker: ")).strip()
        idx = int(choice) - 1
        if not (0 <= idx < len(peers)):
            print(_yellow(f"  Invalid choice: {choice}"))
            return None, None
        peer = peers[idx]
    except (ValueError, EOFError, KeyboardInterrupt):
        print()
        if isinstance(sys.exc_info()[1], EOFError):
            print(_yellow("  Cannot prompt in non-interactive mode."))
        return None, None

    # Prompt for token
    print()
    try:
        token = input(_bold(f"  Enter token for {peer.hostname}: ")).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None, None

    if not token:
        print(_yellow("  No token provided."))
        return None, None

    return peer, token
