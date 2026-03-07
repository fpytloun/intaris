"""Tests for the in-memory sliding window rate limiter."""

from __future__ import annotations

import threading
import time

from intaris.ratelimit import RateLimiter


class TestRateLimiter:
    """Tests for RateLimiter."""

    def test_within_limit(self):
        """Calls within the limit are allowed."""
        limiter = RateLimiter(max_calls=5, window_seconds=60)
        for _ in range(5):
            assert limiter.check_and_record("user1", "sess1") is True

    def test_exceed_limit(self):
        """Calls exceeding the limit are blocked."""
        limiter = RateLimiter(max_calls=3, window_seconds=60)
        for _ in range(3):
            assert limiter.check_and_record("user1", "sess1") is True
        assert limiter.check_and_record("user1", "sess1") is False

    def test_different_sessions_independent(self):
        """Different sessions have independent limits."""
        limiter = RateLimiter(max_calls=2, window_seconds=60)
        assert limiter.check_and_record("user1", "sess1") is True
        assert limiter.check_and_record("user1", "sess1") is True
        assert limiter.check_and_record("user1", "sess1") is False
        # Different session should still be allowed
        assert limiter.check_and_record("user1", "sess2") is True

    def test_different_users_independent(self):
        """Different users have independent limits."""
        limiter = RateLimiter(max_calls=2, window_seconds=60)
        assert limiter.check_and_record("user1", "sess1") is True
        assert limiter.check_and_record("user1", "sess1") is True
        assert limiter.check_and_record("user1", "sess1") is False
        # Different user should still be allowed
        assert limiter.check_and_record("user2", "sess1") is True

    def test_zero_limit_no_limiting(self):
        """Zero max_calls means no rate limiting."""
        limiter = RateLimiter(max_calls=0, window_seconds=60)
        for _ in range(100):
            assert limiter.check_and_record("user1", "sess1") is True

    def test_window_expiry(self):
        """Calls are allowed again after the window expires."""
        limiter = RateLimiter(max_calls=2, window_seconds=1)
        assert limiter.check_and_record("user1", "sess1") is True
        assert limiter.check_and_record("user1", "sess1") is True
        assert limiter.check_and_record("user1", "sess1") is False
        # Wait for window to expire
        time.sleep(1.1)
        assert limiter.check_and_record("user1", "sess1") is True

    def test_sliding_window(self):
        """Sliding window correctly prunes old entries."""
        limiter = RateLimiter(max_calls=2, window_seconds=1)
        assert limiter.check_and_record("user1", "sess1") is True
        time.sleep(0.6)
        assert limiter.check_and_record("user1", "sess1") is True
        # First call should have expired, second still active
        time.sleep(0.5)
        assert limiter.check_and_record("user1", "sess1") is True

    def test_periodic_sweep(self):
        """Periodic sweep removes empty entries."""
        limiter = RateLimiter(max_calls=2, window_seconds=1)
        limiter._sweep_interval = 0  # Sweep on every call

        assert limiter.check_and_record("user1", "sess1") is True
        time.sleep(1.1)
        # This call triggers sweep which should clean up expired entries
        assert limiter.check_and_record("user1", "sess2") is True
        # sess1 should have been swept
        assert ("user1", "sess1") not in limiter._calls

    def test_thread_safety(self):
        """Concurrent access does not cause errors."""
        limiter = RateLimiter(max_calls=1000, window_seconds=60)
        errors = []

        def worker(user_id: str):
            try:
                for _ in range(100):
                    limiter.check_and_record(user_id, "sess1")
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=worker, args=(f"user{i}",)) for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0

    def test_negative_max_calls_no_limiting(self):
        """Negative max_calls is treated as no limiting."""
        limiter = RateLimiter(max_calls=-1, window_seconds=60)
        for _ in range(10):
            assert limiter.check_and_record("user1", "sess1") is True
