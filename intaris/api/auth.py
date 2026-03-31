"""Exchange token authentication for cross-service SSO.

Cognis issues short-lived exchange JWTs when a user clicks "Open Intaris"
in the Cognis UI. This endpoint validates the exchange JWT, creates a
server-side session, and sets a cookie for subsequent browser requests.

The exchange token is single-use (JTI consumption tracking) and the
resulting session has a configurable TTL (default 8 hours).

Both the session store and JTI tracker are backed by the database
(SQLite or PostgreSQL) so they work correctly in multi-worker deployments.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

_SESSION_TTL_SECONDS = 8 * 60 * 60  # 8 hours
_CLEANUP_INTERVAL = 300  # Run cleanup every 5 minutes
_COOKIE_NAME = "intaris_exchange_session"

# Per-IP rate limiting for the unauthenticated exchange endpoint.
# Prevents brute-force and CPU DoS via JWT signature verification.
_EXCHANGE_RATE_LIMIT = 10  # max requests per window
_EXCHANGE_RATE_WINDOW = 60  # seconds


# ── Exchange session store (DB-backed) ────────────────────────────


@dataclass
class ExchangeSession:
    """Resolved exchange session identity."""

    user_id: str
    agent_id: str | None
    expires_at: float


def _get_db():
    """Lazy import to avoid circular dependency at module load."""
    from intaris.server import _get_db

    return _get_db()


def _cleanup_expired_sessions(db) -> None:
    """Remove expired exchange sessions and consumed JTIs."""
    now = time.time()
    with db.cursor() as cur:
        cur.execute("DELETE FROM exchange_sessions WHERE expires_at < ?", (now,))
        cur.execute("DELETE FROM consumed_jtis WHERE expires_at < ?", (now,))


# Track last cleanup time (module-level).  The outer lock-free read is
# safe because _last_cleanup is only ever replaced with a float (atomic
# on CPython via the GIL; on free-threaded builds the double-check
# inside _cleanup_lock guarantees correctness).
_last_cleanup = 0.0
_cleanup_lock = threading.Lock()


def _maybe_cleanup(db) -> None:
    """Run cleanup if enough time has passed since the last one."""
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < _CLEANUP_INTERVAL:
        return
    with _cleanup_lock:
        # Double-check under lock
        if now - _last_cleanup < _CLEANUP_INTERVAL:
            return
        _last_cleanup = now
    # Run outside lock — DB operations are thread-safe
    try:
        _cleanup_expired_sessions(db)
    except Exception:
        # Reset so cleanup is retried sooner rather than waiting
        # the full _CLEANUP_INTERVAL after a transient failure.
        _last_cleanup = 0.0
        logger.debug("Exchange session cleanup failed", exc_info=True)


def create_exchange_session(db, *, user_id: str, agent_id: str | None) -> str:
    """Create a new exchange session in the database.

    Returns the session token (to be set as a cookie).
    """
    token = secrets.token_urlsafe(32)
    expires_at = time.time() + _SESSION_TTL_SECONDS
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO exchange_sessions (token, user_id, agent_id, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (token, user_id, agent_id, expires_at),
        )
    _maybe_cleanup(db)
    return token


def get_exchange_session(token: str) -> ExchangeSession | None:
    """Look up an exchange session by cookie token.

    Returns the session if valid and not expired, else None.
    """
    try:
        db = _get_db()
    except Exception:
        return None

    now = time.time()
    with db.cursor() as cur:
        cur.execute(
            "SELECT user_id, agent_id, expires_at FROM exchange_sessions "
            "WHERE token = ?",
            (token,),
        )
        row = cur.fetchone()

    if row is None:
        return None

    if row["expires_at"] < now:
        # Expired — clean up
        with db.cursor() as cur:
            cur.execute("DELETE FROM exchange_sessions WHERE token = ?", (token,))
        return None

    return ExchangeSession(
        user_id=row["user_id"],
        agent_id=row["agent_id"],
        expires_at=row["expires_at"],
    )


# ── JTI consumption tracking (DB-backed) ─────────────────────────


def _consume_jti(db, jti: str, exp: float) -> bool:
    """Return True if JTI was consumed (first use), False if replayed.

    Uses INSERT OR IGNORE (SQLite) / ON CONFLICT DO NOTHING (PostgreSQL)
    for atomic single-use enforcement across workers. Checks rowcount
    to determine whether the insert was a no-op (replay) or succeeded.
    """
    if db.backend == "postgresql":
        sql = (
            "INSERT INTO consumed_jtis (jti, expires_at) "
            "VALUES (?, ?) ON CONFLICT DO NOTHING"
        )
    else:
        sql = "INSERT OR IGNORE INTO consumed_jtis (jti, expires_at) VALUES (?, ?)"
    with db.cursor() as cur:
        cur.execute(sql, (jti, exp))
        # rowcount is 0 if the insert was a no-op (JTI already exists),
        # 1 if the insert succeeded (first use).
        return cur.rowcount > 0


# ── Per-IP rate limiter ──────────────────────────────────────────


class _ExchangeRateLimiter:
    """Simple per-IP sliding window rate limiter for the exchange endpoint.

    In-memory only — rate limiting is best-effort and per-worker.
    The security-critical single-use enforcement is in the DB-backed
    JTI tracker, not here.
    """

    def __init__(self, max_calls: int, window_seconds: int):
        self._max = max_calls
        self._window = window_seconds
        self._calls: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()

    def check(self, ip: str) -> bool:
        """Return True if the request is allowed, False if rate-limited."""
        now = time.monotonic()
        cutoff = now - self._window

        with self._lock:
            # Periodic sweep every 5 minutes
            if now - self._last_sweep > 300:
                self._sweep(cutoff)
                self._last_sweep = now

            timestamps = self._calls.get(ip)
            if timestamps is None:
                timestamps = deque()
                self._calls[ip] = timestamps

            while timestamps and timestamps[0] < cutoff:
                timestamps.popleft()

            if len(timestamps) >= self._max:
                return False

            timestamps.append(now)
            return True

    def _sweep(self, cutoff: float) -> None:
        empty = [k for k, v in self._calls.items() if not v or v[-1] < cutoff]
        for k in empty:
            del self._calls[k]


_exchange_limiter = _ExchangeRateLimiter(_EXCHANGE_RATE_LIMIT, _EXCHANGE_RATE_WINDOW)


# ── Request/Response models ──────────────────────────────────────


class ExchangeRequest(BaseModel):
    token: str


class ExchangeResponse(BaseModel):
    user_id: str


# ── Endpoint ─────────────────────────────────────────────────────


@router.post("/auth/exchange")
async def exchange_token(
    body: ExchangeRequest, http_request: Request, response: Response
) -> ExchangeResponse:
    """Exchange a Cognis exchange JWT for a browser session cookie.

    Validates the exchange JWT cryptographically, checks single-use
    enforcement, creates a server-side session, and sets a cookie.
    """
    from intaris.auth import get_jwt_validator
    from intaris.server import _get_config

    # Per-IP rate limiting
    client_ip = http_request.client.host if http_request.client else "unknown"
    if not _exchange_limiter.check(client_ip):
        raise HTTPException(status_code=429, detail="Too many requests")

    cfg = _get_config()
    validator = get_jwt_validator(cfg.server.jwt_public_key, cfg.server.jwks_url)
    if validator is None:
        logger.warning("Exchange token rejected: JWT validation not configured")
        raise HTTPException(status_code=503, detail="JWT validation not configured")

    # Validate the exchange JWT
    try:
        claims = validator.decode_claims(body.token)
    except Exception as e:
        logger.info("Exchange token rejected: %s", e)
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # Verify token type
    if claims.get("typ") != "exchange":
        logger.info("Exchange token rejected: wrong typ=%s", claims.get("typ"))
        raise HTTPException(status_code=401, detail="Invalid token type")

    # Verify target
    if claims.get("target") != "intaris":
        logger.info("Exchange token rejected: wrong target=%s", claims.get("target"))
        raise HTTPException(
            status_code=401, detail="Token not intended for this service"
        )

    # Extract identity
    user_id = claims.get("sub")
    if not isinstance(user_id, str) or not user_id.strip():
        logger.info("Exchange token rejected: missing sub claim")
        raise HTTPException(status_code=401, detail="Invalid token")

    # Single-use enforcement — JTI is mandatory for exchange tokens
    jti = claims.get("jti")
    if not jti:
        logger.info("Exchange token rejected: missing jti claim")
        raise HTTPException(status_code=401, detail="Invalid token")
    exp = claims.get("exp", 0)

    db = _get_db()
    if not _consume_jti(db, jti, exp):
        logger.info("Exchange token rejected: JTI already consumed")
        raise HTTPException(status_code=401, detail="Token already used")

    agent_id = claims.get("agent_id")

    # Create server-side session in the database
    session_token = create_exchange_session(db, user_id=user_id, agent_id=agent_id)

    # Set cookie
    response.set_cookie(
        key=_COOKIE_NAME,
        value=session_token,
        max_age=_SESSION_TTL_SECONDS,
        path="/",
        httponly=True,
        samesite="lax",
        secure=cfg.server.cookie_secure,
    )

    logger.info("Exchange session created for user=%s", user_id)
    return ExchangeResponse(user_id=user_id)
