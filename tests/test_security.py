"""Tests for gpumesh.security module."""

import time
import pytest

from gpumesh.security import (
    hash_token,
    verify_token,
    RateLimiter,
    IPAllowlist,
    SecurityManager,
)


class TestTokenHashing:
    """Tests for token hashing and verification."""
    
    def test_hash_token_returns_salt_and_hash(self):
        """Hash should return format 'salt:hash'."""
        result = hash_token("secret123")
        assert ":" in result
        salt, hash_val = result.split(":", 1)
        assert len(salt) == 32  # 16 bytes = 32 hex chars
        assert len(hash_val) == 64  # SHA-256 = 64 hex chars
    
    def test_hash_token_with_custom_salt(self):
        """Hash with custom salt should be deterministic."""
        result1 = hash_token("secret123", salt="abc123")
        result2 = hash_token("secret123", salt="abc123")
        assert result1 == result2
    
    def test_hash_token_different_salts_different_hashes(self):
        """Same token with different salts should produce different hashes."""
        result1 = hash_token("secret123", salt="salt1")
        result2 = hash_token("secret123", salt="salt2")
        assert result1 != result2
    
    def test_verify_token_correct(self):
        """Verify should return True for correct token."""
        hashed = hash_token("secret123")
        assert verify_token("secret123", hashed) is True
    
    def test_verify_token_incorrect(self):
        """Verify should return False for incorrect token."""
        hashed = hash_token("secret123")
        assert verify_token("wrong_token", hashed) is False
    
    def test_verify_token_different_tokens(self):
        """Different tokens should not verify against each other."""
        hashed1 = hash_token("token1")
        hashed2 = hash_token("token2")
        assert verify_token("token1", hashed1) is True
        assert verify_token("token2", hashed2) is True
        assert verify_token("token1", hashed2) is False


class TestRateLimiter:
    """Tests for rate limiting functionality."""
    
    def test_allows_requests_under_limit(self):
        """Should allow requests under the max attempts."""
        limiter = RateLimiter(max_attempts=5, window_seconds=60)
        for _ in range(4):
            assert limiter.is_allowed("192.168.1.1") is True
            limiter.record_failure("192.168.1.1")
    
    def test_locks_out_after_max_attempts(self):
        """Should lockout after exceeding max attempts."""
        limiter = RateLimiter(max_attempts=3, window_seconds=60, lockout_seconds=60)
        for _ in range(3):
            limiter.record_failure("192.168.1.1")
        
        assert limiter.is_allowed("192.168.1.1") is False
    
    def test_lockout_expires(self):
        """Lockout should expire after lockout_seconds."""
        limiter = RateLimiter(max_attempts=2, window_seconds=60, lockout_seconds=1)
        limiter.record_failure("192.168.1.1")
        limiter.record_failure("192.168.1.1")
        
        assert limiter.is_allowed("192.168.1.1") is False
        time.sleep(1.1)
        assert limiter.is_allowed("192.168.1.1") is True
    
    def test_success_clears_history(self):
        """Successful attempt should clear failure history."""
        limiter = RateLimiter(max_attempts=3, window_seconds=60)
        limiter.record_failure("192.168.1.1")
        limiter.record_failure("192.168.1.1")
        limiter.record_success("192.168.1.1")
        
        # Should have fresh attempts
        assert limiter.get_remaining_attempts("192.168.1.1") == 3
    
    def test_different_ips_independent(self):
        """Rate limiting should be per-IP."""
        limiter = RateLimiter(max_attempts=2, window_seconds=60)
        limiter.record_failure("192.168.1.1")
        limiter.record_failure("192.168.1.1")
        
        # Different IP should not be affected
        assert limiter.is_allowed("192.168.1.2") is True
    
    def test_get_remaining_attempts(self):
        """Should return correct remaining attempts."""
        limiter = RateLimiter(max_attempts=5, window_seconds=60)
        limiter.record_failure("192.168.1.1")
        
        assert limiter.get_remaining_attempts("192.168.1.1") == 4
    
    def test_get_lockout_remaining(self):
        """Should return remaining lockout time."""
        limiter = RateLimiter(max_attempts=1, window_seconds=60, lockout_seconds=60)
        limiter.record_failure("192.168.1.1")
        
        remaining = limiter.get_lockout_remaining("192.168.1.1")
        assert remaining > 0
        assert remaining <= 60


