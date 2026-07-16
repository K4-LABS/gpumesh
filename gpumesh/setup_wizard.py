"""Interactive setup wizard for gpumesh.

Bluetooth-like setup flow:
1. Are you the coordinator or a worker?
2. Coordinator: shows token + live radar of nearby workers
3. Worker: broadcasts itself, discovers coordinators, picks one, enters token

All old manual paths (Tailscale, LAN IP) are preserved as fallbacks.
"""

from __future__ import annotations

import platform
import secrets
import shutil
import subprocess
import sys
import threading
import time

from .tunnel import _get_tailscale_ip
from .utils import get_lan_ip, show_firewall_hint, try_add_firewall_rule

_HAS_UI_DEPS = False
_console = None

try:
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    import questionary
    _HAS_UI_DEPS = True
except ImportError:
    pass


# ── helpers ────────────────────────────────────────────────────────────────

def _detect_gpu() -> str:
    """Detect GPU. Returns 'cuda', 'mps', or 'cpu'."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            name = result.stdout.strip().split("\n")[0]
            _console.print(f"  GPU found: {name}", style="green")
            return "cuda"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    if platform.system() == "Darwin" and platform.machine() == "arm64":
        try:
            import torch
            if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                _console.print("  GPU found: Apple Silicon", style="green")
                return "mps"
        except ImportError:
            pass

    _console.print("  No GPU found (CPU only \u2014 still works!)", style="yellow")
    return "cpu"


def _has_tailscale() -> bool:
    """Check if Tailscale is installed."""
    tailscale_bin = shutil.which("tailscale")
    if tailscale_bin is None:
        return False
    try:
        result = subprocess.run(
            [tailscale_bin, "status"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _parse_url(ip_or_url: str, default_port: int = 8000) -> str:
    """Parse a user-entered IP or URL into a normalized http:// URL."""
    url = ip_or_url.strip()
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("["):
        bracket_end = url.find("]")
        if bracket_end != -1:
            rest = url[bracket_end + 1:]
            if rest.startswith(":"):
                try:
                    port = int(rest[1:])
                    return f"http://{url[:bracket_end + 1]}:{port}"
                except ValueError:
                    pass
            return f"http://{url}:{default_port}"
    if ":" in url:
        parts = url.rsplit(":", 1)
        try:
            port = int(parts[1])
            return f"http://{parts[0]}:{port}"
        except ValueError:
            pass
    return f"http://{url}:{default_port}"


def _probe_claim_port(ip: str) -> int:
    """Try common ports to find a claim server when beacon has claim_port=0."""
    import socket
    import urllib.error
    import urllib.request

    common_ports = [49152, 49153, 49154, 8080, 9000, 10000, 12345, 50000]
    for port in common_ports:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.5)
            result = sock.connect_ex((ip, port))
            sock.close()
            if result == 0:
                try:
                    probe_url = f"http://{ip}:{port}/api/claim"
                    req = urllib.request.Request(probe_url, method="GET")
                    urllib.request.urlopen(req, timeout=2)
                except urllib.error.HTTPError as exc:
                    if exc.code in (400, 404, 405):
                        return port
                except Exception:
                    pass
        except Exception:
            pass
    return 0


# ── header ─────────────────────────────────────────────────────────────────

def _print_header():
    """Render the ASCII-art welcome header inside a rich Panel."""
    art = (
        "  [bold cyan]  ____             _ __  __  ____  [/]\n"
        "  [bold cyan] / ___| _   _  ___| | |  \\/  |/ ___| [/]\n"
        "  [bold cyan]| |  _ | | | |/ _ \\ | | |\\/| | |  _  [/]\n"
        "  [bold cyan]| |_| || |_| |  __/ | | |  | | |_| | [/]\n"
        "  [bold cyan] \\____| \\__,_|\\___|_|_|_|  |_|\\____| [/]\n"
    )
    subtitle = (
        "[bold]Share GPU power between machines.\n"
        "Like Bluetooth -- devices find each other.[/]"
    )
    panel_text = Text.from_markup(art + "\n" + subtitle)
    _console.print()
    _console.print(Panel(panel_text, border_style="bright_cyan", padding=(0, 1)))
    _console.print()


