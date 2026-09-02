"""UDP-based peer discovery for gpumesh.

Workers broadcast a beacon every 2 seconds. Coordinators listen for
beacons and maintain a live list of nearby peers.

The beacon is a JSON datagram sent to the subnet broadcast address on
an auto-picked ephemeral port.  Both sides use SO_BROADCAST.

Fallback: if broadcast is blocked (firewall, different subnets), users
can fall back to the manual `gpumesh join URL --token TOKEN` flow.

Thread safety:
    ``start()`` / ``stop()`` are **not** thread-safe — call them from a
    single thread only (typically the main thread).  ``peers()`` and
    ``cleanup_stale()`` are safe to call from any thread.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import threading
import time
from typing import Callable, cast


# -- constants ------------------------------------------------------------

BEACON_MAGIC = "gpumesh"          # prefix to filter non-gpumesh broadcasts
BROADCAST_PORT = 48900            # fixed well-known port for UDP discovery
BEACON_INTERVAL = 2.0             # seconds between broadcasts
PEER_STALE_AFTER = 8.0            # seconds without beacon → peer gone
BROADCAST_ADDR_FALLBACK = "255.255.255.255"
BROADCAST_REFRESH_EVERY = 30      # recompute broadcast address every N sends
CLEANUP_EVERY = 50                # run cleanup_stale every N recv iterations
ROLE_TYPES = {
    "worker": "gpumesh_worker",
    "coordinator": "gpumesh_coordinator",
}


# -- helpers ---------------------------------------------------------------

def netmask_for(local_ip: str) -> str | None:
    """The IPv4 netmask configured on the interface holding ``local_ip``.

    ``None`` when it cannot be determined. psutil is an optional extra
    (``gpumesh[sysinfo]``), so a machine without it is the ordinary case, not
    an error — the caller falls back.

    psutil rather than parsing ``ipconfig`` / ``ip addr`` output: that text is
    localised, so the mask line reads "Subnet Mask" on one machine and
    "Máscara de subred" on the next, and a parser that misses it picks the
    broadcast address of the wrong network — which is the failure this
    function exists to remove, reintroduced by its own fix. psutil reads the
    same value from the OS API as a number.
    """
    try:
        import psutil
    except ImportError:
        return None
    try:
        for addrs in psutil.net_if_addrs().values():
            for addr in addrs:
                if (addr.family == socket.AF_INET
                        and addr.address == local_ip
                        and addr.netmask):
                    return addr.netmask
    except (OSError, AttributeError):
        # Best-effort probe: interface details may be unavailable on some
        # platforms/environments, so fall back to caller's default path.
        return None
    return None


def get_broadcast_address() -> str:
    """Determine the subnet broadcast address.

    Computed from the interface's real address and netmask whenever the
    netmask can be read. The /24 guess below is the fallback, not the rule:
    it is right for most home networks and wrong for every /16 and /23, where
    the address it produces is an ordinary host on the subnet rather than its
    broadcast — so the beacon reaches nobody and discovery silently reports no
    peers on a network where peers are in fact reachable.

    Falls back to 255.255.255.255 when even the local address cannot be found.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
    except OSError:
        return BROADCAST_ADDR_FALLBACK
    finally:
        s.close()

    netmask = netmask_for(local_ip)
    if netmask:
        try:
            return str(
                ipaddress.IPv4Interface(f"{local_ip}/{netmask}")
                .network.broadcast_address
            )
        except ValueError:
            pass

    # No netmask available. The /24 assumption works for most home and office
    # networks, and where it does not, 255.255.255.255 still reaches the local
    # subnet on most OSes.
    parts = local_ip.split(".")
    if len(parts) == 4:
        return f"{parts[0]}.{parts[1]}.{parts[2]}.255"
    return BROADCAST_ADDR_FALLBACK


