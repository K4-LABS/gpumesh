"""gpumesh command line interface.

  gpumesh serve      [--host 127.0.0.1] [--port 8000] [--token SECRET] [--public]
                     [--tailscale] [--no-discovery]
  gpumesh join       URL [--token SECRET]
  gpumesh quickjoin  [URL] --token TOKEN [--tailscale] [--port PORT]
  gpumesh worker     --token TOKEN [--claim-port PORT] [--timeout 240]
  gpumesh submit     SCRIPT --payloads FILE [--wait] [--wait-timeout SECS] [--url URL] [--token SECRET]
  gpumesh status     JOB_ID [--url URL] [--token SECRET]
  gpumesh cancel     JOB_ID [--url URL] [--token SECRET]
  gpumesh retry      JOB_ID [--url URL] [--token SECRET]
  gpumesh kill       [--force] [--url URL] [--token SECRET]
  gpumesh workers    [--url URL] [--token SECRET]
  gpumesh devices    [--url URL] [--token SECRET]
  gpumesh radar      [--mode coordinator|worker]
  gpumesh doctor     [--json]
  gpumesh show-connection
  gpumesh disconnect
"""

import argparse
import errno
import ipaddress
import os
import platform
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from . import (__version__, ansi, capability, client, connection_manager,
               server, status, tunnel, utils, worker)
from .ansi import (safe_print, bold, cyan, green, yellow, red, dim, device_icon,
                     status_alive, status_dead, clear_lines,
                     erase_line)
from .logging_config import setup_logging


def _resolve_conn(args, persist: bool = False) -> tuple[str, str]:
    """Resolve URL and token from args, env vars, or saved config.

    ``persist`` is only set by commands that establish a connection. Query
    commands (status, workers, devices, ...) must not rewrite the saved
    config: a single mistyped --url would otherwise silently replace a
    working connection.
    """
    url = getattr(args, "url", "") or ""
    token = getattr(args, "token", "") or ""
    return connection_manager.get_connection(
        url or None, token or None, persist=persist
    )


def _add_conn_args(p):
    p.add_argument("--url", default="",
                   help="coordinator URL (or set GPUMESH_URL, or saved from previous command)")
    p.add_argument("--token", default="",
                   help="shared auth token (or set GPUMESH_TOKEN, or saved from previous command)")


def _add_color_args(p):
    """Add the --color / --no-color pair to a long-running subcommand.

    Only ``serve`` and ``join`` carry these. They are the two commands that
    keep printing for hours — into a Docker log, a systemd journal, a CI
    pane — which is exactly where isatty() says "no terminal" and the user
    still wants (or emphatically does not want) colour. Everything else is a
    one-shot report where ``GPUMESH_COLOR`` in the environment is enough.

    ``--color`` was deliberately kept as a bare store-style flag rather than
    ``--color={auto,always,never}``: an optional-value flag (``nargs="?"``)
    greedily eats the next token, so ``gpumesh join --color http://host:8732``
    would swallow the URL and fail with an unreadable "invalid choice" error.
    ``--no-color`` also matches the ``--self-worker`` / ``--no-self-worker``
    pair this parser already uses. ``GPUMESH_COLOR=0`` remains available for
    anyone who prefers to say it in the environment.
    """
    g = p.add_mutually_exclusive_group()
    g.add_argument("--color", dest="color", action="store_const", const="always",
                   default="auto",
                   help="force colored output even when stdout is not a "
                        "terminal (same as GPUMESH_COLOR=1)")
    g.add_argument("--no-color", dest="color", action="store_const", const="never",
                   help="never emit ANSI colour, even on a terminal "
                        "(same as GPUMESH_COLOR=0)")


def _apply_color_choice(choice: str):
    """Make --color / --no-color actually reach the code that emits colour.

    This is fiddlier than it looks, which is why the flag sat there accepted
    and ignored for so long. ``ansi._SUPPORTS_COLOR`` is evaluated once, at
    ``import gpumesh.ansi`` time — long before argparse has seen a command
    line — so the obvious fix of setting ``GPUMESH_COLOR`` here does nothing
    at all in this process. It still has to be set, because the env var is
    what propagates to the subprocesses gpumesh spawns (the function-task
    helper, quickjoin's installer) and to any gpumesh module imported later.

    Re-running the detection then fixes the module for *this* process. But
    several modules do ``from .ansi import _SUPPORTS_COLOR``, which binds a
    copy of the bool at their own import time; rebinding only ``ansi``'s
    global would leave those copies stale and the flag would half-work,
    which is worse than not working. So every already-imported gpumesh module
    holding a copy is updated too — one code path, and it keeps working if a
    future command grows a --color flag.

    ``auto`` touches nothing, so an existing ``GPUMESH_COLOR`` in the
    environment keeps its old meaning when neither flag is passed.
    """
    if choice == "always":
        os.environ["GPUMESH_COLOR"] = "1"
    elif choice == "never":
        os.environ["GPUMESH_COLOR"] = "0"
    else:
        return

    resolved = ansi._detect_color()
    for name, module in list(sys.modules.items()):
        if not name.startswith("gpumesh."):
            continue
        if getattr(module, "_SUPPORTS_COLOR", None) is not None:
            module._SUPPORTS_COLOR = resolved


def _nonneg_float(value: str) -> float:
    """argparse type: a float that must be 0 or greater.

    A negative --wait-timeout would disable the timeout entirely (hanging
    forever), so reject it up front instead.
    """
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be 0 or greater (0 = wait forever)")
    return parsed


def _fmt_uptime(seconds: float) -> str:
    """Format seconds as m:ss or h:mm:ss for the ticker."""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


_TICKER_FRAMES = ("|", "/", "-", "\\")


def wait_interruptible(thread, poll: float = 0.2):
    """Block until ``thread`` finishes, staying responsive to Ctrl+C.

    A bare ``Thread.join()`` parks the main thread inside a lock acquire that
    never returns to the interpreter. A signal arriving during it is recorded
    but its handler only runs when the main thread next executes bytecode —
    which never happens — so Ctrl+C, SIGTERM and ``docker stop`` were all
    ignored and the process had to be killed. Joining with a short timeout in
    a loop hands control back to the interpreter between waits, which is what
    lets a pending signal actually be delivered.
    """
    while thread.is_alive():
        thread.join(timeout=poll)


# How long to wait for the self-worker thread to finish on shutdown.
#
# Derived from the worker's own graceful-shutdown budget rather than picked as
# a number: after the stop event is set, _run_function_task waits up to
# GRACEFUL_SHUTDOWN_TIMEOUT before killing a task's subprocess, so any budget
# below that guarantees the join times out whenever a task is in flight. The
# thread is then left to daemon-thread teardown at interpreter exit — killed
# mid-print, possibly holding stdout's lock — which is exactly the hang
# _stop_coordinator exists to prevent. The slack covers the worker noticing the
# subprocess died, posting its result, and sending its final heartbeat.
_SELF_WORKER_JOIN_SLACK = 10.0
_SELF_WORKER_JOIN_TIMEOUT = worker.GRACEFUL_SHUTDOWN_TIMEOUT + _SELF_WORKER_JOIN_SLACK


def _stop_coordinator(httpd, serve_thread, self_worker=None):
    """Shut the coordinator down and stop every thread it started.

    The self-worker is stopped explicitly rather than left to daemon-thread
    teardown: a daemon killed mid-print at interpreter shutdown can leave
    stdout's lock held, hanging the process on the way out.
    """
    safe_print(f"\n{yellow('[!] Shutting down...')}")
    try:
        stop = getattr(self_worker, "gpumesh_stop", None)
        if stop is not None:
            stop.set()
        try:
            httpd.gpumesh_stop.set()
            httpd.shutdown()
            serve_thread.join(timeout=15)
        except (KeyboardInterrupt, SystemExit):
            raise  # a second Ctrl+C — handled below
        except Exception as exc:
            # Only (KeyboardInterrupt, SystemExit) used to be guarded here, so
            # anything else out of shutdown() — an OSError from server_close,
            # say — escaped cmd_serve's finally: and skipped the self-worker
            # join below, leaking the very thread this function exists to stop.
            # Report it and keep cleaning up; a half-shut coordinator with a
            # live worker thread is strictly worse than a noisy one.
            safe_print(yellow(f"[!] Coordinator shutdown raised "
                              f"{type(exc).__name__}: {exc}"))
        if self_worker is not None:
            self_worker.join(timeout=_SELF_WORKER_JOIN_TIMEOUT)
    except (KeyboardInterrupt, SystemExit):
        # A second Ctrl+C during shutdown means the user is done waiting.
        safe_print(yellow("[!] Second interrupt — exiting immediately."))
        os._exit(1)
    except Exception as exc:
        safe_print(yellow(f"[!] Cleanup raised {type(exc).__name__}: {exc}"))


def _ticker_header(frame: str) -> str:
    """One-line banner for the keep-alive ticker (spinner + title)."""
    return (f"  {dim('-' * 18)} {bold(cyan('GPUMESH LIVE'))} "
            f"{dim(frame)} {dim('-' * 18)}")


def _render_keepalive(health: dict, workers: list, frame: str = "|") -> list:
    """Render the live 'GPUMESH LIVE' ticker block for the coordinator.

    Returns a list of display lines using only ASCII-safe glyphs (matching
    the project's legacy-console convention). ``health`` is the /api/health
    payload; ``workers`` is the /api/workers worker list.
    """
    alive = [w for w in workers if w.get("alive")]
    gpus = sum(1 for w in alive if w.get("device") in ("cuda", "mps"))
    cpus = len(alive) - gpus
    score = float(health.get("total_score") or 0.0)
    uptime = float(health.get("uptime_seconds") or 0.0)

    online = green(bold(str(len(alive)))) if alive else yellow("0")
    dev_bits = []
    if gpus:
        dev_bits.append(f"{gpus} GPU")
    if cpus:
        dev_bits.append(f"{cpus} CPU")
    devices = f"  ({', '.join(dev_bits)})" if dev_bits else ""

    jobs = []
    for key, label, color in (
        ("jobs_running", "running", yellow),
        ("jobs_pending", "pending", dim),
        ("jobs_done", "done", green),
        ("jobs_failed", "failed", red),
    ):
        n = int(health.get(key) or 0)
        if n:
            jobs.append(f"{color(str(n))} {label}")
    jobs_text = ", ".join(jobs) if jobs else dim("no jobs")

    lines = [
        _ticker_header(frame),
        f"  Workers online: {online}{devices}    "
        f"{dim('Score:')} {cyan(bold(f'{score:.1f}'))} GFLOP/s",
        f"  Jobs: {jobs_text}    {dim('Uptime:')} {dim(_fmt_uptime(uptime))}",
    ]

    top = sorted(alive, key=lambda w: -(w.get("score") or 0.0))[:4]
    if top:
        row = "   ".join(
            f"{device_icon(w.get('device') or 'cpu')} "
            f"{bold((w.get('device_name') or w.get('hostname') or '?')[:14])}"
            for w in top
        )
        extra = len(alive) - len(top)
        if extra > 0:
            row += f"   {dim(f'... +{extra} more')}"
        lines.append(f"   {row}")
    lines.append(f"  {dim('-' * 46)}")
    return lines


