"""Shared utility functions for gpumesh."""

import os
import platform
import socket
import subprocess
import sys
import threading

from .ansi import safe_print, green, yellow, red, bold, cyan, dim


def _is_private_ip(ip: str) -> bool:
    if ip.startswith("10.") or ip.startswith("192.168."):
        return True
    if ip.startswith("172."):
        try:
            second_octet = int(ip.split(".")[1])
        except (IndexError, ValueError):
            return False
        return 16 <= second_octet <= 31
    return False


def _is_loopback_or_special(ip: str) -> bool:
    if ip.startswith("127."):
        return True
    if ip.startswith("169.254."):  # link-local
        return True
    if ip.startswith("100."):  # Tailscale / CGNAT
        return True
    return False


# Ranges handed out by hypervisors, container runtimes and connection
# sharing. They are private (so _is_private_ip accepts them) but reachable
# only from this host and its own VMs — never from another machine on the
# LAN. Advertising one to a worker produces a connection *timeout*
# (WinError 10060), not a refusal, because the packets are routed nowhere.
# Rank them below real LAN addresses instead of excluding them: on a
# machine that genuinely has nothing else, one is still better than
# loopback.
_VIRTUAL_IP_PREFIXES = (
    "192.168.56.",   # VirtualBox host-only
    "192.168.99.",   # docker-machine / minikube
    "192.168.137.",  # Windows Internet Connection Sharing / mobile hotspot
    "10.0.75.",      # Docker Desktop for Windows (legacy MobyLinux)
    "172.17.", "172.18.", "172.19.", "172.20.", "172.21.",
    "172.22.", "172.23.", "172.24.", "172.25.", "172.26.",
    "172.27.", "172.28.", "172.29.", "172.30.", "172.31.",
)


def _is_virtual_adapter_ip(ip: str) -> bool:
    """True for addresses belonging to a virtual / host-only adapter."""
    return ip.startswith(_VIRTUAL_IP_PREFIXES)


def _rank_ip(ip: str) -> int:
    """Sort key for advertisable addresses; lower is better."""
    if _is_loopback_or_special(ip):
        return 3
    if _is_virtual_adapter_ip(ip):
        return 2
    if _is_private_ip(ip):
        return 0
    return 1


def _gather_candidate_ips() -> list:
    """Collect this machine's IPv4 addresses, default-route address first."""
    candidates = []
    udp_ip = None
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        udp_ip = s.getsockname()[0]
    except OSError:
        pass
    finally:
        s.close()
    if udp_ip:
        candidates.append(udp_ip)

    # getaddrinfo on the bare hostname is a secondary source (the
    # default-route address above is the primary), but on a machine with a
    # slow or broken resolver it can block for tens of seconds — macOS CI
    # runners are the canonical example — which would hang every
    # coordinator and worker start. Give it a short deadline and fall back
    # to what we already have if the resolver does not answer in time.
    extra: list = []

    def _lookup_hostname():
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None):
                ip = info[4][0]
                if ":" in ip:  # skip IPv6
                    continue
                if ip not in extra:
                    extra.append(ip)
        except OSError:
            pass

    lookup = threading.Thread(target=_lookup_hostname, daemon=True)
    lookup.start()
    lookup.join(timeout=2.0)
    candidates.extend(extra)
    return candidates


def lan_ip_candidates() -> list:
    """Return addresses workers could use to reach this machine, best first.

    Ranked so a real LAN address always beats a hypervisor/VPN/container
    host-only address, which this machine can reach but no other can.
    ``sorted`` is stable, so within a rank the default-route address (found
    first) still wins.
    """
    try:
        found = _gather_candidate_ips()
    except Exception:
        return []
    return sorted(
        (ip for ip in found if not _is_loopback_or_special(ip)),
        key=_rank_ip,
    )


def get_lan_ip() -> str:
    """Get the address workers should use to reach this machine.

    ``GPUMESH_HOST_IP`` overrides detection entirely. Set it when this
    machine has several interfaces and auto-detection picks the wrong one —
    auto-detection cannot always tell a real LAN interface from a VPN or
    hypervisor one, and a wrong pick makes every worker time out.

    Returns:
        The local network IP address (string).
    """
    override = os.environ.get("GPUMESH_HOST_IP", "").strip()
    if override:
        return override
    try:
        ranked = lan_ip_candidates()
        if ranked:
            return ranked[0]
        # Every interface looked loopback / link-local / CGNAT. Prefer
        # whatever the default route reported over giving up on loopback.
        raw = _gather_candidate_ips()
        if raw:
            return raw[0]
    except Exception:
        pass
    return "127.0.0.1"


