"""Adding a second machine — what to type, and how to tell it worked.

Since the coordinator binds to 127.0.0.1 by default, a second machine cannot
join until you say so explicitly:

    gpumesh serve --host 0.0.0.0 --port 8000 --token <TOKEN>

That prints a loud warning, and it deserves one. A worker runs whatever
Python the coordinator sends it, as the OS user who started the worker, with
that user's files, GPUs, network and credentials. `--host 0.0.0.0` hands that
to everyone on the network who holds the token. Do it on a LAN you trust, or
put the mesh on Tailscale (`--tailscale`) so the network itself is the
boundary.

This script does not change anything. It reads the mesh you already have and
prints the exact command the other machine needs.

Run it on the coordinator machine:

    python examples/second_machine.py
"""

import socket
import sys

from gpumesh import mesh
from gpumesh.connection_manager import load_connection


def lan_address():
    """This machine's address on the LAN, or None if it has no route out.

    The UDP connect() transmits nothing; it just asks the kernel which local
    address it would use to reach that peer.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def main():
    saved = load_connection() or {}
    url, token = saved.get("url"), saved.get("token")
    if not url or not token:
        print("No saved connection. Start a coordinator first:")
        print('    gpumesh serve --token "$(python -c '
              "'import secrets; print(secrets.token_urlsafe(32))')\"")
        return 1

    devices = mesh.devices()
    print(f"coordinator: {url}")
    print(f"pool:        {len(devices)} device(s)")
    for d in devices:
        # devices() reports a `status` string ("alive"/"dead"); workers()
        # reports an `alive` boolean. Two views, two spellings.
        alive = f"{d.get('status', '?'):5}"
        print(f"  [{alive}] {d.get('device', '?'):5} "
              f"{d.get('device_name') or d.get('hostname', '?')}  "
              f"score={d.get('score', 0):.3f}")
    print()

    port = url.rsplit(":", 1)[-1].split("/")[0]
    host = url.split("://", 1)[-1].rsplit(":", 1)[0]
    loopback = host in ("127.0.0.1", "localhost", "[::1]")

    if loopback:
        print("This coordinator is bound to loopback, so no other machine can")
        print("reach it. That is the default. To open it up, stop it and run:")
        print()
        print(f"    gpumesh serve --host 0.0.0.0 --port {port} --token {token}")
        print()
        print("Read the NETWORK-EXPOSED COORDINATOR banner it prints before")
        print("you leave it running.")
        return 0

    ip = lan_address()
    if ip is None:
        print("This machine has no route to the network, so there is no")
        print("address to hand to a second machine.")
        return 1

    print("Run this on the second machine (same LAN, gpumesh installed):")
    print()
    print(f"    pip install gpumesh")
    print(f"    gpumesh join http://{ip}:{port} --token {token}")
    print()
    print("Then re-run this script. A second row above means it worked.")
    print("If it times out instead, the packets are not arriving:")
    print("  - firewall on this machine (Windows: run serve as Administrator)")
    print("  - the two machines are on different subnets or client-isolated wifi")
    print(f"  - a VPN or hypervisor adapter was picked; pin the right one with")
    print(f"    gpumesh serve --host 0.0.0.0 --host-ip <IP> --port {port}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
