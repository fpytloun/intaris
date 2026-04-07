"""HTTP server for intaris.

FastAPI application with health endpoint, API key authentication
middleware with multi-tenant user_id resolution, REST API sub-app
for safety evaluation endpoints, and MCP proxy server.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
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
from intaris.auth import match_api_key, resolve_auth
from intaris.config import load_config

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
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


def _get_evaluator(alignment_barrier=None):
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
            alignment_barrier=alignment_barrier,
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

        # Skip auth for health endpoint, static UI files, action tokens,
        # and the exchange token endpoint (it validates the token itself).
        if (
            request.url.path in ("/", "/health")
            or request.url.path
            in (
                "/ui",
                "/ui/",
            )
            or request.url.path.startswith("/ui/")
            or request.url.path.startswith("/api/v1/action/")
            or request.url.path == "/api/v1/auth/exchange"
        ):
            return await call_next(request)

        try:
            from intaris.sanitize import validate_agent_id

            raw_agent_id = request.headers.get("x-agent-id", "").strip() or None
            header_agent_id = validate_agent_id(raw_agent_id) if raw_agent_id else None
            header_user_id = request.headers.get("x-user-id", "").strip() or None

            has_auth = bool(
                cfg.server.api_keys
                or cfg.server.api_key
                or cfg.server.jwt_public_key
                or cfg.server.jwks_url
            )

            if has_auth:
                # Check exchange session cookie (cross-service SSO via token exchange).
                # Only checked when auth is configured — prevents stale cookies from
                # authenticating requests when auth is later disabled.
                from intaris.api.auth import _COOKIE_NAME, get_exchange_session

                exchange_cookie = request.cookies.get(_COOKIE_NAME, "")
                if exchange_cookie:
                    exchange_session = get_exchange_session(exchange_cookie)
                    if exchange_session:
                        logger.info(
                            "Auth resolved via exchange session for user=%s",
                            exchange_session.user_id,
                        )
                        _session_user_id.set(exchange_session.user_id)
                        _session_agent_id.set(
                            exchange_session.agent_id or header_agent_id
                        )
                        _session_user_bound.set(True)
                        intention = (
                            request.headers.get("x-intaris-intention", "").strip()
                            or None
                        )
                        _session_intention.set(intention)
                        return await call_next(request)

                # Extract token from Authorization header
                token = _extract_token(request)
                if not token:
                    return JSONResponse(
                        {"error": "Missing credentials"},
                        status_code=401,
                    )

                resolution = resolve_auth(
                    token=token,
                    header_user_id=header_user_id,
                    header_agent_id=header_agent_id,
                    api_key=cfg.server.api_key,
                    api_keys=cfg.server.api_keys,
                    jwt_public_key=cfg.server.jwt_public_key,
                    jwks_url=cfg.server.jwks_url,
                    allow_no_auth=False,
                )
                if resolution is None:
                    return JSONResponse(
                        {"error": "Invalid credentials"},
                        status_code=401,
                    )
            else:
                resolution = resolve_auth(
                    token="",
                    header_user_id=header_user_id,
                    header_agent_id=header_agent_id,
                    api_key=cfg.server.api_key,
                    api_keys=cfg.server.api_keys,
                    jwt_public_key=cfg.server.jwt_public_key,
                    jwks_url=cfg.server.jwks_url,
                    allow_no_auth=True,
                )

            _session_user_id.set(resolution.user_id if resolution else None)
            _session_agent_id.set(resolution.agent_id if resolution else None)
            _session_user_bound.set(resolution.user_bound if resolution else False)

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
    """Extract API token from request headers or cognis_session cookie."""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    # Fallback to X-API-Key header
    key = request.headers.get("x-api-key", "").strip()
    if key:
        return key
    # Fallback to cognis_session cookie (cross-service SSO)
    cookie = request.cookies.get("cognis_session", "")
    if cookie:
        return cookie
    return auth.strip()


def _match_api_key(token: str, api_keys: dict[str, str]) -> str | None:
    """Match a token against the api_keys mapping.

    Iterates ALL keys to prevent timing side-channels that could reveal
    key count or ordering. Uses constant-time comparison for each key.

    Returns:
        The mapped user_id (or "*" for wildcard), or None if no match.
    """
    return match_api_key(token, api_keys)


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
        from urllib.parse import urlparse

        _parsed = urlparse(cfg.webhook.url)
        _masked = f"{_parsed.scheme}://{_parsed.hostname}"
        if _parsed.port:
            _masked += f":{_parsed.port}"
        logger.info("Webhook client initialized (url=%s)", _masked)
    else:
        logger.info("Webhook not configured (standalone mode)")

    # Initialize EventBus
    from intaris.api.stream import EventBus

    app.state.event_bus = EventBus()
    logger.info("EventBus initialized")

    # Initialize intention barrier for immediate user-driven updates
    from intaris.intention import (
        _DEFAULT_POLL_TIMEOUT_MS,
        _DEFAULT_TIMEOUT_MS,
        IntentionBarrier,
    )
    from intaris.llm import LLMClient

    barrier_timeout_ms = int(
        os.environ.get("INTENTION_BARRIER_TIMEOUT_MS", str(_DEFAULT_TIMEOUT_MS))
    )
    barrier_poll_timeout_ms = int(
        os.environ.get(
            "INTENTION_BARRIER_POLL_TIMEOUT_MS", str(_DEFAULT_POLL_TIMEOUT_MS)
        )
    )
    analysis_llm: LLMClient | None = None
    if cfg.analysis.enabled and cfg.llm_analysis.api_key:
        analysis_llm = LLMClient(cfg.llm_analysis)
        app.state.intention_barrier = IntentionBarrier(
            db=_get_db(),
            llm=analysis_llm,
            timeout_ms=barrier_timeout_ms,
            poll_timeout_ms=barrier_poll_timeout_ms,
        )
        app.state.intention_barrier.set_event_bus(app.state.event_bus)
        logger.info(
            "Intention barrier initialized (timeout=%dms, poll_timeout=%dms)",
            barrier_timeout_ms,
            barrier_poll_timeout_ms,
        )
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

    # Restore alignment overrides from database and re-trigger alignment
    # checks for active child sessions that haven't been overridden.
    if app.state.alignment_barrier is not None:
        app.state.alignment_barrier.restore_overrides()

        # Re-trigger alignment checks for active child sessions that
        # haven't been overridden. This closes the post-restart gap
        # where _misaligned is empty after server restart.
        from intaris.session import SessionStore as _SS

        _startup_sessions = _SS(_get_db()).get_active_child_sessions()
        for _s in _startup_sessions:
            await app.state.alignment_barrier.trigger(_s["user_id"], _s["session_id"])
        if _startup_sessions:
            logger.info(
                "Startup: triggered alignment checks for %d active child sessions",
                len(_startup_sessions),
            )

    # Eagerly initialize the evaluator with alignment_barrier reference
    # so it's available for both REST API and MCP proxy.
    _get_evaluator(alignment_barrier=app.state.alignment_barrier)

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
    background_worker.set_evaluator(_get_evaluator())
    background_worker.set_alignment_barrier(app.state.alignment_barrier)
    await background_worker.start()
    logger.info(
        "Background worker initialized (analysis_enabled=%s)",
        cfg.analysis.enabled,
    )

    # Initialize notification dispatcher
    from intaris.notifications.dispatcher import NotificationDispatcher

    notification_dispatcher = NotificationDispatcher(
        db=_get_db(),
        encryption_key=cfg.mcp.encryption_key,
        base_url=cfg.webhook.base_url,
    )
    app.state.notification_dispatcher = notification_dispatcher
    background_worker.set_notification_dispatcher(notification_dispatcher)
    logger.info("Notification dispatcher initialized")

    # Initialize judge reviewer for auto-resolution of escalations
    if cfg.judge.mode != "disabled":
        from intaris.audit import AuditStore as _AuditStore
        from intaris.judge import JudgeReviewer
        from intaris.llm import LLMClient as _LLMClient
        from intaris.session import SessionStore as _SessionStore

        judge_reviewer = JudgeReviewer(
            llm=_LLMClient(cfg.llm_judge),
            config=cfg.judge,
            audit_store=_AuditStore(_get_db()),
            session_store=_SessionStore(_get_db()),
            evaluator=_get_evaluator(),
            intention_barrier=app.state.intention_barrier,
            alignment_barrier=app.state.alignment_barrier,
            event_bus=app.state.event_bus,
            notification_dispatcher=notification_dispatcher,
            metrics=background_worker.metrics,
        )
        app.state.judge_reviewer = judge_reviewer
        logger.info(
            "Judge reviewer initialized (mode=%s, notify=%s)",
            cfg.judge.mode,
            cfg.judge.notify_mode,
        )
    else:
        app.state.judge_reviewer = None
        logger.info("Judge reviewer disabled")

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

    # Set judge reviewer on MCP proxy for escalation auto-resolution
    if mcp_proxy is not None and app.state.judge_reviewer is not None:
        mcp_proxy.set_judge_reviewer(app.state.judge_reviewer)

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
        api_app.state.judge_reviewer = app.state.judge_reviewer

    try:
        if mcp_proxy is not None:
            await mcp_proxy.start()
            # The session manager requires run() as an async context manager
            # to manage its internal task group for handling MCP sessions.
            async with mcp_proxy.session_manager.run():
                logger.info("MCP proxy initialized")
                yield
                # Cleanup MCP proxy before exiting the session manager context.
                # Timeout prevents hanging on unresponsive upstream servers.
                try:
                    await asyncio.wait_for(mcp_proxy.shutdown(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning("MCP proxy shutdown timed out (5s)")
        else:
            yield
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Shutdown interrupted — running cleanup")
        # Best-effort MCP proxy cleanup if interrupted before normal exit
        if mcp_proxy is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(mcp_proxy.shutdown(), timeout=3.0)
    finally:
        # Cleanup always runs, even on CancelledError/KeyboardInterrupt.
        # Each step is individually guarded with timeouts so one failure
        # or hang doesn't block the rest.
        _mcp_proxy_ref = None

        # Flush event store buffers (synchronous — fast)
        if app.state.event_store is not None:
            with contextlib.suppress(Exception):
                app.state.event_store.flush_all()
                logger.info("Event store flushed")

        # Stop background worker (5s timeout — has its own internal timeout)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(background_worker.stop(), timeout=5.0)

        # Close webhook client (2s timeout)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(app.state.webhook.close(), timeout=2.0)

        # Close notification dispatcher (2s timeout)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(notification_dispatcher.close(), timeout=2.0)

        # Close database (synchronous)
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

    server_store = MCPServerStore(db, cfg.mcp.encryption_key)

    conn_mgr = MCPConnectionManager(
        upstream_timeout_ms=cfg.mcp.upstream_timeout_ms,
        allow_stdio=cfg.mcp.allow_stdio,
        cache_dir=cfg.mcp.cache_dir,
        server_store=server_store,
    )

    return MCPProxy(
        connection_manager=conn_mgr,
        evaluator=_get_evaluator(),
        session_store=SessionStore(db),
        audit_store=AuditStore(db),
        server_store=server_store,
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
            qs = str(request.query_params)
            target = f"/ui/?{qs}" if qs else "/ui/"
            return RedirectResponse(target, status_code=302)

        async def _root_redirect(request: Request) -> RedirectResponse:
            qs = str(request.query_params)
            target = f"/ui/?{qs}" if qs else "/ui/"
            return RedirectResponse(target, status_code=302)

        routes.append(Route("/", _root_redirect))
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