def get_ephemeral_port() -> int:
    """Ask the OS for a free UDP port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def build_beacon(payload: dict[str, object]) -> bytes:
    """Build a beacon datagram:  <magic>\\n<json>"""
    data = json.dumps(payload).encode()
    return f"{BEACON_MAGIC}\n".encode() + data


def parse_beacon(data: bytes) -> dict[str, object] | None:
    """Parse a beacon datagram.  Returns None if not a valid gpumesh beacon."""
    try:
        text = data.decode()
        if not text.startswith(BEACON_MAGIC + "\n"):
            return None
        json_str = text[len(BEACON_MAGIC) + 1:]
        payload = json.loads(json_str)
        return cast(dict[str, object], payload) if isinstance(payload, dict) else None
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _validate_role(role: str) -> str:
    if role not in ROLE_TYPES:
        supported = ", ".join(sorted(ROLE_TYPES))
        raise ValueError(f"unsupported discovery role {role!r}; expected {supported}")
    return role


def _payload_role(data: dict[str, object]) -> str | None:
    """Return a beacon role, treating legacy untyped beacons as workers."""
    beacon_type = data.get("type", ROLE_TYPES["worker"])
    for role, role_type in ROLE_TYPES.items():
        if beacon_type == role_type:
            return role
    return None


# -- beacon (worker side) -------------------------------------------------

class Beacon:
    """Broadcasts this machine's presence on the local network.

    Usage::

        beacon = Beacon(device="cuda", device_name="RTX 3080",
                        score=85.2, api_port=8000)
        beacon.start()
        ...
        beacon.stop()

    ``start()`` / ``stop()`` are **not** thread-safe — call from a single
    thread only.
    """

    def __init__(
        self,
        device: str = "cpu",
        device_name: str = "",
        score: float = 0.0,
        api_port: int = 8000,
        claim_port: int = 0,
        hostname: str | None = None,
        port: int = BROADCAST_PORT,
        interval: float = BEACON_INTERVAL,
        role: str = "worker",
    ):
        role = _validate_role(role)
        self._claim_port = claim_port
        # No ``platform`` key. It used to carry platform.platform() — the full
        # OS name, release and build — broadcast unencrypted to every machine
        # on the subnet every two seconds, with no opt-out short of turning
        # discovery off. Nothing consumed it: Peer still parses the field for
        # beacons from older workers, but no caller in this package reads
        # ``peer.platform``, and the radar has never displayed it. An OS
        # fingerprint handed to the whole network for zero benefit is not a
        # trade worth keeping, so it stops being sent.
        self._payload = {
            "type": ROLE_TYPES[role],
            "hostname": hostname or socket.gethostname(),
            "device": device,
            "device_name": device_name,
            "score": score,
            "api_port": api_port,
            "claim_port": claim_port,
        }
        self._port = port or get_ephemeral_port()
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._port

    def start(self):
        """Start broadcasting in a background daemon thread.

        Socket setup and the first broadcast happen synchronously so setup,
        bind, and immediate network errors propagate to the caller.
        """
        if self._thread and self._thread.is_alive():
            return

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(1.0)
            sock.bind(("", 0))
            broadcast_addr = get_broadcast_address()
            datagram = build_beacon(self._payload)
            self._broadcast(sock, datagram, broadcast_addr)
        except Exception:
            sock.close()
            raise

        self._stop.clear()
        thread = threading.Thread(target=self._loop, daemon=True,
                                  name="gpumesh-beacon",
                                  args=(sock, datagram, broadcast_addr))
        self._thread = thread
        try:
            thread.start()
        except Exception:
            self._thread = None
            self._stop.set()
            sock.close()
            raise

    def stop(self):
        """Stop broadcasting and wait for the thread to finish."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _broadcast(self, sock: socket.socket, datagram: bytes,
                   broadcast_addr: str):
        # The subnet broadcast already went out with the first send; the
        # 255.255.255.255 fallback is redundant, and on machines without a
        # route for it (macOS CI runners are the canonical example) the
        # second send raises "No route to host". A discovery nicety must not
        # crash the beacon, so drop that error and keep the primary send's
        # behavior (start() still surfaces it if the subnet broadcast fails).
        sock.sendto(datagram, (broadcast_addr, self._port))
        if broadcast_addr != BROADCAST_ADDR_FALLBACK:
            try:
                sock.sendto(datagram, (BROADCAST_ADDR_FALLBACK, self._port))
            except OSError:
                pass

    def _loop(self, sock: socket.socket, datagram: bytes,
              broadcast_addr: str):
        send_count = 1
        try:
            while not self._stop.wait(self._interval):
                try:
                    self._broadcast(sock, datagram, broadcast_addr)
                except OSError:
                    pass  # network down or firewall — keep trying
                send_count += 1
                if send_count % BROADCAST_REFRESH_EVERY == 0:
                    broadcast_addr = get_broadcast_address()
        finally:
            sock.close()


