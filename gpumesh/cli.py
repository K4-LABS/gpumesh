"""gpumesh command line interface.

  gpumesh serve      [--port 8000] [--token SECRET] [--public] [--tailscale]
  gpumesh join       URL [--token SECRET]
  gpumesh quickjoin  [URL] --token TOKEN [--tailscale] [--port PORT]
  gpumesh submit     SCRIPT --payloads FILE [--wait] [--url URL] [--token SECRET]
  gpumesh status     JOB_ID [--url URL] [--token SECRET]
  gpumesh cancel     JOB_ID [--url URL] [--token SECRET]
  gpumesh workers    [--url URL] [--token SECRET]
  gpumesh disconnect
"""

import argparse
import os
import secrets
import socket
import subprocess
import sys

from . import __version__, client, connection_manager, server, tunnel, utils, worker





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
    httpd = server.serve("0.0.0.0", args.port, args.db, token)
    ip = utils.get_lan_ip()
    print(f"[mesh] coordinator listening on 0.0.0.0:{args.port}")
    print(f"[mesh] token: {token}")
    print(f"[mesh] LAN join command:")
    print(f"       gpumesh join http://{ip}:{args.port} --token {token}")

    # Determine tunnel mode
    if args.tailscale:
        tunnel_mode = "tailscale"
    elif args.public:
        tunnel_mode = "ngrok"
    else:
        tunnel_mode = "none"

    if tunnel_mode != "none":
        tunnel.open_tunnel(args.port, mode=tunnel_mode)

    print("[mesh] Ctrl+C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[mesh] shutting down")
        httpd.gpumesh_stop.set()
        httpd.shutdown()


def cmd_join(args):
    # Save connection for future commands
    connection_manager.save_connection(args.url, args.token)
    print(f"[mesh] connection saved (use 'gpumesh disconnect' to clear)")
    worker.run_worker(args.url, args.token, task_timeout=args.timeout)


def cmd_submit(args):
    url, token = _resolve_conn(args)
    if not url or not token:
        print("[mesh] ERROR: No connection found.")
        print("[mesh] Run with --url and --token, or connect first with 'gpumesh join'")
        sys.exit(1)
    # Save for future use
    connection_manager.save_connection(url, token)

    job_id = client.submit_job(url, token, args.script, args.payloads, name=args.name)
    print(f"[client] submitted job {job_id}")
    if args.wait:
        job = client.wait_for_job(url, token, job_id)
        client.print_job(job)
    else:
        print(f"[client] check progress: gpumesh status {job_id}")


def cmd_status(args):
    url, token = _resolve_conn(args)
    if not url or not token:
        print("[mesh] ERROR: No connection found.")
        print("[mesh] Run with --url and --token, or connect first with 'gpumesh join'")
        sys.exit(1)
    job = client.get_status(url, token, args.job_id)
    client.print_job(job)


def cmd_cancel(args):
    url, token = _resolve_conn(args)
    if not url or not token:
        print("[mesh] ERROR: No connection found.")
        print("[mesh] Run with --url and --token, or connect first with 'gpumesh join'")
        sys.exit(1)
    result = client.cancel_job(url, token, args.job_id)
    if result is None:
        print(f"[client] job {args.job_id} not found")
    else:
        print(f"[client] cancelled job {args.job_id}")
        print(f"  pending tasks cancelled: {result['pending']}")
        print(f"  running tasks cancelled: {result['running']}")
        print(f"  already finished: {result['already_finished']}")


def cmd_workers(args):
    from .worker import MeshClient

    url, token = _resolve_conn(args)
    if not url or not token:
        print("[mesh] ERROR: No connection found.")
        print("[mesh] Run with --url and --token, or connect first with 'gpumesh join'")
        sys.exit(1)

    resp = MeshClient(url, token).call("GET", "/api/workers")
    for w in resp["workers"]:
        state = "alive" if w["alive"] else "dead"
        print(f"  {w['id']}  {w['hostname']:<20} {w['device']:<5} "
              f"score={w['score']:<10} [{state}]")
    if not resp["workers"]:
        print("  (no workers yet)")


def cmd_disconnect(args):
    connection_manager.clear_connection()
    print("[mesh] connection cleared")


def cmd_show_connection(args):
    """Show saved connection details for easy sharing."""
    saved = connection_manager.load_connection()
    if not saved:
        print("[mesh] No saved connection found.")
        print("[mesh] Run 'gpumesh join' or 'gpumesh setup' first.")
        return

    url = saved["url"]
    token = saved["token"]

    print()
    print("=" * 50)
    print("  SAVED CONNECTION DETAILS")
    print("=" * 50)
    print()
    print(f"  URL:   {url}")
    print(f"  Token: {token}")
    print()
    print("=" * 50)
    print()
    print("  Share these with your workers!")
    print()
    print("  Workers can join by running:")
    print(f"    gpumesh quickjoin {url} --token {token}")
    print()
    print("  Or run 'gpumesh setup' and enter the URL and Token.")
    print()


def cmd_setup(args):
    from .setup_wizard import run_setup_wizard
    run_setup_wizard()


