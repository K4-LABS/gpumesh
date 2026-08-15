"""Interactive setup wizard for gpumesh.

Bluetooth-like setup flow:
1. Are you the coordinator or a worker?
2. Coordinator: shows token + live radar of nearby workers
3. Worker: broadcasts itself, discovers coordinators, picks one, enters token

All old manual paths (Tailscale, LAN IP) are preserved as fallbacks.
"""

from __future__ import annotations

import os
import platform
import secrets
import shutil
import subprocess
import sys
import threading
import time

from .tunnel import _get_tailscale_ip
from .utils import (coordinator_url_candidates, get_lan_ip, show_firewall_hint,
                    show_ip_alternatives, try_add_firewall_rule)

_HAS_UI_DEPS = False
_console = None

try:
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich.table import Table
    from rich.text import Text
    import questionary
    _HAS_UI_DEPS = True
except ImportError:
    pass


# ── helpers ────────────────────────────────────────────────────────────────

def _detect_gpu() -> str:
    """Detect GPU with a live spinner. Returns 'cuda', 'mps', or 'cpu'."""
    found: str | None = None
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=_console,
        transient=True,
    ) as progress:
        task = progress.add_task("  Detecting your GPU...", total=None)
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                found = result.stdout.strip().split("\n")[0]
                progress.update(task, description=f"  GPU found: {found}")
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

        if found is None and platform.system() == "Darwin" and platform.machine() == "arm64":
            try:
                import torch
                if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                    found = "Apple Silicon"
                    progress.update(task, description="  GPU found: Apple Silicon")
            except ImportError:
                pass

    if found is not None:
        _console.print(f"  GPU found: {found}", style="green")
        return "cuda" if found != "Apple Silicon" else "mps"
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


# Shown with every address rejection. One string, so the wizard always tells
# the user the same thing to type no matter which check failed.
_ADDRESS_HINT = ("Type an address like 192.168.1.10:8000, or a full URL "
                 "like http://192.168.1.10:8000.")

# Every re-prompting question needs an explicit way out, or a user who does
# not have the answer to hand is stuck in a loop with no exit but Ctrl+C.
# The first entry is the one shown in prompts.
_QUIT_WORDS = ("q", "quit", "cancel")


def _parse_port(raw: str, original: str) -> int:
    """Parse the port half of a user-entered address, or raise ValueError."""
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"'{raw}' is not a port number (in '{original}'). {_ADDRESS_HINT}"
        ) from None
    if not 1 <= port <= 65535:
        raise ValueError(
            f"Port {port} is out of range 1-65535 (in '{original}'). "
            f"{_ADDRESS_HINT}"
        )
    return port


def _check_host(host: str, original: str):
    """Reject host parts that cannot appear in a URL."""
    if not host:
        raise ValueError(f"'{original}' has no host. {_ADDRESS_HINT}")
    if "/" in host or any(ch.isspace() for ch in host):
        raise ValueError(f"'{original}' is not a valid address. {_ADDRESS_HINT}")


