"""gpumesh command line interface.

  gpumesh serve      [--port 8000] [--token SECRET] [--public] [--tailscale]
  gpumesh join       URL [--token SECRET]
  gpumesh quickjoin  [URL] --token TOKEN [--tailscale] [--port PORT]
  gpumesh worker     --token TOKEN [--claim-port PORT] [--timeout 240]
  gpumesh submit     SCRIPT --payloads FILE [--wait] [--url URL] [--token SECRET]
  gpumesh status     JOB_ID [--url URL] [--token SECRET]
  gpumesh cancel     JOB_ID [--url URL] [--token SECRET]
  gpumesh kill       [--force] [--url URL] [--token SECRET]
  gpumesh workers    [--url URL] [--token SECRET]
  gpumesh devices    [--url URL] [--token SECRET]
  gpumesh radar      [--mode coordinator|worker]
  gpumesh show-connection
  gpumesh disconnect
"""

import argparse
import os
import platform
import secrets
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

from . import __version__, client, connection_manager, server, tunnel, utils, worker
from .ansi import safe_print, bold, cyan, green, yellow, red, dim, device_icon, status_alive, status_dead


def _resolve_conn(args) -> tuple[str, str]:
    """Resolve URL and token from args, env vars, or saved config."""
    url = getattr(args, "url", "") or ""
    token = getattr(args, "token", "") or ""
    return connection_manager.get_connection(
        url or None, token or None
    )


def _add_conn_args(p):
    p.add_argument("--url", default="",
                   help="coordinator URL (or set GPUMESH_URL, or saved from previous command)")
    p.add_argument("--token", default="",
                   help="shared auth token (or set GPUMESH_TOKEN, or saved from previous command)")


def cmd_serve(args):
    token = args.token or secrets.token_urlsafe(12)
    # Try to add Windows Firewall rules automatically.
    if not utils.try_add_firewall_rule(args.port):
        utils.show_firewall_hint(args.port)
        print(yellow("[!] WARNING: Workers on OTHER machines may be blocked by the "
              "firewall."))
        print(dim("   Start 'gpumesh serve' as Administrator, or open "
              f"port {args.port} manually (see hint above)."))
    try:
        httpd = server.serve("0.0.0.0", args.port, args.db, token)
    except OSError as exc:
        print(red(f"[ERROR] {exc}"))
        print(yellow(f"   Port {args.port} is already in use."))
        print(dim(f"   Try: gpumesh serve --port {args.port + 1}"))
        sys.exit(1)
    ip = utils.get_lan_ip()
    # Save connection so other commands can use it without --url/--token.
    # The LAN IP is the address other machines should use.
    connection_manager.save_connection(f"http://{ip}:{args.port}", token)
    print(green(f"[OK] Coordinator listening on 0.0.0.0:{args.port}"))
    print(dim(f"   Token: {token}"))
    print()
    print(f"   Join from THIS machine:   "
          f"{cyan(f'gpumesh join http://127.0.0.1:{args.port} --token {token}')}")
    print(f"   Join from ANOTHER machine: "
          f"{cyan(f'gpumesh join http://{ip}:{args.port} --token {token}')}")
    print(dim("   (127.0.0.1 always works locally; the LAN IP is for "
          "other machines)"))

    # Determine tunnel mode
    if args.tailscale:
        tunnel_mode = "tailscale"
    elif args.public:
        tunnel_mode = "ngrok"
    else:
        tunnel_mode = "none"

    if tunnel_mode != "none":
        tunnel.open_tunnel(args.port, mode=tunnel_mode)

    def _sigterm_handler(signum, frame):
        raise SystemExit(0)
    if platform.system() != "Windows":
        signal.signal(signal.SIGTERM, _sigterm_handler)
    else:
        try:
            signal.signal(signal.SIGBREAK, _sigterm_handler)
        except AttributeError:
            pass

    print(dim("   Ctrl+C to stop"))
    # Start the accept loop in the background. serve_forever() MUST be running
    # before any request can be processed — running the self-check beforehand
    # (while the server only had its socket bound) caused it to always time out.
    import threading as _threading
    _serve_thread = _threading.Thread(target=httpd.serve_forever, daemon=True)
    _serve_thread.start()

    # Self-check: confirm the coordinator is actually reachable on loopback.
    # Any HTTP response (even a 401 for a missing token) proves the server
    # is alive and serving requests.
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{args.port}/api/workers", timeout=5
        ) as _resp:
            _ = _resp.status
        print(green("[OK] Self-check: coordinator reachable on 127.0.0.1"))
    except urllib.error.HTTPError:
        # Server responded with an error status (e.g. 401) — it's up.
        print(green("[OK] Self-check: coordinator reachable on 127.0.0.1"))
    except Exception as exc:
        print(yellow(f"[!] WARNING: coordinator self-check failed on 127.0.0.1: {exc}"))
        print(dim("   The server may not have started correctly."))

    try:
        _serve_thread.join()
    except KeyboardInterrupt:
        pass
    except SystemExit:
        pass
    finally:
        print(f"\n{yellow('[!] Shutting down...')}")
        httpd.gpumesh_stop.set()
        httpd.shutdown()