# ── MAIN WIZARD ────────────────────────────────────────────────────────────


def run_setup_wizard():
    """Main setup wizard -- simple 2-step flow."""
    global _console

    if not _HAS_UI_DEPS:
        print("[ERROR] Setup wizard requires optional dependencies.")
        print("   Install them with: pip install gpumesh[ui]")
        print("   Or configure manually: gpumesh serve / gpumesh join")
        sys.exit(1)

    _console = Console()

    _print_header()

    # --- Detect hardware ---
    _console.print("  Detecting your hardware...", style="bold")
    device = _detect_gpu()
    _console.print()

    # --- Ask the ONE question ---
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold cyan")
    table.add_column()
    table.add_row("1)", "Set up this machine to MANAGE jobs (Coordinator)")
    table.add_row("2)", "Add this machine to someone else's mesh (Worker)")

    _console.print("  What do you want to do?", style="bold")
    _console.print()

    choice = questionary.select(
        "",
        choices=[
            "1) Set up this machine to MANAGE jobs (Coordinator)",
            "2) Add this machine to someone else's mesh (Worker)",
        ],
        style=questionary.Style([
            ("pointer", "bold cyan"),
            ("selected", "bold"),
        ]),
    ).ask()

    if choice is None:
        return

    if choice.startswith("1)"):
        _setup_coordinator_radar(device)
    elif choice.startswith("2)"):
        _setup_worker_radar(device)
    else:
        _console.print("  Please enter 1 or 2.", style="red")
        _console.print()
        _console.print("  Try again: gpumesh setup", style="yellow")
        _console.print()


# ============================================================================
#  COORDINATOR SETUP (with radar)
# ============================================================================