# -- listener (coordinator side) -------------------------------------------

class Peer:
    """Represents a discovered peer (worker or coordinator).

    All attributes are read/written under the listener's lock except
    ``last_seen`` which is also read by ``peers()`` after releasing the
    lock (safe under CPython's GIL for simple attribute reads).
    """

    __slots__ = ("hostname", "role", "device", "device_name", "score",
                 "api_port", "claim_port", "addr", "last_seen", "platform")

    def __init__(self, data: dict[str, object], addr: tuple[str, int]):
        role = _payload_role(data)
        if role is None:
            raise ValueError(f"invalid discovery beacon type: {data.get('type')!r}")
        self.role = role
        self.hostname = str(data.get("hostname", "unknown"))[:255]
        self.device = str(data.get("device", "cpu"))[:31]
        self.device_name = str(data.get("device_name", ""))[:127]
        try:
            self.score = float(data.get("score", 0.0))
        except (TypeError, ValueError):
            self.score = 0.0
        try:
            port = int(data.get("api_port", 8000))
            self.api_port = port if 0 <= port <= 65535 else 8000
        except (TypeError, ValueError):
            self.api_port = 8000
        self.platform = str(data.get("platform", ""))[:255]
        try:
            cport = int(data.get("claim_port", 0))
            self.claim_port = cport if 0 <= cport <= 65535 else 0
        except (TypeError, ValueError):
            self.claim_port = 0
        self.addr = addr  # (ip, port)
        self.last_seen = time.time()

    def update(self, data: dict[str, object], addr: tuple[str, int]):
        """Update this peer using the same validation as initial discovery."""
        updated = Peer(data, addr)
        for attribute in self.__slots__:
            setattr(self, attribute, getattr(updated, attribute))

    @property
    def ip(self) -> str:
        return self.addr[0]

    @property
    def alive(self) -> bool:
        """Check if this peer is still alive.

        NOTE: This reads ``last_seen`` without holding the listener's lock.
        It is safe to call on **copies** returned by ``Listener.peers()``,
        but NOT on live peer objects inside the listener's ``_peers`` dict
        (callers should always use ``listener.peers()`` which returns copies).
        """
        return (time.time() - self.last_seen) < PEER_STALE_AFTER

    @property
    def display_name(self) -> str:
        if self.device == "cuda":
            return self.device_name or "CUDA GPU"
        elif self.device == "mps":
            return "Apple Silicon GPU"
        else:
            return "CPU"

    def copy(self) -> Peer:
        """Return a shallow copy safe to read without holding the listener lock."""
        p = object.__new__(Peer)
        p.hostname = self.hostname
        p.role = self.role
        p.device = self.device
        p.device_name = self.device_name
        p.score = self.score
        p.api_port = self.api_port
        p.claim_port = self.claim_port
        p.platform = self.platform
        p.addr = self.addr
        p.last_seen = self.last_seen
        return p

    def __repr__(self):
        return (f"Peer({self.hostname}, {self.display_name}, "
                f"score={self.score}, {self.ip})")