def cmd_quickjoin(args):
    """One-click setup: install dependencies, detect GPU, and join mesh."""
    token = args.token

    # Determine URL: explicit URL takes precedence over --tailscale
    if args.url:
        url = args.url
        print(f"[mesh] Using provided URL: {url}")
    elif args.tailscale:
        print("[mesh] Auto-detecting coordinator via Tailscale...")
        tailscale_ip = tunnel._get_tailscale_ip()
        if not tailscale_ip:
            print("[mesh] ERROR: Tailscale not found or not running")
            print("[mesh] Install from: https://tailscale.com/download")
            return
        # Use Tailscale IP with specified port
        url = f"http://{tailscale_ip}:{args.port}"
        print(f"[mesh] Found Tailscale IP: {tailscale_ip}")
    else:
        print("[mesh] ERROR: URL required or use --tailscale flag")
        return

    print("[mesh] Quick Join - Setting up your machine as a worker...")
    print()

    # Step 1: Check Python version
    print("[1/4] Checking Python version...")
    if sys.version_info < (3, 9):
        print("[mesh] ERROR: Python 3.9 or higher is required.")
        print("[mesh] Please install Python 3.9+ from https://www.python.org/downloads/")
        return
    print(f"[mesh] Python {sys.version_info.major}.{sys.version_info.minor} ✓")

    # Step 2: Install gpumesh if not already installed
    print("[2/4] Checking gpumesh installation...")
    try:
        import gpumesh
        print("[mesh] gpumesh already installed ✓")
    except ImportError:
        print("[mesh] Installing gpumesh...")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-e", "."],
                check=True,
                timeout=120,
            )
            print("[mesh] gpumesh installed ✓")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            print(f"[mesh] ERROR: Failed to install gpumesh: {e}")
            print("[mesh] Please install manually: pip install gpumesh")
            return

    # Step 3: Detect hardware
    print("[3/4] Detecting hardware...")
    from . import capability
    info = capability.probe_device()
    device = info.get("device", "cpu")
    device_name = info.get("device_name", "cpu")
    print(f"[mesh] Detected device: {device} ({device_name})")
    if device == "cuda":
        try:
            import torch
            if torch.cuda.is_available():
                print(f"[mesh] PyTorch with CUDA ready ✓")
            else:
                print("[mesh] PyTorch installed but CUDA not available")
        except ImportError:
            print("[mesh] PyTorch not found. Install with: pip install torch")
    elif device == "mps":
        print("[mesh] Apple Silicon GPU detected ✓")
    else:
        print("[mesh] Using CPU")

    # Step 4: Join the mesh
    print("[4/4] Joining the mesh...")
    print(f"[mesh] Connecting to {url}...")
    print()

    # Save connection for future commands
    connection_manager.save_connection(url, token)

    # Now run the normal join command
    worker.run_worker(url, token)


def main():
    ap = argparse.ArgumentParser(prog="gpumesh",
                                 description="borrow your friends' GPUs")
    ap.add_argument("--version", action="version",
                    version=f"gpumesh {__version__}",
                    help="show version and exit")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("serve", help="start a coordinator on this machine")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--db", default="gpumesh.db")
    p.add_argument("--token", default="", help="auth token (random if omitted)")
    p.add_argument("--public", action="store_true",
                   help="expose a public URL via ngrok")
    p.add_argument("--tailscale", action="store_true",
                   help="use Tailscale for network access (auto-detects IP)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("join", help="offer this machine's compute to a mesh")
    p.add_argument("url", help="coordinator URL")
    p.add_argument("--token", default=os.environ.get("GPUMESH_TOKEN", ""))
    p.add_argument("--timeout", type=float, default=240.0,
                   help="per-task wall clock limit in seconds")
    p.set_defaults(func=cmd_join)

    p = sub.add_parser("submit", help="submit a job to the mesh")
    p.add_argument("script", help="python script to run for every payload")
    p.add_argument("--payloads", required=True,
                   help="JSON file: list of payload objects (each may set 'cost')")
    p.add_argument("--name", default="")
    p.add_argument("--wait", action="store_true", help="block until finished")
    _add_conn_args(p)
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("status", help="show job progress and results")
    p.add_argument("job_id")
    _add_conn_args(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("cancel", help="cancel a running job")
    p.add_argument("job_id")
    _add_conn_args(p)
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("workers", help="list workers in the mesh")
    _add_conn_args(p)
    p.set_defaults(func=cmd_workers)

    p = sub.add_parser("disconnect", help="clear saved connection")
    p.set_defaults(func=cmd_disconnect)

    p = sub.add_parser("show-connection", help="show saved URL and token for sharing")
    p.set_defaults(func=cmd_show_connection)

    p = sub.add_parser("setup", help="interactive setup wizard")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("quickjoin", help="one-click setup: install, detect GPU, and join mesh")
    p.add_argument("url", nargs="?", default="", help="coordinator URL (optional with --tailscale)")
    p.add_argument("--token", required=True, help="shared auth token")
    p.add_argument("--tailscale", action="store_true",
                   help="auto-detect coordinator via Tailscale")
    p.add_argument("--port", type=int, default=8000,
                   help="coordinator port (default: 8000)")
    p.set_defaults(func=cmd_quickjoin)

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