def _run_keepalive_ticker(health_fn, workers_fn, thread, tick: float = 2.0):
    """Live 'workers online' ticker on the coordinator keep-alive screen.

    Polls /api/health + /api/workers every ``tick`` seconds and redraws a
    small block in place (radar-style) until ``thread`` stops. Polling
    failures (e.g. during shutdown) render a dim 'unreachable' state
    instead of crashing the coordinator screen.

    While active, the shared status sink buffers the coordinator's mesh
    log lines and the most recent ones are rendered in the same redraw
    region (above the block), so background events — worker joins, job
    IDs — are never erased by the redraw.
    """
    prev_height = 0
    frame = 0
    try:
        status.set_active(True)
        while thread.is_alive():
            try:
                health = health_fn() or {}
                workers = (workers_fn() or {}).get("workers", [])
                status_lines = _render_keepalive(
                    health, workers, _TICKER_FRAMES[frame % 4]
                )
            except Exception:
                status_lines = [
                    _ticker_header(_TICKER_FRAMES[frame % 4]),
                    f"  {yellow('coordinator unreachable')}",
                ]
            lines = status.snapshot() + status_lines
            if prev_height:
                try:
                    clear_lines(prev_height)
                except (OSError, ValueError):
                    pass  # terminal closed mid-redraw; loop exits shortly
            for line in lines:
                try:
                    erase_line()
                except (OSError, ValueError):
                    pass
                try:
                    sys.stdout.write(line + "\n")
                except UnicodeEncodeError:
                    # Legacy Windows console: drop non-cp1252 glyphs
                    # rather than crashing the keep-alive screen.
                    safe = line.encode("ascii", errors="replace").decode("ascii")
                    try:
                        sys.stdout.write(safe + "\n")
                    except (OSError, ValueError):
                        pass
                except (OSError, ValueError):
                    pass
            try:
                sys.stdout.flush()
            except (OSError, ValueError):
                pass
            prev_height = len(lines)
            frame += 1
            time.sleep(tick)
    finally:
        status.set_active(False)


DEFAULT_BIND_HOST = "127.0.0.1"


def _is_loopback_bind(host) -> bool:
    """True when ``host`` only accepts connections from this machine.

    Note that an empty host is NOT loopback: ``bind(("", port))`` is
    INADDR_ANY, the same wildcard as ``0.0.0.0``.
    """
    h = str(host or "").strip().strip("[]").lower()
    if h in ("localhost", "::1", "0:0:0:0:0:0:0:1"):
        return True
    return h.startswith("127.")


def _resolve_bind_host(args) -> str:
    """Resolve the address the coordinator's listening socket binds to.

    Precedence is the usual one: an explicit ``--host`` beats the
    ``GPUMESH_HOST`` environment variable, which beats the loopback default.
    The env var is read here as well as in argparse so a caller that builds
    its own args object (tests, embedders) gets the same answer.

    This is deliberately NOT ``--host-ip``: that one only changes the address
    printed for workers to dial, and used to be the only host-shaped flag,
    which made it look like it controlled the bind. It never did — the bind
    was hard-coded to 0.0.0.0.
    """
    explicit = getattr(args, "host", "") or ""
    try:
        explicit = explicit.strip()
    except AttributeError:
        explicit = str(explicit)
    if explicit:
        return explicit
    env = os.environ.get("GPUMESH_HOST", "").strip()
    if env:
        return env
    return DEFAULT_BIND_HOST


def _local_probe_host(bind_host) -> str:
    """The address THIS machine should dial to reach the coordinator.

    A wildcard bind answers on loopback, so 127.0.0.1 is right there. A bind
    pinned to one interface does not answer on loopback at all, so probing
    127.0.0.1 would report a perfectly healthy coordinator as broken — dial
    the bound address itself instead.
    """
    h = str(bind_host or "").strip()
    if h in ("", "0.0.0.0", "*"):
        return "127.0.0.1"
    if h.strip("[]") in ("::", "0:0:0:0:0:0:0:0"):
        return "::1"
    return h


def _url_host(host: str) -> str:
    """Wrap a bare IPv6 literal in brackets so it is legal in a URL."""
    h = str(host or "")
    if ":" in h and not h.startswith("["):
        return f"[{h}]"
    return h


def _exposure_identity() -> tuple:
    """Who submitted code would run as, and on what hardware.

    Split out because every exposure banner has to name both, and they are
    the two facts that make a warning land. "Your coordinator is exposed" is
    abstract enough to scroll past; "anyone out there can run code as
    <your login> on <your RTX 3080>" is not. Both lookups are best-effort —
    a banner that raised while trying to describe a risk would suppress the
    warning entirely, which is the worst possible failure mode for it.
    """
    try:
        import getpass
        user = getpass.getuser()
    except Exception:
        user = (os.environ.get("USERNAME") or os.environ.get("USER")
                or "the user running this process")
    try:
        from . import capability
        info = capability.probe_device()
        device = (info.get("device_name") or info.get("device") or "unknown")
        device = f"{device_icon(info.get('device') or 'cpu')} {device}"
    except Exception:
        device = "unknown"
    return user, device


def _print_exposure_warning(bind_host: str, port: int):
    """Loud banner shown when the coordinator binds beyond loopback.

    A worker runs whatever Python the coordinator hands it, in a subprocess
    owned by the OS user who started that worker — so "reachable from the LAN"
    means "arbitrary code execution as that user for anyone on the LAN holding
    the token". That is the same shape as TorchServe's CVE-2023-43654 and
    Dask's CVE-2021-42343. Name the user and the device explicitly: an
    abstract warning about "exposure" is easy to skim past, "runs code as
    <you> on <your RTX 3080>" is not.
    """
    user, device = _exposure_identity()

    print()
    print(red(bold("  " + "!" * 62)))
    print(red(bold("  !! NETWORK-EXPOSED COORDINATOR")))
    print(red(bold("  " + "!" * 62)))
    print(f"   Bound to:      {bold(f'{bind_host}:{port}')} "
          f"{dim('(not loopback — other machines can connect)')}")
    print(f"   Tasks run as:  {bold(user)}")
    print(f"   On device:     {bold(device)}")
    print()
    print(yellow("   Anyone who can reach this port AND has the token can run"))
    print(yellow(f"   arbitrary code on this machine as {user}."))
    print(dim("   Only do this on a network you trust, and keep the token secret."))
    print(dim(f"   Loopback-only (the default) is: gpumesh serve --port {port}"))
    print(red(bold("  " + "!" * 62)))
    print()


def _tunnel_mode(args) -> str:
    """Which tunnel `serve` was asked for: 'tailscale', 'ngrok' or 'none'.

    Resolved once, up front, because the answer now decides things that
    happen long before the tunnel is opened — whether the run is refused at
    all, and what the join block is allowed to claim about reachability.
    ``--tailscale`` beating ``--public`` when both are passed is
    long-standing behaviour and is kept exactly as it was.
    """
    if getattr(args, "tailscale", False):
        return "tailscale"
    if getattr(args, "public", False):
        return "ngrok"
    return "none"


# What each tunnel mode actually opens the coordinator up to, in the words the
# banner uses. Kept as data so the two banners cannot drift apart.
_TUNNEL_LABEL = {
    "ngrok": {
        "flag": "--public",
        "title": "INTERNET-EXPOSED COORDINATOR",
        "reach": "THE PUBLIC INTERNET",
        "via": "the ngrok URL printed below",
        "who": "Anyone on the internet who learns the ngrok URL",
        "off": "re-run without --public",
    },
    "tailscale": {
        "flag": "--tailscale",
        "title": "TAILNET-EXPOSED COORDINATOR",
        "reach": "EVERY DEVICE ON YOUR TAILNET",
        "via": "the tailnet address printed below",
        "who": "Anyone signed into your tailnet",
        "off": "re-run without --tailscale",
    },
}


def _print_tunnel_exposure_warning(mode: str, bind_host: str, port: int):
    """Loud banner shown before a tunnel is opened.

    A tunnel is a strictly LARGER exposure than any bind address can be.
    ``--host 0.0.0.0`` reaches whoever shares the local network; an ngrok
    tunnel reaches everyone with a route to the internet, which is everyone.
    Until this banner existed the larger exposure printed the smaller warning
    — and on the default loopback bind it printed no warning at all, because
    ``_print_exposure_warning`` lives in the non-loopback branch and the
    tunnel was opened four lines later regardless. So this is deliberately
    the same shape and the same 62-column width as the bind banner, and it
    prints IN ADDITION to it rather than instead of it: a wide bind and a
    tunnel are two different doors into the same process and closing one does
    not close the other.

    Naming the reach explicitly matters more here than for a bind. "Bound to
    0.0.0.0" at least hints at a network; an ngrok URL looks like a harmless
    random hostname, and nothing about it says "the entire internet can now
    execute code on this laptop".
    """
    label = _TUNNEL_LABEL[mode]
    user, device = _exposure_identity()

    print()
    print(red(bold("  " + "!" * 62)))
    print(red(bold(f"  !! {label['title']}")))
    print(red(bold("  " + "!" * 62)))
    print(f"   Opened by:      {bold(label['flag'])}")
    print(f"   Reachable from: {bold(label['reach'])} "
          f"{dim('(' + label['via'] + ')')}")
    print(f"   Bound to:       {bold(f'{bind_host}:{port}')} "
          f"{dim('(the tunnel forwards into it regardless)')}")
    print(f"   Tasks run as:   {bold(user)}")
    print(f"   On device:      {bold(device)}")
    print()
    print(yellow(f"   {label['who']} AND has the token can run"))
    print(yellow(f"   arbitrary code on this machine as {user}."))
    print(dim("   The URL is not a secret and is not access control — the "
              "token is the"))
    print(dim("   only thing left protecting this machine. "
              f"To close the tunnel, {label['off']}."))
    print(red(bold("  " + "!" * 62)))
    print()