def _run_worker_with_report(url: str, token: str, task_timeout: float):
    """Run a worker and, if it fails to connect quickly, print a clear
    final message (run_worker returns silently on connection failure)."""
    import threading

    print(dim(f"   Connecting worker to {url} ..."))
    t = threading.Thread(
        target=worker.run_worker,
        args=(url, token, task_timeout),
        daemon=True,
    )
    t.start()
    # Give it a short window to register. If it's still alive afterwards,
    # it connected and is running its heartbeat loop; keep it in foreground.
    t.join(timeout=10)
    if t.is_alive():
        t.join()
    else:
        print(red("[ERROR] Worker could not connect."))
        print(dim("   Verify the coordinator is running and the URL/token are correct."))
        print(dim("   If on another machine, ensure the coordinator was "
              "started as Administrator (Windows Firewall)."))
        # A stale saved config pointing at a dead URL may be the culprit;
        # drop it so the next command forces a fresh --url.
        if connection_manager.clear_if_stale():
            print(dim("   (cleared a stale saved connection)"))


def cmd_join(args):
    _run_worker_with_report(args.url, args.token, args.timeout)


def cmd_submit(args):
    url, token = _resolve_conn(args)
    if not url or not token:
        print(red("[ERROR] No connection found."))
        print(dim("   Run with --url and --token, or connect first with 'gpumesh join'"))
        sys.exit(1)
    # Save for future use
    connection_manager.save_connection(url, token)

    job_id = client.submit_job(url, token, args.script, args.payloads, name=args.name)
    print(green(f"[OK] Submitted job {bold(job_id)}"))
    if args.wait:
        job = client.wait_for_job(url, token, job_id)
        client.print_job(job)
    else:
        print(dim(f"   Check progress: gpumesh status {job_id}"))


def cmd_status(args):
    url, token = _resolve_conn(args)
    if not url or not token:
        print(red("[ERROR] No connection found."))
        print(dim("   Run with --url and --token, or connect first with 'gpumesh join'"))
        sys.exit(1)
    try:
        job = client.get_status(url, token, args.job_id)
    except (urllib.error.URLError, OSError) as exc:
        print(red(f"[ERROR] Could not reach coordinator: {exc}"))
        sys.exit(1)
    client.print_job(job)


def cmd_cancel(args):
    url, token = _resolve_conn(args)
    if not url or not token:
        print(red("[ERROR] No connection found."))
        print(dim("   Run with --url and --token, or connect first with 'gpumesh join'"))
        sys.exit(1)
    try:
        result = client.cancel_job(url, token, args.job_id)
    except (urllib.error.URLError, OSError) as exc:
        print(red(f"[ERROR] Could not communicate with coordinator: {exc}"))
        sys.exit(1)
    if result is None:
        print(yellow(f"[!] Job {args.job_id} not found"))
    else:
        print(green(f"[OK] Cancelled job {args.job_id}"))
        print(dim(f"   Pending tasks cancelled: {result['pending']}"))
        print(dim(f"   Running tasks cancelled: {result['running']}"))