def _parse_url(ip_or_url: str, default_port: int = 8000) -> str:
    """Parse a user-entered IP or URL into a normalized http:// URL.

    Raises ValueError on anything that cannot become a working URL. This
    used to normalize unconditionally, which meant every typo was accepted
    here and only failed much later as an unexplained connection error on
    the far side: a mistyped port became "http://192.168.1.10:80o0:8000",
    an empty answer became "http://:8000", and because the scheme test was
    case-sensitive "HTTP://host:8000" became "http://HTTP://host:8000".
    Reject at the keystroke that caused it, and say what to type instead.
    """
    url = (ip_or_url or "").strip()
    if not url:
        raise ValueError(f"No address entered. {_ADDRESS_HINT}")

    # URL schemes are case-insensitive (RFC 3986), so compare lowered but
    # return the user's host untouched — hostnames may be case-sensitive to
    # look at even though they resolve case-insensitively.
    lowered = url.lower()
    for scheme in ("http://", "https://"):
        if lowered.startswith(scheme):
            rest = url[len(scheme):]
            if not rest or rest.startswith("/"):
                raise ValueError(f"'{url}' has no host. {_ADDRESS_HINT}")
            return scheme + rest
    if "://" in url:
        got = url.split("://", 1)[0]
        raise ValueError(
            f"'{got}://' is not a supported scheme — gpumesh speaks http and "
            f"https. {_ADDRESS_HINT}"
        )

    if url.startswith("["):
        # Bracketed IPv6 literal: the brackets are what separate the address
        # from the port, since the address itself is full of colons.
        bracket_end = url.find("]")
        if bracket_end == -1:
            raise ValueError(
                f"'{url}' is missing the closing ']'. Type an IPv6 address as "
                f"[::1]:8000."
            )
        host = url[:bracket_end + 1]
        if len(host) <= 2:
            raise ValueError(f"'{url}' has no host. {_ADDRESS_HINT}")
        rest = url[bracket_end + 1:]
        if not rest:
            return f"http://{host}:{default_port}"
        if not rest.startswith(":"):
            raise ValueError(
                f"'{url}' has unexpected text after the address. Type an IPv6 "
                f"address as [::1]:8000."
            )
        return f"http://{host}:{_parse_port(rest[1:], url)}"

    if ":" in url:
        host, _, raw_port = url.rpartition(":")
        port = _parse_port(raw_port, url)
    else:
        host, port = url, default_port
    _check_host(host, url)
    return f"http://{host}:{port}"


def _response_header(obj, name: str) -> str:
    """Read a header off either a urlopen response or an HTTPError."""
    for source in (getattr(obj, "headers", None), getattr(obj, "info", None)):
        try:
            headers = source() if callable(source) else source
            value = headers.get(name) if headers is not None else None
        except Exception:
            value = None
        if value:
            return str(value)
    return ""


def _probe_claim_port(ip: str) -> int:
    """Try common ports to find a claim server when beacon has claim_port=0."""
    import socket
    import urllib.error
    import urllib.request

    common_ports = [49152, 49153, 49154, 8080, 9000, 10000, 12345, 50000]
    # First port that answered HTTP without identifying itself as gpumesh.
    # It is a fallback, not an answer: see the return below.
    fallback = 0
    for port in common_ports:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.5)
            result = sock.connect_ex((ip, port))
            sock.close()
            if result == 0:
                # ANY HTTP response proves something is listening and speaking
                # HTTP. The old test — accept only 400/404/405 — rejected the
                # very server it was hunting for: claimer.ClaimHandler defines
                # no do_GET, so BaseHTTPRequestHandler answers a GET with 501,
                # and the coordinator then reported "Could not find a claim
                # server" about a worker that was perfectly claimable.
                answered = False
                server = ""
                try:
                    probe_url = f"http://{ip}:{port}/api/claim"
                    req = urllib.request.Request(probe_url, method="GET")
                    with urllib.request.urlopen(req, timeout=2) as resp:
                        answered = True
                        server = _response_header(resp, "Server")
                except urllib.error.HTTPError as exc:
                    answered = True
                    server = _response_header(exc, "Server")
                except Exception:
                    pass
                if answered:
                    # claimer sets server_version = "gpumesh-claim", so a
                    # worker that names itself is a certainty and we can stop.
                    if "gpumesh-claim" in server.lower():
                        return port
                    if fallback == 0:
                        fallback = port
        except Exception:
            pass
    # Nothing self-identified. Returning the first HTTP responder is still
    # better than giving up: this only runs against a peer that just
    # broadcast a gpumesh beacon, and a wrong guess fails loudly on the
    # claim POST a moment later instead of stalling the whole flow with
    # "could not find a claim server".
    return fallback


def _ask_url(prompt: str) -> str | None:
    """Prompt for a coordinator address until it parses, or the user quits.

    Returns None when the user gives up (Ctrl+C, or the quit word), which is
    the caller's cue to print its own "nothing entered" message and stop.
    """
    while True:
        answer = questionary.text(prompt).ask()
        if answer is None:
            return None
        answer = answer.strip()
        if answer.lower() in _QUIT_WORDS:
            return None
        try:
            return _parse_url(answer)
        except ValueError as exc:
            # Re-prompt rather than return: a typo in an IP address is the
            # single most likely thing to happen here, and making the user
            # restart 'gpumesh setup' over one is why people give up.
            _console.print(f"  {exc}", style="red")
            _console.print(f"  Try again, or type '{_QUIT_WORDS[0]}' to quit.",
                           style="yellow")