def _refuse_tailscale_on_loopback(bind_host: str, port: int, token: str):
    """Stop `serve --tailscale` on a loopback bind, and say what to run.

    See the long note in cmd_serve for why this refuses instead of widening
    the bind on the user's behalf.
    """
    print()
    print(red(bold(f"[ERROR] --tailscale cannot work with a loopback bind "
                   f"({bind_host}).")))
    print(yellow("   --tailscale does not tunnel anything. It looks up this "
                 "machine's tailnet"))
    print(yellow("   address and prints it for your friends to dial. Tailnet "
                 "traffic arrives"))
    print(yellow(f"   addressed to that 100.x.y.z address, and a socket bound "
                 f"to {bind_host}"))
    print(yellow("   never accepts it — so the URL would be printed and "
                 "nothing could connect."))
    print()
    print("   Bind to your tailnet address instead:")
    print(f"     {cyan('tailscale ip -4')}"
          f"          {dim('# prints something like 100.101.102.103')}")
    rerun = (f"gpumesh serve --host 100.101.102.103 --port {port} "
             f"--token {token} --tailscale")
    print(f"     {cyan(rerun)}")
    print(dim("   That accepts tailnet peers and nothing else. --host 0.0.0.0 "
              "also works,"))
    print(dim("   but it accepts your local network too — read the banner it "
              "prints."))
    print()
    sys.exit(1)


def _refuse_foreign_host_ip(ip: str, port: int):
    """Stop `serve --host-ip <IP>` when <IP> is not an address of this machine.

    ``--host-ip`` only changes the address that is *printed* for workers to
    dial; the bind is untouched (that is ``--host``). A pin that does not
    belong to this machine is not a bind failure — the coordinator starts
    fine and then advertises an address nothing here answers, which turns
    into every remote worker timing out (WinError 10060) with the
    coordinator looking perfectly healthy. That is the exact failure the
    ``--host-ip`` flag exists to prevent, so fail fast instead of printing
    the dead URL.

    Addresses that ARE this machine's come from ``utils.lan_ip_candidates()``
    (the same ranking the coordinator uses to advertise). Loopback is allowed
    too: ``--host-ip 127.0.0.1`` is the sensible pin for a loopback-bound
    coordinator. CGNAT/tailnet (100.x) addresses are deliberately NOT in the
    candidate list — they are reachable only inside a tailnet — so they are
    allowed as well, since ``--tailscale`` pins exactly that.
    """
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        # Not an IP literal. The GPUMESH_HOST_IP env-var path already
        # rejects hostnames with its own warning; for the flag, let the
        # auto-detection path fall back rather than double-reporting.
        return
    candidates = utils.lan_ip_candidates()
    if ip in candidates:
        return
    # The candidate list deliberately excludes loopback and CGNAT/tailnet
    # (100.x) addresses: loopback is the correct pin for a loopback-bound
    # coordinator, and ``--tailscale`` pins exactly a tailnet address. Both
    # are "special" in the same sense lan_ip_candidates filters out, so the
    # same helper judges them.
    if utils._is_loopback_or_special(ip):
        return
    print()
    print(red(bold(f"[ERROR] --host-ip {ip} is not an address of this machine.")))
    print(yellow("   Workers would be told to dial that address, and nothing here"))
    print(yellow("   answers it — every remote worker would time out (WinError 10060)."))
    print()
    print("   Addresses this machine actually has:")
    for candidate in candidates:
        print(f"     {cyan(candidate)}")
    print()
    print("   Re-run with one of those, or drop --host-ip and let")
    print("   auto-detection pick (it only ever chooses a real address).")
    sys.exit(1)


