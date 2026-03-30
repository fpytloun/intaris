"""Exchange token authentication for cross-service SSO.

Cognis issues short-lived exchange JWTs when a user clicks "Open Intaris"
in the Cognis UI. This endpoint validates the exchange JWT, creates a
server-side session, and sets a cookie for subsequent browser requests.

The exchange token is single-use (JTI consumption tracking) and the
resulting session has a configurable TTL (default 8 hours).
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

_SESSION_TTL_SECONDS = 8 * 60 * 60  # 8 hours
_CLEANUP_THRESHOLD = 100  # Run cleanup when dict exceeds this size
_COOKIE_NAME = "intaris_exchange_session"


# ── Exchange session store ────────────────────────────────────────


@dataclass
class _ExchangeSession:
    user_id: str
    agent_id: str | None
    expires_at: float


_sessions: dict[str, _ExchangeSession] = {}


def _cleanup_expired() -> None:
    """Remove expired sessions. Called lazily when store grows large."""
    now = time.time()
    expired = [k for k, v in _sessions.items() if v.expires_at < now]
    for k in expired:
        del _sessions[k]


def get_exchange_session(token: str) -> _ExchangeSession | None:
    """Look up an exchange session by cookie token.

    Returns the session if valid and not expired, else None.
    """
    session = _sessions.get(token)
    if session is None:
        return None
    if session.expires_at < time.time():
        del _sessions[token]
        return None
    return session


# ── JTI consumption tracking ─────────────────────────────────────


_consumed_jtis: dict[str, float] = {}  # jti -> expiration timestamp


def _consume_jti(jti: str, exp: float) -> bool:
    """Return True if JTI was consumed (first use), False if replayed."""
    now = time.time()
    # Clean expired entries
    if len(_consumed_jtis) > _CLEANUP_THRESHOLD:
        expired = [k for k, v in _consumed_jtis.items() if v < now]
        for k in expired:
            del _consumed_jtis[k]
    if jti in _consumed_jtis:
        return False
    _consumed_jtis[jti] = exp
    return True


# ── Request/Response models ──────────────────────────────────────


class ExchangeRequest(BaseModel):
    token: str


class ExchangeResponse(BaseModel):
    user_id: str


# ── Endpoint ─────────────────────────────────────────────────────


@router.post("/auth/exchange")
async def exchange_token(body: ExchangeRequest, response: Response) -> ExchangeResponse:
    """Exchange a Cognis exchange JWT for a browser session cookie.

    Validates the exchange JWT cryptographically, checks single-use
    enforcement, creates a server-side session, and sets a cookie.
    """
    from intaris.auth import get_jwt_validator
    from intaris.server import _get_config

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
    if not _consume_jti(jti, exp):
        logger.info("Exchange token rejected: JTI already consumed")
        raise HTTPException(status_code=401, detail="Token already used")

    agent_id = claims.get("agent_id")

    # Create server-side session
    session_token = secrets.token_urlsafe(32)
    _sessions[session_token] = _ExchangeSession(
        user_id=user_id,
        agent_id=agent_id,
        expires_at=time.time() + _SESSION_TTL_SECONDS,
    )

    # Lazy cleanup
    if len(_sessions) > _CLEANUP_THRESHOLD:
        _cleanup_expired()

    # Set cookie
    response.set_cookie(
        key=_COOKIE_NAME,
        value=session_token,
        max_age=_SESSION_TTL_SECONDS,
        path="/",
        httponly=True,
        samesite="lax",
        secure=False,  # localhost dev; production behind TLS termination
    )

    logger.info("Exchange session created for user=%s", user_id)
    return ExchangeResponse(user_id=user_id)
