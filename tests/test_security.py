"""Tests for gpumesh.security module."""

import hashlib
import inspect
import time
import pytest

import gpumesh.security

from gpumesh.security import (
    hash_token,
    verify_token,
    is_loopback,
    RateLimiter,
    IPAllowlist,
    SecurityManager,
)


class TestTokenHashing:
    """Tests for token hashing and verification."""
    
    def test_hash_token_defaults_to_pbkdf2(self):
        """The default derivation is PBKDF2, tagged with its own cost factor."""
        result = hash_token("secret123")
        scheme, iterations, salt, hash_val = result.split("$")
        assert scheme == "pbkdf2_sha256"
        assert int(iterations) == gpumesh.security.DEFAULT_KDF_ITERATIONS
        assert len(salt) == 32  # 16 random bytes, hex
        assert len(hash_val) == 64  # SHA-256 output, hex

    def test_pbkdf2_salt_is_random_per_call(self):
        """Two hashes of the same token must differ -- the whole point of #16."""
        assert hash_token("secret123") != hash_token("secret123")

    def test_pbkdf2_cost_factor_is_configurable(self):
        """The iteration count is a knob, and it is recorded in the hash."""
        result = hash_token("secret123", iterations=5000)
        assert result.split("$")[1] == "5000"
        assert verify_token("secret123", result) is True

    def test_pbkdf2_rejects_a_cost_factor_that_would_disable_the_kdf(self):
        with pytest.raises(ValueError):
            hash_token("secret123", iterations=1)

    def test_hash_token_legacy_scheme_returns_salt_and_hash(self):
        """The legacy scheme still produces 'salt:hash' when asked for."""
        result = hash_token("secret123", scheme="sha256")
        assert ":" in result
        salt, hash_val = result.split(":", 1)
        assert len(salt) == 16  # Deterministic salt: 16 hex chars
        assert len(hash_val) == 64  # SHA-256 = 64 hex chars

    def test_legacy_hashes_still_verify(self):
        """A hash written by an older gpumesh keeps working. No flag day."""
        salted = hash_token("secret123", scheme="sha256")
        assert verify_token("secret123", salted) is True
        assert verify_token("wrong", salted) is False
        unsalted = hashlib.sha256(b"secret123").hexdigest()
        assert verify_token("secret123", unsalted) is True

    def test_malformed_pbkdf2_hash_is_a_denial_not_a_crash(self):
        """verify_token runs on the request path; it must never raise there."""
        assert verify_token("secret123", "pbkdf2_sha256$notanint$aa$bb") is False
        assert verify_token("secret123", "pbkdf2_sha256$1000$aa") is False
        assert verify_token("secret123", None) is False

    def test_env_var_selects_the_legacy_scheme(self, monkeypatch):
        monkeypatch.setenv("GPUMESH_AUTH_KDF", "sha256")
        assert ":" in hash_token("secret123")
        monkeypatch.setenv("GPUMESH_AUTH_KDF", "nonsense")
        with pytest.raises(ValueError):
            hash_token("secret123")
    
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


class TestLoopbackDetection:
    """Tests for is_loopback()."""

    @pytest.mark.parametrize("ip", [
        "127.0.0.1",
        "127.0.0.53",   # systemd-resolved's address, still 127.0.0.0/8
        "127.1.2.3",
        "127.255.255.254",
        "::1",
        "::ffff:127.0.0.1",   # dual-stack listener's view of an IPv4 loopback
        " 127.0.0.1 ",        # surrounding whitespace
    ])
    def test_loopback_addresses_detected(self, ip):
        """Every loopback form must be recognised, not just 127.0.0.1."""
        assert is_loopback(ip) is True

    @pytest.mark.parametrize("ip", [
        "192.168.1.1",
        "10.0.0.1",
        "8.8.8.8",
        "128.0.0.1",          # one octet away from the loopback range
        "::ffff:8.8.8.8",
        "2001:db8::1",
        "localhost",          # a name, not an address
        "127.0.0.1.evil.com",
        "",
        None,
    ])
    def test_non_loopback_addresses_rejected(self, ip):
        """Fail closed: anything not provably loopback is not exempt."""
        assert is_loopback(ip) is False


