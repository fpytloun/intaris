"""HTTP server for intaris.

FastAPI application with health endpoint, API key authentication
middleware with multi-tenant user_id resolution, REST API sub-app
for safety evaluation endpoints, and MCP proxy server.
"""

from __future__ import annotations

import asyncio
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

_session_intention: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "session_intention", default=None
)
# Optional intention hint for MCP proxy session auto-creation.
# Set from X-Intaris-Intention header.

# ── Lazy Initialization ──────────────────────────────────────────────

_config = None
_db = None
_evaluator = None
_mcp_proxy_ref = None  # Module-level reference for _MCPEndpoint ASGI wrapper


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
            db=db,
            analysis_config=cfg.analysis,
        )
    return _evaluator


# ── Health Endpoint ───────────────────────────────────────────────────


async def health_check(request: Request) -> JSONResponse:
    """Health check endpoint for Kubernetes readiness probes.

    Includes behavioral analysis pipeline status when analysis is enabled.
    """
    response: dict = {
        "healthy": True,
        "service": "intaris",
        "version": __version__,
    }

    # Include analysis pipeline status if available
    try:
        cfg = _get_config()
        if cfg.analysis.enabled:
            worker = getattr(request.app.state, "background_worker", None)
            if worker is not None:
                stats = worker._task_queue.get_queue_stats()
            else:
                from intaris.background import TaskQueue

                stats = TaskQueue(_get_db()).get_queue_stats()
            response["analysis"] = {
                "enabled": True,
                "queue": stats,
            }
    except Exception:
        pass  # Health check should never fail due to analysis

    # Include intention barrier metrics if available
    try:
        barrier = getattr(request.app.state, "intention_barrier", None)
        if barrier is not None:
            response["intention_barrier"] = barrier.metrics()
    except Exception:
        pass  # Health check should never fail due to barrier

    # Include alignment barrier metrics if available
    try:
        alignment_barrier = getattr(request.app.state, "alignment_barrier", None)
        if alignment_barrier is not None:
            response["alignment_barrier"] = alignment_barrier.metrics()
    except Exception:
        pass  # Health check should never fail due to barrier

    return JSONResponse(response)


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

        # Skip auth for health endpoint, static UI files, and action tokens
        if (
            request.url.path == "/health"
            or request.url.path
            in (
                "/ui",
                "/ui/",
            )
            or request.url.path.startswith("/ui/")
            or request.url.path.startswith("/api/v1/action/")
        ):
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

            # Read optional intention hint for MCP proxy sessions
            intention = request.headers.get("x-intaris-intention", "").strip() or None
            _session_intention.set(intention)

            return await call_next(request)
        finally:
            # Always reset ContextVars to prevent identity leakage
            _session_user_id.set(None)
            _session_agent_id.set(None)
            _session_user_bound.set(False)
            _session_intention.set(None)


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


# ── MCP Endpoint ─────────────────────────────────────────────────────


