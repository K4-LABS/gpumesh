"""Security features for gpumesh coordinator.

Provides token hashing, rate limiting, and IP-based protection.
"""

import hashlib
import hmac
import ipaddress
import os
import secrets
import time
import threading
from collections import defaultdict, OrderedDict


# --- Key derivation -------------------------------------------------------
#
# The coordinator holds one bearer token in memory and compares every request
# against it. Two schemes exist:
#
#   "pbkdf2_sha256"  (default)  PBKDF2-HMAC-SHA256, random per-token salt,
#                               configurable iteration count. Stored as
#                               "pbkdf2_sha256$<iterations>$<salt>$<hash>".
#   "sha256"         (legacy)   One SHA-256 round with a salt derived from the
#                               token itself. Stored as "<salt>:<hash>".
#
# The legacy scheme stays *verifiable* forever so that a hash written by an
# older gpumesh, or pinned in a config file, keeps working. It is no longer
# what `hash_token` produces unless it is asked for.
#
# Override with GPUMESH_AUTH_KDF=sha256 (or pass scheme=) if a deployment
# needs the old, fast, weak derivation back.
KDF_PBKDF2 = "pbkdf2_sha256"
KDF_LEGACY_SHA256 = "sha256"
DEFAULT_KDF = KDF_PBKDF2

# 200k rounds is ~80ms on a 2020-era laptop core. The number is a compromise
# nobody should have to guess at: high enough that offline cracking of a
# hand-chosen token costs real money, low enough that an unauthenticated
# request cannot be used to pin a coordinator CPU. Successful verifications
# are memoised (see _VerifyCache) so the steady-state cost of a busy mesh is
# one HMAC per request, not one PBKDF2.
DEFAULT_KDF_ITERATIONS = 200_000
MIN_KDF_ITERATIONS = 1_000

_PBKDF2_PREFIX = KDF_PBKDF2 + "$"


def _resolve_kdf(scheme: str = None) -> str:
    """Pick the derivation scheme: explicit argument, then env, then default."""
    if scheme:
        return scheme
    env = (os.environ.get("GPUMESH_AUTH_KDF") or "").strip().lower()
    if env in (KDF_PBKDF2, "pbkdf2"):
        return KDF_PBKDF2
    if env in (KDF_LEGACY_SHA256, "legacy"):
        return KDF_LEGACY_SHA256
    if env:
        raise ValueError(
            f"GPUMESH_AUTH_KDF={env!r} is not a known scheme. "
            f"Use {KDF_PBKDF2!r} (default) or {KDF_LEGACY_SHA256!r} (legacy)."
        )
    return DEFAULT_KDF


def _resolve_iterations(iterations: int = None) -> int:
    """Pick the PBKDF2 cost factor: explicit argument, then env, then default.

    Clamped at MIN_KDF_ITERATIONS. A cost factor set to 1 by a typo'd
    environment variable would silently turn the KDF back into a single round,
    which is the exact failure this function exists to prevent.
    """
    if iterations is None:
        raw = (os.environ.get("GPUMESH_AUTH_KDF_ITERATIONS") or "").strip()
        if raw:
            try:
                iterations = int(raw)
            except ValueError:
                raise ValueError(
                    f"GPUMESH_AUTH_KDF_ITERATIONS={raw!r} is not an integer."
                )
        else:
            iterations = DEFAULT_KDF_ITERATIONS
    if iterations < MIN_KDF_ITERATIONS:
        raise ValueError(
            f"PBKDF2 iterations must be at least {MIN_KDF_ITERATIONS}, "
            f"got {iterations}."
        )
    return iterations