def _ask_token(prompt: str, min_length: int = 0) -> str | None:
    """Prompt for a token until it is acceptable, or the user quits.

    ``min_length`` is only set where the wizard is *choosing* a token (the
    worker's own). When it is *receiving* one that a coordinator generated,
    any non-empty answer has to be allowed — we do not get to re-specify
    somebody else's token.
    """
    # questionary's own validator has to let the quit word through, or the
    # advertised way out is the one answer the prompt refuses to accept.
    too_short = (f"Token must be at least {min_length} characters"
                 if min_length else "Token cannot be empty")
    complaint = f"{too_short} (or '{_QUIT_WORDS[0]}' to quit)."

    while True:
        answer = questionary.text(
            prompt,
            validate=lambda t: (
                True
                if (t.strip().lower() in _QUIT_WORDS
                    or len(t.strip()) >= max(min_length, 1))
                else complaint
            ),
        ).ask()
        if answer is None:
            return None
        answer = answer.strip()
        # A real token is a secrets.token_urlsafe() string, so treating a
        # bare "q" as "quit" costs nothing anyone will ever type on purpose.
        if answer.lower() in _QUIT_WORDS:
            return None
        if not answer:
            _console.print(
                f"  Token cannot be empty. Type it again, or "
                f"'{_QUIT_WORDS[0]}' to quit.",
                style="red",
            )
            continue
        if min_length and len(answer) < min_length:
            _console.print(
                f"  Token must be at least {min_length} characters. Type it "
                f"again, or '{_QUIT_WORDS[0]}' to quit.",
                style="red",
            )
            continue
        return answer


# ── visual helpers ─────────────────────────────────────────────────────────

_RAINBOW = ["red", "yellow", "green", "cyan", "blue", "magenta"]


def _rainbow(text: str, style: str = "bold") -> Text:
    """Colorize text with a per-character rainbow (hand-rolled gradient)."""
    out = Text()
    for i, ch in enumerate(text):
        out.append(ch, style=f"{style} {_RAINBOW[i % len(_RAINBOW)]}")
    return out


def _step_badge(current: int, total: int, label: str, done: bool = False) -> Text:
    """Render a '[1/4] label' step badge."""
    badge = Text()
    badge.append(f"  [{current}/{total}] ", style="bold cyan")
    badge.append("[OK] " if done else "> ",
                 style="bold green" if done else "bold yellow")
    badge.append(label, style="bold")
    return badge


# ── header ─────────────────────────────────────────────────────────────────

def _print_header():
    """Render the rainbow ASCII-art welcome header inside a rich Panel."""
    art = (
        "  ____             _ __  __  ____  \n"
        " / ___| _   _  ___| | |  \\/  |/ ___| \n"
        "| |  _ | | | |/ _ \\ | | |\\/| | |  _  \n"
        "| |_| || |_| |  __/ | | |  | | |_| | \n"
        " \\____| \\__,_|\\___|_|_|_|  |_|\\____| \n"
    )
    subtitle = (
        "[bold]Share GPU power between machines.\n"
        "Like Bluetooth -- devices find each other.[/]"
    )
    panel_text = _rainbow(art)
    panel_text.append("\n")
    panel_text.append_text(Text.from_markup(subtitle))
    _console.print()
    _console.print(Panel(
        panel_text,
        title="[bold magenta]GPUMESH[/]",
        subtitle="[bold cyan]setup wizard[/]",
        border_style="bright_magenta",
        padding=(0, 1),
    ))
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
    _console.print(_step_badge(1, 2, "Detecting hardware"))
    device = _detect_gpu()
    _console.print()

    # --- Ask the ONE question ---
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold cyan")
    table.add_column()
    table.add_row("1)", "Set up this machine to MANAGE jobs (Coordinator)")
    table.add_row("2)", "Add this machine to someone else's mesh (Worker)")

    _console.print(_step_badge(2, 2, "Choose your role"))
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