class TestLoopbackRateLimitExemption:
    """The operator's own machine must never be locked out of its own mesh."""

    def test_loopback_never_locked_out(self):
        """Regression: 5 bad probes from 127.0.0.1 used to wedge the mesh."""
        limiter = RateLimiter(max_attempts=3, window_seconds=60, lockout_seconds=900)
        for _ in range(20):
            assert limiter.record_failure("127.0.0.1") is False
            assert limiter.is_allowed("127.0.0.1") is True

    @pytest.mark.parametrize("ip", ["127.0.0.1", "127.0.0.9", "::1", "::ffff:127.0.0.1"])
    def test_every_loopback_form_is_exempt(self, ip):
        """The exemption covers 127.0.0.0/8 and IPv6, not one literal string."""
        limiter = RateLimiter(max_attempts=2, window_seconds=60, lockout_seconds=900)
        for _ in range(5):
            limiter.record_failure(ip)
        assert limiter.is_allowed(ip) is True
        assert limiter.get_lockout_remaining(ip) == 0

    def test_loopback_remaining_attempts_stays_full(self):
        """An exempt IP never burns through its budget."""
        limiter = RateLimiter(max_attempts=5, window_seconds=60)
        for _ in range(10):
            limiter.record_failure("127.0.0.1")
        assert limiter.get_remaining_attempts("127.0.0.1") == 5

    def test_loopback_failures_are_not_recorded_at_all(self):
        """Exempt means untracked, so history cannot bite if the flag flips."""
        limiter = RateLimiter(max_attempts=2, window_seconds=60)
        for _ in range(10):
            limiter.record_failure("127.0.0.1")
        limiter.exempt_loopback = False
        assert limiter.get_remaining_attempts("127.0.0.1") == 2
        assert limiter.is_allowed("127.0.0.1") is True

    def test_exemption_can_be_disabled(self):
        """exempt_loopback=False restores the old behaviour for anyone who wants it."""
        limiter = RateLimiter(max_attempts=2, window_seconds=60, lockout_seconds=60,
                              exempt_loopback=False)
        limiter.record_failure("127.0.0.1")
        limiter.record_failure("127.0.0.1")
        assert limiter.is_allowed("127.0.0.1") is False

    def test_non_loopback_still_locked_out(self):
        """The exemption must not disarm rate limiting for the network."""
        limiter = RateLimiter(max_attempts=2, window_seconds=60, lockout_seconds=60)
        limiter.record_failure("192.168.1.7")
        limiter.record_failure("192.168.1.7")
        assert limiter.is_allowed("192.168.1.7") is False
        assert limiter.get_lockout_remaining("192.168.1.7") > 0


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


class TestOperatorLockout:
    """Regression tests for the loopback lockout that wedged a live mesh."""

    def test_loopback_survives_repeated_bad_tokens(self):
        """Five bad probes from the coordinator's own host, then the real token.

        This is the exact sequence that produced
        "Too many attempts. Try again in 404s" and locked the operator out of
        their own coordinator. The correct token must still be accepted.
        """
        mgr = SecurityManager("secret123", max_attempts=5)
        for _ in range(5):
            allowed, msg = mgr.verify_request("typo", "127.0.0.1")
            assert allowed is False
            assert "Too many attempts" not in msg

        allowed, msg = mgr.verify_request("secret123", "127.0.0.1")
        assert allowed is True
        assert msg == ""

    @pytest.mark.parametrize("ip", ["127.0.0.1", "127.0.0.2", "::1", "::ffff:127.0.0.1"])
    def test_self_worker_and_cli_addresses_all_exempt(self, ip):
        """The self-worker and the operator's CLI both arrive over loopback."""
        mgr = SecurityManager("secret123", max_attempts=2)
        for _ in range(6):
            mgr.verify_request("wrong", ip)
        allowed, msg = mgr.verify_request("secret123", ip)
        assert allowed is True

    def test_loopback_bad_token_message_names_the_real_cause(self):
        """A loopback rejection must read as a token problem, not a lockout."""
        mgr = SecurityManager("secret123", max_attempts=2)
        for _ in range(4):
            allowed, msg = mgr.verify_request("wrong", "127.0.0.1")
            assert allowed is False
            assert "Invalid token" in msg
            assert "never locked out" in msg
            assert "Too many attempts" not in msg

    def test_remote_ip_is_still_locked_out(self):
        """Loopback is the exception; the LAN is not."""
        mgr = SecurityManager("secret123", max_attempts=2)
        mgr.verify_request("wrong", "192.168.1.50")
        mgr.verify_request("wrong", "192.168.1.50")
        allowed, msg = mgr.verify_request("wrong", "192.168.1.50")
        assert allowed is False
        assert "Too many attempts" in msg