def cmd_serve(args):
    token = args.token or secrets.token_urlsafe(12)
    # Bind to loopback unless the user asked for more. See _resolve_bind_host
    # and _print_exposure_warning for why the default changed.
    bind_host = _resolve_bind_host(args)
    loopback = _is_loopback_bind(bind_host)
    local_host = _url_host(_local_probe_host(bind_host))
    # Every URL this command prints, saves or probes has to carry the scheme
    # the socket is actually speaking. An https listener advertised as http://
    # fails on the worker with a connection reset and no clue why.
    tls_on = bool(getattr(args, "tls", False) or getattr(args, "tls_cert", ""))
    scheme = "https" if tls_on else "http"
    local_url = f"{scheme}://{local_host}:{args.port}"
    tunnel_mode = _tunnel_mode(args)

    # ---- the tunnel flags versus the bind address -------------------------
    #
    # --public and --tailscale used to ignore the bind entirely: the tunnel
    # was opened unconditionally, four lines after the exposure banner that
    # only ran on a non-loopback bind. On the default bind that produced
    # "Other machines CANNOT reach this coordinator" followed immediately by
    # a public ngrok URL. Both halves of that had to change, and the two
    # flags do NOT get the same treatment, because they are not the same kind
    # of thing:
    #
    #   --public is a real tunnel. The pyngrok agent is a process on THIS
    #     machine that dials 127.0.0.1:PORT itself, so a loopback bind does
    #     not stop it — it works, and that is the problem. It converts the
    #     closed default into the widest exposure gpumesh can produce while
    #     printing less warning than `--host 0.0.0.0` does. Refusing it would
    #     be the wrong fix twice over: loopback is in fact the *correct* bind
    #     to pair with ngrok (only the local agent can reach the port; the
    #     LAN cannot), so pushing the user to `--host 0.0.0.0 --public` would
    #     make them add LAN reachability the tunnel never needed. So --public
    #     is allowed on any bind and now carries its own full-width banner,
    #     printed before the tunnel opens, naming the internet as the reach.
    #
    #   --tailscale is not a tunnel at all. tunnel.open_tunnel only shells out
    #     to `tailscale ip -4` and prints http://100.x.y.z:PORT. Tailnet
    #     packets arrive on this machine addressed to that 100.x address, and
    #     a socket bound to 127.0.0.1 never accepts them. There is nothing to
    #     warn about because there is nothing reachable — the URL is simply
    #     dead, which is the exact failure the loopback join text was written
    #     to prevent: a printed address that cannot connect and an hour spent
    #     blaming a firewall. So this combination is refused.
    #
    # Refusing rather than widening the bind for --tailscale is deliberate.
    # The bind is the operator's single most consequential choice (SECURITY.md
    # says so in as many words), and making it a side effect of a flag that
    # says nothing about binding is how a security default gets hollowed out;
    # "announced" only helps the operator who reads the announcement, on a
    # screen they have already learned to scroll past. The asymmetry is also
    # recoverable in the right direction — a refused command costs one re-run,
    # a wrongly widened one is accepting connections before anyone reads about
    # it. And there is no single widening that is right anyway: the correct
    # bind for Tailscale is the tailnet address specifically, not 0.0.0.0,
    # which is a choice only the operator can make.
    if loopback and tunnel_mode == "tailscale":
        _refuse_tailscale_on_loopback(bind_host, args.port, token)

    if args.safe_mode:
        print(yellow("[!] SAFE MODE enabled — function distribution disabled. "
                     "Script-based jobs only."))
    # Try to add Windows Firewall rules automatically — but only when the
    # socket is actually reachable from off-box. On a loopback bind no
    # inbound packet can arrive at all, so punching a hole in the firewall
    # would be pointless, and warning that "workers on OTHER machines may be
    # blocked by the firewall" would send the user to fix the wrong thing:
    # the bind is what is stopping them, not the firewall.
    if not loopback:
        if not utils.try_add_firewall_rule(args.port):
            utils.show_firewall_hint(args.port)
            print(yellow("[!] WARNING: Workers on OTHER machines may be blocked by the "
                  "firewall."))
            print(dim("   Start 'gpumesh serve' as Administrator, or open "
                  f"port {args.port} manually (see hint above)."))
    # A pinned --host-ip that is not an address of this machine is not a bind
    # failure — the coordinator starts fine and then advertises a dead URL.
    # Fail before binding, while the mistake is cheap to explain.
    if getattr(args, "host_ip", ""):
        _refuse_foreign_host_ip(args.host_ip, args.port)
    # ``--db`` defaults to None so the stable location (next to the saved
    # connection, not the working directory) can be filled in here. The
    # relative-path default that preceded this lost every queued job when the
    # coordinator was restarted from a different folder: a fresh, empty
    # database is indistinguishable from "no jobs", and nothing says why.
    db_path = args.db or connection_manager.default_db_path()
    try:
        httpd = server.serve(bind_host, args.port, db_path, token,
                             discovery=not args.no_discovery,
                             safe_mode=args.safe_mode,
                             tls=getattr(args, "tls", False),
                             tls_cert=getattr(args, "tls_cert", "") or None,
                             tls_key=getattr(args, "tls_key", "") or None)
    except sqlite3.Error as exc:
        # An unwritable --db path surfaces as sqlite3.OperationalError, which
        # is NOT an OSError — the port-in-use handler below never saw it, so
        # the coordinator died with a raw traceback naming no path at all.
        # Name the path and say what to do instead.
        print(red(f"[ERROR] Cannot open database at {db_path}: {exc}"))
        print(yellow("   The coordinator stores queued jobs here, and it is not"))
        print(yellow("   writable (or its parent directory does not exist)."))
        print(dim("   Check the directory is writable, or point --db at a"))
        print(dim("   writable location, e.g.:  gpumesh serve --db ./gpumesh.db"))
        sys.exit(1)
    except PermissionError as exc:
        # A privileged port (<1024) is refused by the OS with EACCES/EPERM on
        # POSIX (Windows has no privileged ports, so a PermissionError there
        # is the database, handled below). The generic port-in-use message
        # would send the user to pick another port — which does not fix a
        # permission error.
        if (platform.system() != "Windows" and args.port < 1024
                and getattr(exc, "errno", None) in (errno.EACCES, errno.EPERM)):
            print(red(f"[ERROR] Permission denied binding port {args.port}: {exc}"))
            print(yellow("   Ports below 1024 are privileged and are refused by the"))
            print(yellow("   OS for unprivileged processes."))
            print(dim(f"   Try a port above 1024, e.g.:  gpumesh serve --port 8000"))
            sys.exit(1)
        # The other PermissionError source inside serve() is the database's
        # parent directory (Database._open_db calls os.makedirs). Name it the
        # way the sqlite branch below names an unwritable file.
        print(red(f"[ERROR] Cannot open database at {db_path}: {exc}"))
        print(yellow("   The coordinator stores queued jobs here, and its parent"))
        print(yellow("   directory is not writable."))
        print(dim("   Check the directory is writable, or point --db at a"))
        print(dim("   writable location, e.g.:  gpumesh serve --db ./gpumesh.db"))
        sys.exit(1)
    except OSError as exc:
        print(red(f"[ERROR] {exc}"))
        print(yellow(f"   Port {args.port} is already in use."))
        print(dim(f"   Try: gpumesh serve --port {args.port + 1}"))
        sys.exit(1)
    # --host-ip pins the address advertised to workers. Auto-detection cannot
    # always tell a real LAN interface from a VPN or hypervisor one, and a
    # wrong pick makes every remote worker time out (WinError 10060).
    ip = getattr(args, "host_ip", "") or utils.get_lan_ip()
    # Save connection so other commands can use it without --url/--token.
    # On a wider bind the LAN IP is the address other machines should use; on
    # loopback that address answers nothing, so saving it would break `gpumesh
    # submit` on this very machine.
    connection_manager.save_connection(
        local_url if loopback else f"{scheme}://{ip}:{args.port}", token
    )
    print(green(f"[OK] Coordinator listening on {bind_host}:{args.port}"))
    print(dim(f"   Token: {token}"))
    print()
    print(f"   Join from THIS machine:   "
          f"{cyan(f'gpumesh join {local_url} --token {token}')}")
    if loopback and tunnel_mode != "none":
        # A loopback bind with a tunnel over it. The old text claimed "Other
        # machines CANNOT reach this coordinator" and was then followed by a
        # public URL, which is the single most misleading thing this command
        # could print. The bind really is closed; the tunnel is a second door
        # that does not go through it, so say both facts and let the banner
        # below carry the weight.
        print(yellow(f"   The socket is bound to {bind_host} only — nothing on "
                     f"your local network"))
        print(yellow(f"   can connect to it directly. {_TUNNEL_LABEL[tunnel_mode]['flag']} "
                     f"opens a separate way in, and that"))
        print(yellow("   way in is NOT limited to your local network. See the "
                     "banner below."))
    elif loopback:
        # Do NOT print a LAN join URL here. It cannot connect to a loopback
        # bind, and printing it anyway is what sends a user off to debug a
        # firewall, a VPN or an antivirus for an hour. Say what is true and
        # exactly what to type instead — this default is a breaking change,
        # and the migration has to be readable at the moment it bites.
        print(yellow(f"   Other machines CANNOT reach this coordinator "
                     f"(bound to {bind_host} only)."))
        print(dim("   That is the default now: a worker executes code as the "
                  "user who started it."))
        print(f"   To let other machines join, re-run with: "
              f"{cyan(f'gpumesh serve --host 0.0.0.0 --port {args.port} --token {token}')}")
        print(dim("   (or set GPUMESH_HOST=0.0.0.0). Read the warning it prints "
                  "before you do."))
    else:
        print(f"   Join from ANOTHER machine: "
              f"{cyan(f'gpumesh join {scheme}://{ip}:{args.port} --token {token}')}")
        print(dim("   (127.0.0.1 always works locally; the LAN IP is for "
              "other machines)"))
        if not getattr(args, "host_ip", ""):
            utils.show_ip_alternatives(ip, args.port)
        _print_exposure_warning(bind_host, args.port)

    # The banner goes BEFORE open_tunnel, not after: open_tunnel prints the
    # public URL and the join command, and a warning that arrives after the
    # thing it warns about is decoration. It is printed for every bind, wide
    # ones included — a wide bind and a tunnel are separate exposures, so the
    # NETWORK-EXPOSED banner above does not cover this one.
    if tunnel_mode != "none":
        _print_tunnel_exposure_warning(tunnel_mode, bind_host, args.port)
        tunnel.open_tunnel(args.port, mode=tunnel_mode)

    _signals_seen = [0]

    def _sigterm_handler(signum, frame):
        _signals_seen[0] += 1
        if _signals_seen[0] > 1:
            # Asked twice — stop cleaning up and just go.
            os._exit(1)
        raise SystemExit(0)
    if platform.system() != "Windows":
        signal.signal(signal.SIGTERM, _sigterm_handler)
    else:
        try:
            signal.signal(signal.SIGBREAK, _sigterm_handler)
        except (AttributeError, ValueError):
            pass

    print(dim("   Ctrl+C to stop"))
    # Start the accept loop in the background. serve_forever() MUST be running
    # before any request can be processed — running the self-check beforehand
    # (while the server only had its socket bound) caused it to always time out.
    import threading as _threading
    _serve_thread = _threading.Thread(target=httpd.serve_forever, daemon=True)
    _serve_thread.start()

    # Self-check: confirm the process is actually serving requests. Any HTTP
    # response (even a 401 for a missing token) proves that. It still runs on
    # a loopback bind — there it is the *most* meaningful it ever gets, because
    # the address probed is the address bound.
    #
    # It proves ONLY that. A local probe can never tell us whether another
    # machine can reach the advertised address — that depends on the network
    # between here and there, which only the worker can observe. Say what was
    # tested so a green line is not read as a promise that remote workers will
    # connect.
    _selfcheck_ctx = None
    if tls_on:
        # The self-check talks to our own listener over our own certificate.
        # Verifying it here would fail on the hostname the probe happens to
        # use and would prove nothing about the thing being tested, which is
        # "did the socket come up".
        import ssl as _ssl
        _selfcheck_ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
        _selfcheck_ctx.check_hostname = False
        _selfcheck_ctx.verify_mode = _ssl.CERT_NONE
    try:
        with urllib.request.urlopen(
            f"{local_url}/api/workers", timeout=5, context=_selfcheck_ctx
        ) as _resp:
            _ = _resp.status
        print(green(f"[OK] Self-check: server is up (tested on {local_host} only)"))
    except urllib.error.HTTPError:
        # Server responded with an error status (e.g. 401) — it's up.
        print(green(f"[OK] Self-check: server is up (tested on {local_host} only)"))
    except Exception as exc:
        print(yellow(f"[!] WARNING: coordinator self-check failed on {local_host}: {exc}"))
        print(dim("   The server may not have started correctly."))
    if not loopback:
        print(dim(f"   Reachability of {scheme}://{ip}:{args.port} from other machines "
                  f"is not tested here — a worker joining confirms it."))

    # Self-worker: this machine joins its own pool so its CPU/GPU is used
    # alongside the laptops that joined as workers.
    _self_worker = None
    if getattr(args, "self_worker", True):
        try:
            from .worker import spawn_local_worker
            # Always dial the local address: the self-worker lives in this
            # process, and on a loopback bind the LAN IP answers nothing.
            _self_worker = spawn_local_worker(
                local_url, token,
                task_timeout=240.0, safe_mode=args.safe_mode,
                persist_connection=False,
            )
            print(green("[OK] Self-worker started — this machine's CPU/GPU is part of the pool"))
            print(dim("   (disable with --no-self-worker)"))
        except Exception as exc:
            print(yellow(f"[!] Could not start self-worker: {exc}"))

    try:
        # Read through the module, never a ``from ansi import _SUPPORTS_COLOR``
        # copy. That copy is bound at *this module's* import time, which is
        # before argparse has run, so --color/--no-color could never change it
        # and the ticker would pick the wrong branch for the rest of the run.
        if getattr(sys.stdout, "isatty", lambda: False)() and ansi._SUPPORTS_COLOR:
            # Live keep-alive screen: poll the coordinator itself so the
            # worker count / job totals / uptime stay fresh on screen.
            mesh = worker.MeshClient(local_url, token)
            _run_keepalive_ticker(
                lambda: mesh.call("GET", "/api/health"),
                lambda: mesh.call("GET", "/api/workers"),
                _serve_thread,
            )
        else:
            # Piped / Docker / non-ANSI output: no redraws, just wait — but
            # never in a plain join(), which would swallow Ctrl+C entirely.
            wait_interruptible(_serve_thread)
    except KeyboardInterrupt:
        pass
    except SystemExit:
        pass
    finally:
        _stop_coordinator(httpd, _serve_thread, _self_worker)


def _run_worker_with_report(url: str, token: str, task_timeout: float,
                            safe_mode: bool = False):
    """Run a worker and, if it fails to connect quickly, print a clear
    final message (run_worker returns silently on connection failure)."""
    import threading

    print(dim(f"   Connecting worker to {url} ..."))
    # run_worker only sees signals when it owns the main thread, and it does
    # not here — so it is given an explicit stop event instead. Without one
    # there is no way to shut this worker down short of killing the process.
    stop = threading.Event()
    t = threading.Thread(
        target=worker.run_worker,
        args=(url, token, task_timeout, safe_mode, True, stop),
        daemon=True,
    )
    t.start()
    # Give it a short window to register. If it's still alive afterwards,
    # it connected and is running its heartbeat loop; keep it in foreground.
    t.join(timeout=10)
    if not t.is_alive():
        print(red("[ERROR] Worker could not connect."))
        print(dim("   Verify the coordinator is running and the URL/token are correct."))
        print(dim("   If on another machine, ensure the coordinator was "
              "started as Administrator (Windows Firewall)."))
        # A stale saved config pointing at a dead URL may be the culprit;
        # drop it so the next command forces a fresh --url.
        if connection_manager.clear_if_stale():
            print(dim("   (cleared a stale saved connection)"))
        return

    try:
        wait_interruptible(t)
    except (KeyboardInterrupt, SystemExit):
        safe_print(f"\n{yellow('[!] Stopping worker...')}")
        stop.set()
        t.join(timeout=15)
        if t.is_alive():
            safe_print(dim("   Worker did not stop in time — exiting anyway."))


