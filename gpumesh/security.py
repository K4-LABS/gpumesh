"""Security features for gpumesh coordinator.

Provides token hashing, rate limiting, and IP-based protection.
"""

import hashlib
import hmac
import time
import threading
from collections import defaultdict


def hash_token(token: str, salt: str = None) -> str:
    """Hash a token using SHA-256 with a salt.

    Returns format: "salt:hash" so salt is stored alongside the hash.
    This prevents storing plain-text tokens in the database.

    When no salt is provided, a deterministic salt is derived from the token
    itself so that the same token always produces the same hash across
    coordinator restarts.  This is critical: workers persist the plain token
    and expect it to keep working after the coordinator is restarted.
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
    """
    
    def __init__(self, max_attempts: int = 5, window_seconds: int = 300, 
                 lockout_seconds: int = 900):
        """
        Args:
            max_attempts: Maximum failed attempts before lockout
            window_seconds: Time window for counting attempts
            lockout_seconds: How long to lockout after exceeding max attempts
        """
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.lockout_seconds = lockout_seconds
        self._attempts = defaultdict(list)  # ip -> [timestamps]
        self._lockouts = {}  # ip -> lockout_until
        self._lock = threading.Lock()
    
    def is_allowed(self, ip: str) -> bool:
        """Check if an IP is allowed to make a request."""
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
        with self._lock:
            now = time.time()
            recent = [
                t for t in self._attempts.get(ip, [])
                if now - t < self.window_seconds
            ]
            return max(0, self.max_attempts - len(recent))
    
    def get_lockout_remaining(self, ip: str) -> float:
        """Get remaining lockout time in seconds (0 if not locked out)."""
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
        """Verify a request. Returns (allowed, error_message)."""
        # Check IP allowlist
        if not self.ip_allowlist.is_allowed(ip):
            return False, "IP not allowed"
        
        # Check rate limit
        if not self.rate_limiter.is_allowed(ip):
            remaining = self.rate_limiter.get_lockout_remaining(ip)
            return False, f"Too many attempts. Try again in {int(remaining)}s"
        
        # Verify token
        if not verify_token(token, self.token_hash):
            self.rate_limiter.record_failure(ip)
            remaining = self.rate_limiter.get_remaining_attempts(ip)
            return False, f"Invalid token. {remaining} attempts remaining"
        
        # Success - clear failure history
        self.rate_limiter.record_success(ip)
        return True, ""
