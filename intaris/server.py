"""HTTP server for intaris.

FastAPI application with health endpoint, API key authentication
middleware with multi-tenant user_id resolution, and REST API sub-app
for safety evaluation endpoints.
"""

from __future__ import annotations

import contextlib
import contextvars
import hmac
import logging
import os
import sys

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute

from intaris import __version__
from intaris.config import load_config

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("intaris")

# ── Session Identity Context ──────────────────────────────────────────
# Set by APIKeyMiddleware per-request, read by api/deps.py.

_session_user_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "session_user_id", default=None
)
_session_agent_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "session_agent_id", default=None
)
_session_user_bound: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "session_user_bound", default=False
)
# user_bound is True when the API key maps to a specific user_id.
# When True, the UI should prevent user impersonation (X-User-Id override).
# Enforcement is deferred to the UI layer — the middleware always sets it.

# ── Lazy Initialization ──────────────────────────────────────────────

_config = None
_db = None
_evaluator = None


def _get_config():
    global _config
    if _config is None:
        _config = load_config()
    return _config


def _get_db():
    global _db
    if _db is None:
        from intaris.db import Database

        _db = Database(_get_config().db)
    return _db


def _get_evaluator():
    global _evaluator
    if _evaluator is None:
        from intaris.audit import AuditStore
        from intaris.evaluator import Evaluator
        from intaris.llm import LLMClient
        from intaris.session import SessionStore

        cfg = _get_config()
        db = _get_db()
        _evaluator = Evaluator(
            llm=LLMClient(cfg.llm),
            session_store=SessionStore(db),
            audit_store=AuditStore(db),
        )
    return _evaluator


# ── Health Endpoint ───────────────────────────────────────────────────


async def health_check(request: Request) -> JSONResponse:
    """Health check endpoint for Kubernetes readiness probes."""
    return JSONResponse(
        {
            "healthy": True,
            "service": "intaris",
            "version": __version__,
        }
    )


# ── API Key Middleware ────────────────────────────────────────────────


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Authenticate requests and resolve user identity.

    Identity resolution priority:
    1. API key mapping (INTARIS_API_KEYS): key → user_id binding
    2. Single API key (INTARIS_API_KEY): auth only, no user binding
    3. X-User-Id header: fallback when key doesn't bind a user
    4. No auth mode: if no keys configured, read identity from headers

    Agent identity is always read from X-Agent-Id header.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        cfg = _get_config()

        # Skip auth for health endpoint
        if request.url.path == "/health":
            return await call_next(request)

        try:
            has_auth = bool(cfg.server.api_keys or cfg.server.api_key)

            if has_auth:
                # Extract token from Authorization header
                token = _extract_token(request)
                if not token:
                    return JSONResponse(
                        {"error": "Missing API key"},
                        status_code=401,
                    )

                # Try multi-key mapping first
                mapped_user_id = _match_api_key(token, cfg.server.api_keys)
                if mapped_user_id is not None:
                    # Key found in api_keys
                    if mapped_user_id != "*":
                        _session_user_id.set(mapped_user_id)
                        _session_user_bound.set(True)
                    else:
                        # Wildcard key — auth OK but no user binding
                        _set_user_from_header(request)
                elif cfg.server.api_key and hmac.compare_digest(
                    token, cfg.server.api_key
                ):
                    # Matched single api_key — auth OK, no user binding
                    _set_user_from_header(request)
                else:
                    return JSONResponse(
                        {"error": "Invalid API key"},
                        status_code=401,
                    )
            else:
                # No auth configured (dev mode) — read identity from headers
                _set_user_from_header(request)

            # Always read agent_id from header
            agent_id = request.headers.get("x-agent-id", "").strip() or None
            _session_agent_id.set(agent_id)

            return await call_next(request)
        finally:
            # Always reset ContextVars to prevent identity leakage
            _session_user_id.set(None)
            _session_agent_id.set(None)
            _session_user_bound.set(False)


def _extract_token(request: Request) -> str:
    """Extract API token from request headers."""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    # Fallback to X-API-Key header
    return request.headers.get("x-api-key", "").strip() or auth.strip()


def _match_api_key(token: str, api_keys: dict[str, str]) -> str | None:
    """Match a token against the api_keys mapping.

    Iterates ALL keys to prevent timing side-channels that could reveal
    key count or ordering. Uses constant-time comparison for each key.

    Returns:
        The mapped user_id (or "*" for wildcard), or None if no match.
    """
    matched_user: str | None = None
    for key, user_id in api_keys.items():
        if hmac.compare_digest(token, key):
            matched_user = user_id
    return matched_user


def _set_user_from_header(request: Request) -> None:
    """Set user_id from request headers (fallback when key doesn't bind)."""
    user_id = request.headers.get("x-user-id", "").strip() or None
    _session_user_id.set(user_id)


# ── Application Factory ──────────────────────────────────────────────


@contextlib.asynccontextmanager
async def lifespan(app):
    """Application lifespan: initialize on startup, cleanup on shutdown."""
    logger.info("Intaris %s starting up", __version__)

    # Initialize database (creates tables if needed)
    _get_db()
    logger.info("Database initialized")

    # Initialize rate limiter
    cfg = _get_config()
    from intaris.ratelimit import RateLimiter

    app.state.rate_limiter = RateLimiter(
        max_calls=cfg.server.rate_limit, window_seconds=60
    )
    logger.info("Rate limiter initialized (max=%d/min)", cfg.server.rate_limit)

    # Initialize webhook client
    from intaris.webhook import WebhookClient

    app.state.webhook = WebhookClient(cfg.webhook)
    if app.state.webhook.is_configured():
        logger.info("Webhook client initialized (url=%s)", cfg.webhook.url)
    else:
        logger.info("Webhook not configured (standalone mode)")

    # Initialize EventBus
    from intaris.api.stream import EventBus

    app.state.event_bus = EventBus()
    logger.info("EventBus initialized")

    # Propagate state to FastAPI sub-app so endpoints can access it
    # via request.app.state (request.app is the sub-app, not parent)
    api_app = getattr(app.state, "_api_app", None)
    if api_app is not None:
        api_app.state.rate_limiter = app.state.rate_limiter
        api_app.state.webhook = app.state.webhook
        api_app.state.event_bus = app.state.event_bus

    yield

    # Cleanup
    await app.state.webhook.close()
    db = _get_db()
    db.close()
    logger.info("Intaris shutting down")


def create_app() -> Starlette:
    """Create the Starlette application."""
    middleware = [Middleware(APIKeyMiddleware)]

    from intaris.api.stream import stream_websocket

    api_app = _create_api_app()

    routes = [
        Route("/health", health_check),
        Mount("/api/v1", app=api_app),
        WebSocketRoute("/api/v1/stream", stream_websocket),
    ]

    starlette_app = Starlette(
        routes=routes,
        middleware=middleware,
        lifespan=lifespan,
    )
    # Store reference to sub-app so lifespan can propagate state
    starlette_app.state._api_app = api_app
    return starlette_app


def _create_api_app():
    """Create the FastAPI sub-application for REST API."""
    from intaris.api import create_api_app

    return create_api_app()


app = create_app()


def main():
    """Entry point for the intaris server."""
    cfg = _get_config()
    logger.info("Starting Intaris on %s:%d", cfg.server.host, cfg.server.port)
    uvicorn.run(
        "intaris.server:app",
        host=cfg.server.host,
        port=cfg.server.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