def _setup_coordinator_radar(device: str):
    """Set up this machine as the coordinator with live radar."""

    _console.print()
    _console.print("  Great! This machine will manage the jobs.", style="bold green")
    _console.print()

    # Try to add firewall rules automatically
    firewall_ok = try_add_firewall_rule(8000)
    if not firewall_ok:
        show_firewall_hint(8000)

    # Detect network options
    tailscale_ok = _has_tailscale()
    tailscale_ip = _get_tailscale_ip() if tailscale_ok else None
    lan_ip = get_lan_ip()

    # Ask network type
    _console.print("  How will other machines connect to this one?", style="bold")
    _console.print()
    if tailscale_ok:
        net_choices = [
            "1) Same WiFi / LAN (auto-discover nearby devices)",
            "2) Tailscale (machines on different networks)",
            "3) Manual setup (enter IP/port yourself)",
        ]
    else:
        net_choices = [
            "1) Same WiFi / LAN (auto-discover nearby devices)",
            "2) Manual setup (enter IP/port yourself)",
        ]
        _console.print(
            "  Tailscale not found. Install from: https://tailscale.com/download",
            style="yellow",
        )
        _console.print(
            "  Then run 'gpumesh setup' again for remote connections.",
            style="yellow",
        )
    _console.print()

    net_choice = questionary.select(
        "",
        choices=net_choices,
        style=questionary.Style([
            ("pointer", "bold cyan"),
            ("selected", "bold"),
        ]),
    ).ask()

    if net_choice is None:
        return

    # Generate token
    token = secrets.token_urlsafe(16)

    # --- TAILSCALE MODE ---
    if tailscale_ok and net_choice.startswith("2)") and tailscale_ip:
        coordinator_url = f"http://{tailscale_ip}:8000"
        _show_coordinator_instructions(coordinator_url, token, "tailscale")
        return

    # --- MANUAL MODE ---
    manual_choice = "3)" if tailscale_ok else "2)"
    if net_choice.startswith(manual_choice):
        _setup_coordinator_manual(device, tailscale_ok, tailscale_ip, lan_ip, token)
        return

    # --- AUTO-DISCOVERY MODE (default) ---
    coordinator_url = f"http://{lan_ip}:8000"

    # Start the coordinator server in background
    from . import server, connection_manager

    try:
        httpd = server.serve("0.0.0.0", 8000, "gpumesh.db", token)
    except OSError as exc:
        _console.print(f"  [ERROR] Failed to start server: {exc}", style="red")
        _console.print("  Port 8000 may already be in use.", style="yellow")
        try:
            httpd = server.serve("0.0.0.0", 8001, "gpumesh.db", token)
            coordinator_url = f"http://{lan_ip}:8001"
            _console.print("  Server started on port 8001 instead.", style="green")
        except OSError:
            _console.print("  Ports 8000 and 8001 are both in use.", style="red")
            _console.print("  Try: gpumesh serve --port 8002 --token <token>", style="yellow")
            _console.print()
            return

    # Start serve_forever in a daemon thread so the HTTP API actually works
    serve_thread = threading.Thread(
        target=httpd.serve_forever, daemon=True, name="gpumesh-httpd",
    )
    serve_thread.start()

    connection_manager.save_connection(coordinator_url, token)

    # Start radar listener for workers
    from .discovery import Listener
    from .radar import print_radar_peers

    listener = Listener()
    try:
        listener.start()
    except OSError as exc:
        _console.print(f"  [ERROR] Failed to start discovery listener: {exc}", style="red")
        _console.print(
            "  Workers won't be auto-discovered, but manual join still works.",
            style="yellow",
        )
        _console.print()

    # Scan for workers using rich.live.Live for smooth updates
    prev_lines = 0
    peers = []
    scan_count = 0
    max_scans = 15  # 15 x 2s = 30s max scan time

    _console.print("  Scanning for workers...", style="bold cyan")
    _console.print()

    try:
        with Live(console=_console, refresh_per_second=1, transient=True) as live:
            while scan_count < max_scans:
                peers = listener.peers()
                # Build radar display for Live
                if not peers:
                    radar_text = Text("  No devices found yet...", style="dim")
                else:
                    radar_lines = []
                    for peer in sorted(peers, key=lambda p: (p.hostname, p.ip)):
                        line = Text()
                        line.append("  + ", style="green")
                        line.append(peer.hostname, style="bold")
                        line.append("  ", style="default")
                        line.append(peer.display_name, style="cyan")
                        line.append(f"  {peer.score:.1f} GFLOP/s  ", style="default")
                        line.append(peer.ip, style="dim")
                        radar_lines.append(line)
                    radar_text = Text("\n").join(radar_lines)

                header = Text()
                header.append("  RADAR -- Nearby Workers\n", style="bold cyan")
                header.append("  (scanning every 2s, press Ctrl+C to stop)\n\n", style="dim")
                header.append_text(radar_text)
                live.update(header)

                if peers:
                    break  # found at least one worker -- stop scanning
                scan_count += 1
                time.sleep(2)
    except KeyboardInterrupt:
        pass

    if not peers:
        _console.print("  No workers found after 30 seconds.", style="yellow")
        _console.print(
            "  Make sure workers are running 'gpumesh worker --token <token>'.",
            style="yellow",
        )
        _console.print()
        return

    # Show selection and claim
    _claim_worker(peers, coordinator_url, token)

    # Keep main thread alive so daemon HTTP server stays running.
    _console.print()
    _console.print(f"  Coordinator running at {coordinator_url}", style="green")
    _console.print("  Press Ctrl+C to stop the coordinator", style="yellow")
    _console.print()
    _shutdown_called = False
    try:
        serve_thread.join()
    except KeyboardInterrupt:
        _console.print("\n  Shutting down coordinator...", style="yellow")
    finally:
        if not _shutdown_called:
            _shutdown_called = True
            try:
                listener.stop()
            except Exception:
                pass
            httpd.shutdown()