class _MCPEndpoint:
    """ASGI wrapper for the MCP session manager.

    Delegates to the MCPProxy's session manager, which is initialized
    during the lifespan. Before initialization, returns 503.

    This is mounted at /mcp and handles all MCP protocol traffic.
    The auth middleware has already run, so ContextVars are set.

    Uses a module-level reference to the proxy instead of scope["app"]
    because Starlette's Mount sets scope["app"] to the Mount instance,
    not the root app — so state would not be accessible.
    """

    async def __call__(self, scope, receive, send):
        """ASGI interface."""
        if scope["type"] not in ("http", "websocket"):
            return

        # Get the MCP proxy from the module-level reference.
        mcp_proxy = _mcp_proxy_ref

        if mcp_proxy is None:
            # MCP proxy not initialized — return 503.
            if scope["type"] == "http":
                await send(
                    {
                        "type": "http.response.start",
                        "status": 503,
                        "headers": [
                            [b"content-type", b"application/json"],
                        ],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b'{"error": "MCP proxy not available"}',
                    }
                )
            return

        # Delegate to the session manager's ASGI handler.
        await mcp_proxy.session_manager.handle_request(scope, receive, send)


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

    # Initialize intention barrier for immediate user-driven updates
    from intaris.intention import IntentionBarrier
    from intaris.llm import LLMClient

    barrier_timeout_ms = int(os.environ.get("INTENTION_BARRIER_TIMEOUT_MS", "1000"))
    analysis_llm: LLMClient | None = None
    if cfg.analysis.enabled and cfg.llm_analysis.api_key:
        analysis_llm = LLMClient(cfg.llm_analysis)
        app.state.intention_barrier = IntentionBarrier(
            db=_get_db(),
            llm=analysis_llm,
            timeout_ms=barrier_timeout_ms,
        )
        app.state.intention_barrier.set_event_bus(app.state.event_bus)
        logger.info("Intention barrier initialized (timeout=%dms)", barrier_timeout_ms)
    else:
        app.state.intention_barrier = None
        logger.info("Intention barrier not initialized (analysis disabled)")

    # Initialize alignment barrier for parent/child intention enforcement
    from intaris.alignment import AlignmentBarrier

    alignment_timeout_ms = int(os.environ.get("ALIGNMENT_BARRIER_TIMEOUT_MS", "15000"))
    if cfg.analysis.enabled and analysis_llm is not None:
        app.state.alignment_barrier = AlignmentBarrier(
            db=_get_db(),
            llm=analysis_llm,
            timeout_ms=alignment_timeout_ms,
        )
        app.state.alignment_barrier.set_event_bus(app.state.event_bus)
        # Chain: IntentionBarrier → AlignmentBarrier for child sessions
        if app.state.intention_barrier is not None:
            app.state.intention_barrier.set_alignment_barrier(
                app.state.alignment_barrier
            )
        logger.info(
            "Alignment barrier initialized (timeout=%dms)", alignment_timeout_ms
        )
    else:
        app.state.alignment_barrier = None
        logger.info("Alignment barrier not initialized (analysis disabled)")

    # Initialize event store for session recording (before background worker
    # so the flush loop is included when the worker starts).
    from intaris.events.store import EventStore

    if cfg.event_store.enabled:
        event_store = EventStore(cfg.event_store)
        event_store.set_event_bus(app.state.event_bus)
        app.state.event_store = event_store
        logger.info("Event store initialized (backend=%s)", cfg.event_store.backend)
    else:
        app.state.event_store = None
        logger.info("Event store disabled")

    # Initialize background worker for behavioral analysis
    from intaris.background import BackgroundWorker, TaskQueue

    task_queue = TaskQueue(_get_db())
    background_worker = BackgroundWorker(
        db=_get_db(),
        config=cfg.analysis,
        task_queue=task_queue,
    )
    app.state.background_worker = background_worker
    background_worker.set_event_bus(app.state.event_bus)
    background_worker.set_event_store(app.state.event_store)
    if cfg.analysis.enabled:
        await background_worker.start()
        logger.info("Background worker started (analysis enabled)")
    else:
        logger.info("Background worker not started (analysis disabled)")

    # Initialize notification dispatcher
    from intaris.notifications.dispatcher import NotificationDispatcher

    notification_dispatcher = NotificationDispatcher(
        db=_get_db(),
        encryption_key=cfg.mcp.encryption_key,
        base_url=cfg.webhook.base_url,
    )
    app.state.notification_dispatcher = notification_dispatcher
    logger.info("Notification dispatcher initialized")

    # Warn if notification channels exist without encryption key
    if not cfg.mcp.encryption_key:
        try:
            with _get_db().cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM notification_channels "
                    "WHERE config_encrypted IS NOT NULL"
                )
                count = cur.fetchone()[0]
                if count > 0:
                    logger.warning(
                        "Found %d notification channel(s) with encrypted "
                        "secrets but INTARIS_ENCRYPTION_KEY is not set. "
                        "These channels will not be able to send "
                        "notifications until the key is provided.",
                        count,
                    )
        except Exception:
            pass  # Table may not exist yet on first run.

    # Initialize MCP proxy
    global _mcp_proxy_ref
    mcp_proxy = _init_mcp_proxy(cfg)
    app.state.mcp_proxy = mcp_proxy
    _mcp_proxy_ref = mcp_proxy

    # Propagate state to FastAPI sub-app so endpoints can access it
    # via request.app.state (request.app is the sub-app, not parent)
    api_app = getattr(app.state, "_api_app", None)
    if api_app is not None:
        api_app.state.rate_limiter = app.state.rate_limiter
        api_app.state.webhook = app.state.webhook
        api_app.state.event_bus = app.state.event_bus
        api_app.state.mcp_proxy = mcp_proxy
        api_app.state.background_worker = background_worker
        api_app.state.intention_barrier = app.state.intention_barrier
        api_app.state.alignment_barrier = app.state.alignment_barrier
        api_app.state.notification_dispatcher = notification_dispatcher
        api_app.state.event_store = app.state.event_store

    try:
        if mcp_proxy is not None:
            await mcp_proxy.start()
            # The session manager requires run() as an async context manager
            # to manage its internal task group for handling MCP sessions.
            async with mcp_proxy.session_manager.run():
                logger.info("MCP proxy initialized")
                yield
                # Cleanup MCP proxy before exiting the session manager context.
                await mcp_proxy.shutdown()
        else:
            yield
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Shutdown interrupted — running cleanup")
        # Best-effort MCP proxy cleanup if interrupted before normal exit
        if mcp_proxy is not None:
            with contextlib.suppress(Exception):
                await mcp_proxy.shutdown()
    finally:
        # Cleanup always runs, even on CancelledError/KeyboardInterrupt.
        # Each step is individually guarded so one failure doesn't block
        # the rest.
        _mcp_proxy_ref = None

        # Flush event store buffers (deterministic — no event loss)
        if app.state.event_store is not None:
            with contextlib.suppress(Exception):
                app.state.event_store.flush_all()
                logger.info("Event store flushed")

        with contextlib.suppress(Exception):
            await background_worker.stop()

        with contextlib.suppress(Exception):
            await app.state.webhook.close()

        with contextlib.suppress(Exception):
            await notification_dispatcher.close()

        with contextlib.suppress(Exception):
            db = _get_db()
            db.close()

        logger.info("Intaris shut down")