def cmd_workers(args):
    from .worker import MeshClient

    url, token = _resolve_conn(args)
    if not url or not token:
        safe_print(red("[ERROR] No connection found."))
        safe_print(dim("   Run with --url and --token, or connect first with 'gpumesh join'"))
        sys.exit(1)

    try:
        resp = MeshClient(url, token).call("GET", "/api/workers")
    except (urllib.error.URLError, OSError) as exc:
        print(red(f"[ERROR] Could not reach coordinator: {exc}"))
        sys.exit(1)

    if not resp["workers"]:
        safe_print(dim("  (no workers connected)"))
        return

    safe_print()
    safe_print(bold("  WORKERS"))
    safe_print(dim("  " + "-" * 54))
    for w in resp["workers"]:
        state = status_alive() if w["alive"] else status_dead()
        dev = device_icon(w.get("device", "cpu"))
        safe_print(f"  {dev}  {bold(w['id'][:8]):<10}  {w['hostname']:<16}  "
              f"score={w['score']:<8}  [{state}]")
    safe_print()


def cmd_devices(args):
    """Show all compute devices (local + remote) as one unified pool."""
    url, token = _resolve_conn(args)
    if not url or not token:
        safe_print(red("[ERROR] No connection found."))
        safe_print(dim("   Run with --url and --token, or connect first with 'gpumesh join'"))
        sys.exit(1)

    from .client import list_devices
    try:
        summary = list_devices(url, token)
    except (urllib.error.URLError, OSError) as exc:
        print(red(f"[ERROR] Could not reach coordinator: {exc}"))
        sys.exit(1)

    if not summary.get("devices"):
        safe_print(yellow("[!] No devices found."))
        safe_print(dim("   Make sure the coordinator is running and workers are connected."))
        return

    safe_print()
    safe_print(cyan("=" * 60))
    safe_print(bold("  GPUMESH DEVICES -- All Compute Resources"))
    safe_print(cyan("=" * 60))
    safe_print()

    alive = [d for d in summary["devices"] if d["status"] == "alive"]
    dead = [d for d in summary["devices"] if d["status"] == "dead"]

    if alive:
        safe_print(green("  ALIVE DEVICES:"))
        safe_print()
        for d in alive:
            icon = device_icon(d["device"])
            safe_print(f"    Device {d['index']:>2}: {icon}  {d['device_name']:<20} "
                  f"({d['hostname']})  score={d['score']:.1f}")
        safe_print()

    if dead:
        safe_print(red("  DEAD DEVICES (offline):"))
        safe_print()
        for d in dead:
            icon = device_icon(d["device"])
            safe_print(f"    Device {d['index']:>2}: {icon}  {d['device_name']:<20} "
                  f"({d['hostname']})  score={d['score']:.1f}")
        safe_print()

    safe_print(cyan("-" * 60))
    safe_print(f"  Total GPUs:  {bold(str(summary['total_gpus']))}")
    safe_print(f"  Total CPUs:  {bold(str(summary['total_cpus']))}")
    total_score = f"{summary['total_score']:.1f}"
    safe_print(f"  Total Score: {bold(total_score)} GFLOP/s")
    safe_print(f"  Devices:     {green(str(summary['alive_devices']))} alive / {summary['total_devices']} total")
    safe_print(cyan("=" * 60))
    safe_print()


def cmd_kill(args):
    """Kill all gpumesh tasks — graceful or force."""
    url, token = _resolve_conn(args)
    if not url or not token:
        print(red("[ERROR] No connection found."))
        print(dim("   Run with --url and --token, or connect first with 'gpumesh join'"))
        sys.exit(1)

    from .client import MeshClient

    mesh = MeshClient(url, token)

    if args.force:
        print(yellow("[!] FORCE KILL -- killing all tasks immediately..."))
    else:
        print(dim("[*] GRACEFUL KILL -- waiting for running tasks to finish..."))

    try:
        # Single global kill request — no worker_id means cancel all tasks
        result = mesh.call("POST", "/api/kill", {"force": args.force})
        pending = result.get("pending", 0)
        running = result.get("running", 0)
        print()
        print(green(f"[OK] Cancelled {pending} pending task(s), {running} running task(s)"))
    except Exception as exc:
        print(red(f"[ERROR] Could not communicate with coordinator: {exc}"))
        sys.exit(1)

    if not args.force:
        print(dim("   Workers will finish their current task before stopping."))

    print()
    print(green("[OK] Done."))