# ── claim flow ─────────────────────────────────────────────────────────────

def _claim_worker(peers: list, coordinator_url: str, coordinator_token: str):
    """Let the coordinator select a worker and send a claim request."""
    from .radar import select_worker_for_claim

    _console.print()
    peer, token = select_worker_for_claim(peers)
    if peer is None:
        _console.print("  Claim cancelled.", style="yellow")
        return

    # Send POST /api/claim to the worker
    import json
    import urllib.error
    import urllib.request

    # Determine the claim port -- probe fallback if beacon says 0
    claim_port = peer.claim_port
    if claim_port == 0:
        _console.print(
            f"  {peer.hostname} does not advertise a claim port in its beacon.",
            style="yellow",
        )
        _console.print("  Probing common claim ports...", style="yellow")
        claim_port = _probe_claim_port(peer.ip)
        if claim_port == 0:
            _console.print("  Could not find a claim server on this worker.", style="red")
            _console.print(
                "  Make sure the worker was started with 'gpumesh setup' option 2.",
                style="yellow",
            )
            _console.print("  Or run: gpumesh worker --token <token>", style="yellow")
            return
        _console.print(f"  Found claim server on port {claim_port}", style="green")

    claim_url = f"http://{peer.ip}:{claim_port}/api/claim"
    payload = json.dumps({
        "token": token,
        "coordinator_url": coordinator_url,
        "coordinator_token": coordinator_token,
    }).encode()

    _console.print()
    _console.print(f"  Claiming {peer.hostname} ({peer.ip})...", style="bold")
    try:
        req = urllib.request.Request(
            claim_url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                _console.print(
                    f"  {peer.hostname} claimed successfully! It will join the mesh shortly.",
                    style="green",
                )
            else:
                _console.print(
                    f"  Claim rejected: {result.get('error', 'unknown error')}",
                    style="red",
                )
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
            err = body.get("error", str(exc))
        except Exception:
            err = str(exc)
        _console.print(f"  [ERROR] Claim failed: {err}", style="red")
    except (urllib.error.URLError, OSError) as exc:
        _console.print(f"  [ERROR] Could not reach worker at {claim_url}: {exc}", style="red")
    _console.print()


# ── manual coordinator setup ───────────────────────────────────────────────

def _setup_coordinator_manual(
    device: str,
    tailscale_ok: bool,
    tailscale_ip: str | None,
    lan_ip: str,
    token: str | None = None,
):
    """Manual coordinator setup (old-style IP entry)."""
    _console.print()
    _console.print("  Manual setup mode.", style="bold")
    _console.print()
    _console.print("  NOTE: The server is NOT running yet.", style="yellow")
    _console.print(
        "  Run the command below in a SEPARATE terminal to start it:",
        style="yellow",
    )
    _console.print()

    if token is None:
        token = secrets.token_urlsafe(16)

    if tailscale_ip:
        coordinator_url = f"http://{tailscale_ip}:8000"
    else:
        coordinator_url = f"http://{lan_ip}:8000"

    _show_coordinator_instructions(coordinator_url, token, "manual")

    from . import connection_manager
    connection_manager.save_connection(coordinator_url, token)


def _show_coordinator_instructions(url: str, token: str, mode: str):
    """Show coordinator instructions (shared by all modes)."""
    _console.print()
    panel_content = Text()
    panel_content.append("  YOUR CONNECTION DETAILS\n\n", style="bold")
    panel_content.append(f"  URL:   ", style="default")
    panel_content.append(f"{url}\n", style="cyan")
    panel_content.append(f"  Token: ", style="default")
    panel_content.append(f"{token}\n", style="cyan")
    panel_content.append("\n  Save this! Workers need it to join.", style="dim")

    _console.print(
        Panel(panel_content, border_style="bright_cyan", padding=(0, 1)),
    )
    _console.print(
        "  SECURITY: Treat this token like a password. Do not share it publicly.",
        style="yellow",
    )
    _console.print()

    if mode == "tailscale":
        _console.print("  STEP-BY-STEP INSTRUCTIONS:", style="bold")
        _console.print()
        _console.print("  Step 1: Start the coordinator on this machine:", style="cyan")
        _console.print(
            f"    gpumesh serve --port 8000 --token {token} --tailscale",
            style="green",
        )
        _console.print()
        _console.print(
            "  Step 2: Tell your friend to install Tailscale:", style="cyan",
        )
        _console.print("    https://tailscale.com/download", style="green")
        _console.print(
            "    (They must log into the SAME Tailscale account)", style="yellow",
        )
        _console.print()
        _console.print("  Step 3: Tell your friend to run:", style="cyan")
        _console.print(
            f"    gpumesh quickjoin --token {token} --tailscale", style="green",
        )
        _console.print()
        _console.print(
            "  Step 4: Or tell them to run 'gpumesh setup' and enter:",
            style="cyan",
        )
        _console.print(f"    URL:   {url}", style="cyan")
        _console.print(f"    Token: {token}", style="cyan")
    else:
        _console.print("  STEP-BY-STEP INSTRUCTIONS:", style="bold")
        _console.print()
        _console.print("  Step 1: Start the coordinator on this machine:", style="cyan")
        _console.print(
            f"    gpumesh serve --port 8000 --token {token}", style="green",
        )
        _console.print()
        _console.print(
            "  Step 2: Make sure your friend is on the SAME WiFi/LAN",
            style="cyan",
        )
        _console.print(
            "    (Both machines must be connected to the same network)",
            style="yellow",
        )
        _console.print()
        _console.print("  Step 3: Tell your friend to run:", style="cyan")
        _console.print(
            f"    gpumesh quickjoin {url} --token {token}", style="green",
        )
        _console.print()
        _console.print(
            "  Step 4: Or tell them to run 'gpumesh setup' and enter:",
            style="cyan",
        )
        _console.print(f"    URL:   {url}", style="cyan")
        _console.print(f"    Token: {token}", style="cyan")
    _console.print()


# ============================================================================
#  WORKER SETUP (with radar)
# ============================================================================

def _setup_worker_radar(device: str):
    """Set up this machine as a worker with claim-based discovery."""

    _console.print()
    _console.print("  Let's add this machine to the mesh.", style="bold green")
    _console.print()
    _console.print(
        "  This worker will broadcast its presence on the LAN.", style="cyan",
    )
    _console.print(
        "  A coordinator can then claim it by entering your token.",
        style="cyan",
    )
    _console.print()

    _setup_worker_radar_scan(device)


def _setup_worker_radar_scan(device: str):
    """Worker setup: set a token, start broadcasting, wait for coordinator to claim."""
    from . import capability, worker

    # Detect hardware
    _console.print()
    _console.print("  Detecting your hardware...", style="bold")
    info = capability.full_probe()
    _console.print(
        f"  Device: {info['device']} ({info['device_name']})", style="green",
    )
    _console.print(f"  Score:  {info['score']} GFLOP/s", style="green")
    _console.print()

    # Ask for a token
    _console.print("  Enter a token for this worker", style="bold")
    _console.print(
        "  (Other coordinators will need this to connect)", style="cyan",
    )
    _console.print()

    token = questionary.text(
        "Token:",
        validate=lambda t: True if len(t.strip()) >= 8 else "Token must be at least 8 characters.",
    ).ask()

    if token is None:
        return

    token = token.strip()
    if not token:
        _console.print("  Token cannot be empty. Try again: gpumesh setup", style="red")
        return

    # Confirm broadcast
    _console.print()
    confirm = questionary.confirm("Start broadcasting?", default=True).ask()

    if confirm is None or not confirm:
        _console.print("  Cancelled. Try again: gpumesh setup", style="yellow")
        return

    # Start claim server + UDP beacon
    _console.print()
    _console.print("  Starting broadcast...", style="bold green")
    _console.print()
    try:
        worker.run_worker_broadcast(token)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        _console.print(f"  [ERROR] Failed to start broadcast: {exc}", style="red")
        _console.print("  Check your network settings and try again.", style="yellow")
        _console.print()


def _setup_worker_tailscale(device: str):
    """Worker setup via Tailscale."""
    from . import capability, connection_manager, worker

    _console.print()
    _console.print("  TAILSCALE MODE", style="bold")
    _console.print()
    _console.print("  Ask the coordinator person for:", style="cyan")
    _console.print(
        "    - Their Tailscale URL (looks like: http://100.x.x.x:8000)",
        style="cyan",
    )
    _console.print("    - The Token (a random code)", style="cyan")
    _console.print()

    url = questionary.text("Coordinator Tailscale URL:").ask()
    if url is None or not url.strip():
        _console.print("  No URL provided. Try again: gpumesh setup", style="red")
        return
    url = _parse_url(url)

    # Get token
    _console.print()
    token = questionary.text("Token:").ask()
    if token is None or not token.strip():
        _console.print("  No token provided. Try again: gpumesh setup", style="red")
        return
    token = token.strip()

    # Save and join
    connection_manager.save_connection(url, token)

    _console.print()
    _console.print("  Joining the mesh...", style="bold green")
    _console.print()

    info = capability.full_probe()
    _console.print(
        f"  Device: {info['device']} ({info['device_name']})", style="green",
    )
    _console.print(f"  Score:  {info['score']} GFLOP/s", style="green")
    _console.print()
    _console.print(
        "  Joining now... (press Ctrl+C to stop)", style="bold",
    )
    _console.print()
    try:
        worker.run_worker(url, token)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        _console.print(f"  [ERROR] Failed to connect: {exc}", style="red")
        _console.print(
            "  Check that the coordinator is running and the URL is correct.",
            style="yellow",
        )
        _console.print()


def _setup_worker_manual(device: str):
    """Worker setup with manual IP entry."""
    from . import capability, connection_manager, worker

    _console.print()
    _console.print("  MANUAL MODE", style="bold")
    _console.print()
    _console.print("  Ask the coordinator person for:", style="cyan")
    _console.print(
        "    - Their IP address (looks like: 192.168.1.10)", style="cyan",
    )
    _console.print("    - The Token (a random code)", style="cyan")
    _console.print(
        "  They can find this by running 'gpumesh serve' on their machine",
        style="yellow",
    )
    _console.print()

    ip = questionary.text(
        "Coordinator IP address (e.g. 192.168.1.10):",
    ).ask()
    if ip is None or not ip.strip():
        _console.print("  No IP provided. Try again: gpumesh setup", style="red")
        return
    url = _parse_url(ip)
    _console.print(f"  Connecting to: {url}", style="cyan")

    _console.print()
    token = questionary.text("Token:").ask()
    if token is None or not token.strip():
        _console.print("  No token provided. Try again: gpumesh setup", style="red")
        return
    token = token.strip()

    connection_manager.save_connection(url, token)

    _console.print()
    _console.print("  Joining the mesh...", style="bold green")
    _console.print()

    info = capability.full_probe()
    _console.print(
        f"  Device: {info['device']} ({info['device_name']})", style="green",
    )
    _console.print(f"  Score:  {info['score']} GFLOP/s", style="green")
    _console.print()
    _console.print(
        "  Joining now... (press Ctrl+C to stop)", style="bold",
    )
    _console.print()
    try:
        worker.run_worker(url, token)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        _console.print(f"  [ERROR] Failed to connect: {exc}", style="red")
        _console.print(
            "  Check that the coordinator is running and the IP is correct.",
            style="yellow",
        )
        _console.print()


# ============================================================================
#  POST-INSTALL MESSAGES
# ============================================================================

if __name__ == "__main__":
    run_setup_wizard()
