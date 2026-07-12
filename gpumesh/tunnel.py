"""Optional public URL via ngrok (needs `pip install pyngrok` + an ngrok
account). Without it the coordinator is LAN-only, which is fine for
machines on the same Wi-Fi."""

from __future__ import annotations

import shutil
import subprocess


def _get_tailscale_ip() -> str | None:
    """Detect the local Tailscale IP address.

    Returns the IP string if Tailscale is installed and running,
    otherwise returns None.
    """
    tailscale_bin = shutil.which("tailscale")
    if tailscale_bin is None:
        return None
    try:
        result = subprocess.run(
            [tailscale_bin, "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        ip = result.stdout.strip()
        # Validate it looks like a Tailscale IP (100.x.x.x)
        if ip and ip.startswith("100."):
            return ip
        return None
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError):
        return None


def open_tunnel(port: int, mode: str = "auto") -> str | None:
    """Open a tunnel or detect network access.

    Args:
        port: The port the coordinator is listening on.
        mode: One of 'auto', 'tailscale', 'ngrok', 'none'.
              - 'auto': Try Tailscale first, then ngrok, then LAN-only.
              - 'tailscale': Use Tailscale IP only.
              - 'ngrok': Use ngrok public URL only.
              - 'none': No tunnel, LAN-only.

    Returns:
        The URL string if a tunnel was established, or None for LAN-only.
    """
    if mode == "none":
        return None

    if mode in ("auto", "tailscale"):
        tailscale_ip = _get_tailscale_ip()
        if tailscale_ip:
            url = f"http://{tailscale_ip}:{port}"
            print(f"[mesh] Tailscale URL: {url}")
            print(f"[mesh] friends join with: gpumesh quickjoin --token <TOKEN> --tailscale")
            return url
        if mode == "tailscale":
            print("[mesh] ERROR: Tailscale not found or not running")
            print("[mesh] Install from: https://tailscale.com/download")
            return None

    if mode in ("auto", "ngrok"):
        try:
            from pyngrok import ngrok
            tunnel = ngrok.connect(port, "http")
            print(f"[mesh] public URL: {tunnel.public_url}")
            print(f"[mesh] friends join with: gpumesh join {tunnel.public_url}"
                  " --token <TOKEN>")
            return tunnel.public_url
        except ImportError:
            if mode == "ngrok":
                print("[mesh] pyngrok not installed.")
                print("[mesh] Install with: pip install gpumesh[tunnel]")
            return None
        except Exception as e:
            print(f"[mesh] ngrok failed: {e}")
            if mode == "ngrok":
                return None

    # Fallback: LAN-only
    print(f"[mesh] coordinator listening on port {port}")
    print(f"[mesh] friends join with: gpumesh join http://<YOUR_IP>:{port} --token <TOKEN>")
    return None