def cmd_disconnect(args):
    connection_manager.clear_connection()
    print(green("[OK] Connection cleared."))


def cmd_show_connection(args):
    """Show saved connection details for easy sharing."""
    saved = connection_manager.load_connection()
    if not saved:
        print(yellow("[!] No saved connection found."))
        print(dim("   Run 'gpumesh join' or 'gpumesh setup' first."))
        return

    url = saved["url"]
    token = saved["token"]

    print()
    print(cyan("=" * 50))
    print(bold("  SAVED CONNECTION DETAILS"))
    print(cyan("=" * 50))
    print()
    print(f"  URL:   {cyan(url)}")
    print(f"  Token: {cyan(token)}")
    print()
    print(cyan("=" * 50))
    print()
    print(dim("  Share these with your workers!"))
    print()
    print(f"  Workers can join by running:")
    print(f"    {cyan(f'gpumesh quickjoin {url} --token {token}')}")
    print()
    print(dim("  Or run 'gpumesh setup' and enter the URL and Token."))
    print()


def cmd_setup(args):
    from .setup_wizard import run_setup_wizard
    run_setup_wizard()


def cmd_worker(args):
    """Start a worker that broadcasts presence and waits to be claimed."""
    token = args.token
    if not token:
        print(red("[ERROR] --token is required"))
        sys.exit(1)
    if len(token) < 8:
        print(red("[ERROR] Token must be at least 8 characters"))
        sys.exit(1)
    worker.run_worker_broadcast(
        token=token,
        claim_port=args.claim_port,
        task_timeout=args.timeout,
    )


def cmd_quickjoin(args):
    """One-click setup: install dependencies, detect GPU, and join mesh."""
    token = args.token

    # Determine URL: explicit URL takes precedence over --tailscale
    if args.url:
        url = args.url
        if not url.startswith(("http://", "https://")):
            print(red("[ERROR] URL must start with http:// or https://"))
            sys.exit(1)
        print(dim(f"   Using provided URL: {url}"))
    elif args.tailscale:
        print(dim("   Auto-detecting coordinator via Tailscale..."))
        tailscale_ip = tunnel._get_tailscale_ip()
        if not tailscale_ip:
            print(red("[ERROR] Tailscale not found or not running"))
            print(dim("   Install from: https://tailscale.com/download"))
            sys.exit(1)
        # Use Tailscale IP with specified port
        url = f"http://{tailscale_ip}:{args.port}"
        print(dim(f"   Found Tailscale IP: {tailscale_ip}"))
    else:
        print(red("[ERROR] URL required or use --tailscale flag"))
        sys.exit(1)

    print(bold("   Quick Join -- Setting up your machine as a worker..."))
    print()

    # Step 1: Check Python version
    safe_print(dim("  [1/4]") + " Checking Python version...")
    if sys.version_info < (3, 9):
        safe_print(red("  [ERROR] Python 3.9 or higher is required."))
        safe_print(dim("   Install from: https://www.python.org/downloads/"))
        sys.exit(1)
    safe_print(dim("  [1/4]") + f" Python {sys.version_info.major}.{sys.version_info.minor} {green('[OK]')}")

    # Step 2: Install gpumesh if not already installed
    safe_print(dim("  [2/4]") + " Checking gpumesh installation...")
    try:
        import gpumesh
        safe_print(dim("  [2/4]") + f" gpumesh already installed {green('[OK]')}")
    except ImportError:
        safe_print(dim("  [2/4]") + " Installing gpumesh...")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-e", "."],
                check=True,
                timeout=120,
            )
            safe_print(dim("  [2/4]") + f" gpumesh installed {green('[OK]')}")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            safe_print(red(f"  [ERROR] Failed to install gpumesh: {e}"))
            safe_print(dim("   Install manually: pip install gpumesh"))
            sys.exit(1)

    # Step 3: Detect hardware
    safe_print(dim("  [3/4]") + " Detecting hardware...")
    from . import capability
    info = capability.probe_device()
    device = info.get("device", "cpu")
    device_name = info.get("device_name", "cpu")
    safe_print(dim("  [3/4]") + f" Detected device: {device_icon(device)} {device_name}")
    if device == "cuda":
        try:
            import torch
            if torch.cuda.is_available():
                safe_print(dim("  [3/4]") + f" PyTorch with CUDA ready {green('[OK]')}")
            else:
                safe_print(dim("  [3/4]") + yellow(" PyTorch installed but CUDA not available"))
        except ImportError:
            safe_print(dim("  [3/4]") + yellow(" PyTorch not found. Install with: pip install torch"))
    elif device == "mps":
        safe_print(dim("  [3/4]") + f" Apple Silicon GPU detected {green('[OK]')}")
    else:
        safe_print(dim("  [3/4]") + " Using CPU")

    # Step 4: Join the mesh
    safe_print(dim("  [4/4]") + " Joining the mesh...")
    safe_print(dim(f"   Connecting to {url}..."))
    safe_print()

    _run_worker_with_report(url, token, args.timeout)


