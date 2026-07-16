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
                   red as _red, magenta as _magenta, blue as _blue,
                   dim as _dim, erase_line as _erase_line,
                   move_up as _move_up, move_down as _move_down,
                   clear_lines as _clear_lines, device_icon as _device_icon)


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

def _get_device_color(device: str) -> callable:
    """Return color function based on device type."""
    if device == "cuda":
        return _green
    elif device == "mps":
        return _magenta
    else:
        return _yellow


def _get_status_icon(peer: Peer) -> str:
    """Return status icon based on peer health."""
    if not peer.alive:
        return _red("x")
    elif peer.score > 50:
        return _green("+")
    elif peer.score > 10:
        return _yellow("+")
    else:
        return _dim("+")


def _format_peer_line(peer: Peer, index: int) -> str:
    """Format a single peer as a radar line with GPU status indicators."""
    columns = shutil.get_terminal_size((80, 24)).columns
    icon = _get_status_icon(peer)
    name = _bold(peer.hostname)
    device_color = _get_device_color(peer.device)
    device = device_color(peer.display_name)
    
    # GPU status indicators
    if peer.device == "cuda":
        status = _green("GPU")
    elif peer.device == "mps":
        status = _magenta("GPU")
    else:
        status = _yellow("CPU")
    score = f"{peer.score:.1f} GFLOP/s"

    # Mini score bar (0-100 scale)
    bar_width = 10
    filled = min(bar_width, int(peer.score / 100 * bar_width))
    if _SUPPORTS_COLOR:
        bar = f"{_green('#' * filled)}{_dim('.' * (bar_width - filled))}"
    else:
        bar = '#' * filled + '.' * (bar_width - filled)

    ip = _dim(peer.ip)
    
    # Network topology indicator
    if peer.claim_port > 0:
        topology = _cyan("CLAIM")
    else:
        topology = _dim("-----")

    line = f"  {icon} {name}  {device}  [{status}]  {score}  {topology}  {ip}"
    # Truncate to terminal width (leave 1 char margin)
    max_len = columns - 1
    if len(line) > max_len:
        line = line[:max_len - 3] + "..."
    return line


def print_radar_header(mode: str = "coordinator"):
    """Print the radar header with network topology info."""
    columns = shutil.get_terminal_size((80, 24)).columns
    if mode == "coordinator":
        title = "RADAR — Nearby Workers"
        subtitle = "Scanning network for GPU nodes..."
    else:
        title = "RADAR — Nearby Coordinators"
        subtitle = "Broadcasting presence, scanning for coordinators..."

    width = max(60, len(title) + 8)
    # Clamp to terminal width
    width = min(width, columns - 1)
    print()
    print(_cyan("=" * width))
    print(_bold(f"  {title}"))
    print(_dim(f"  {subtitle}"))
    print(_cyan("=" * width))
    print()
    print(_dim("  Legend: ") + _green("+") + _dim(" online  ") + 
          _yellow("+") + _dim(" medium  ") + _red("x") + _dim(" offline"))
    print(_dim("  [GPU] device type  CLAIM = claimable  GFLOP/s = compute score"))
    print()


def _print_network_topology(peers: list[Peer]):
    """Print network topology summary."""
    if not peers:
        return
    
    # Count device types
    cuda_count = sum(1 for p in peers if p.device == "cuda")
    mps_count = sum(1 for p in peers if p.device == "mps")
    cpu_count = sum(1 for p in peers if p.device == "cpu")
    claimable = sum(1 for p in peers if p.claim_port > 0)
    total_score = sum(p.score for p in peers)
    
    # Network topology display
    lines = []
    lines.append(_dim("  Network Topology:"))
    if cuda_count > 0:
        lines.append(f"    {_green('CUDA GPUs')}: {cuda_count}")
    if mps_count > 0:
        lines.append(f"    {_magenta('Apple Silicon')}: {mps_count}")
    if cpu_count > 0:
        lines.append(f"    {_yellow('CPUs')}: {cpu_count}")
    
    lines.append(_dim("  ─────────────────────────────────"))
    lines.append(f"    Total: {len(peers)} nodes | "
                f"{_cyan(f'{claimable} claimable')} | "
                f"{_green(f'{total_score:.1f} GFLOP/s')}")
    
    for line in lines:
        _erase_line()
        _safe_write(f"{line}\n")


