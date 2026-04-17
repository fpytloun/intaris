"""Session event recording API endpoints.

Provides endpoints for:
- POST /session/{id}/events — append events (single or batch)
- GET /session/{id}/events — read events with pagination and filtering
- POST /session/{id}/events/flush — force flush buffered events
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from intaris.api.deps import SessionContext, get_session_context
from intaris.api.schemas import (
    EventAppendResponse,
    EventReadResponse,
    SessionEvent,
)
from intaris.events.idempotency import EventAppendIdempotencyStore
from intaris.events.store import VALID_EVENT_TYPES

logger = logging.getLogger(__name__)

router = APIRouter()

_IDEMPOTENCY_KEY_RE = re.compile(r"^[a-zA-Z0-9._:@-]{1,256}$")
_IDEMPOTENCY_STALE_AFTER = timedelta(seconds=60)
_IDEMPOTENCY_RETENTION = timedelta(hours=24)
_IDEMPOTENCY_CLEANUP_INTERVAL_S = 300.0
_last_idempotency_cleanup_monotonic = 0.0


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


def _get_idempotency_store() -> EventAppendIdempotencyStore:
    """Return the DB-backed idempotency ledger helper."""
    from intaris.server import _get_db

    return EventAppendIdempotencyStore(_get_db())


def _response_from_record(record: dict[str, Any]) -> EventAppendResponse:
    """Build an append response from an idempotency ledger record."""
    return EventAppendResponse(
        count=int(record.get("count") or 0),
        first_seq=int(record.get("first_seq") or 0),
        last_seq=int(record.get("last_seq") or 0),
    )


async def _wait_for_completed_record(
    store: EventAppendIdempotencyStore,
    user_id: str,
    session_id: str,
    idempotency_key: str,
    *,
    attempts: int = 20,
    delay_seconds: float = 0.05,
) -> dict[str, Any] | None:
    """Wait briefly for an in-flight idempotent append to complete."""
    for _ in range(attempts):
        await asyncio.sleep(delay_seconds)
        record = store.get(user_id, session_id, idempotency_key)
        if record and record.get("status") == "completed":
            return record
    return store.get(user_id, session_id, idempotency_key)


def _validate_idempotency_key(session_id: str, idempotency_key: str) -> None:
    """Validate the optional idempotency key format."""
    if not _IDEMPOTENCY_KEY_RE.match(idempotency_key):
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid idempotency_key. Use up to 256 characters from "
                "[a-zA-Z0-9._:@-]."
            ),
        )
    if not idempotency_key.startswith(f"{session_id}:"):
        raise HTTPException(
            status_code=400,
            detail="idempotency_key must start with the current session_id",
        )


def _maybe_cleanup_idempotency_store(store: EventAppendIdempotencyStore) -> None:
    """Run retention cleanup periodically instead of on every request."""
    global _last_idempotency_cleanup_monotonic

    now = time.monotonic()
    if now - _last_idempotency_cleanup_monotonic < _IDEMPOTENCY_CLEANUP_INTERVAL_S:
        return
    store.delete_expired(max_age=_IDEMPOTENCY_RETENTION)
    _last_idempotency_cleanup_monotonic = now


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
    idempotency_key: str | None = Query(
        None,
        description="Optional idempotency key for duplicate-safe event append retries",
    ),
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

    idempotency_store: EventAppendIdempotencyStore | None = None
    claimed_idempotency = False
    if idempotency_key:
        _validate_idempotency_key(session_id, idempotency_key)
        idempotency_store = _get_idempotency_store()
        _maybe_cleanup_idempotency_store(idempotency_store)
        claimed_idempotency, existing = idempotency_store.claim(
            ctx.user_id,
            session_id,
            idempotency_key,
        )
        if not claimed_idempotency:
            if existing and idempotency_store.is_stale_pending(
                existing,
                stale_after=_IDEMPOTENCY_STALE_AFTER,
            ):
                idempotency_store.delete(ctx.user_id, session_id, idempotency_key)
                claimed_idempotency, existing = idempotency_store.claim(
                    ctx.user_id,
                    session_id,
                    idempotency_key,
                )
            if not claimed_idempotency:
                if existing and existing.get("status") == "completed":
                    return _response_from_record(existing)
                completed = await _wait_for_completed_record(
                    idempotency_store,
                    ctx.user_id,
                    session_id,
                    idempotency_key,
                )
                if completed and completed.get("status") == "completed":
                    return _response_from_record(completed)
                raise HTTPException(
                    status_code=409,
                    detail="idempotency key is already in progress",
                )

    # Convert to dicts for the event store
    event_dicts = [{"type": e.type, "data": e.data} for e in events]

    try:
        seqs = event_store.append(ctx.user_id, session_id, event_dicts, source=source)
    except Exception as e:
        if idempotency_store is not None and claimed_idempotency:
            idempotency_store.delete(ctx.user_id, session_id, idempotency_key or "")
        logger.exception("Failed to append events for %s/%s", ctx.user_id, session_id)
        raise HTTPException(status_code=500, detail=f"Failed to append events: {e}")

    response = EventAppendResponse(
        count=len(seqs),
        first_seq=seqs[0],
        last_seq=seqs[-1],
    )

    if idempotency_store is not None and claimed_idempotency:
        finalized = False
        for _ in range(5):
            try:
                idempotency_store.mark_completed(
                    ctx.user_id,
                    session_id,
                    idempotency_key or "",
                    count=response.count,
                    first_seq=response.first_seq,
                    last_seq=response.last_seq,
                )
                finalized = True
                break
            except Exception:
                logger.exception(
                    "Failed to persist idempotency completion for %s/%s/%s",
                    ctx.user_id,
                    session_id,
                    idempotency_key,
                )
                await asyncio.sleep(0.05)
        if not finalized:
            logger.error(
                "Returning successful append response with pending idempotency row "
                "after finalize retries were exhausted for %s/%s/%s",
                ctx.user_id,
                session_id,
                idempotency_key,
            )

    return response


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
    last_n: int = Query(
        0,
        ge=0,
        description="Return the last N matching events in chronological order",
    ),
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
    data_source: str | None = Query(
        None,
        description=(
            "Comma-separated payload source filter on event.data.source "
            "(e.g., 'memory_instructions,environment_info')"
        ),
    ),
    turn_id: str | None = Query(
        None,
        description="Return events with event.data.turn_id matching this value",
    ),
    min_position: int | None = Query(
        None,
        ge=0,
        description="Return events with event.data.position >= this value",
    ),
    max_position: int | None = Query(
        None,
        ge=0,
        description="Return events with event.data.position <= this value",
    ),
    after_ts: str | None = Query(
        None,
        description="Return events with ts >= this ISO 8601 timestamp",
    ),
    before_ts: str | None = Query(
        None,
        description="Return events with ts <= this ISO 8601 timestamp",
    ),
) -> EventReadResponse:
    """Read events from a session's event log."""
    event_store = _get_event_store(request)

    if last_n and after_seq:
        raise HTTPException(
            status_code=400,
            detail="last_n and after_seq are mutually exclusive",
        )
    if last_n and limit:
        raise HTTPException(
            status_code=400,
            detail="last_n and limit are mutually exclusive",
        )
    if (
        min_position is not None
        and max_position is not None
        and min_position > max_position
    ):
        raise HTTPException(
            status_code=400,
            detail="min_position must be <= max_position",
        )

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
    event_data_sources: set[str] | None = None
    if data_source:
        event_data_sources = set(data_source.split(","))

    # Validate session exists
    _validate_session_exists(request, ctx.user_id, session_id)

    try:
        if last_n:
            fetch_limit = last_n + 1
            events = event_store.read_tail(
                ctx.user_id,
                session_id,
                limit=fetch_limit,
                event_types=event_types,
                sources=event_sources,
                exclude_sources=event_exclude_sources,
                data_sources=event_data_sources,
                turn_id=turn_id,
                min_position=min_position,
                max_position=max_position,
                after_ts=after_ts,
                before_ts=before_ts,
            )
        else:
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
                data_sources=event_data_sources,
                turn_id=turn_id,
                min_position=min_position,
                max_position=max_position,
                after_ts=after_ts,
                before_ts=before_ts,
            )
    except Exception as e:
        logger.exception("Failed to read events for %s/%s", ctx.user_id, session_id)
        raise HTTPException(status_code=500, detail=f"Failed to read events: {e}")

    # Determine has_more
    has_more = False
    if last_n and len(events) > last_n:
        has_more = True
        events = events[-last_n:]
    elif limit and len(events) > limit:
        has_more = True
        events = events[:limit]

    last_seq = event_store.last_seq(ctx.user_id, session_id)

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
