"""Interactive setup wizard for gpumesh.

Simple 2-question wizard:
1. Are you setting up the first machine, or joining someone else?
2. (If joining) What's the address and token?

No technical jargon. Just copy-paste commands.
"""

from __future__ import annotations

import os
import platform
import secrets
import shutil
import subprocess
import sys


# ANSI color helpers (graceful fallback on Windows cmd)
_SUPPORTS_COLOR = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
_C = {
    "reset":   "\033[0m"   if _SUPPORTS_COLOR else "",
    "bold":    "\033[1m"   if _SUPPORTS_COLOR else "",
    "cyan":    "\033[36m"  if _SUPPORTS_COLOR else "",
    "green":   "\033[32m"  if _SUPPORTS_COLOR else "",
    "yellow":  "\033[33m"  if _SUPPORTS_COLOR else "",
    "red":     "\033[31m"  if _SUPPORTS_COLOR else "",
}


def _c(color: str, text: str) -> str:
    return f"{_C.get(color, '')}{text}{_C['reset']}"


def _print_box(lines: list[str]):
    """Print a centered box with cyan borders."""
    width = max(len(line) for line in lines) + 4
    print()
    print(_c("cyan", "=" * width))
    for line in lines:
        print(_c("bold", f"  {line}"))
    print(_c("cyan", "=" * width))
    print()


def _detect_gpu() -> str:
    """Detect GPU. Returns 'cuda', 'mps', or 'cpu'."""
    # NVIDIA
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            name = result.stdout.strip().split("\n")[0]
            print(_c("green", f"  GPU found: {name}"))
            return "cuda"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    # Apple Silicon
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        try:
            import torch
            if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                print(_c("green", "  GPU found: Apple Silicon"))
                return "mps"
        except ImportError:
            pass

    print(_c("yellow", "  No GPU found (CPU only — still works!)"))
    return "cpu"