def is_loopback(ip: str) -> bool:
    """Return True if ``ip`` is a loopback address.

    Covers the whole ``127.0.0.0/8`` range (not just ``127.0.0.1``), ``::1``,
    and IPv4-mapped forms such as ``::ffff:127.0.0.1`` — a dual-stack listener
    on Windows and Linux reports connections from ``127.0.0.1`` in exactly that
    mapped form, and ``ipaddress.IPv6Address.is_loopback`` is False for it, so
    the mapping has to be unwrapped by hand.

    Anything unparseable (empty string, a hostname, a spoofed header value)
    is not loopback. Failing closed here matters: this function decides who
    skips rate limiting.
    """
    try:
        addr = ipaddress.ip_address(ip.strip())
    except (ValueError, AttributeError):
        return False
    if addr.version == 6:
        mapped = addr.ipv4_mapped
        if mapped is not None:
            return mapped.is_loopback
    return addr.is_loopback


def _hash_legacy_sha256(token: str, salt: str = None) -> str:
    """The pre-3.2 derivation: one SHA-256 round, salt derived from the token.

    Kept so that a hash produced by an older gpumesh still verifies, and so
    that a deployment that has measured the PBKDF2 cost and does not want it
    can ask for this by name. Be honest about what it buys: a salt that is a
    pure function of its input cannot make two hashes of the same token
    differ, so it gives no protection against precomputation, and a single
    SHA-256 round is fast to brute-force offline. It is fine for the tokens
    gpumesh generates -- ``secrets.token_urlsafe(12)`` is ~72 bits, which no
    rainbow table or GPU cracker will reach -- and it is *not* fine for a
    short or guessable ``--token`` the user chose by hand.
    """
    if salt is None:
        salt = hashlib.sha256(token.encode()).hexdigest()[:16]
    hash_val = hashlib.sha256(f"{salt}{token}".encode()).hexdigest()
    return f"{salt}:{hash_val}"


def _hash_pbkdf2(token: str, salt: str = None, iterations: int = None) -> str:
    """Derive a token hash with PBKDF2-HMAC-SHA256.

    Returns ``"pbkdf2_sha256$<iterations>$<salt>$<hash>"``. The salt is 16
    random bytes (32 hex chars) unless one is supplied, so two coordinators
    started with the same token hold different hashes -- which is the property
    the legacy deterministic salt could never have.

    A supplied salt makes the call deterministic; that exists for tests and
    for re-deriving against a stored hash, not as a mode to run in.
    """
    iterations = _resolve_iterations(iterations)
    if salt is None:
        salt = secrets.token_hex(16)
    derived = hashlib.pbkdf2_hmac(
        "sha256", token.encode(), salt.encode(), iterations
    ).hex()
    return f"{_PBKDF2_PREFIX}{iterations}${salt}${derived}"


def hash_token(token: str, salt: str = None, *, scheme: str = None,
               iterations: int = None) -> str:
    """Hash a token for in-memory comparison by the coordinator.

    Defaults to PBKDF2-HMAC-SHA256 with a random per-token salt and a
    configurable cost factor (``GPUMESH_AUTH_KDF_ITERATIONS``, default
    200000). Pass ``scheme="sha256"`` -- or set ``GPUMESH_AUTH_KDF=sha256`` --
    for the legacy single-round derivation.

    The result is never written to the database. The coordinator derives it
    once at startup from the token it was given and keeps it in memory; see
    :class:`SecurityManager`. Workers persist the *plain* token, which is why
    a random salt is safe here: nothing has to re-derive the same hash after a
    restart.
    """
    if _resolve_kdf(scheme) == KDF_LEGACY_SHA256:
        return _hash_legacy_sha256(token, salt)
    return _hash_pbkdf2(token, salt, iterations)