class TestRejectionMessagesAreDistinguishable:
    """Each 401 reason must tell the reader a different thing to do."""

    def test_lockout_message_denies_being_a_token_verdict(self):
        """The old message read as "your token is wrong". Say it is not."""
        mgr = SecurityManager("secret123", max_attempts=2)
        mgr.verify_request("wrong", "10.0.0.5")
        mgr.verify_request("wrong", "10.0.0.5")
        allowed, msg = mgr.verify_request("wrong", "10.0.0.5")
        assert allowed is False
        assert "NOT a token rejection" in msg
        assert "not checked" in msg
        assert "retry with the same token" in msg

    def test_correct_token_during_lockout_gets_the_same_lockout_message(self):
        """No correctness oracle: a locked-out IP learns nothing about the token.

        Telling a locked-out caller "your token is right, just wait" would let
        a brute-forcer guess through the lockout at full speed and be told the
        moment they hit. The right token and the wrong token must be
        indistinguishable here.
        """
        mgr = SecurityManager("secret123", max_attempts=2)
        mgr.verify_request("wrong", "10.0.0.6")
        mgr.verify_request("wrong", "10.0.0.6")

        allowed_right, msg_right = mgr.verify_request("secret123", "10.0.0.6")
        allowed_wrong, msg_wrong = mgr.verify_request("still-wrong", "10.0.0.6")

        assert allowed_right is False
        assert allowed_wrong is False
        assert "correct" not in msg_right.lower()
        # Only the countdown may differ between the two.
        assert msg_right.split("for another")[0] == msg_wrong.split("for another")[0]

    def test_invalid_token_message_states_the_lockout_it_is_counting_down_to(self):
        """"3 attempts remaining" is useless without saying remaining until what."""
        mgr = SecurityManager("secret123", max_attempts=5)
        allowed, msg = mgr.verify_request("wrong", "10.0.0.7")
        assert allowed is False
        assert "Invalid token" in msg
        assert "4 attempts remaining" in msg
        assert "900s" in msg

    def test_final_attempt_says_the_lockout_has_started(self):
        """The attempt that trips the lockout should say so, not say "0 remaining"."""
        mgr = SecurityManager("secret123", max_attempts=2)
        mgr.verify_request("wrong", "10.0.0.8")
        allowed, msg = mgr.verify_request("wrong", "10.0.0.8")
        assert allowed is False
        assert "Invalid token" in msg
        assert "now rate limited" in msg

    def test_allowlist_rejection_is_distinct_from_both(self):
        """A blocked address is permanent; it must not look like a wait-and-retry."""
        mgr = SecurityManager("secret123", allowed_ips=["192.168.1.1"])
        allowed, msg = mgr.verify_request("secret123", "10.0.0.9")
        assert allowed is False
        assert "IP not allowed" in msg
        assert "10.0.0.9" in msg
        assert "Too many attempts" not in msg
        assert "Invalid token" not in msg

    def test_three_reasons_produce_three_different_messages(self):
        """No two rejection reasons may share a message."""
        blocked = SecurityManager("secret123", allowed_ips=["192.168.1.1"])
        _, allowlist_msg = blocked.verify_request("secret123", "10.0.0.10")

        mgr = SecurityManager("secret123", max_attempts=2)
        _, bad_token_msg = mgr.verify_request("wrong", "10.0.0.11")
        mgr.verify_request("wrong", "10.0.0.11")
        _, lockout_msg = mgr.verify_request("wrong", "10.0.0.11")

        assert len({allowlist_msg, bad_token_msg, lockout_msg}) == 3


class TestTokenHashingLimits:
    """The legacy scheme's weaknesses are documented, not secret."""

    def test_legacy_salt_is_a_pure_function_of_the_token(self):
        """Legacy only: same token -> same salt -> same hash.

        This is what let a worker's persisted plain token keep working after
        the coordinator restarted. It also means the legacy salt adds no
        precomputation resistance, which is why it is no longer the default.
        The current scheme does not need the property: the hash lives in the
        coordinator's memory and is re-derived from the token at every start.
        """
        assert (hash_token("secret123", scheme="sha256")
                == hash_token("secret123", scheme="sha256"))
        salt = hash_token("secret123", scheme="sha256").split(":", 1)[0]
        assert salt == hashlib.sha256(b"secret123").hexdigest()[:16]

    def test_verification_is_timing_safe(self):
        """verify_token must go through hmac.compare_digest, not ==."""
        source = inspect.getsource(gpumesh.security.verify_token)
        # Count the code, not the docstring -- the docstring names the
        # function too, and a prose edit must not be able to fail this test.
        body = source.replace(gpumesh.security.verify_token.__doc__ or "", "")
        assert "hmac.compare_digest" in body
        # pbkdf2 + legacy salted + legacy unsalted, and no bare == on a digest.
        assert body.count("hmac.compare_digest") == 3
        assert "== hash_val" not in body

    def test_verify_cache_is_keyed_not_plaintext(self):
        """A cached verification must not leave the token in the cache."""
        manager = SecurityManager("secret123", kdf_iterations=1000)
        ok, _ = manager.verify_request("secret123", "203.0.113.9")
        assert ok is True
        stored = list(manager._verify_cache._entries.keys())
        assert stored and "secret123" not in stored[0]

    def test_verify_cache_does_not_admit_a_wrong_token(self):
        manager = SecurityManager("secret123", kdf_iterations=1000)
        assert manager.verify_request("secret123", "203.0.113.9")[0] is True
        assert manager.verify_request("wrong", "203.0.113.9")[0] is False

    def test_manager_accepts_the_legacy_scheme(self):
        manager = SecurityManager("secret123", kdf="sha256")
        assert ":" in manager.token_hash
        assert manager.verify_request("secret123", "203.0.113.9")[0] is True