def cmd_join(args):
    # Validate the URL scheme up front so a missing "http://" produces a
    # clear message instead of a cryptic "unknown url type" error.
    if not args.url.startswith(("http://", "https://")):
        print(red("[ERROR] URL must start with http:// or https://"))
        print(dim("   Example: gpumesh join http://192.168.1.10:8000 --token SECRET"))
        sys.exit(1)
    _run_worker_with_report(args.url, args.token, args.timeout,
                            safe_mode=args.safe_mode)


def cmd_submit(args):
    url, token = _resolve_conn(args)
    if not url or not token:
        print(red("[ERROR] No connection found."))
        print(dim("   Run with --url and --token, or connect first with 'gpumesh join'"))
        sys.exit(1)

    job_id = client.submit_job(url, token, args.script, args.payloads, name=args.name)
    print(green(f"[OK] Submitted job {bold(job_id)}"))

    # If no workers are connected the job will just sit in the queue; tell
    # the user so they don't wait wondering where their result went. Use a
    # direct call (not the error-swallowing _get_workers helper) so we can
    # tell "no workers" apart from "coordinator unreachable" and never warn
    # incorrectly.
    try:
        resp = worker.MeshClient(url, token).call("GET", "/api/workers")
        workers = (resp or {}).get("workers", [])
        if not any(w.get("alive") for w in workers):
            print(yellow("[!] No workers connected — this job is queued and will run "
                  "as soon as a worker joins."))
            print(dim("   Join one with: gpumesh join URL --token TOKEN  "
                  "(or gpumesh worker --token TOKEN)"))
    except Exception:
        pass  # best-effort warning; the job is already submitted

    if args.wait:
        # Cap the wait so a queued job with no workers can't hang the terminal
        # forever; --wait-timeout 0 explicitly restores the old wait-forever.
        timeout = getattr(args, "wait_timeout", 3600.0) or 0.0
        try:
            client.wait_for_job(url, token, job_id, timeout=timeout)
        except TimeoutError as exc:
            safe_print(yellow(f"[!] {exc}"))
            safe_print(dim("   The job is still running on the coordinator (no result yet)."))
            safe_print(dim("   Check back any time with: " + cyan(f"gpumesh status {job_id}")))
            safe_print(dim("   To wait longer next time, raise --wait-timeout (0 = wait forever)."))
            sys.exit(1)
        except RuntimeError as exc:
            safe_print(red(f"[ERROR] {exc}"))
            sys.exit(1)
        # wait_for_job() prints the final job report itself.
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
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(yellow(f"[!] Job {args.job_id} not found — the coordinator may have "
                  "been restarted with a fresh database."))
        else:
            print(red(f"[ERROR] Coordinator returned HTTP {exc.code}: {exc}"))
        sys.exit(1)
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


def cmd_retry(args):
    """Re-queue failed tasks of a job so workers run them again."""
    url, token = _resolve_conn(args)
    if not url or not token:
        print(red("[ERROR] No connection found."))
        print(dim("   Run with --url and --token, or connect first with 'gpumesh join'"))
        sys.exit(1)
    try:
        result = client.retry_job(url, token, args.job_id)
    except (urllib.error.URLError, OSError) as exc:
        print(red(f"[ERROR] Could not communicate with coordinator: {exc}"))
        sys.exit(1)
    if result is None:
        print(yellow(f"[!] Job {args.job_id} not found"))
        sys.exit(1)
    if result["requeued"] == 0:
        print(yellow(f"[!] Job {args.job_id} has no failed tasks to retry"))
        print(dim("   Only failed or timed-out tasks are re-queued; done tasks keep"))
        print(dim("   their results. Check the state with: " + cyan(f"gpumesh status {args.job_id}")))
        return
    print(green(f"[OK] Re-queued {result['requeued']} failed task(s) for job {bold(args.job_id)}"))
    print(dim(f"   New task counts: {result['counts']}"))
    print(dim(f"   The job will run again as workers become available."))
    print(dim("   Check progress: " + cyan(f"gpumesh status {args.job_id}")))


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