def verify_token(token: str, stored: str) -> bool:
    """Verify a token against its stored hash, in any of the three formats.

    Format is detected from the string, not from configuration, so a
    coordinator running with ``GPUMESH_AUTH_KDF=sha256`` still verifies a
    PBKDF2 hash and the other way round. All three comparisons go through
    ``hmac.compare_digest``.

      * ``pbkdf2_sha256$<iterations>$<salt>$<hash>`` -- current
      * ``<salt>:<hash>``                            -- legacy salted SHA-256
      * ``<hash>``                                   -- legacy unsalted SHA-256

    A malformed PBKDF2 string returns False rather than raising: this runs on
    the request path, and an exception there is a 500 that tells an
    unauthenticated caller something about the coordinator's state.
    """
    if not isinstance(stored, str):
        return False

    if stored.startswith(_PBKDF2_PREFIX):
        parts = stored.split("$")
        if len(parts) != 4:
            return False
        _, iter_text, salt, hash_val = parts
        try:
            iterations = int(iter_text)
        except ValueError:
            return False
        if iterations < 1:
            return False
        computed = hashlib.pbkdf2_hmac(
            "sha256", token.encode(), salt.encode(), iterations
        ).hex()
        return hmac.compare_digest(computed, hash_val)

    if ":" not in stored:
        # Legacy format without salt
        raw_hash = hashlib.sha256(token.encode()).hexdigest()
        return hmac.compare_digest(raw_hash, stored)

    salt, hash_val = stored.split(":", 1)
    computed = hashlib.sha256(f"{salt}{token}".encode()).hexdigest()
    return hmac.compare_digest(computed, hash_val)


class _VerifyCache:
    """Memoise successful token verifications so PBKDF2 is paid once.

    Without this, raising the KDF cost would hand anyone who can reach the
    port a CPU amplifier: every unauthenticated request would cost the
    coordinator 200000 SHA-256 rounds, and a worker polling for tasks would
    pay it several times a second for a token that has not changed.

    What is cached is a keyed digest of the token, never the token itself:
    ``HMAC-SHA256(process_key, token)``, where ``process_key`` is 32 random
    bytes generated at construction and never leaves the process. A heap dump
    of the cache therefore yields nothing an attacker can replay against
    another coordinator, or against this one after a restart.

    Only *successes* are cached. Caching failures would let a brute-forcer
    replay a wrong guess for free, and would sit in front of the rate limiter,
    which is the thing that is supposed to be counting those guesses.
    """

    def __init__(self, maxsize: int = 64):
        self._key = secrets.token_bytes(32)
        self._maxsize = maxsize
        self._entries = OrderedDict()
        self._lock = threading.Lock()

    def _digest(self, token: str) -> str:
        return hmac.new(self._key, token.encode(), hashlib.sha256).hexdigest()

    def check(self, token: str, stored: str) -> bool:
        """Return True if ``token`` previously verified against ``stored``."""
        digest = self._digest(token)
        with self._lock:
            hit = self._entries.get(digest)
            if hit is None or not hmac.compare_digest(hit, stored):
                return False
            self._entries.move_to_end(digest)
            return True

    def remember(self, token: str, stored: str) -> None:
        digest = self._digest(token)
        with self._lock:
            self._entries[digest] = stored
            self._entries.move_to_end(digest)
            while len(self._entries) > self._maxsize:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class RateLimiter:
    """Rate limiter to prevent brute-force attacks.

    Tracks failed attempts per IP and temporarily blocks IPs
    that exceed the maximum allowed attempts.

    Loopback is exempt by default (``exempt_loopback``).  The reasoning is
    that the lockout has nothing left to protect there.  Rate limiting exists
    to stop someone guessing the token over the network; anybody who can open
    a socket from 127.0.0.1 is already executing code on the coordinator host,
    where the token is sitting in the process's argv, in the environment, and
    in ``~/.gpumesh/config.json`` at mode 0600 owned by that same user.  They
    read it, they do not guess it.  So a loopback lockout costs an attacker
    nothing and costs the operator their whole mesh: the coordinator's own
    self-worker and the operator's own CLI both arrive over loopback, and five
    mistyped tokens used to wedge a healthy mesh for fifteen minutes on the one
    machine that cannot simply be told to come from a different IP.

    Exempting outright rather than raising the ceiling, because a higher
    ceiling still ends in a lockout on the one path where a lockout is never
    the right answer — it only moves the outage from five typos to fifty.
    """

    def __init__(self, max_attempts: int = 5, window_seconds: int = 300,
                 lockout_seconds: int = 900, exempt_loopback: bool = True):
        """
        Args:
            max_attempts: Maximum failed attempts before lockout
            window_seconds: Time window for counting attempts
            lockout_seconds: How long to lockout after exceeding max attempts
            exempt_loopback: Never track or block loopback addresses
        """
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.lockout_seconds = lockout_seconds
        self.exempt_loopback = exempt_loopback
        self._attempts = defaultdict(list)  # ip -> [timestamps]
        self._lockouts = {}  # ip -> lockout_until
        self._lock = threading.Lock()

    def is_exempt(self, ip: str) -> bool:
        """Return True if ``ip`` is never rate limited."""
        return self.exempt_loopback and is_loopback(ip)

    def is_allowed(self, ip: str) -> bool:
        """Check if an IP is allowed to make a request."""
        if self.is_exempt(ip):
            return True
        with self._lock:
            now = time.time()
            
            # Check if IP is locked out
            if ip in self._lockouts:
                if now < self._lockouts[ip]:
                    return False
                else:
                    del self._lockouts[ip]
                    self._attempts[ip] = []
            
            return True
    
    def record_failure(self, ip: str) -> bool:
        """Record a failed attempt. Returns True if IP should be locked out."""
        if self.is_exempt(ip):
            # Not merely "never locked out" — never *counted*, so an exempt IP
            # cannot accumulate history that would bite later if the exemption
            # were turned off mid-run.
            return False
        with self._lock:
            now = time.time()
            
            # Clean old attempts outside the window
            self._attempts[ip] = [
                t for t in self._attempts[ip] 
                if now - t < self.window_seconds
            ]
            
            # Add current attempt
            self._attempts[ip].append(now)
            
            # Check if we should lockout
            if len(self._attempts[ip]) >= self.max_attempts:
                self._lockouts[ip] = now + self.lockout_seconds
                self._attempts[ip] = []
                return True
            
            return False
    
    def record_success(self, ip: str):
        """Record a successful attempt (clears failure history)."""
        with self._lock:
            self._attempts[ip] = []
    
    def get_remaining_attempts(self, ip: str) -> int:
        """Get remaining attempts before lockout."""
        if self.is_exempt(ip):
            return self.max_attempts
        with self._lock:
            now = time.time()
            recent = [
                t for t in self._attempts.get(ip, [])
                if now - t < self.window_seconds
            ]
            return max(0, self.max_attempts - len(recent))
    
    def get_lockout_remaining(self, ip: str) -> float:
        """Get remaining lockout time in seconds (0 if not locked out)."""
        if self.is_exempt(ip):
            return 0.0
        with self._lock:
            if ip in self._lockouts:
                remaining = self._lockouts[ip] - time.time()
                return max(0, remaining)
            return 0


