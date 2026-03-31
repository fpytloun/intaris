"""Tests for exchange token authentication (intaris.api.auth).

Tests cover:
- DB-backed exchange session CRUD (create, lookup, expiry)
- DB-backed JTI consumption (single-use enforcement, replay rejection)
- Per-IP rate limiter for the exchange endpoint
- Expired session cleanup
"""

from __future__ import annotations

import time

import pytest

from intaris.api.auth import (
    _consume_jti,
    _ExchangeRateLimiter,
    _maybe_cleanup,
    create_exchange_session,
    get_exchange_session,
)
from intaris.config import DBConfig
from intaris.db import Database

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    """Fresh SQLite database with schema."""
    config = DBConfig()
    config.path = str(tmp_path / "test.db")
    return Database(config)


# ── Exchange Session Store ────────────────────────────────────────────


class TestExchangeSessionStore:
    """Tests for DB-backed exchange session CRUD."""

    def test_create_and_lookup(self, db, monkeypatch):
        """Created session can be looked up by token."""
        # Patch _get_db to return our test DB
        monkeypatch.setattr("intaris.api.auth._get_db", lambda: db)

        token = create_exchange_session(db, user_id="user-1", agent_id="agent-1")
        assert isinstance(token, str)
        assert len(token) > 20  # token_urlsafe(32) produces ~43 chars

        session = get_exchange_session(token)
        assert session is not None
        assert session.user_id == "user-1"
        assert session.agent_id == "agent-1"
        assert session.expires_at > time.time()

    def test_lookup_nonexistent_returns_none(self, db, monkeypatch):
        """Looking up a nonexistent token returns None."""
        monkeypatch.setattr("intaris.api.auth._get_db", lambda: db)

        session = get_exchange_session("nonexistent-token")
        assert session is None

    def test_expired_session_returns_none(self, db, monkeypatch):
        """Expired sessions return None and are cleaned up."""
        monkeypatch.setattr("intaris.api.auth._get_db", lambda: db)

        # Insert an already-expired session directly
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO exchange_sessions (token, user_id, agent_id, expires_at) "
                "VALUES (?, ?, ?, ?)",
                ("expired-token", "user-1", None, time.time() - 100),
            )

        session = get_exchange_session("expired-token")
        assert session is None

        # Verify it was cleaned up from the DB
        with db.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM exchange_sessions WHERE token = ?",
                ("expired-token",),
            )
            assert cur.fetchone()[0] == 0

    def test_session_without_agent_id(self, db, monkeypatch):
        """Sessions can be created without an agent_id."""
        monkeypatch.setattr("intaris.api.auth._get_db", lambda: db)

        token = create_exchange_session(db, user_id="user-1", agent_id=None)
        session = get_exchange_session(token)
        assert session is not None
        assert session.user_id == "user-1"
        assert session.agent_id is None

    def test_multiple_sessions_independent(self, db, monkeypatch):
        """Multiple sessions for the same user are independent."""
        monkeypatch.setattr("intaris.api.auth._get_db", lambda: db)

        token1 = create_exchange_session(db, user_id="user-1", agent_id=None)
        token2 = create_exchange_session(db, user_id="user-1", agent_id=None)
        assert token1 != token2

        session1 = get_exchange_session(token1)
        session2 = get_exchange_session(token2)
        assert session1 is not None
        assert session2 is not None


# ── JTI Consumption ───────────────────────────────────────────────────


class TestJTIConsumption:
    """Tests for DB-backed JTI single-use enforcement."""

    def test_first_use_succeeds(self, db):
        """First consumption of a JTI returns True."""
        assert _consume_jti(db, "jti-1", time.time() + 300) is True

    def test_replay_rejected(self, db):
        """Second consumption of the same JTI returns False."""
        exp = time.time() + 300
        assert _consume_jti(db, "jti-replay", exp) is True
        assert _consume_jti(db, "jti-replay", exp) is False

    def test_different_jtis_independent(self, db):
        """Different JTIs are consumed independently."""
        exp = time.time() + 300
        assert _consume_jti(db, "jti-a", exp) is True
        assert _consume_jti(db, "jti-b", exp) is True
        assert _consume_jti(db, "jti-a", exp) is False
        assert _consume_jti(db, "jti-b", exp) is False


# ── Cleanup ───────────────────────────────────────────────────────────


class TestCleanup:
    """Tests for expired entry cleanup."""

    def test_cleanup_removes_expired(self, db, monkeypatch):
        """_maybe_cleanup removes expired sessions and JTIs."""
        # Force cleanup to run by resetting the last cleanup time
        import intaris.api.auth as auth_mod

        monkeypatch.setattr(auth_mod, "_last_cleanup", 0.0)

        now = time.time()
        # Insert expired entries
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO exchange_sessions (token, user_id, agent_id, expires_at) "
                "VALUES (?, ?, ?, ?)",
                ("old-token", "user-1", None, now - 100),
            )
            cur.execute(
                "INSERT INTO consumed_jtis (jti, expires_at) VALUES (?, ?)",
                ("old-jti", now - 100),
            )
            # Insert valid entries
            cur.execute(
                "INSERT INTO exchange_sessions (token, user_id, agent_id, expires_at) "
                "VALUES (?, ?, ?, ?)",
                ("valid-token", "user-1", None, now + 3600),
            )
            cur.execute(
                "INSERT INTO consumed_jtis (jti, expires_at) VALUES (?, ?)",
                ("valid-jti", now + 3600),
            )

        _maybe_cleanup(db)

        # Expired entries should be gone
        with db.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM exchange_sessions")
            assert cur.fetchone()[0] == 1  # only valid-token remains
            cur.execute("SELECT COUNT(*) FROM consumed_jtis")
            assert cur.fetchone()[0] == 1  # only valid-jti remains


# ── Per-IP Rate Limiter ───────────────────────────────────────────────


class TestExchangeRateLimiter:
    """Tests for the per-IP rate limiter."""

    def test_allows_within_limit(self):
        """Requests within the limit are allowed."""
        limiter = _ExchangeRateLimiter(max_calls=5, window_seconds=60)
        for _ in range(5):
            assert limiter.check("1.2.3.4") is True

    def test_blocks_over_limit(self):
        """Requests over the limit are blocked."""
        limiter = _ExchangeRateLimiter(max_calls=3, window_seconds=60)
        for _ in range(3):
            assert limiter.check("1.2.3.4") is True
        assert limiter.check("1.2.3.4") is False

    def test_different_ips_independent(self):
        """Different IPs have independent limits."""
        limiter = _ExchangeRateLimiter(max_calls=2, window_seconds=60)
        assert limiter.check("1.1.1.1") is True
        assert limiter.check("1.1.1.1") is True
        assert limiter.check("1.1.1.1") is False
        # Different IP still has budget
        assert limiter.check("2.2.2.2") is True

    def test_window_expiry(self):
        """Requests are allowed again after the window expires."""
        limiter = _ExchangeRateLimiter(max_calls=1, window_seconds=1)
        assert limiter.check("1.2.3.4") is True
        assert limiter.check("1.2.3.4") is False
        # Wait for window to expire
        time.sleep(1.1)
        assert limiter.check("1.2.3.4") is True