# What the wizard binds to when nothing overrides it. `gpumesh serve` defaults
# to loopback, and the wizard deliberately does not: option 1 is literally
# "Same WiFi / LAN (auto-discover nearby devices)", and every part of that —
# the UDP radar, the claim POST to a worker, the worker dialling back — needs
# a socket other machines can reach. A loopback bind here would not be a safer
# wizard, it would be a wizard that cannot complete its own flow. So the
# exposure is chosen on purpose, resolved through the same helper `serve` uses
# so an override still works, and announced with the same warning.
WIZARD_LAN_BIND_HOST = "0.0.0.0"


def _resolve_wizard_bind_host() -> str:
    """Resolve the wizard's bind address through cli's single resolver.

    Imported lazily: cli imports this module (for `gpumesh setup`), so a
    module-level import would be a cycle.
    """
    from types import SimpleNamespace

    from .cli import _resolve_bind_host

    # argparse pre-fills `serve --host` from GPUMESH_HOST; do the same so the
    # env override reaches the same resolver by the same route. Only when
    # neither is set does the wizard supply its own default, because cli's
    # (loopback) is the one answer this flow cannot use.
    env_host = os.environ.get("GPUMESH_HOST", "").strip()
    return _resolve_bind_host(
        SimpleNamespace(host=env_host or WIZARD_LAN_BIND_HOST)
    )


def _announce_bind_host(bind_host: str, port: int):
    """Print what this bind address means for the user's machine."""
    from .cli import _is_loopback_bind, _print_exposure_warning

    if _is_loopback_bind(bind_host):
        # Reachable only when someone set GPUMESH_HOST. Honour it — but the
        # rest of this flow is about to look broken, so say why now rather
        # than let the radar scan silently find nothing for 30 seconds.
        _console.print(
            f"  Bound to {bind_host} only (GPUMESH_HOST is set), so machines "
            f"on your network CANNOT reach this coordinator.",
            style="yellow",
        )
        _console.print(
            "  Discovery and claiming need a reachable socket: unset "
            "GPUMESH_HOST, or set it to 0.0.0.0, to use this mode.",
            style="yellow",
        )
        _console.print()
        return
    # The same banner `gpumesh serve` prints, from the same function, so
    # there is one wording of this warning to keep honest.
    _print_exposure_warning(bind_host, port)


