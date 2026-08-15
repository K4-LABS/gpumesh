"""Security features for gpumesh coordinator.

Provides token hashing, rate limiting, and IP-based protection.
"""

import hashlib
import hmac
import ipaddress
import time
import threading
from collections import defaultdict


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


def hash_token(token: str, salt: str = None) -> str:
    """Hash a token using SHA-256 with a salt.

    Returns format: "salt:hash" so salt is stored alongside the hash.
    This keeps the plain token out of the coordinator's stored state.

    When no salt is provided, a deterministic salt is derived from the token
    itself so that the same token always produces the same hash across
    coordinator restarts.  This is critical: workers persist the plain token
    and expect it to keep working after the coordinator is restarted.

    Be honest about what that buys.  A salt that is a pure function of its
    input is not a salt in the sense that matters: it cannot make two hashes
    of the same token differ, so it gives no protection against precomputation.
    Together with a single SHA-256 round (no KDF, no iteration count), this is
    fast to brute-force offline.  It is fine for the tokens gpumesh generates —
    ``secrets.token_urlsafe(12)`` is ~72 bits of entropy, which no rainbow
    table or GPU cracker will reach — and it is *not* fine for a short or
    guessable ``--token`` the user chose by hand.  The defence against a weak
    token is the token's entropy, not this function.
    """
    if salt is None:
        # Deterministic salt: hash of the token ensures the same token always
        # produces the same salt, while different tokens get different salts.
        salt = hashlib.sha256(token.encode()).hexdigest()[:16]
    hash_val = hashlib.sha256(f"{salt}{token}".encode()).hexdigest()
    return f"{salt}:{hash_val}"


def verify_token(token: str, stored: str) -> bool:
    """Verify a token against its stored hash.
    
    The stored format is "salt:hash".
    """
    if ":" not in stored:
        # Legacy format without salt
        raw_hash = hashlib.sha256(token.encode()).hexdigest()
        return hmac.compare_digest(raw_hash, stored)
    salt, hash_val = stored.split(":", 1)
    computed = hashlib.sha256(f"{salt}{token}".encode()).hexdigest()
    return hmac.compare_digest(computed, hash_val)


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
                 allowed_ips: list = None):
        self.token_hash = hash_token(token)
        self.rate_limiter = RateLimiter(max_attempts=max_attempts)
        self.ip_allowlist = IPAllowlist(allowed_ips)
    
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

        # Verify token
        if not verify_token(token, self.token_hash):
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
        self.rate_limiter.record_success(ip)
        return True, ""