def print_radar_peers(peers: list[Peer], prev_count: int = 0) -> int:
    """Print/overwrite the peer list with real-time updates."""
    lines = []

    if not peers:
        lines.append(_dim("  Scanning for devices..."))
    else:
        # Sort by score (highest first) then hostname
        sorted_peers = sorted(peers, 
                             key=lambda p: (-p.score, p.hostname, p.ip))
        for i, peer in enumerate(sorted_peers):
            lines.append(_format_peer_line(peer, i))
        
        # Add network topology summary
        lines.append("")  # blank line separator
        _print_network_topology(peers)

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
    """Display a numbered list of peers with GPU status indicators.

    Returns the selected Peer or None on cancel.
    """
    if not peers:
        print(_yellow("  No devices found."))
        return None

    print()
    print(_dim("  Available devices:"))
    print()
    
    # Sort by score (highest first)
    sorted_peers = sorted(peers, key=lambda p: -p.score)
    
    for i, peer in enumerate(sorted_peers, 1):
        device_color = _get_device_color(peer.device)
        status_icon = _get_status_icon(peer)
        
        if peer.device == "cuda":
            status = _green("GPU")
        elif peer.device == "mps":
            status = _magenta("GPU")
        else:
            status = _yellow("CPU")
        
        claimable = _cyan("CLAIM") if peer.claim_port > 0 else _dim("-----")
        
        print(f"  {_cyan(str(i))}) {status_icon} {_bold(peer.hostname)} — "
              f"{device_color(peer.display_name)} [{status}] "
              f"({peer.score:.1f} GFLOP/s) {claimable} — {_dim(peer.ip)}")
    print()
    print(_dim("  Enter number to connect, or Ctrl+C to cancel"))
    print()

    try:
        choice = input(_bold("  Pick device: ")).strip()
        idx = int(choice) - 1
        if 0 <= idx < len(sorted_peers):
            return sorted_peers[idx]
        print(_yellow(f"  Invalid choice: {choice}"))
        return None
    except (ValueError, EOFError, KeyboardInterrupt):
        print()
        if isinstance(sys.exc_info()[1], EOFError):
            print(_yellow("  Cannot prompt in non-interactive mode."))
        return None


def select_worker_for_claim(peers: list[Peer]) -> tuple[Peer, str] | tuple[None, None]:
    """Display a numbered list of workers with GPU status for claiming.

    Returns (peer, token) on success, or (None, None) on cancel.
    """
    if not peers:
        print(_yellow("  No workers found."))
        return None, None

    print()
    print(_dim("  Claimable workers:"))
    print()
    
    # Sort by score (highest first)
    sorted_peers = sorted(peers, key=lambda p: -p.score)
    
    for i, peer in enumerate(sorted_peers, 1):
        device_color = _get_device_color(peer.device)
        status_icon = _get_status_icon(peer)
        
        if peer.device == "cuda":
            status = _green("GPU")
        elif peer.device == "mps":
            status = _magenta("GPU")
        else:
            status = _yellow("CPU")
        
        claimable = _cyan("CLAIM") if peer.claim_port > 0 else _dim("-----")
        
        print(f"  {_cyan(str(i))}) {status_icon} {_bold(peer.hostname)} — "
              f"{device_color(peer.display_name)} [{status}] "
              f"({peer.score:.1f} GFLOP/s) {claimable} — {_dim(peer.ip)}")
    print()
    print(_dim("  Enter number to claim, or Ctrl+C to cancel"))
    print()

    try:
        choice = input(_bold("  Pick worker: ")).strip()
        idx = int(choice) - 1
        if not (0 <= idx < len(sorted_peers)):
            print(_yellow(f"  Invalid choice: {choice}"))
            return None, None
        peer = sorted_peers[idx]
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