def _setup_coordinator_radar(device: str):
    """Set up this machine as the coordinator with live radar."""

    _console.print()
    _console.print(_step_badge(1, 3, "Coordinator role", done=True))
    _console.print("  Great! This machine will manage the jobs.", style="bold green")
    _console.print()

    # Detect network options
    tailscale_ok = _has_tailscale()
    tailscale_ip = _get_tailscale_ip() if tailscale_ok else None
    lan_ip = get_lan_ip()

    # Ask network type
    _console.print(_step_badge(2, 3, "Network setup"))
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

    bind_host = _resolve_wizard_bind_host()

    try:
        httpd = server.serve(bind_host, 8000, "gpumesh.db", token)
    except OSError as exc:
        _console.print(f"  [ERROR] Failed to start server: {exc}", style="red")
        _console.print("  Port 8000 may already be in use.", style="yellow")
        try:
            httpd = server.serve(bind_host, 8001, "gpumesh.db", token)
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

    # This machine joins its own pool so its own CPU/GPU is used too.
    try:
        from .worker import spawn_local_worker
        spawn_local_worker(coordinator_url, token, persist_connection=False)
        _console.print("  Self-worker started — this machine's compute joins the pool.", style="dim")
    except Exception:
        pass

    # Try to add firewall rules now that the actual port is known
    actual_port = int(coordinator_url.rsplit(":", 1)[-1]) if ":" in coordinator_url else 8000

    # Say out loud what this bind means before the user starts handing the
    # token to friends. `gpumesh serve` does this; the wizard used to bind
    # wider than `serve` does and print nothing at all.
    _announce_bind_host(bind_host, actual_port)

    firewall_ok = try_add_firewall_rule(actual_port)
    if not firewall_ok:
        show_firewall_hint(actual_port)

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

    # The coordinator is live — show the URL + token + join command so the
    # user can share them with friends immediately (the token is otherwise
    # never displayed in auto-discovery mode).
    _show_running_coordinator_panel(coordinator_url, token)

    # A claimed worker is handed this exact URL and cannot correct it, so if
    # auto-detection picked a VPN or hypervisor address every claim will end
    # in a connection timeout. Surface the alternatives before that happens.
    show_ip_alternatives(lan_ip, actual_port)

    # Scan for workers using rich.live.Live for smooth updates
    prev_lines = 0
    peers = []
    scan_count = 0
    max_scans = 15  # 15 x 2s = 30s max scan time

    _console.print(_step_badge(3, 3, "Scanning for workers"))
    _console.print("  Scanning for workers...", style="bold cyan")
    _console.print()

    scan_interrupted = False
    scan_frames = ["|", "/", "-", "\\"]
    try:
        with Live(console=_console, refresh_per_second=6, transient=True) as live:
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
                spin = scan_frames[scan_count % len(scan_frames)]
                header.append(f"  {spin} RADAR -- Nearby Workers\n", style="bold cyan")
                header.append("  (scanning every 2s, press Ctrl+C to stop)\n\n", style="dim")
                filled = int(20 * scan_count / max_scans)
                bar = "#" * filled + "." * (20 - filled)
                header.append(f"  [{bar}] {scan_count}/{max_scans}\n\n", style="dim")
                header.append_text(radar_text)
                live.update(header)

                if peers:
                    break  # found at least one worker -- stop scanning
                scan_count += 1
                time.sleep(2)
    except KeyboardInterrupt:
        scan_interrupted = True

    if not peers:
        if scan_interrupted:
            # The user pressed Ctrl+C to abort the scan — honor it and stop
            # the coordinator too, instead of blocking again and forcing a
            # second Ctrl+C.
            _console.print("  Scan interrupted — stopping the coordinator.", style="yellow")
            try:
                listener.stop()
            except Exception:
                pass
            httpd.shutdown()
            return
        _console.print("  No workers found after 30 seconds.", style="yellow")
        _console.print(
            "  The coordinator is STILL RUNNING — friends can join with the",
            style="yellow",
        )
        _console.print(
            "  command shown above, or with: gpumesh quickjoin <URL> --token <TOKEN>",
            style="yellow",
        )
        _console.print()
        # Keep the coordinator alive so friends can still join manually —
        # never let the wizard exit and silently kill the server here.
        try:
            serve_thread.join()
        except KeyboardInterrupt:
            _console.print("\n  Shutting down coordinator...", style="yellow")
        finally:
            try:
                listener.stop()
            except Exception:
                pass
            httpd.shutdown()
        return

    # Show selection and claim
    _claim_worker(peers, coordinator_url, token)

    # Keep main thread alive so daemon HTTP server stays running.
    _console.print()
    _console.print(Panel(
        Text.from_markup(
            "[bold green]  YOU'RE LIVE!  Your mesh is online.  [/]\n"
            "[dim]  Workers can join anytime with the command above.[/]"
        ),
        border_style="green",
        padding=(0, 1),
    ))
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

    # Offer every address this worker might reach us on, best first, and let
    # it choose. We cannot determine that from here: which of our addresses
    # is routable is a property of the network between the two machines, so
    # sending a single guess means any wrong guess becomes the worker's
    # silent 20-second timeout.
    port = int(coordinator_url.rsplit(":", 1)[-1])
    candidates = coordinator_url_candidates(peer.ip, port)
    if coordinator_url not in candidates:
        candidates.append(coordinator_url)

    payload = json.dumps({
        "token": token,
        # Kept for workers predating the candidate list.
        "coordinator_url": candidates[0],
        "coordinator_urls": candidates,
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
        # Must outlast the worker probing every candidate in turn.
        with urllib.request.urlopen(req, timeout=25) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                used = result.get("coordinator_url", candidates[0])
                _console.print(
                    f"  {peer.hostname} reached us at {used} and is joining the mesh.",
                    style="green",
                )
                _console.print(Panel(
                    Text.from_markup(
                        "[bold green]  WORKER CLAIMED!  [/]\n"
                        f"[dim]  {peer.hostname} is joining the mesh now.[/]"
                    ),
                    border_style="green",
                    padding=(0, 1),
                ))
            else:
                _console.print(
                    f"  Claim rejected: {result.get('error', 'unknown error')}",
                    style="red",
                )
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
            err = body.get("error", str(exc))
            tried = body.get("tried") or []
        except Exception:
            err = str(exc)
            tried = []
        _console.print(f"  [ERROR] Claim failed: {err}", style="red")
        if tried:
            _console.print(
                f"  {peer.hostname} could not reach this machine on any of:",
                style="yellow",
            )
            for candidate in tried:
                _console.print(f"    {candidate}", style="dim")
            _console.print(
                "  The two machines are on different networks, or a firewall is",
                style="yellow",
            )
            _console.print(
                "  dropping the port. Pin a reachable address with --host-ip, or",
                style="yellow",
            )
            _console.print("  use Tailscale on both machines.", style="yellow")
    except (urllib.error.URLError, OSError) as exc:
        _console.print(f"  [ERROR] Could not reach worker at {claim_url}: {exc}", style="red")
    _console.print()


# ── running coordinator panel ──────────────────────────────────────────────

def _show_running_coordinator_panel(url: str, token: str):
    """Show that the coordinator is LIVE and what friends need to join.

    Unlike the manual-mode instructions (which tell the user to start their
    own coordinator), this panel is shown when the server is ALREADY running,
    so it tells friends to JOIN it instead.
    """
    panel_content = Text()
    panel_content.append("  YOUR COORDINATOR IS RUNNING\n\n", style="bold green")
    panel_content.append(f"  URL:   ", style="default")
    panel_content.append(f"{url}\n", style="cyan")
    panel_content.append(f"  Token: ", style="default")
    panel_content.append(f"{token}\n", style="cyan")
    panel_content.append("\n  Friends join with:\n", style="default")
    panel_content.append(f"    gpumesh quickjoin {url} --token {token}\n", style="green")
    panel_content.append("\n  (or: gpumesh join <URL> --token <TOKEN>)\n", style="dim")

    _console.print()
    _console.print(Panel(
        panel_content,
        title="[bold green]ONLINE[/]",
        border_style="bright_green",
        padding=(0, 1),
    ))
    _console.print(
        "  SECURITY: Treat this token like a password. Do not share it publicly.",
        style="yellow",
    )
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


def _serve_target(url: str, default_port: int = 8000) -> tuple[str, int]:
    """Split an advertised coordinator URL into (host, port) for `serve`.

    The printed `gpumesh serve` command has to name a bind address now that
    the default is loopback, and the only address we know is reachable is
    the one this panel just advertised.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        port = parts.port or default_port
    except ValueError:
        port = default_port
    return host, port


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

    # `gpumesh serve` binds 127.0.0.1 unless told otherwise, so a command
    # without --host starts a coordinator that no worker can ever reach —
    # while the panel above advertises a LAN/tailnet URL and step 3 tells the
    # friend to dial exactly that. Name the bind address explicitly, and say
    # what opening it means in the same breath as the command that opens it.
    serve_host, serve_port = _serve_target(url)

    if mode == "tailscale":
        _console.print("  STEP-BY-STEP INSTRUCTIONS:", style="bold")
        _console.print()
        _console.print("  Step 1: Start the coordinator on this machine:", style="cyan")
        _console.print(
            f"    gpumesh serve --host {serve_host} --port {serve_port} "
            f"--token {token} --tailscale",
            style="green",
        )
        _console.print(
            f"    EXPOSURE: --host opens port {serve_port} to your tailnet. "
            f"Anyone who can",
            style="yellow",
        )
        _console.print(
            "    reach it AND has the token runs code on this machine as you.",
            style="yellow",
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
            f"    gpumesh serve --host 0.0.0.0 --port {serve_port} "
            f"--token {token}",
            style="green",
        )
        _console.print(
            f"    EXPOSURE: --host 0.0.0.0 opens port {serve_port} to your "
            f"whole LAN. Anyone",
            style="yellow",
        )
        _console.print(
            "    who can reach it AND has the token runs code on this machine "
            "as you.",
            style="yellow",
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
    """Set up this machine as a worker: pick a network, then join.

    This used to jump straight into the LAN broadcast, which left the wizard
    with no way to join a coordinator that is not on the same LAN — no URL
    and token entry at all — even though the coordinator side offers
    Tailscale and its own printed instructions tell the friend to run
    'gpumesh setup'. The two halves of the documented flow never met. The
    choices below deliberately mirror the coordinator's, so the person
    reading out the instructions and the person following them see the same
    three options in the same order.
    """

    _console.print()
    _console.print(_step_badge(1, 2, "Worker role", done=True))
    _console.print("  Let's add this machine to the mesh.", style="bold green")
    _console.print()

    tailscale_ok = _has_tailscale()

    _console.print(_step_badge(2, 2, "Network setup"))
    _console.print("  How will this machine reach the coordinator?", style="bold")
    _console.print()
    if tailscale_ok:
        net_choices = [
            "1) Same WiFi / LAN (let a coordinator discover and claim this machine)",
            "2) Tailscale (the coordinator is on a different network)",
            "3) Manual setup (enter the coordinator's URL and token yourself)",
        ]
    else:
        net_choices = [
            "1) Same WiFi / LAN (let a coordinator discover and claim this machine)",
            "2) Manual setup (enter the coordinator's URL and token yourself)",
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

    if tailscale_ok and net_choice.startswith("2)"):
        _setup_worker_tailscale(device)
        return

    manual_choice = "3)" if tailscale_ok else "2)"
    if net_choice.startswith(manual_choice):
        _setup_worker_manual(device)
        return

    # --- LAN / claim mode (default) ---
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
    _console.print(_step_badge(1, 3, "Detecting hardware"))
    info = capability.full_probe()
    _console.print(
        f"  Device: {info['device']} ({info['device_name']})", style="green",
    )
    _console.print(f"  Score:  {info['score']} GFLOP/s", style="green")
    _console.print()

    # Ask for a token
    _console.print(_step_badge(2, 3, "Set a token"))
    _console.print("  Enter a token for this worker", style="bold")
    _console.print(
        "  (Other coordinators will need this to connect)", style="cyan",
    )
    _console.print(
        f"  At least 8 characters, or '{_QUIT_WORDS[0]}' to quit.", style="cyan",
    )
    _console.print()

    # Re-prompt on a bad token instead of returning. Returning meant one
    # mistyped character cost the user the entire wizard — hardware detection
    # included — and the only instruction offered was to start over.
    token = _ask_token("Token:", min_length=8)
    if token is None:
        _console.print("  Cancelled. Try again: gpumesh setup", style="yellow")
        return

    # Confirm broadcast
    _console.print()
    confirm = questionary.confirm("Start broadcasting?", default=True).ask()

    if confirm is None or not confirm:
        _console.print("  Cancelled. Try again: gpumesh setup", style="yellow")
        return

    # Start claim server + UDP beacon
    _console.print(_step_badge(3, 3, "Starting broadcast"))
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

    url = _ask_url("Coordinator Tailscale URL:")
    if url is None:
        _console.print("  No URL provided. Try again: gpumesh setup", style="red")
        return

    # Get token
    _console.print()
    token = _ask_token("Token:")
    if token is None:
        _console.print("  No token provided. Try again: gpumesh setup", style="red")
        return

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
        "  They get these by running 'gpumesh serve --host 0.0.0.0' — a plain",
        style="yellow",
    )
    _console.print(
        "  'gpumesh serve' only listens on their own machine, so it cannot be "
        "joined.",
        style="yellow",
    )
    _console.print()

    url = _ask_url("Coordinator IP address (e.g. 192.168.1.10):")
    if url is None:
        _console.print("  No IP provided. Try again: gpumesh setup", style="red")
        return
    _console.print(f"  Connecting to: {url}", style="cyan")

    _console.print()
    token = _ask_token("Token:")
    if token is None:
        _console.print("  No token provided. Try again: gpumesh setup", style="red")
        return

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
