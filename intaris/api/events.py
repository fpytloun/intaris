"""Session event recording API endpoints.

Provides endpoints for:
- POST /session/{id}/events — append events (single or batch)
- GET /session/{id}/events — read events with pagination and filtering
- POST /session/{id}/events/flush — force flush buffered events
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from intaris.api.deps import SessionContext, get_session_context
from intaris.api.schemas import (
    EventAppendResponse,
    EventReadResponse,
    SessionEvent,
)
from intaris.events.store import VALID_EVENT_TYPES

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_event_store(request: Request):
    """Get the event store from app state, or raise 404 if disabled."""
    event_store = getattr(request.app.state, "event_store", None)
    if event_store is None:
        raise HTTPException(
            status_code=404,
            detail="Event store is not enabled. Set EVENT_STORE_ENABLED=true.",
        )
    return event_store


def _validate_session_exists(request: Request, user_id: str, session_id: str) -> None:
    """Validate that the session exists and belongs to the user."""
    from intaris.server import _get_db
    from intaris.session import SessionStore

    db = _get_db()
    store = SessionStore(db)
    try:
        store.get(session_id, user_id=user_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail=f"Session {session_id} not found.",
        )


@router.post(
    "/session/{session_id}/events",
    response_model=EventAppendResponse,
    summary="Append events to session recording",
    description=(
        "Append one or more events to a session's event log. "
        "Events are buffered and flushed to storage periodically. "
        "Each event must have a 'type' and 'data' field."
    ),
)
async def append_events(
    session_id: str,
    events: list[SessionEvent] | SessionEvent,
    request: Request,
    ctx: SessionContext = Depends(get_session_context),
) -> EventAppendResponse:
    """Append events to a session's event log."""
    event_store = _get_event_store(request)

    # Normalize single event to list
    if isinstance(events, SessionEvent):
        events = [events]

    if not events:
        raise HTTPException(status_code=400, detail="No events provided.")

    # Limit batch size to prevent memory exhaustion
    _MAX_BATCH_SIZE = 1000
    if len(events) > _MAX_BATCH_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"Too many events ({len(events)}). Max {_MAX_BATCH_SIZE} per request.",
        )

    # Validate event types
    for event in events:
        if event.type not in VALID_EVENT_TYPES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Invalid event type: {event.type!r}. "
                    f"Valid types: {', '.join(sorted(VALID_EVENT_TYPES))}"
                ),
            )

    # Validate session exists
    _validate_session_exists(request, ctx.user_id, session_id)

    # Determine source from request header or default
    source = request.headers.get("X-Intaris-Source", "client")

    # Convert to dicts for the event store
    event_dicts = [{"type": e.type, "data": e.data} for e in events]

    try:
        seqs = event_store.append(ctx.user_id, session_id, event_dicts, source=source)
    except Exception as e:
        logger.exception("Failed to append events for %s/%s", ctx.user_id, session_id)
        raise HTTPException(status_code=500, detail=f"Failed to append events: {e}")

    return EventAppendResponse(
        count=len(seqs),
        first_seq=seqs[0],
        last_seq=seqs[-1],
    )


@router.get(
    "/session/{session_id}/events",
    response_model=EventReadResponse,
    summary="Read session events",
    description=(
        "Read events from a session's event log with optional filtering. "
        "Supports pagination via after_seq and limit parameters."
    ),
)
async def read_events(
    session_id: str,
    request: Request,
    ctx: SessionContext = Depends(get_session_context),
    after_seq: int = Query(0, ge=0, description="Return events with seq > this value"),
    limit: int = Query(0, ge=0, description="Max events to return (0 = all)"),
    type: str | None = Query(
        None,
        description="Comma-separated event type filter (e.g., 'tool_call,evaluation')",
    ),
    source: str | None = Query(
        None,
        description="Comma-separated source include filter (e.g., 'opencode,client')",
    ),
    exclude_source: str | None = Query(
        None,
        description="Comma-separated source exclude filter (e.g., 'intaris')",
    ),
) -> EventReadResponse:
    """Read events from a session's event log."""
    event_store = _get_event_store(request)

    # Parse type filter
    event_types: set[str] | None = None
    if type:
        event_types = set(type.split(","))
        invalid = event_types - VALID_EVENT_TYPES
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Invalid event type(s): {', '.join(sorted(invalid))}. "
                    f"Valid types: {', '.join(sorted(VALID_EVENT_TYPES))}"
                ),
            )

    # Parse source filter
    event_sources: set[str] | None = None
    if source:
        event_sources = set(source.split(","))
    event_exclude_sources: set[str] | None = None
    if exclude_source:
        event_exclude_sources = set(exclude_source.split(","))

    # Validate session exists
    _validate_session_exists(request, ctx.user_id, session_id)

    try:
        # Request one extra to determine has_more
        fetch_limit = limit + 1 if limit else 0
        events = event_store.read(
            ctx.user_id,
            session_id,
            after_seq=after_seq,
            limit=fetch_limit,
            event_types=event_types,
            sources=event_sources,
            exclude_sources=event_exclude_sources,
        )
    except Exception as e:
        logger.exception("Failed to read events for %s/%s", ctx.user_id, session_id)
        raise HTTPException(status_code=500, detail=f"Failed to read events: {e}")

    # Determine has_more
    has_more = False
    if limit and len(events) > limit:
        has_more = True
        events = events[:limit]

    last_seq = events[-1]["seq"] if events else after_seq

    return EventReadResponse(
        events=events,
        last_seq=last_seq,
        has_more=has_more,
    )


@router.post(
    "/session/{session_id}/events/flush",
    summary="Flush buffered events to storage",
    description=(
        "Force flush any buffered events for this session to storage. "
        "Useful before reading events that were just appended."
    ),
)
async def flush_events(
    session_id: str,
    request: Request,
    ctx: SessionContext = Depends(get_session_context),
) -> dict[str, Any]:
    """Force flush buffered events for a session."""
    event_store = _get_event_store(request)

    try:
        event_store.flush_session(ctx.user_id, session_id)
    except Exception as e:
        logger.exception("Failed to flush events for %s/%s", ctx.user_id, session_id)
        raise HTTPException(status_code=500, detail=f"Failed to flush events: {e}")

    return {"ok": True}