def local_ip_for_peer(peer_ip: str):
    """Return this machine's address on the route to ``peer_ip``.

    ``connect()`` on a UDP socket transmits nothing — it only asks the
    kernel to bind the local end using the routing table. The result is the
    source address a peer at ``peer_ip`` would actually see us as, which is
    a fact about the route rather than a guess about which local interface
    looks most like a LAN. Prefer this over :func:`get_lan_ip` whenever the
    peer is known.

    Returns None when no route to that peer exists.
    """
    if not peer_ip:
        return None
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((peer_ip, 9))  # discard port; no packet is sent
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def coordinator_url_candidates(peer_ip: str, port: int, limit: int = 4) -> list:
    """Rank the URLs a peer at ``peer_ip`` could use to reach this machine.

    The route-derived address leads: the kernel already knows which
    interface reaches that peer, so it beats every heuristic. An explicit
    ``GPUMESH_HOST_IP`` outranks even that, because a human who pinned an
    address knows something the routing table does not (a port forward, or
    a NAT the peer sits behind). The general candidates follow as fallbacks,
    covering asymmetric routing and a peer that changed networks between its
    beacon and the claim.

    The caller sends the whole list; the peer picks the one that works.
    """
    ordered = []
    routed = local_ip_for_peer(peer_ip)
    if routed and not _is_loopback_or_special(routed):
        ordered.append(routed)
    override = os.environ.get("GPUMESH_HOST_IP", "").strip()
    if override:
        if override in ordered:
            ordered.remove(override)
        ordered.insert(0, override)
    for ip in lan_ip_candidates():
        if ip not in ordered:
            ordered.append(ip)
    return [f"http://{ip}:{port}" for ip in ordered[:limit]]


def show_ip_alternatives(chosen: str, port: int = 8000):
    """List the other addresses workers could use, when detection is ambiguous.

    Auto-detection cannot reliably tell a real LAN interface from a VPN or
    hypervisor one. When a worker's registration times out (WinError 10060)
    the advertised address was unroutable from that machine, and one of
    these is the fix — so show them up front instead of after the failure.
    """
    if os.environ.get("GPUMESH_HOST_IP", "").strip():
        return
    candidates = lan_ip_candidates()
    if _is_virtual_adapter_ip(chosen):
        safe_print(yellow(
            f"[!] WARNING: {chosen} looks like a virtual adapter (VM, Docker or "
            f"connection sharing)."
        ))
        safe_print(dim("   Other machines usually cannot reach it — their "
                       "connection will time out."))
    others = [ip for ip in candidates if ip != chosen]
    if not others:
        return
    safe_print(dim("   This machine has other addresses. If a worker times out, "
                   "try one of these instead:"))
    for ip in others:
        note = ("   (virtual adapter — usually unreachable from other machines)"
                if _is_virtual_adapter_ip(ip) else "")
        safe_print(dim(f"     http://{ip}:{port}{note}"))
    safe_print(dim("   Pin one with --host-ip <IP>, or set GPUMESH_HOST_IP=<IP>."))


def try_add_firewall_rule(port: int = 8000) -> bool:
    """Try to add a Windows Firewall rule for gpumesh.

    Returns:
        True if the rule was added or already exists, False otherwise.
    """
    if platform.system() != "Windows":
        return True  # Not Windows, no firewall rules needed

    try:
        import ctypes

        if os.name == "nt" and not ctypes.windll.shell32.IsUserAnAdmin():
            safe_print(
                f"{bold('[gpumesh]')} {yellow('WARNING')}: could not add firewall rule (need admin). "
                f"Run 'gpumesh serve' as Administrator, or manually allow port {port}."
            )
            return False

        commands = [
            [
                "netsh", "advfirewall", "firewall", "add", "rule",
                f"name=gpumesh-{port}",
                "dir=in", "action=allow",
                "protocol=TCP", f"localport={port}",
                "profile=any", "edge=yes",
            ],
            [
                "netsh", "advfirewall", "firewall", "add", "rule",
                "name=gpumesh-discovery-udp",
                "dir=in", "action=allow",
                "protocol=UDP", "localport=48900",
                "profile=any", "edge=yes",
            ],
        ]
        for cmd in commands:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                return True

        exe = sys.executable
        if exe:
            prog = os.path.basename(exe).lower()
            if prog in ("py.exe", "python.exe", "pythonw.exe"):
                result = subprocess.run(
                    [
                        "netsh", "advfirewall", "firewall", "add", "rule",
                        f"name=gpumesh-exe-{port}",
                        "dir=in", "action=allow",
                        "program=" + exe,
                        "profile=any", "edge=yes",
                    ],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0:
                    return True

        safe_print(
            f"{bold('[gpumesh]')} {yellow('WARNING')}: could not add firewall rule. "
            f"Run 'gpumesh serve' as Administrator, or manually allow port {port}."
        )
        return False
    except Exception:
        return False


def show_firewall_hint(port: int = 8000):
    """Show a brief firewall hint if needed (only on Windows)."""
    if platform.system() != "Windows":
        return
    safe_print(f"{bold('[gpumesh]')} {yellow('TIP:')} If workers can't connect, allow port {port} through Windows Firewall.")