class Listener:
    """Listens for gpumesh beacons on the local network.

    Usage::

        listener = Listener()
        listener.start()
        ...
        peers = listener.peers()     # list of live Peer objects
        listener.stop()

    ``start()`` / ``stop()`` are **not** thread-safe — call from a single
    thread only.  ``peers()`` and ``cleanup_stale()`` are safe to call
    from any thread.
    """

    def __init__(self, port: int = BROADCAST_PORT,
                 expected_role: str | None = None):
        # port=0 means "any free port", and it stays 0 until start() binds.
        # Resolving it here via get_ephemeral_port() would bind a socket, read
        # the port, close it, and only bind for real later — between those two
        # binds anything else on the machine can take the port, and on Linux
        # the second bind then fails with EADDRINUSE. (Windows hides the race:
        # its SO_REUSEADDR lets the second bind take the port regardless.)
        # Letting the kernel assign the port at the one bind that matters has
        # no window to lose.
        self._port = port
        self._expected_role = (_validate_role(expected_role)
                               if expected_role is not None else None)
        self._peers: dict[str, Peer] = {}   # key = "hostname:api_port"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._on_peer: Callable[[Peer], None] | None = None
        self._bind_error: OSError | None = None

    @property
    def port(self) -> int:
        return self._port

    def on_peer(self, callback: Callable[[Peer], None]):
        """Register a callback that fires when a new peer is seen.

        The callback is invoked **outside** the internal lock, so it is
        safe to call ``listener.peers()`` from within the callback.
        """
        with self._lock:
            self._on_peer = callback

    def start(self):
        """Start listening in a background daemon thread.

        Binds the UDP socket **before** spawning the thread so that
        ``OSError`` (e.g. port already in use) propagates to the caller
        instead of being silently swallowed inside the thread.

        The probe socket is kept open and passed to the receive thread
        to prevent TOCTOU port-stealing races.
        """
        if self._thread and self._thread.is_alive():
            return
        # Pre-bind to surface errors to the caller.
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except (OSError, AttributeError):
            pass
        try:
            probe.bind(("", self._port))
        except OSError as exc:
            probe.close()
            self._bind_error = exc
            raise
        # With port=0 the kernel picked the port; record what it chose so
        # callers can read `.port` and send beacons to this listener.
        self._port = probe.getsockname()[1]
        self._bind_error = None
        self._stop.clear()
        self._sock = probe
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="gpumesh-listener",
                                        args=(probe,))
        self._thread.start()

    def stop(self):
        """Stop listening, wait for the thread, and release the port.

        The port is closed here, synchronously, rather than inside the
        receive thread, so a stopped listener always frees its port. If the
        close happened only in the thread's exit path, a thread that was
        slow to wake (a fresh thread starved on a loaded machine — macOS
        CI runners included) could outlive the 3-second join and leave the
        socket bound, making the next listener on the same port fail with
        EADDRINUSE for the rest of the process.
        """
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def peers(self) -> list[Peer]:
        """Return a list of currently alive peers (shallow copies)."""
        with self._lock:
            now = time.time()
            alive = [p.copy() for p in self._peers.values()
                     if (now - p.last_seen) < PEER_STALE_AFTER]
        return alive

    def _loop(self, sock: socket.socket):
        sock.settimeout(2.0)
        iter_count = 0
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                continue

            iter_count += 1

            # Periodic cleanup of stale peers
            if iter_count % CLEANUP_EVERY == 0:
                self.cleanup_stale()

            beacon = parse_beacon(data)
            if beacon is None:
                continue

            role = _payload_role(beacon)
            if role is None or (self._expected_role is not None
                                and role != self._expected_role):
                continue

            try:
                incoming = Peer(beacon, addr)
            except ValueError:
                continue
            peer_key = f"{incoming.hostname}:{incoming.ip}:{incoming.api_port}"
            new_peer = None
            on_peer_cb = None
            with self._lock:
                if peer_key in self._peers:
                    peer = self._peers[peer_key]
                    peer.update(beacon, addr)
                else:
                    peer = incoming
                    self._peers[peer_key] = peer
                    new_peer = peer
                # Capture callback under lock to avoid TOCTOU race
                on_peer_cb = self._on_peer

            # Invoke callback OUTSIDE the lock to prevent deadlock
            # if the callback calls peers() or cleanup_stale().
            # Pass a copy to prevent races with concurrent update() calls.
            if new_peer is not None and on_peer_cb is not None:
                try:
                    on_peer_cb(new_peer.copy())
                except Exception:
                    pass

    def cleanup_stale(self):
        """Remove peers that haven't been seen recently."""
        with self._lock:
            now = time.time()
            stale = [ip for ip, p in self._peers.items()
                     if (now - p.last_seen) >= PEER_STALE_AFTER]
            for ip in stale:
                del self._peers[ip]