def cmd_radar(args):
    """Scan for nearby gpumesh devices using UDP broadcast."""
    from .discovery import Beacon, Listener
    from .radar import print_radar_header, print_radar_peers, _clear_lines
    from . import capability

    mode = args.mode

    if mode == "worker":
        # Worker radar: broadcast self + listen for coordinators
        print(bold("   Starting worker radar..."))
        print(dim("   Broadcasting presence and scanning for coordinators..."))
        print()

        info = capability.full_probe()

        beacon = Beacon(
            device=info["device"],
            device_name=info["device_name"],
            score=info["score"],
        )
        try:
            beacon.start()
        except OSError as exc:
            print(red(f"[ERROR] Could not start beacon: {exc}"))
            print(dim("   Another instance may already be using the broadcast port."))
            return

        listener = Listener()
        try:
            listener.start()
        except OSError as exc:
            print(red(f"[ERROR] Could not start listener: {exc}"))
            beacon.stop()
            return

        print_radar_header("worker")
        prev_lines = 0
        try:
            while True:
                peers = listener.peers()
                prev_lines = print_radar_peers(peers, prev_lines)
                time.sleep(2)
        except KeyboardInterrupt:
            _clear_lines(prev_lines)
            print()
            print(dim("   Radar stopped."))
        finally:
            beacon.stop()
            listener.stop()
    else:
        # Coordinator radar: listen for workers
        print(bold("   Starting coordinator radar..."))
        print(dim("   Scanning for nearby workers..."))
        print()

        listener = Listener()
        try:
            listener.start()
        except OSError as exc:
            print(red(f"[ERROR] Could not start listener: {exc}"))
            print(dim("   Another instance may already be using the discovery port."))
            return

        print_radar_header("coordinator")
        prev_lines = 0
        try:
            while True:
                peers = listener.peers()
                prev_lines = print_radar_peers(peers, prev_lines)
                time.sleep(2)
        except KeyboardInterrupt:
            _clear_lines(prev_lines)
            print()
            print(dim("   Radar stopped."))
        finally:
            listener.stop()


