"""Shared utility functions for gpumesh."""

import os
import platform
import socket
import subprocess
import sys


def _is_private_ip(ip: str) -> bool:
    return (
        ip.startswith("10.")
        or ip.startswith("192.168.")
        or (ip.startswith("172.") and 16 <= int(ip.split(".")[1]) <= 31)
    )


def _is_loopback_or_special(ip: str) -> bool:
    if ip.startswith("127."):
        return True
    if ip.startswith("169.254."):  # link-local
        return True
    if ip.startswith("100."):  # Tailscale / CGNAT
        return True
    return False


def get_lan_ip() -> str:
    """Get the LAN IP address.

    Prefer a real private-ranged IPv4 address on the default outbound
    interface, falling back to other non-special NICs, then loopback only
    as a last resort.

    Returns:
        The local network IP address (string).
    """
    try:
        udp_ip = None
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            udp_ip = s.getsockname()[0]
        except OSError:
            pass
        finally:
            s.close()

        candidates = []
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None):
                ip = info[4][0]
                if ":" in ip:  # skip IPv6
                    continue
                candidates.append(ip)
        except OSError:
            pass

        if udp_ip:
            candidates.insert(0, udp_ip)

        for ip in candidates:
            if _is_loopback_or_special(ip):
                continue
            if _is_private_ip(ip):
                return ip

        for ip in candidates:
            if _is_loopback_or_special(ip):
                continue
            return ip

        if udp_ip:
            return udp_ip
    except Exception:
        pass
    return "127.0.0.1"


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
            print(
                f"[gpumesh] WARNING: could not add firewall rule (need admin). "
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

        print(
            f"[gpumesh] WARNING: could not add firewall rule. "
            f"Run 'gpumesh serve' as Administrator, or manually allow port {port}."
        )
        return False
    except Exception:
        return False


def show_firewall_hint(port: int = 8000):
    """Show a brief firewall hint if needed (only on Windows)."""
    if platform.system() != "Windows":
        return
    print(f"[gpumesh] TIP: If workers can't connect, allow port {port} through Windows Firewall.")
