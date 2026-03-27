"""EventBus and WebSocket streaming for real-time evaluation events.

Provides an in-process pub/sub EventBus and a WebSocket endpoint at
/api/v1/stream for real-time monitoring of evaluation decisions.

Authentication uses a first-message protocol: the client connects,
sends {"type": "auth", "token": "Bearer ..."} as the first message,
and the server validates the token before streaming events.

Events are filtered by user_id at publish time (multi-tenancy isolation)
and optionally by session_id. Each subscriber has a bounded queue
(maxsize=1000) with drop-oldest overflow handling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

# Maximum events buffered per subscriber before dropping oldest.
_MAX_QUEUE_SIZE = 1000

# Timeout for the first auth message (seconds).
_AUTH_TIMEOUT = 5.0

# Ping interval to keep connections alive through proxies (seconds).
_PING_INTERVAL = 30.0

# Maximum WebSocket connections per user.
_MAX_CONNECTIONS_PER_USER = 10


class EventBus:
    """In-process pub/sub for real-time event streaming.

    Subscribers are keyed by (user_id, session_id) for multi-tenant
    isolation. Events are only delivered to subscribers whose user_id
    matches the event's user_id.

    Thread-safe: publish() can be called from synchronous code (uses
    put_nowait on asyncio.Queue).
    """

    def __init__(self):
        # Key: (user_id, session_id | None) → list of subscriber queues
        self._subscribers: dict[tuple[str, str | None], list[asyncio.Queue]] = {}
        self._counter: int = 0
        self._lock = threading.Lock()

    def subscribe(self, user_id: str, session_id: str | None = None) -> asyncio.Queue:
        """Subscribe to events for a user (and optionally a session).

        Args:
            user_id: Tenant identifier (required for isolation).
            session_id: Optional session filter. None means all sessions.

        Returns:
            An asyncio.Queue that will receive matching events.

        Raises:
            ValueError: If the per-user connection limit is exceeded.
        """
        key = (user_id, session_id)
        with self._lock:
            # Check per-user connection limit
            user_count = sum(
                len(queues)
                for (uid, _), queues in self._subscribers.items()
                if uid == user_id
            )
            if user_count >= _MAX_CONNECTIONS_PER_USER:
                raise ValueError(
                    f"Connection limit exceeded: max {_MAX_CONNECTIONS_PER_USER} "
                    f"per user"
                )

            queue: asyncio.Queue = asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)
            if key not in self._subscribers:
                self._subscribers[key] = []
            self._subscribers[key].append(queue)
            logger.debug(
                "EventBus: subscribed user=%s session=%s (total=%d)",
                user_id,
                session_id,
                user_count + 1,
            )
            return queue

    def unsubscribe(
        self, user_id: str, session_id: str | None, queue: asyncio.Queue
    ) -> None:
        """Remove a subscriber.

        Args:
            user_id: Tenant identifier.
            session_id: Session filter used when subscribing.
            queue: The queue returned by subscribe().
        """
        key = (user_id, session_id)
        with self._lock:
            queues = self._subscribers.get(key, [])
            try:
                queues.remove(queue)
            except ValueError:
                pass
            if not queues and key in self._subscribers:
                del self._subscribers[key]
            logger.debug(
                "EventBus: unsubscribed user=%s session=%s", user_id, session_id
            )

    def publish(self, event: dict[str, Any]) -> None:
        """Publish an event to matching subscribers.

        Synchronous method — safe to call from non-async code.
        Uses put_nowait() on asyncio.Queue. On overflow, drops the
        oldest event and logs a warning.

        The event must contain "user_id" and "session_id" keys for
        routing. A monotonic "seq" counter is added to each event.

        Args:
            event: Event dict with at least "user_id" and "session_id".
        """
        event_user_id = event.get("user_id")
        event_session_id = event.get("session_id")

        if not event_user_id:
            logger.warning(
                "EventBus: event missing user_id, dropping (type=%s)",
                event.get("type", "unknown"),
            )
            return

        with self._lock:
            self._counter += 1
            event = {**event, "seq": self._counter}

            # Find matching subscribers:
            # 1. Exact match: (user_id, session_id)
            # 2. Global for user: (user_id, None)
            targets: list[asyncio.Queue] = []
            exact_key = (event_user_id, event_session_id)
            global_key = (event_user_id, None)

            if exact_key in self._subscribers:
                targets.extend(self._subscribers[exact_key])
            if event_session_id is not None and global_key in self._subscribers:
                targets.extend(self._subscribers[global_key])

            for queue in targets:
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    # Drop oldest event to make room
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        queue.put_nowait(event)
                    except asyncio.QueueFull:
                        pass
                    logger.warning(
                        "EventBus: queue overflow for user=%s, dropped oldest event",
                        event_user_id,
                    )


async def stream_websocket(websocket: WebSocket) -> None:
    """WebSocket endpoint for real-time event streaming.

    Authentication protocol:
    1. Accept the WebSocket connection
    2. Wait for first message (5s timeout)
    3. Parse as JSON: {"type": "auth", "token": "Bearer ...", "session_id": "..."}
    4. Validate token using the same auth logic as REST API
    5. If valid: start streaming events
    6. If invalid: close with code 4001

    Events are JSON objects with a "seq" field for gap detection.
    Ping frames are sent every 30s to keep connections alive.

    Args:
        websocket: Starlette WebSocket instance.
    """
    await websocket.accept()

    # Get EventBus from app state
    event_bus: EventBus | None = getattr(websocket.app.state, "event_bus", None)
    if event_bus is None:
        await websocket.close(code=1011, reason="EventBus not available")
        return

    # Step 1: Wait for auth message
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=_AUTH_TIMEOUT)
    except asyncio.TimeoutError:
        await websocket.close(code=4001, reason="Authentication timeout")
        return
    except WebSocketDisconnect:
        return

    # Step 2: Parse auth message
    try:
        auth_msg = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        await websocket.close(code=4001, reason="Invalid auth message format")
        return

    if not isinstance(auth_msg, dict) or auth_msg.get("type") != "auth":
        await websocket.close(code=4001, reason="Expected auth message")
        return

    token = auth_msg.get("token", "")
    if isinstance(token, str) and token.startswith("Bearer "):
        token = token[7:]

    # Step 3: Validate token
    msg_user_id = auth_msg.get("user_id")
    if isinstance(msg_user_id, str):
        msg_user_id = msg_user_id.strip() or None
    else:
        msg_user_id = None

    user_id = _authenticate_token(websocket, token, fallback_user_id=msg_user_id)
    if user_id is None:
        await websocket.close(code=4001, reason="Authentication failed")
        return

    # Step 4: Subscribe to events
    session_id = auth_msg.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        session_id = str(session_id)

    try:
        queue = event_bus.subscribe(user_id, session_id)
    except ValueError as e:
        await websocket.close(code=4008, reason=str(e))
        return

    logger.info("WebSocket connected: user=%s session=%s", user_id, session_id)

    # Step 5: Stream events with ping keepalive
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=_PING_INTERVAL)
                await websocket.send_json(event)
            except asyncio.TimeoutError:
                # Send ping to keep connection alive
                await websocket.send_json({"type": "ping"})
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected: user=%s session=%s", user_id, session_id)
    except asyncio.CancelledError:
        logger.info(
            "WebSocket cancelled (shutdown): user=%s session=%s", user_id, session_id
        )
    except Exception:
        logger.exception("WebSocket error: user=%s session=%s", user_id, session_id)
    finally:
        event_bus.unsubscribe(user_id, session_id, queue)


def _authenticate_token(
    websocket: WebSocket,
    token: str,
    fallback_user_id: str | None = None,
) -> str | None:
    """Validate a WebSocket auth token and return the user_id.

    Uses the same authentication logic as the REST API middleware.
    When the token authenticates but does not bind to a specific user
    (dev mode, wildcard key, single shared key), falls back to the
    user_id provided in the auth message — mirroring how the REST
    middleware falls back to the X-User-Id header.

    Args:
        websocket: WebSocket instance (for accessing app config).
        token: API token to validate.
        fallback_user_id: user_id from the auth message (used when
            the token doesn't bind a user).

    Returns:
        The resolved user_id, or None if authentication fails.
    """
    from intaris.auth import resolve_auth
    from intaris.server import _get_config

    cfg = _get_config()
    has_auth = bool(
        cfg.server.api_keys
        or cfg.server.api_key
        or cfg.server.jwt_public_key
        or cfg.server.jwks_url
    )

    if not has_auth:
        # No auth configured (dev mode) — accept any connection,
        # use user_id from auth message
        return fallback_user_id

    if not token:
        return None

    resolution = resolve_auth(
        token=token,
        header_user_id=fallback_user_id,
        header_agent_id=None,
        api_key=cfg.server.api_key,
        api_keys=cfg.server.api_keys,
        jwt_public_key=cfg.server.jwt_public_key,
        jwks_url=cfg.server.jwks_url,
        allow_no_auth=False,
    )
    if resolution is None:
        return None
    return resolution.user_id