def main():
    ap = argparse.ArgumentParser(
        prog="gpumesh",
        description="Borrow your friends' GPUs -- a distributed compute mesh.",
        epilog=(
            "Examples:\n"
            "  gpumesh serve                    Start a coordinator on this machine\n"
            "  gpumesh join http://HOST:8000    Offer this machine's GPU to the mesh\n"
            "  gpumesh submit run.py --payloads data.json   Submit a job\n"
            "  gpumesh status JOB_ID            Check job progress\n"
            "  gpumesh radar                    Scan for nearby devices\n"
            "  gpumesh setup                    Interactive setup wizard\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--version", action="version",
                    version=f"gpumesh {__version__} ({platform.python_version()}, {platform.system()})",
                    help="show version and exit")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("serve",
                       help="start a coordinator on this machine",
                       description="Start a coordinator that manages GPU jobs across the mesh.")
    p.add_argument("--port", type=int, default=8000,
                   help="port to listen on (default: 8000)")
    p.add_argument("--db", default="gpumesh.db",
                   help="database file for job storage (default: gpumesh.db)")
    p.add_argument("--token", default="",
                   help="auth token (random if omitted)")
    p.add_argument("--public", action="store_true",
                   help="expose a public URL via ngrok")
    p.add_argument("--tailscale", action="store_true",
                   help="use Tailscale for network access (auto-detects IP)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("join",
                       help="offer this machine's compute to a mesh",
                       description="Connect this machine as a worker to a coordinator.")
    p.add_argument("url", help="coordinator URL (e.g. http://192.168.1.10:8000)")
    p.add_argument("--token", default=os.environ.get("GPUMESH_TOKEN", ""),
                   help="auth token (or set GPUMESH_TOKEN)")
    p.add_argument("--timeout", type=float, default=240.0,
                   help="per-task wall clock limit in seconds (default: 240)")
    p.set_defaults(func=cmd_join)

    p = sub.add_parser("submit",
                       help="submit a job to the mesh",
                       description="Upload a script and payloads to be distributed across workers.")
    p.add_argument("script", help="python script to run for every payload")
    p.add_argument("--payloads", required=True,
                   help="JSON file: list of payload objects (each may set 'cost')")
    p.add_argument("--name", default="",
                   help="optional job name (default: script filename)")
    p.add_argument("--wait", action="store_true",
                   help="block until the job finishes")
    _add_conn_args(p)
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("status",
                       help="show job progress and results",
                       description="Check the status and results of a submitted job.")
    p.add_argument("job_id", help="job ID to check")
    _add_conn_args(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("cancel",
                       help="cancel a running job",
                       description="Cancel all pending and running tasks for a job.")
    p.add_argument("job_id", help="job ID to cancel")
    _add_conn_args(p)
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("workers",
                       help="list workers in the mesh",
                       description="Show all connected workers and their status.")
    _add_conn_args(p)
    p.set_defaults(func=cmd_workers)

    p = sub.add_parser("devices",
                       help="show all compute devices as one unified pool",
                       description="Display a unified view of all GPUs and CPUs across the mesh.")
    _add_conn_args(p)
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("kill",
                       help="kill all gpumesh tasks (graceful or force)",
                       description="Cancel all tasks across all workers.")
    p.add_argument("--force", action="store_true",
                   help="kill immediately without waiting for tasks to finish")
    _add_conn_args(p)
    p.set_defaults(func=cmd_kill)

    p = sub.add_parser("disconnect",
                       help="clear saved connection",
                       description="Remove the saved URL and token from this machine.")
    p.set_defaults(func=cmd_disconnect)

    p = sub.add_parser("show-connection",
                       help="show saved URL and token for sharing",
                       description="Display the saved connection details so you can share them.")
    p.set_defaults(func=cmd_show_connection)

    p = sub.add_parser("setup",
                       help="interactive setup wizard",
                       description="Guided setup for coordinators and workers.")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("quickjoin",
                       help="one-click setup: install, detect GPU, and join mesh",
                       description="Automatically install dependencies, detect hardware, and join.")
    p.add_argument("url", nargs="?", default="",
                   help="coordinator URL (optional with --tailscale)")
    p.add_argument("--token", required=True,
                   help="shared auth token")
    p.add_argument("--tailscale", action="store_true",
                   help="auto-detect coordinator via Tailscale")
    p.add_argument("--port", type=int, default=8000,
                   help="coordinator port (default: 8000)")
    p.add_argument("--timeout", type=float, default=240.0,
                   help="per-task wall clock limit in seconds (default: 240)")
    p.set_defaults(func=cmd_quickjoin)

    p = sub.add_parser("radar",
                       help="scan for nearby gpumesh devices",
                       description="Live-updating display of nearby devices on the network.")
    p.add_argument("--mode", choices=["coordinator", "worker"], default="coordinator",
                   help="radar mode: coordinator (listen) or worker (broadcast+listen)")
    p.set_defaults(func=cmd_radar)

    p = sub.add_parser("worker",
                       help="start a worker that broadcasts and waits to be claimed",
                       description="Start a worker that advertises itself for claim by coordinators.")
    p.add_argument("--token", required=True,
                   help="worker's unique token (min 8 chars)")
    p.add_argument("--claim-port", type=int, default=0,
                   help="port for the claim server (default: auto)")
    p.add_argument("--timeout", type=float, default=240.0,
                   help="per-task wall clock limit in seconds (default: 240)")
    p.set_defaults(func=cmd_worker)

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
