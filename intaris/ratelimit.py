"""In-memory sliding window rate limiter for intaris.

Provides per-session rate limiting on the /evaluate endpoint to prevent
runaway agents from overwhelming the LLM evaluation pipeline.

Uses a sliding window counter with deque-based timestamp tracking.
Thread-safe via threading.Lock. Single-process only — for horizontal
scaling, an external store (Redis) would be needed.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

logger = logging.getLogger(__name__)


class RateLimiter:
    """In-memory sliding window rate limiter.

    Tracks call timestamps per (user_id, session_id) pair and enforces
    a maximum number of calls within a rolling time window.

    Args:
        max_calls: Maximum calls allowed per window. 0 means no limit.
        window_seconds: Window size in seconds (default 60).
    """

    def __init__(self, max_calls: int, window_seconds: int = 60):
        self._max = max_calls
        self._window = window_seconds
        self._calls: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()
        self._sweep_interval = 300.0  # 5 minutes

    def check_and_record(self, user_id: str, session_id: str) -> bool:
        """Check if a call is within the rate limit and record it.

        Atomically checks the limit and records the call if allowed.

        Args:
            user_id: Tenant identifier.
            session_id: Session identifier.

        Returns:
            True if the call is allowed, False if rate limit exceeded.
        """
        if self._max <= 0:
            return True

        now = time.monotonic()
        key = (user_id, session_id)
        cutoff = now - self._window

        with self._lock:
            # Periodic sweep of abandoned sessions
            if now - self._last_sweep > self._sweep_interval:
                self._sweep(cutoff)
                self._last_sweep = now

            # Get or create deque for this session
            timestamps = self._calls.get(key)
            if timestamps is None:
                timestamps = deque()
                self._calls[key] = timestamps

            # Prune expired entries
            while timestamps and timestamps[0] < cutoff:
                timestamps.popleft()

            # Check limit
            if len(timestamps) >= self._max:
                logger.warning(
                    "Rate limit exceeded for user=%s session=%s "
                    "(%d calls in %ds window)",
                    user_id,
                    session_id,
                    len(timestamps),
                    self._window,
                )
                return False

            # Record the call
            timestamps.append(now)
            return True

    def _sweep(self, cutoff: float) -> None:
        """Remove entries for sessions with no recent calls.

        Called periodically under the lock to prevent unbounded memory
        growth from abandoned sessions.
        """
        empty_keys = []
        for key, timestamps in self._calls.items():
            # Prune expired entries
            while timestamps and timestamps[0] < cutoff:
                timestamps.popleft()
            if not timestamps:
                empty_keys.append(key)

        for key in empty_keys:
            del self._calls[key]

        if empty_keys:
            logger.debug(
                "Rate limiter sweep: removed %d empty entries", len(empty_keys)
            )
