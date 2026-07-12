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
    print(_c("cyan", "    1) Set up this machine to MANAGE jobs (Coordinator)"))
    print(_c("cyan", "    2) Add this machine to someone else's mesh (Worker)"))
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
    tailscale_ip = _get_tailscale_ip() if tailscale_ok else None
    from .utils import get_lan_ip
    lan_ip = get_lan_ip()

    # Ask network type
    print(_c("bold", "  How will other machines connect to this one?"))
    print()
    if tailscale_ok:
        print(_c("cyan", "    1) Same WiFi / LAN (both on same network)"))
        print(_c("cyan", "    2) Tailscale (machines on different networks)"))
    else:
        print(_c("cyan", "    1) Same WiFi / LAN (both on same network)"))
        print()
        print(_c("yellow", "  Tailscale not found. Install from: https://tailscale.com/download"))
        print(_c("yellow", "  Then run 'gpumesh setup' again to use remote connections."))
    print()

    net_choice = _ask("Enter 1 or 2:")

    use_tailscale = tailscale_ok and net_choice == "2"

    # Generate token
    token = secrets.token_urlsafe(16)

    # Determine URL
    if use_tailscale and tailscale_ip:
        coordinator_url = f"http://{tailscale_ip}:8000"
        network_type = "tailscale"
    elif tailscale_ip and not use_tailscale:
        # User chose same network but has tailscale - use LAN IP
        coordinator_url = f"http://{lan_ip}:8000"
        network_type = "lan"
    else:
        coordinator_url = f"http://{lan_ip}:8000"
        network_type = "lan"

    # Show connection details PROMINENTLY
    print()
    _print_box([
        "  YOUR CONNECTION DETAILS",
        "",
        f"  URL:   {coordinator_url}",
        f"  Token: {token}",
        "",
        "  Save this! Workers need it to join.",
    ])

    # Different instructions based on network type
    if network_type == "tailscale":
        # --- TAILSCALE FLOW ---
        print(_c("bold", "  STEP-BY-STEP INSTRUCTIONS:"))
        print()
        print(_c("cyan", "  Step 1: Start the coordinator on this machine:"))
        print(_c("green", f"    gpumesh serve --port 8000 --token {token} --tailscale"))
        print()
        print(_c("cyan", "  Step 2: Tell your friend to install Tailscale:"))
        print(_c("green", "    https://tailscale.com/download"))
        print(_c("yellow", "    (They must log into the SAME Tailscale account)"))
        print()
        print(_c("cyan", "  Step 3: Tell your friend to run this command:"))
        print(_c("green", f"    gpumesh quickjoin --token {token} --tailscale"))
        print()
        print(_c("cyan", "  Step 4: Or tell them to run 'gpumesh setup' and enter:"))
        print(_c("cyan", f"    URL:   {coordinator_url}"))
        print(_c("cyan", f"    Token: {token}"))
    else:
        # --- SAME NETWORK FLOW ---
        print(_c("bold", "  STEP-BY-STEP INSTRUCTIONS:"))
        print()
        print(_c("cyan", "  Step 1: Start the coordinator on this machine:"))
        print(_c("green", f"    gpumesh serve --port 8000 --token {token}"))
        print()
        print(_c("cyan", "  Step 2: Make sure your friend is on the SAME WiFi/LAN"))
        print(_c("yellow", "    (Both machines must be connected to the same network)"))
        print()
        print(_c("cyan", "  Step 3: Tell your friend to run this command:"))
        print(_c("green", f"    gpumesh quickjoin {coordinator_url} --token {token}"))
        print()
        print(_c("cyan", "  Step 4: Or tell them to run 'gpumesh setup' and enter:"))
        print(_c("cyan", f"    URL:   {coordinator_url}"))
        print(_c("cyan", f"    Token: {token}"))
    print()

    # Save connection locally
    from . import connection_manager
    connection_manager.save_connection(coordinator_url, token)

    # Next steps
    print(_c("bold", "  After workers join, submit jobs with:"))
    print()
    print(_c("green", "    gpumesh submit your_script.py --payloads params.json --wait"))
    print()
    print(_c("yellow", "  (Your script reads JSON from stdin, prints JSON result)"))
    print()


def _setup_worker():
    """Set up this machine as a worker (join an existing mesh)."""

    print()
    print(_c("bold", "  Let's add this machine to the mesh."))
    print()

    # Detect network options
    tailscale_ok = _has_tailscale()
    tailscale_ip = _get_tailscale_ip() if tailscale_ok else None

    # Ask how they're connecting
    print(_c("bold", "  How are you connecting to the coordinator?"))
    print()
    if tailscale_ok:
        print(_c("cyan", "    1) Same WiFi / LAN (both on same network)"))
        print(_c("cyan", "    2) Tailscale (coordinator is on a different network)"))
    else:
        print(_c("cyan", "    1) Same WiFi / LAN (both on same network)"))
        print()
        print(_c("yellow", "  Tailscale not found. If you need remote access,"))
        print(_c("yellow", "  install Tailscale: https://tailscale.com/download"))
    print()

    net_choice = _ask("Enter 1 or 2:")

    use_tailscale = tailscale_ok and net_choice == "2"

    # Get coordinator details
    print()
    if use_tailscale:
        # --- TAILSCALE WORKER FLOW ---
        print(_c("bold", "  TAILSCALE MODE"))
        print()
        print(_c("cyan", "  Ask the coordinator person for their Token."))
        print(_c("cyan", "  (They can find it by running 'gpumesh serve' on their machine)"))
        print()

        # Option to auto-detect
        print(_c("cyan", "  How do you want to connect?"))
        print(_c("cyan", "    1) Auto-detect coordinator (recommended)"))
        print(_c("cyan", "    2) Enter URL manually"))
        print()

        connect_choice = _ask("Enter 1 or 2:")

        if connect_choice == "1":
            if not tailscale_ip:
                print(_c("red", "  Could not detect Tailscale IP."))
                print(_c("yellow", "  Make sure Tailscale is running and you're logged in."))
                print(_c("yellow", "  Try: tailscale status"))
                print()
                url = _ask("Enter coordinator Tailscale URL manually:")
                if not url:
                    return
            else:
                # Auto-detect - try to connect
                print()
                print(_c("bold", "  Searching for coordinator on Tailscale..."))
                url = f"http://{tailscale_ip}:8000"
                print(_c("cyan", f"  Found: {url}"))
        else:
            # Manual entry
            print()
            print(_c("cyan", "  Enter the coordinator's Tailscale URL:"))
            print(_c("cyan", "  (Looks like: http://100.x.x.x:8000)"))
            url = _ask("URL:")
            if not url:
                print(_c("red", "  No URL provided. Try again: gpumesh setup"))
                return
    else:
        # --- SAME NETWORK WORKER FLOW ---
        print(_c("bold", "  SAME NETWORK MODE"))
        print()
        print(_c("cyan", "  Ask the coordinator person for:"))
        print(_c("cyan", "    - Their IP address (looks like: 192.168.1.10)"))
        print(_c("cyan", "    - The Token (a random code)"))
        print(_c("yellow", "  They can find this by running 'gpumesh serve' on their machine"))
        print()

        ip = _ask("Coordinator IP address (e.g. 192.168.1.10):")
        if not ip:
            print(_c("red", "  No IP provided. Try again: gpumesh setup"))
            return
        url = f"http://{ip}:8000"
        print(_c("cyan", f"  Connecting to: {url}"))

    # Get token (same for both modes)
    print()
    token = _ask("Token:")
    if not token:
        print(_c("red", "  No token provided. Try again: gpumesh setup"))
        return
    token = token.strip()

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