class TestIPAllowlist:
    """Tests for IP allowlist functionality."""
    
    def test_no_allowlist_allows_all(self):
        """Without allowlist, all IPs should be allowed."""
        allowlist = IPAllowlist()
        assert allowlist.is_allowed("192.168.1.1") is True
        assert allowlist.is_allowed("10.0.0.1") is True
    
    def test_allowlist_restricts_ips(self):
        """Allowlist should only allow listed IPs."""
        allowlist = IPAllowlist(allowed_ips=["192.168.1.1", "10.0.0.1"])
        assert allowlist.is_allowed("192.168.1.1") is True
        assert allowlist.is_allowed("10.0.0.1") is True
        assert allowlist.is_allowed("172.16.0.1") is False
    
    def test_add_ip(self):
        """Should be able to add IPs to allowlist."""
        allowlist = IPAllowlist(allowed_ips=["192.168.1.1"])
        allowlist.add_ip("10.0.0.1")
        
        assert allowlist.is_allowed("10.0.0.1") is True
    
    def test_remove_ip(self):
        """Should be able to remove IPs from allowlist."""
        allowlist = IPAllowlist(allowed_ips=["192.168.1.1", "10.0.0.1"])
        allowlist.remove_ip("10.0.0.1")
        
        assert allowlist.is_allowed("10.0.0.1") is False
        assert allowlist.is_allowed("192.168.1.1") is True
    
    def test_set_allowlist(self):
        """Should be able to replace entire allowlist."""
        allowlist = IPAllowlist(allowed_ips=["192.168.1.1"])
        allowlist.set_allowlist(["10.0.0.1", "172.16.0.1"])
        
        assert allowlist.is_allowed("192.168.1.1") is False
        assert allowlist.is_allowed("10.0.0.1") is True
        assert allowlist.is_allowed("172.16.0.1") is True


class TestSecurityManager:
    """Tests for the combined SecurityManager."""
    
    def test_valid_token_allowed(self):
        """Valid token should be allowed."""
        mgr = SecurityManager("secret123")
        allowed, msg = mgr.verify_request("secret123", "192.168.1.1")
        assert allowed is True
        assert msg == ""
    
    def test_invalid_token_rejected(self):
        """Invalid token should be rejected."""
        mgr = SecurityManager("secret123")
        allowed, msg = mgr.verify_request("wrong_token", "192.168.1.1")
        assert allowed is False
        assert "Invalid token" in msg
    
    def test_rate_limiting_works(self):
        """Should rate limit after too many failures."""
        mgr = SecurityManager("secret123", max_attempts=2)
        
        # Fail twice
        mgr.verify_request("wrong", "192.168.1.1")
        mgr.verify_request("wrong", "192.168.1.1")
        
        # Third attempt should be rate limited
        allowed, msg = mgr.verify_request("wrong", "192.168.1.1")
        assert allowed is False
        assert "Too many attempts" in msg
    
    def test_ip_allowlist_works(self):
        """Should reject IPs not in allowlist."""
        mgr = SecurityManager("secret123", allowed_ips=["192.168.1.1"])
        
        allowed, msg = mgr.verify_request("secret123", "192.168.1.1")
        assert allowed is True
        
        allowed, msg = mgr.verify_request("secret123", "10.0.0.1")
        assert allowed is False
        assert "IP not allowed" in msg
    
    def test_success_clears_rate_limit(self):
        """Successful auth should clear rate limit history."""
        mgr = SecurityManager("secret123", max_attempts=3)
        
        # Fail twice
        mgr.verify_request("wrong", "192.168.1.1")
        mgr.verify_request("wrong", "192.168.1.1")
        
        # Success should clear history
        mgr.verify_request("secret123", "192.168.1.1")
        
        # Should have fresh attempts now
        allowed, msg = mgr.verify_request("wrong", "192.168.1.1")
        assert "2 attempts remaining" in msg  # Not rate limited yet