def _has_tailscale() -> bool:
    """Check if Tailscale is installed."""
    tailscale_bin = shutil.which("tailscale")
    if tailscale_bin is None:
        return False
    try:
        result = subprocess.run(
            [tailscale_bin, "status"],
            capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False





def _get_tailscale_ip() -> str | None:
    """Get the Tailscale IP address."""
    tailscale_bin = shutil.which("tailscale")
    if tailscale_bin is None:
        return None
    try:
        result = subprocess.run(
            [tailscale_bin, "ip", "-4"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            ip = result.stdout.strip()
            if ip.startswith("100."):
                return ip
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def _ask(question: str) -> str:
    """Ask a question and get input."""
    try:
        return input(_c("bold", f"  {question} ")).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def run_setup_wizard():
    """Main setup wizard — simple 2-step flow."""

    # --- Header ---
    _print_box([
        "GPUMESH SETUP",
        "",
        "Share GPU power between machines.",
        "No complex setup needed.",
    ])

    # --- Detect hardware ---
    print(_c("bold", "  Detecting your hardware..."))
    device = _detect_gpu()
    print()

    # --- Ask the ONE question ---
    print(_c("bold", "  What do you want to do?"))
    print()
    print(_c("cyan", "    1) Set up this machine to MANAGE jobs"))
    print(_c("cyan", "    2) Add this machine to someone else's mesh"))
    print()

    choice = _ask("Enter 1 or 2:")

    if choice == "1":
        _setup_coordinator(device)
    elif choice == "2":
        _setup_worker()
    else:
        print(_c("red", "  Please enter 1 or 2."))
        print()
        print(_c("yellow", "  Try again: gpumesh setup"))
        print()


def _setup_coordinator(device: str):
    """Set up this machine as the coordinator."""

    print()
    print(_c("bold", "  Great! This machine will manage the jobs."))
    print()

    # Detect network options
    tailscale_ok = _has_tailscale()
    from .utils import get_lan_ip
    lan_ip = get_lan_ip()

    # Ask network type
    print(_c("bold", "  How will other machines connect to this one?"))
    print()
    if tailscale_ok:
        print(_c("cyan", "    1) Tailscale (works even on different networks)"))
        print(_c("cyan", "    2) Same WiFi / LAN (no extra setup)"))
    else:
        print(_c("cyan", "    1) Same WiFi / LAN (no extra setup)"))
    print()

    net_choice = _ask("Enter 1 or 2:")

    use_tailscale = tailscale_ok and net_choice == "1"

    # Generate token
    token = secrets.token_urlsafe(16)

    # Build the command
    if use_tailscale:
        cmd = f"gpumesh serve --port 8000 --token {token} --tailscale"
    else:
        cmd = f"gpumesh serve --port 8000 --token {token}"

    # Show the command they need to run
    print()
    print(_c("bold", "  Run this command:"))
    print()
    print(_c("green", f"    {cmd}"))
    print()

    # Show what friends need to run
    print(_c("bold", "  Give this command to your friend to join:"))
    print()
    if use_tailscale:
        print(_c("green", f"    gpumesh quickjoin --token {token} --tailscale"))
        print()
        print(_c("yellow", "  (They need Tailscale installed too)"))
    else:
        tailscale_ip = _get_tailscale_ip()
        if tailscale_ip:
            friend_cmd = f"gpumesh quickjoin http://{tailscale_ip}:8000 --token {token}"
        else:
            friend_cmd = f"gpumesh quickjoin http://{lan_ip}:8000 --token {token}"
        print(_c("green", f"    {friend_cmd}"))
    print()

    # Save connection locally
    from . import connection_manager
    if use_tailscale:
        tailscale_ip = _get_tailscale_ip()
        if tailscale_ip:
            connection_manager.save_connection(f"http://{tailscale_ip}:8000", token)
    else:
        connection_manager.save_connection(f"http://{lan_ip}:8000", token)

    # Next steps
    print(_c("bold", "  What happens next:"))
    print()
    print(_c("cyan", "  1. Your friend runs the command above on their machine"))
    print(_c("cyan", "  2. They join automatically"))
    print(_c("cyan", "  3. You submit jobs with:"))
    print()
    print(_c("green", "      gpumesh submit your_script.py --payloads params.json --wait"))
    print()
    print(_c("yellow", "  (Your script reads JSON from stdin, prints JSON result)"))
    print()


def _setup_worker():
    """Set up this machine as a worker (join an existing mesh)."""

    print()
    print(_c("bold", "  Let's add this machine to the mesh."))
    print()

    # Ask for coordinator address
    print(_c("bold", "  Ask the person running the coordinator for:"))
    print(_c("cyan", "    - Their IP address or URL"))
    print(_c("cyan", "    - The token (password)"))
    print()

    url = _ask("Coordinator address (e.g. http://192.168.1.10:8000):")
    if not url:
        print(_c("red", "  No address provided. Try again: gpumesh setup"))
        return

    token = _ask("Token:")
    if not token:
        print(_c("red", "  No token provided. Try again: gpumesh setup"))
        return

    # Save connection
    from . import connection_manager
    connection_manager.save_connection(url, token)

    # Join
    print()
    print(_c("bold", "  Joining the mesh..."))
    print()

    # Detect hardware for the join
    from . import capability
    info = capability.full_probe()
    print(_c("green", f"  Your device: {info['device']} ({info['device_name']})"))
    print(_c("green", f"  Your speed:  {info['score']} GFLOP/s"))
    print()

    # Run the worker
    from . import worker
    print(_c("bold", "  Joining now... (press Ctrl+C to stop)"))
    print()
    worker.run_worker(url, token)


def run_post_install_message():
    """Print a friendly message after pip install."""
    if not (hasattr(sys.stdout, "isatty") and sys.stdout.isatty()):
        return

    _print_box([
        "gpumesh installed!",
        "",
        "Run this to get started:",
        "",
        _c("green", "  gpumesh setup"),
    ])


def run_post_upgrade_message(old_version: str, new_version: str):
    """Print a brief upgrade message."""
    if not (hasattr(sys.stdout, "isatty") and sys.stdout.isatty()):
        return

    _print_box([
        f"gpumesh upgraded: {old_version} -> {new_version}",
        "",
        "Run 'gpumesh setup' to reconfigure.",
    ])


if __name__ == "__main__":
    run_setup_wizard()
