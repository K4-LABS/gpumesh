"""Shared utility functions for gpumesh."""

import socket


def get_lan_ip() -> str:
    """Get the LAN IP address.
    
    Returns:
        The local network IP address, or "127.0.0.1" if detection fails.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()