def _url_is_loopback(url: str) -> tuple:
    """Is this saved URL reachable only from the machine that saved it?

    Returns ``(is_loopback, port)``. The port comes back too because the
    advice printed for a local-only URL has to name it, and re-parsing the
    same string twice is how the two drift apart.

    A malformed URL is reported as NOT loopback. That errs towards the
    existing "share this" text, which is the honest default: we could not
    establish that the address is local-only, so claiming it is would be a
    guess presented as a fact.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        port = parts.port or 8000
    except ValueError:
        return False, 8000
    if not host:
        return False, port
    return _is_loopback_bind(host), port


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
    local_only, port = _url_is_loopback(url)
    if local_only:
        # Since the loopback bind default this is the COMMON case, and the old
        # unconditional "Share these with your workers!" turned it into a trap:
        # 127.0.0.1 means "the machine you typed it on", so a worker running
        # the printed quickjoin command dials itself, gets connection refused,
        # and goes looking for a firewall problem that does not exist. Say the
        # URL is local-only before showing anything that looks shareable.
        print(yellow("  This URL is LOCAL-ONLY. Do NOT share it."))
        print(dim(f"  {url} means 'this computer'. On someone else's machine it "
                  f"points at"))
        print(dim("  their machine, not yours, so the join will always fail."))
        print()
        print("  It works here, on this machine:")
        print(f"    {cyan(f'gpumesh join {url} --token {token}')}")
        print()
        print("  To get an address you CAN share, restart the coordinator on a "
              "bind")
        print("  other machines can reach:")
        print(f"    {cyan(f'gpumesh serve --host 0.0.0.0 --port {port} --token {token}')}")
        print(dim("  Read the exposure banner it prints, then run "
                  "'gpumesh show-connection' again."))
        print()
        return
    print(dim("  Share these with your workers!"))
    print()
    print(f"  Workers can join by running:")
    print(f"    {cyan(f'gpumesh quickjoin {url} --token {token}')}")
    print()
    print(dim("  Or run 'gpumesh setup' and enter the URL and Token."))
    print()


# ---------------------------------------------------------------------------
# doctor — read-only environment diagnostics
# ---------------------------------------------------------------------------
#
# Nearly every support thread on this project turns out to be one machine's
# environment disagreeing with another's: two different Python minors, so a
# pickled function will not load on the far side; a CPU-only torch wheel on a
# box that has a 4090 in it; a coordinator advertising an address no worker can
# route to; a firewall quietly dropping the port. Each of those costs a long
# back-and-forth of "run this, paste the output" before anyone can even name
# the problem. `gpumesh doctor` is that whole conversation in one command, in a
# form that can be pasted into an issue.
#
# Two rules hold the design together:
#
#   1. It is READ-ONLY. It never adds a firewall rule, never saves a
#      connection, never installs anything. A diagnostic that changes the
#      system destroys the evidence it was run to collect.
#   2. It NEVER prints the token. The saved token grants code execution on
#      every machine in the mesh, and the whole point of this command is that
#      its output gets pasted into a public bug report.

_DOCTOR_REDACTED = "<redacted>"


def _redact_url(url: str) -> str:
    """A URL safe to paste into a bug report.

    Only the userinfo is removed. Host and port are the diagnostic payload —
    "the coordinator is advertising 192.168.56.1" is usually the whole answer —
    but ``http://user:secret@host`` is a form people do paste, and a password
    in the netloc is as much a secret as the token.
    """
    if not url:
        return ""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    if not parts.netloc or "@" not in parts.netloc:
        return url
    host = parts.netloc.rsplit("@", 1)[-1]
    return urllib.parse.urlunsplit(
        (parts.scheme, f"{_DOCTOR_REDACTED}@{host}", parts.path,
         parts.query, parts.fragment)
    )


def _doctor_gpumesh() -> dict:
    """Which gpumesh is actually imported, and from where.

    "Where" matters as much as "which": a `pip install -e .` checkout shadowing
    a released wheel, or a stale copy on PYTHONPATH, produces a version number
    that does not match the code being run.
    """
    import gpumesh as _pkg

    package_file = getattr(_pkg, "__file__", None)
    return {
        "version": __version__,
        "install_path": os.path.dirname(package_file) if package_file else None,
    }


def _doctor_python() -> dict:
    """This interpreter, plus the Windows Store alias trap.

    Windows ships zero-byte "app execution alias" stubs at
    ``%LOCALAPPDATA%\\Microsoft\\WindowsApps\\python.exe`` /
    ``python3.11.exe``. Running one opens the Microsoft Store instead of
    Python. That is a nuisance in a shell, but it is fatal here: the worker
    executes every function task by spawning ``sys.executable``, so a stub on
    PATH turns each task into a silent store popup on a machine nobody is
    looking at. The genuine Store *install* (a real python.exe that also lives
    under WindowsApps) is a milder case — it works, but it redirects AppData
    writes, which is worth naming when the config file mysteriously does not
    persist.
    """
    executable = sys.executable or ""
    info = {
        "version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "executable": executable or None,
        "windows_store_path": False,
        "windows_store_alias_stub": False,
    }
    if os.name == "nt" and executable:
        lowered = executable.replace("/", "\\").lower()
        info["windows_store_path"] = "\\microsoft\\windowsapps\\" in lowered
        try:
            # A real interpreter is megabytes; the alias stub is a zero-length
            # reparse point. Size is the only check that separates them without
            # actually executing the thing.
            info["windows_store_alias_stub"] = (
                info["windows_store_path"] and os.path.getsize(executable) == 0
            )
        except OSError:
            pass
    return info


def _doctor_torch() -> dict:
    """torch, and specifically whether it can see the GPU that is installed.

    Three outcomes have to stay distinguishable, because the fix differs for
    each: no torch at all (fine — gpumesh runs CPU tasks without it), torch
    with CUDA (fine), and a CPU-only wheel on a machine that demonstrably has
    an NVIDIA GPU (the classic ``pip install torch`` on Windows landing the
    CPU build, which quietly halves a mesh's capacity). nvidia-smi answering
    while ``torch.cuda.is_available()`` says False is what separates the third
    from the first two, and capability.py already knows how to ask it.
    """
    info = {
        "installed": False,
        "version": None,
        "cuda_available": False,
        "device_count": 0,
        "device_names": [],
        "nvidia_gpu_present": None,
        "error": None,
    }
    try:
        import torch
    except ImportError:
        # An absent torch is a supported configuration, not a fault, so the
        # nvidia-smi probe still runs: "no torch on a box with a 4090" is
        # exactly the report worth making.
        info["nvidia_gpu_present"] = _doctor_nvidia_smi_present()
        return info
    except Exception as exc:  # a broken install: bad DLL, ABI mismatch
        info["error"] = f"{type(exc).__name__}: {exc}"
        info["nvidia_gpu_present"] = _doctor_nvidia_smi_present()
        return info

    info["installed"] = True
    info["version"] = getattr(torch, "__version__", "unknown")
    try:
        info["cuda_available"] = bool(torch.cuda.is_available())
        if info["cuda_available"]:
            info["device_count"] = torch.cuda.device_count()
            info["device_names"] = [
                torch.cuda.get_device_name(i) for i in range(info["device_count"])
            ]
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
    mps = getattr(getattr(torch, "backends", None), "mps", None)
    try:
        info["mps_available"] = bool(mps and mps.is_available())
    except Exception:
        info["mps_available"] = False
    if not info["cuda_available"]:
        info["nvidia_gpu_present"] = _doctor_nvidia_smi_present()
    return info


def _doctor_nvidia_smi_present():
    """True if nvidia-smi answers, False if it does not, None if unknown."""
    try:
        return capability._get_gpu_utilization_nvidia_smi() is not None
    except Exception:
        return None


def _doctor_cloudpickle() -> dict:
    """cloudpickle is the wire format, so its version is a mesh-wide fact.

    A function pickled here is unpickled inside a worker process on another
    machine. cloudpickle does not promise cross-version pickle compatibility,
    so a skew between two members of a mesh is a task-time failure with a
    traceback pointing at pickle internals — see the long note in pyproject.
    """
    try:
        import cloudpickle
    except ImportError:
        return {"installed": False, "version": None}
    return {
        "installed": True,
        "version": getattr(cloudpickle, "__version__", "unknown"),
    }


def _doctor_connection() -> dict:
    """The saved coordinator, with the token withheld.

    ``token_length`` is reported instead of the token. It is not a secret and
    it catches the one token problem you cannot see any other way — a value
    truncated by a shell, a copy-paste, or a YAML quoting accident.
    """
    saved = connection_manager.load_connection()
    if not saved:
        return {"configured": False, "url": None, "token": None,
                "token_length": 0, "age_hours": None}
    token = saved.get("token") or ""
    saved_at = saved.get("saved_at")
    age_hours = None
    if saved_at:
        age_hours = round(max(0.0, time.time() - saved_at) / 3600.0, 1)
    return {
        "configured": True,
        "url": _redact_url(saved.get("url") or ""),
        "token": _DOCTOR_REDACTED,
        "token_length": len(token),
        "age_hours": age_hours,
    }


def _doctor_coordinator(url: str, token: str, this_python: str) -> dict:
    """Ask the coordinator how it is, and whether its workers match this box.

    Everything here is best-effort and short-timeout: doctor has to finish on
    a machine with no network at all, and an unreachable coordinator is a
    finding to report, never an exception to propagate.

    Worker records currently carry no gpumesh/Python version — the coordinator
    stores id/hostname/device/score and nothing else — so the mismatch check
    reads whichever of those keys a newer coordinator happens to send and says
    plainly when they are absent. Guessing "all match" from missing data would
    be worse than silence: cross-version function shipping is the exact failure
    this section exists to catch.
    """
    from .worker import MeshClient

    result = {
        "configured": True,
        "url": _redact_url(url),
        "reachable": False,
        "error": None,
        "health": None,
        "workers": [],
        "version_info_reported": False,
        "mismatched_workers": [],
    }
    if not url or not token:
        result["configured"] = False
        return result

    client_ = MeshClient(url, token)
    try:
        result["health"] = client_.call("GET", "/api/health", timeout=5.0)
        result["reachable"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    try:
        resp = client_.call("GET", "/api/workers", timeout=5.0) or {}
        workers = resp.get("workers", [])
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    coordinator_version = (result["health"] or {}).get("version")
    for w in workers:
        entry = {
            "id": (w.get("id") or "")[:8],
            "hostname": w.get("hostname"),
            "device": w.get("device"),
            "device_name": w.get("device_name"),
            "alive": bool(w.get("alive")),
            "version": w.get("version"),
            "python_version": w.get("python_version"),
        }
        if entry["version"] is not None or entry["python_version"] is not None:
            result["version_info_reported"] = True
            reasons = []
            if entry["version"] and entry["version"] != __version__:
                reasons.append(
                    f"gpumesh {entry['version']} (this machine: {__version__})"
                )
            if (entry["python_version"]
                    and entry["python_version"] != this_python):
                reasons.append(
                    f"Python {entry['python_version']} (this machine: {this_python})"
                )
            if reasons:
                result["mismatched_workers"].append(
                    {"worker": entry["hostname"] or entry["id"],
                     "differences": reasons}
                )
        result["workers"].append(entry)

    result["coordinator_version"] = coordinator_version
    return result


def _doctor_network(port: int) -> dict:
    """Where a coordinator started here would bind, and what it would advertise.

    The bind and the advertised address are different decisions that fail in
    different ways (see the --host / --host-ip note on `serve`), and half the
    "workers time out" reports are one of them being wrong. Listing every LAN
    candidate lets the reader spot the case auto-detection cannot: a
    hypervisor or VPN adapter that this machine can reach and no other can.
    """
    bind_host = _resolve_bind_host(argparse.Namespace())
    info = {
        "port": port,
        "bind_host": bind_host,
        "bind_is_loopback": _is_loopback_bind(bind_host),
        "host_ip_override": os.environ.get("GPUMESH_HOST_IP", "") or None,
        "advertised_ip": None,
        "lan_ip_candidates": [],
        "virtual_adapter_candidates": [],
    }
    try:
        info["advertised_ip"] = utils.get_lan_ip()
        candidates = utils.lan_ip_candidates()
        info["lan_ip_candidates"] = candidates
        info["virtual_adapter_candidates"] = [
            ip for ip in candidates if utils._is_virtual_adapter_ip(ip)
        ]
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def _doctor_firewall(port: int) -> dict:
    """Does a Windows Firewall rule for this port already exist?

    A QUERY, never `netsh ... add rule`. utils.try_add_firewall_rule is the
    write-side helper and is deliberately not called here: doctor must not
    change the machine it is inspecting, and a silently-dropped port is
    something the user needs to *see*, not have papered over mid-diagnosis.

    Absence of a rule is reported, not judged. Plenty of working setups have
    no rule at all — a loopback-only coordinator needs none, and a third-party
    firewall would not show up in netsh anyway.
    """
    info = {"platform": platform.system(), "checked": False,
            "rules_found": [], "rules_missing": [], "error": None}
    if platform.system() != "Windows":
        return info

    names = [f"gpumesh-{port}", f"gpumesh-exe-{port}", "gpumesh-discovery-udp"]
    info["checked"] = True
    for name in names:
        try:
            proc = subprocess.run(
                ["netsh", "advfirewall", "firewall", "show", "rule",
                 f"name={name}"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            info["error"] = f"{type(exc).__name__}: {exc}"
            info["checked"] = False
            return info
        # netsh exits non-zero and prints "No rules match..." when the rule
        # does not exist, so the return code is the reliable signal.
        if proc.returncode == 0:
            info["rules_found"].append(name)
        else:
            info["rules_missing"].append(name)
    return info


def _doctor_collect() -> dict:
    """Gather the whole report. Pure data — no printing, no exit codes.

    Split from the rendering so the text and --json outputs are two views of
    one collection, and so a test can assert the token is absent from the data
    itself rather than from a string that happened to be formatted a certain
    way.
    """
    this_python = platform.python_version()
    saved = connection_manager.load_connection() or {}
    # Same precedence connection_manager.get_connection uses (env beats saved),
    # spelled out here rather than calling it: get_connection prints a
    # staleness note as a side effect, and a diagnostic that writes to stdout
    # while collecting would end up inside its own --json document.
    url = os.environ.get("GPUMESH_URL", "") or saved.get("url") or ""
    token = os.environ.get("GPUMESH_TOKEN", "") or saved.get("token") or ""

    port = 8000
    if url:
        try:
            parsed_port = urllib.parse.urlsplit(url).port
            if parsed_port:
                port = parsed_port
        except ValueError:
            pass

    report = {
        "gpumesh": _doctor_gpumesh(),
        "python": _doctor_python(),
        "torch": _doctor_torch(),
        "cloudpickle": _doctor_cloudpickle(),
        "connection": _doctor_connection(),
        "network": _doctor_network(port),
        "firewall": _doctor_firewall(port),
        "problems": [],
        "warnings": [],
    }
    report["platform"] = {
        "system": platform.system(),
        "release": platform.platform(),
        "hostname": socket.gethostname(),
    }
    if url and token:
        report["coordinator"] = _doctor_coordinator(url, token, this_python)
    else:
        report["coordinator"] = {"configured": False, "reachable": False,
                                 "url": None, "error": None, "health": None,
                                 "workers": [], "version_info_reported": False,
                                 "mismatched_workers": []}

    _doctor_diagnose(report)
    return report


def _doctor_diagnose(report: dict) -> None:
    """Turn the collected facts into problems (exit 1) and warnings (exit 0).

    The split is deliberate and narrow. A *problem* is something broken on
    THIS machine that gpumesh itself cannot work around — a missing wire
    format, an interpreter that cannot spawn. Everything else is a warning,
    however suspicious, because doctor is run exactly when things are already
    unclear and a non-zero exit is a claim, not a hunch.

    Not configuring a coordinator is neither. A laptop that has never joined a
    mesh is a completely healthy laptop, and failing here would make `doctor`
    useless as a pre-flight check in a script.
    """
    problems = report["problems"]
    warnings_ = report["warnings"]

    if not report["cloudpickle"]["installed"]:
        problems.append(
            "cloudpickle is not installed. It is gpumesh's wire format — "
            "@mesh/@accelerate functions cannot be sent or received without "
            "it. Fix: pip install 'cloudpickle>=3.0.0'"
        )

    py = report["python"]
    if py["windows_store_alias_stub"]:
        problems.append(
            f"sys.executable ({py['executable']}) is a zero-byte Windows Store "
            f"app-execution alias, not a real interpreter. Workers spawn this "
            f"path for every function task, so each task would open the "
            f"Microsoft Store instead of running. Fix: install Python from "
            f"python.org, or turn the alias off under Settings > Apps > "
            f"Advanced app settings > App execution aliases."
        )
    elif py["windows_store_path"]:
        warnings_.append(
            "This is the Microsoft Store build of Python. It works, but it "
            "redirects writes under AppData, which can make ~/.gpumesh/"
            "config.json appear not to persist. A python.org install avoids it."
        )
    if not py["executable"]:
        problems.append(
            "sys.executable is empty, so a worker on this machine cannot spawn "
            "the subprocess it runs function tasks in."
        )

    torch_info = report["torch"]
    if torch_info["error"]:
        problems.append(
            f"torch is installed but failed to initialise: {torch_info['error']}. "
            f"GPU tasks will fall back to CPU or fail outright."
        )
    elif not torch_info["installed"]:
        if torch_info["nvidia_gpu_present"]:
            warnings_.append(
                "No torch, but nvidia-smi reports an NVIDIA GPU on this "
                "machine. gpumesh will register it as a CPU-only worker and "
                "the GPU will sit idle. Fix: pip install 'gpumesh[gpu]'"
            )
        else:
            warnings_.append(
                "torch is not installed. That is fine for CPU work; GPU "
                "scoring and gpu= placement need it."
            )
    elif not torch_info["cuda_available"]:
        if torch_info["nvidia_gpu_present"]:
            warnings_.append(
                f"torch {torch_info['version']} is installed and nvidia-smi "
                f"reports an NVIDIA GPU, but torch.cuda.is_available() is "
                f"False — this is a CPU-only torch wheel. The GPU will never "
                f"be used. Fix: reinstall torch with a CUDA build from "
                f"https://pytorch.org/get-started/locally/"
            )
        elif not torch_info.get("mps_available"):
            warnings_.append(
                f"torch {torch_info['version']} sees no GPU on this machine; "
                f"tasks will run on CPU."
            )

    conn = report["connection"]
    coord = report["coordinator"]
    if not coord.get("configured"):
        warnings_.append(
            "No coordinator configured. @mesh functions run locally. "
            "Run 'gpumesh join URL --token TOKEN' to join a mesh."
        )
    elif not coord["reachable"]:
        warnings_.append(
            f"Coordinator {coord.get('url')} is not reachable: "
            f"{coord.get('error')}. If it moved networks, run "
            f"'gpumesh disconnect' and re-join."
        )
    if conn["configured"] and 0 < conn["token_length"] < 8:
        warnings_.append(
            f"The saved token is only {conn['token_length']} characters. "
            f"gpumesh requires at least 8 — it may have been truncated."
        )

    for mismatch in coord.get("mismatched_workers", []):
        warnings_.append(
            f"Worker {mismatch['worker']} differs from this machine: "
            f"{'; '.join(mismatch['differences'])}. Cross-version function "
            f"shipping is the most common cause of NameError/unpickling "
            f"failures on a task."
        )
    if coord.get("reachable") and coord.get("workers") \
            and not coord.get("version_info_reported"):
        warnings_.append(
            "This coordinator does not report worker gpumesh/Python versions, "
            "so a cross-version mismatch cannot be checked from here. Run "
            "'gpumesh doctor' on each worker machine and compare the Python "
            "lines by hand."
        )

    net = report["network"]
    if net["advertised_ip"] and net["advertised_ip"] in net["virtual_adapter_candidates"]:
        warnings_.append(
            f"The address this machine would advertise ({net['advertised_ip']}) "
            f"belongs to a virtual adapter (VM, Docker or connection sharing). "
            f"Other machines usually cannot route to it and will time out. "
            f"Pin a real one with GPUMESH_HOST_IP=<IP>."
        )


def _doctor_print(report: dict) -> None:
    """Render the report as text meant to be pasted into a bug report."""
    def line(label, value):
        safe_print(f"  {label:<22} {value}")

    g = report["gpumesh"]
    py = report["python"]
    plat = report["platform"]
    torch_info = report["torch"]
    cp = report["cloudpickle"]
    conn = report["connection"]
    coord = report["coordinator"]
    net = report["network"]
    fw = report["firewall"]

    safe_print()
    safe_print(cyan("=" * 62))
    safe_print(bold("  GPUMESH DOCTOR"))
    safe_print(cyan("=" * 62))

    safe_print()
    safe_print(bold("  INSTALL"))
    line("gpumesh version", g["version"])
    line("install path", g["install_path"] or "unknown")
    line("machine", f"{plat['hostname']}  ({plat['release']})")

    safe_print()
    safe_print(bold("  PYTHON"))
    line("version", f"{py['version']} ({py['implementation']})")
    line("executable", py["executable"] or "unknown")
    if py["windows_store_alias_stub"]:
        line("", red("Windows Store app-execution ALIAS STUB — not a real "
                     "interpreter"))
    elif py["windows_store_path"]:
        line("", yellow("Microsoft Store build (AppData writes are redirected)"))

    safe_print()
    safe_print(bold("  TORCH"))
    if torch_info["error"]:
        line("status", red(f"broken: {torch_info['error']}"))
    elif not torch_info["installed"]:
        line("status", yellow("not installed"))
    else:
        line("version", torch_info["version"])
        if torch_info["cuda_available"]:
            line("cuda", green(f"available ({torch_info['device_count']} device(s))"))
            for i, name in enumerate(torch_info["device_names"]):
                line(f"  device {i}", name)
        elif torch_info.get("mps_available"):
            line("cuda", dim("not available"))
            line("mps", green("available (Apple Silicon)"))
        else:
            line("cuda", yellow("NOT available — CPU-only"))
    if torch_info["nvidia_gpu_present"] is True:
        line("nvidia-smi", yellow("reports an NVIDIA GPU on this machine"))
    elif torch_info["nvidia_gpu_present"] is False:
        line("nvidia-smi", dim("no NVIDIA GPU detected"))

    safe_print()
    safe_print(bold("  CLOUDPICKLE") + dim("  (the wire format — must match across machines)"))
    if cp["installed"]:
        line("version", cp["version"])
    else:
        line("status", red("NOT INSTALLED"))

    safe_print()
    safe_print(bold("  CONNECTION") + dim("  (~/.gpumesh/config.json)"))
    if not conn["configured"]:
        if coord.get("configured"):
            # An env-var-only setup is a real configuration; saying "none"
            # here would send the reader off to run `join` for no reason.
            line("saved", dim("none — using GPUMESH_URL / GPUMESH_TOKEN"))
        else:
            line("saved", dim("none — @mesh functions run locally"))
    else:
        line("url", conn["url"])
        line("token", dim(f"{_DOCTOR_REDACTED} ({conn['token_length']} chars)"))
        if conn["age_hours"] is not None:
            line("saved", f"{conn['age_hours']}h ago")

    safe_print()
    safe_print(bold("  COORDINATOR"))
    if not coord.get("configured"):
        line("status", dim("not configured"))
    elif not coord["reachable"]:
        line("status", yellow(f"unreachable: {coord.get('error')}"))
    else:
        health = coord.get("health") or {}
        line("status", green("reachable"))
        line("version", health.get("version", "unknown"))
        line("uptime", f"{health.get('uptime_seconds', '?')}s")
        line("workers", f"{health.get('workers_alive', '?')} alive / "
                        f"{health.get('workers_dead', '?')} dead")
        line("jobs", f"{health.get('jobs_pending', '?')} pending, "
                     f"{health.get('jobs_running', '?')} running, "
                     f"{health.get('jobs_done', '?')} done, "
                     f"{health.get('jobs_failed', '?')} failed")
        for w in coord["workers"]:
            state = status_alive() if w["alive"] else status_dead()
            versions = []
            if w.get("version"):
                versions.append(f"gpumesh {w['version']}")
            if w.get("python_version"):
                versions.append(f"py {w['python_version']}")
            suffix = ("  " + dim(", ".join(versions))) if versions else ""
            line(f"  {w['id']}", f"{w['hostname']}  {w['device_name']}  "
                                 f"[{state}]{suffix}")
        if coord["workers"] and not coord["version_info_reported"]:
            line("", dim("(this coordinator does not report worker "
                         "gpumesh/Python versions)"))

    safe_print()
    safe_print(bold("  NETWORK"))
    line("would bind to", f"{net['bind_host']}:{net['port']}"
                          + ("  (loopback only)" if net["bind_is_loopback"] else ""))
    if net["host_ip_override"]:
        line("GPUMESH_HOST_IP", net["host_ip_override"])
    line("would advertise", net["advertised_ip"] or "unknown")
    for ip in net["lan_ip_candidates"]:
        note = ""
        if ip in net["virtual_adapter_candidates"]:
            note = dim("  (virtual adapter — usually unreachable from other "
                       "machines)")
        line("  candidate", f"{ip}{note}")

    if fw["platform"] == "Windows":
        safe_print()
        safe_print(bold("  FIREWALL") + dim("  (Windows, read-only check)"))
        if not fw["checked"]:
            line("status", dim(f"could not query netsh: {fw['error']}"))
        else:
            for name in fw["rules_found"]:
                line("rule", green(f"{name}  present"))
            for name in fw["rules_missing"]:
                line("rule", dim(f"{name}  absent"))

    safe_print()
    if report["problems"]:
        safe_print(red(bold("  PROBLEMS")))
        for item in report["problems"]:
            safe_print(red(f"  [X] {item}"))
        safe_print()
    if report["warnings"]:
        safe_print(yellow(bold("  WARNINGS")))
        for item in report["warnings"]:
            safe_print(yellow(f"  [!] {item}"))
        safe_print()
    if not report["problems"] and not report["warnings"]:
        safe_print(green("  [OK] No problems found."))
        safe_print()
    safe_print(cyan("=" * 62))
    safe_print()


def cmd_doctor(args):
    """Print a read-only environment report; exit 1 only on a local fault."""
    if getattr(args, "json", False):
        import contextlib
        import json as _json

        # Collection is not silent — connection_manager warns about a
        # world-readable config, utils warns about a bad GPUMESH_HOST_IP, and
        # both write to stdout. In --json mode stdout is a document, so those
        # lines are diverted to stderr where a human still sees them and `jq`
        # does not. Everything is collected before the first byte of JSON is
        # written, so a warning can never land mid-document.
        with contextlib.redirect_stdout(sys.stderr):
            report = _doctor_collect()
        # Plain print, not safe_print: this output is meant to be piped into a
        # parser, and safe_print's cp1252 transliteration would corrupt a
        # non-ASCII hostname or device name into something no longer matching
        # the machine it describes.
        print(_json.dumps(report, indent=2, default=str))
    else:
        report = _doctor_collect()
        _doctor_print(report)

    if report["problems"]:
        sys.exit(1)


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
        safe_mode=args.safe_mode,
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
                [sys.executable, "-m", "pip", "install", "gpumesh"],
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
    safe_print(dim("   Ready — this machine will offer its compute to the pool."))
    safe_print(dim("   (Ctrl+C to stop the worker)"))
    safe_print()

    _run_worker_with_report(url, token, args.timeout,
                            safe_mode=args.safe_mode)


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
            "  gpumesh serve                    Start a coordinator (127.0.0.1 only)\n"
            "  gpumesh serve --host 0.0.0.0     ...and let OTHER machines join it\n"
            "  gpumesh join http://HOST:8000    Offer this machine's GPU to the mesh\n"
            "  gpumesh submit run.py --payloads data.json   Submit a job\n"
            "  gpumesh status JOB_ID            Check job progress\n"
            "  gpumesh radar                    Scan for nearby devices\n"
            "  gpumesh doctor                   Diagnose this machine's setup\n"
            "  gpumesh setup                    Interactive setup wizard\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--version", action="version",
                    version=f"gpumesh {__version__} ({platform.python_version()}, {platform.system()})",
                    help="show version and exit")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="enable debug-level logging")
    ap.add_argument("--json-logs", action="store_true",
                    help="output logs as JSON lines (for Docker)")
    ap.add_argument("--strict", action="store_true",
                    help="refuse to unpickle results sent by workers. "
                         "Unpickling a result runs the worker's code on THIS "
                         "machine; --strict trades that away, so JSON-encodable "
                         "results still work and anything else (tensors, numpy "
                         "arrays, DataFrames) raises instead of executing. "
                         "This is a TOP-LEVEL flag, so it goes before the "
                         "subcommand: 'gpumesh --strict status JOB_ID', not "
                         "'gpumesh status --strict JOB_ID' (which is a parse "
                         "error). GPUMESH_STRICT_RESULTS=1 works anywhere and "
                         "is equivalent")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser(
        "serve",
        help="start a coordinator on this machine",
        description=(
            "Start a coordinator that manages GPU jobs across the mesh.\n"
            "\n"
            "By default it listens on 127.0.0.1 only, so nothing outside this\n"
            "machine can connect. Workers on other machines need\n"
            "  gpumesh serve --host 0.0.0.0\n"
            "which is a real decision: a worker executes whatever code the\n"
            "coordinator sends it, as the OS user that started the worker.\n"
            "\n"
            "--host and --host-ip are different things:\n"
            "  --host     WHERE THE SOCKET LISTENS (who can connect at all)\n"
            "  --host-ip  WHICH ADDRESS IS PRINTED for workers to dial\n"
            "Setting --host-ip alone never opens the port up.\n"
            "\n"
            "--public is a third, larger thing again: an ngrok tunnel is\n"
            "dialled from this machine, so it reaches into the coordinator\n"
            "whatever --host says, and it is reachable from the whole\n"
            "internet rather than one network. --tailscale is not a tunnel\n"
            "at all -- it only prints this machine's tailnet address, so the\n"
            "socket has to be bound somewhere tailnet peers can reach first."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--port", type=int, default=8000,
                   help="port to listen on (default: 8000)")
    p.add_argument("--db", default=None,
                   help="database file for job storage (default: "
                        "~/.gpumesh/gpumesh.db, so queued jobs survive "
                        "restarts from any directory)")
    # Two host-shaped flags is confusing, so the help text has to carry the
    # distinction: --host is the bind (a security boundary), --host-ip is
    # cosmetic (which address gets printed).
    p.add_argument("--host", default=os.environ.get("GPUMESH_HOST", ""),
                   help="BIND address, i.e. who can connect at all (default: "
                        "127.0.0.1, this machine only). Use 0.0.0.0 to accept "
                        "workers from other machines; or set GPUMESH_HOST. "
                        "NOT the same as --host-ip")
    p.add_argument("--host-ip", default="",
                   help="ADVERTISED address only: which address is printed for "
                        "workers to dial (or set GPUMESH_HOST_IP); use when "
                        "auto-detection picks a VPN or virtual adapter. This "
                        "does NOT change what the socket binds to (see --host)")
    # Reads GPUMESH_TOKEN for parity with `join`. Without it, a deployment
    # that sets the variable once for both roles (docker compose, systemd)
    # gave the coordinator a random token while workers used the variable —
    # a mesh that could never authenticate, with no hint as to why.
    p.add_argument("--token", default=os.environ.get("GPUMESH_TOKEN", ""),
                   help="auth token (or set GPUMESH_TOKEN; random if omitted)")
    p.add_argument("--public", action="store_true",
                   help="expose this coordinator to the PUBLIC INTERNET via an "
                        "ngrok tunnel. The tunnel is dialled locally, so it "
                        "works whatever --host is set to — including the "
                        "loopback default. Prints a full-width warning")
    p.add_argument("--tailscale", action="store_true",
                   help="advertise this machine's tailnet address (auto-detects "
                        "it). This is NOT a tunnel: the socket must already be "
                        "bound to an address tailnet peers can reach, so it is "
                        "refused on the loopback default")
    p.add_argument("--no-discovery", action="store_true",
                   help="disable UDP broadcast discovery")
    p.add_argument("--safe-mode", action="store_true",
                   help="disable function distribution; scripts only")
    p.add_argument("--tls", action="store_true",
                   help="serve HTTPS instead of HTTP, generating a self-signed "
                        "certificate under ~/.gpumesh/tls on first use. This "
                        "encrypts the token and the pickled payloads on the "
                        "wire; it does NOT authenticate the coordinator unless "
                        "workers set GPUMESH_TLS_CA to a copy of the "
                        "certificate. LAN-grade, not internet-grade")
    p.add_argument("--tls-cert", default="",
                   help="use this certificate instead of a generated one "
                        "(requires --tls-key)")
    p.add_argument("--tls-key", default="",
                   help="private key for --tls-cert")
    p.add_argument("--self-worker", action="store_true", default=True,
                   help="join this machine's CPU/GPU to its own pool (default: on)")
    p.add_argument("--no-self-worker", action="store_false", dest="self_worker",
                   help="do not join this machine's compute to its own pool")
    _add_color_args(p)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("join",
                       help="offer this machine's compute to a mesh",
                       description="Connect this machine as a worker to a coordinator.")
    p.add_argument("url", help="coordinator URL (e.g. http://192.168.1.10:8000)")
    p.add_argument("--token", default=os.environ.get("GPUMESH_TOKEN", ""),
                   help="auth token (or set GPUMESH_TOKEN)")
    p.add_argument("--timeout", type=float, default=240.0,
                   help="per-task wall clock limit in seconds (default: 240)")
    p.add_argument("--safe-mode", action="store_true",
                   help="disable function distribution; scripts only")
    _add_color_args(p)
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
    p.add_argument("--wait-timeout", type=_nonneg_float, default=3600.0,
                   help="max seconds to wait for --wait (default: 3600; 0 = wait forever)")
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

    p = sub.add_parser("retry",
                       help="re-queue failed or timed-out tasks of a job",
                       description="Reset all failed tasks of a job so workers run them again.")
    p.add_argument("job_id", help="job ID to retry")
    _add_conn_args(p)
    p.set_defaults(func=cmd_retry)

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

    p = sub.add_parser(
        "doctor",
        help="check this machine's environment and print a report",
        description=(
            "Read-only environment diagnostics, formatted for pasting into a\n"
            "bug report. Checks the gpumesh install, the Python interpreter,\n"
            "torch and CUDA, cloudpickle (the wire format), the saved\n"
            "coordinator and its workers, the addresses this machine would\n"
            # ASCII only: argparse prints this with plain print(), which has
            # none of safe_print's cp1252 transliteration, so a dash here
            # would crash `gpumesh doctor --help` on a Windows console.
            "bind and advertise, and (on Windows) whether a firewall rule\n"
            "for the port exists.\n"
            "\n"
            "It changes nothing: no firewall rules are added, no connection is\n"
            "saved, nothing is installed. The mesh token is never printed.\n"
            "\n"
            "Exits 1 only for a fault on THIS machine. Having no coordinator\n"
            "configured, or an unreachable one, is reported as a warning and\n"
            "still exits 0, so `doctor` is usable as a pre-flight check."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--json", action="store_true",
                   help="emit the report as JSON instead of text")
    p.set_defaults(func=cmd_doctor)

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
    p.add_argument("--safe-mode", action="store_true",
                   help="disable function distribution; scripts only")
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
    p.add_argument("--safe-mode", action="store_true",
                   help="disable function distribution; scripts only")
    p.set_defaults(func=cmd_worker)

    args = ap.parse_args()
    # Before anything prints, including the log formatter's own colour
    # decision, which reads ansi._SUPPORTS_COLOR when it builds a handler.
    _apply_color_choice(getattr(args, "color", "auto"))
    setup_logging(verbose=args.verbose, json_logs=args.json_logs)
    # Exported rather than threaded through every call: results are decoded in
    # three places (api.py, accelerate.py, client.py) and in worker subprocesses
    # this command spawns. An environment variable reaches all of them; a
    # keyword argument would have to be re-plumbed at each one and would miss
    # the subprocess entirely.
    if getattr(args, "strict", False):
        os.environ["GPUMESH_STRICT_RESULTS"] = "1"
    if not args.cmd:
        ap.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