class IPAllowlist:
    """Optional IP allowlist for restricting access."""
    
    def __init__(self, allowed_ips: list = None):
        self._allowed_ips = set(allowed_ips) if allowed_ips else None
        self._lock = threading.Lock()
    
    def is_allowed(self, ip: str) -> bool:
        """Check if an IP is allowed. Returns True if no allowlist is set."""
        if self._allowed_ips is None:
            return True
        with self._lock:
            return ip in self._allowed_ips
    
    def add_ip(self, ip: str):
        """Add an IP to the allowlist."""
        with self._lock:
            if self._allowed_ips is None:
                self._allowed_ips = set()
            self._allowed_ips.add(ip)
    
    def remove_ip(self, ip: str):
        """Remove an IP from the allowlist."""
        with self._lock:
            if self._allowed_ips is not None:
                self._allowed_ips.discard(ip)
    
    def set_allowlist(self, ips: list):
        """Set the complete allowlist."""
        with self._lock:
            self._allowed_ips = set(ips) if ips else None


class SecurityManager:
    """Combined security manager for the coordinator."""
    
    def __init__(self, token: str, max_attempts: int = 5,
                 allowed_ips: list = None, *, kdf: str = None,
                 kdf_iterations: int = None):
        """
        Args:
            token: The mesh bearer token, hashed once here and then dropped.
            max_attempts: Failed attempts per IP before lockout.
            allowed_ips: Optional IP allowlist.
            kdf: Derivation scheme -- ``"pbkdf2_sha256"`` (default) or
                ``"sha256"`` (legacy). Falls back to ``GPUMESH_AUTH_KDF``.
            kdf_iterations: PBKDF2 cost factor. Falls back to
                ``GPUMESH_AUTH_KDF_ITERATIONS``, then 200000.
        """
        self.kdf = _resolve_kdf(kdf)
        self.token_hash = hash_token(token, scheme=self.kdf,
                                     iterations=kdf_iterations)
        self.rate_limiter = RateLimiter(max_attempts=max_attempts)
        self.ip_allowlist = IPAllowlist(allowed_ips)
        # See _VerifyCache: the KDF cost is paid on the first request bearing
        # a given token and never again, so a polling worker does not turn the
        # cost factor into a self-inflicted denial of service.
        self._verify_cache = _VerifyCache()
    
    def verify_request(self, token: str, ip: str) -> tuple[bool, str]:
        """Verify a request. Returns (allowed, error_message).

        The error message is the only thing the caller ever sees — it goes
        straight into the 401 body (``server.CoordinatorHandler._authed``) and
        from there onto somebody's terminal. There are three distinct reasons
        to say no, and each one calls for a different action from the person
        reading it, so each one has to be unmistakable on sight:

          * not on the allowlist   -> your address will never be accepted
          * rate limited           -> wait; do not go hunting for a new token
          * bad token              -> the token really is wrong

        The rate-limit branch used to be the trap. "Too many attempts. Try
        again in 404s" reads as a verdict on the token, so an operator who had
        typo'd twice and then pasted the *correct* token was told, in effect,
        that their correct token was rejected — and went looking for a
        coordinator bug. Say plainly that the token was not examined.

        It was not examined on purpose. Checking it under lockout and reporting
        "your token is right, but wait" would hand a brute-forcer a free
        correctness oracle: they could keep guessing at full speed through the
        lockout and learn the instant they hit, which is exactly the thing the
        lockout exists to prevent. So the honest message is "not checked",
        not "checked and correct".

        These strings are deliberately plain ASCII. They travel back as a JSON
        401 body and are printed by whatever CLI made the request, on consoles
        that are still cp1252 on Windows. An em dash in an authentication
        error is a way for the error to become a UnicodeEncodeError instead.
        """
        # Check IP allowlist
        if not self.ip_allowlist.is_allowed(ip):
            return False, f"IP not allowed: {ip} is not on the coordinator's allowlist"

        # Check rate limit
        if not self.rate_limiter.is_allowed(ip):
            remaining = int(self.rate_limiter.get_lockout_remaining(ip))
            return False, (
                f"Too many attempts. This IP is rate limited for another "
                f"{remaining}s. This is NOT a token rejection: the token in "
                f"this request was not checked at all. Wait {remaining}s and "
                f"retry with the same token."
            )

        # Verify token. The cache short-circuits the KDF for a token that has
        # already verified against this exact hash; a miss falls through to
        # the full derivation.
        if not (self._verify_cache.check(token, self.token_hash)
                or verify_token(token, self.token_hash)):
            locked_out = self.rate_limiter.record_failure(ip)
            if self.rate_limiter.is_exempt(ip):
                # The operator's own machine. Never locked out, so do not
                # threaten a countdown that will never arrive.
                return False, (
                    "Invalid token. This is a token rejection, not a rate "
                    "limit: loopback is never locked out, so retry as often "
                    "as you need."
                )
            if locked_out:
                return False, (
                    f"Invalid token. That was attempt "
                    f"{self.rate_limiter.max_attempts}, so this IP is now rate "
                    f"limited for {self.rate_limiter.lockout_seconds}s."
                )
            remaining = self.rate_limiter.get_remaining_attempts(ip)
            plural = "attempt" if remaining == 1 else "attempts"
            return False, (
                f"Invalid token. {remaining} {plural} remaining before this IP "
                f"is rate limited for {self.rate_limiter.lockout_seconds}s."
            )

        # Success - clear failure history
        self._verify_cache.remember(token, self.token_hash)
        self.rate_limiter.record_success(ip)
        return True, ""