def _init_mcp_proxy(cfg):
    """Initialize the MCP proxy if configured.

    Returns MCPProxy instance or None if MCP proxy is not needed.
    The proxy is always created — it's lightweight and allows dynamic
    server configuration via the REST API even without a config file.
    """
    from intaris.audit import AuditStore
    from intaris.mcp.client import MCPConnectionManager
    from intaris.mcp.proxy import MCPProxy
    from intaris.mcp.store import MCPServerStore
    from intaris.session import SessionStore

    db = _get_db()

    # Warn if encrypted data exists without encryption key.
    if not cfg.mcp.encryption_key:
        try:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM mcp_servers "
                    "WHERE env_encrypted IS NOT NULL OR headers_encrypted IS NOT NULL"
                )
                count = cur.fetchone()[0]
                if count > 0:
                    logger.warning(
                        "Found %d MCP server(s) with encrypted secrets but "
                        "INTARIS_ENCRYPTION_KEY is not set. These servers will "
                        "not be able to connect until the key is provided.",
                        count,
                    )
        except Exception:
            pass  # Table may not exist yet on first run.

    # Sync file-based config if configured.
    if cfg.mcp.config_file:
        try:
            from intaris.mcp.config import sync_file_configs

            server_store = MCPServerStore(db, cfg.mcp.encryption_key)
            sync_file_configs(
                store=server_store,
                path=cfg.mcp.config_file,
            )
            logger.info("Synced MCP config from %s", cfg.mcp.config_file)
        except Exception:
            logger.exception("Failed to sync MCP config file")

    conn_mgr = MCPConnectionManager(
        upstream_timeout_ms=cfg.mcp.upstream_timeout_ms,
        allow_stdio=cfg.mcp.allow_stdio,
    )

    return MCPProxy(
        connection_manager=conn_mgr,
        evaluator=_get_evaluator(),
        session_store=SessionStore(db),
        audit_store=AuditStore(db),
        server_store=MCPServerStore(db, cfg.mcp.encryption_key),
        upstream_timeout_ms=cfg.mcp.upstream_timeout_ms,
    )


def create_app() -> Starlette:
    """Create the Starlette application."""
    from pathlib import Path

    from starlette.responses import RedirectResponse
    from starlette.staticfiles import StaticFiles

    middleware = [Middleware(APIKeyMiddleware)]

    from intaris.api.stream import stream_websocket

    api_app = _create_api_app()

    routes: list[Route | Mount | WebSocketRoute] = []

    # Mount management UI if static directory exists (graceful degradation)
    ui_static_dir = Path(__file__).parent / "ui" / "static"
    if ui_static_dir.is_dir() and any(ui_static_dir.iterdir()):

        async def _ui_redirect(request: Request) -> RedirectResponse:
            return RedirectResponse("/ui/", status_code=301)

        routes.append(Route("/ui", _ui_redirect))
        routes.append(Mount("/ui", app=StaticFiles(directory=ui_static_dir, html=True)))
        logger.info("Management UI mounted at /ui")

    # Mount MCP proxy endpoint. The session manager's handle_request is
    # an ASGI handler that processes MCP protocol messages over HTTP.
    # Auth middleware runs before this, setting ContextVars for identity.
    routes.append(
        Mount("/mcp", app=_MCPEndpoint()),
    )
    logger.info("MCP proxy endpoint mounted at /mcp")

    # Action token endpoints (unauthenticated — token is the auth).
    # Mounted as Starlette routes before the FastAPI sub-app so they
    # bypass the auth middleware and return HTML, not JSON.
    from intaris.api.actions import action_get, action_post

    routes.extend(
        [
            Route("/health", health_check),
            Route("/api/v1/action/{token:path}", action_get, methods=["GET"]),
            Route("/api/v1/action/{token:path}", action_post, methods=["POST"]),
            WebSocketRoute("/api/v1/stream", stream_websocket),
            Mount("/api/v1", app=api_app),
        ]
    )

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
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
