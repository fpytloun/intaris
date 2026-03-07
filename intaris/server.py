"""HTTP server for intaris.

FastAPI application with health endpoint, API key authentication
middleware, and REST API sub-app for safety evaluation endpoints.
"""

from __future__ import annotations

import contextlib
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
from starlette.routing import Mount, Route

from intaris import __version__
from intaris.config import load_config

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("intaris")

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
    """Authenticate requests using API key in Authorization header.

    If INTARIS_API_KEY is not set, authentication is disabled (dev mode).
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        cfg = _get_config()

        # Skip auth for health endpoint
        if request.url.path == "/health":
            return await call_next(request)

        # If no API key configured, skip auth (dev mode)
        if not cfg.server.api_key:
            return await call_next(request)

        # Check Authorization header
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
        else:
            token = auth

        if not token or not hmac.compare_digest(token, cfg.server.api_key):
            return JSONResponse(
                {"error": "Invalid or missing API key"},
                status_code=401,
            )

        return await call_next(request)


# ── Application Factory ──────────────────────────────────────────────


@contextlib.asynccontextmanager
async def lifespan(app):
    """Application lifespan: initialize on startup, cleanup on shutdown."""
    logger.info("Intaris %s starting up", __version__)

    # Initialize database (creates tables if needed)
    _get_db()
    logger.info("Database initialized")

    yield

    # Cleanup
    db = _get_db()
    db.close()
    logger.info("Intaris shutting down")


def create_app() -> Starlette:
    """Create the Starlette application."""
    middleware = [Middleware(APIKeyMiddleware)]

    routes = [
        Route("/health", health_check),
        Mount("/api/v1", app=_create_api_app()),
    ]

    return Starlette(
        routes=routes,
        middleware=middleware,
        lifespan=lifespan,
    )


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
