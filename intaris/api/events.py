"""Session event recording API endpoints.

Provides endpoints for:
- POST /session/{id}/events — append events (single or batch)
- GET /session/{id}/events — read events with pagination and filtering
- GET /session/{id}/events/export — export a reconstructable session snapshot
- POST /session/{id}/events/flush — force flush buffered events
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

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


def _get_session_or_404(user_id: str, session_id: str) -> dict[str, Any]:
    """Return session metadata, or raise a tenant-scoped 404."""
    from intaris.server import _get_db
    from intaris.session import SessionStore

    store = SessionStore(_get_db())
    try:
        return store.get(session_id, user_id=user_id)
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


def _parse_event_filters(
    *,
    type: str | None,
    source: str | None,
    exclude_source: str | None,
    data_source: str | None,
) -> tuple[set[str] | None, set[str] | None, set[str] | None, set[str] | None]:
    """Parse comma-separated event filters shared by read and export endpoints."""
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

    event_sources = set(source.split(",")) if source else None
    event_exclude_sources = set(exclude_source.split(",")) if exclude_source else None
    event_data_sources = set(data_source.split(",")) if data_source else None

    return event_types, event_sources, event_exclude_sources, event_data_sources


def _parse_event_seqs(seqs: str | None) -> set[int] | None:
    """Parse comma-separated exact event sequence filters."""
    if not seqs:
        return None

    parsed: set[int] = set()
    for raw_seq in seqs.split(","):
        raw_seq = raw_seq.strip()
        if not raw_seq:
            continue
        try:
            seq = int(raw_seq)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="seqs must contain positive integer sequence numbers",
            )
        if seq <= 0:
            raise HTTPException(
                status_code=400,
                detail="seqs must contain positive integer sequence numbers",
            )
        parsed.add(seq)

    if not parsed:
        raise HTTPException(
            status_code=400,
            detail="seqs must contain at least one positive integer sequence number",
        )
    if len(parsed) > 1000:
        raise HTTPException(
            status_code=400,
            detail="seqs may contain at most 1000 sequence numbers",
        )
    return parsed


def _export_filter_metadata(
    *,
    type: str | None,
    source: str | None,
    exclude_source: str | None,
    data_source: str | None,
    turn_id: str | None,
    min_position: int | None,
    max_position: int | None,
    after_ts: str | None,
    before_ts: str | None,
) -> dict[str, Any]:
    """Return only active event filters for export provenance."""
    filters: dict[str, Any] = {}
    if type:
        filters["type"] = type.split(",")
    if source:
        filters["source"] = source.split(",")
    if exclude_source:
        filters["exclude_source"] = exclude_source.split(",")
    if data_source:
        filters["data_source"] = data_source.split(",")
    if turn_id:
        filters["turn_id"] = turn_id
    if min_position is not None:
        filters["min_position"] = min_position
    if max_position is not None:
        filters["max_position"] = max_position
    if after_ts:
        filters["after_ts"] = after_ts
    if before_ts:
        filters["before_ts"] = before_ts
    return filters


def _parse_json_field(row: dict[str, Any], field: str) -> None:
    """Parse a JSON-encoded DB field in-place when present."""
    if row.get(field):
        try:
            row[field] = json.loads(row[field])
        except (json.JSONDecodeError, TypeError):
            pass


def _get_export_audit_log(user_id: str, session_id: str) -> list[dict[str, Any]]:
    """Return complete audit rows for a session in chronological order."""
    from intaris.server import _get_db

    with _get_db().cursor() as cur:
        cur.execute(
            """
            SELECT * FROM audit_log
            WHERE user_id = ? AND session_id = ?
            ORDER BY timestamp ASC, id ASC
            """,
            (user_id, session_id),
        )
        rows = [dict(row) for row in cur.fetchall()]

    for row in rows:
        _parse_json_field(row, "args_redacted")
        if "injection_detected" in row and row["injection_detected"] is not None:
            row["injection_detected"] = bool(row["injection_detected"])
    return rows


def _get_export_summaries(
    user_id: str, session_id: str
) -> dict[str, list[dict[str, Any]]]:
    """Return Intaris and agent summaries with JSON fields decoded."""
    from intaris.server import _get_db

    with _get_db().cursor() as cur:
        cur.execute(
            """
            SELECT * FROM session_summaries
            WHERE user_id = ? AND session_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (user_id, session_id),
        )
        intaris_summaries = [dict(row) for row in cur.fetchall()]

        cur.execute(
            """
            SELECT * FROM agent_summaries
            WHERE user_id = ? AND session_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (user_id, session_id),
        )
        agent_summaries = [dict(row) for row in cur.fetchall()]

    for summary in intaris_summaries:
        _parse_json_field(summary, "tools_used")
        _parse_json_field(summary, "risk_indicators")

    return {
        "session_summaries": intaris_summaries,
        "agent_summaries": agent_summaries,
    }


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
        seqs = await asyncio.to_thread(
            event_store.append,
            ctx.user_id,
            session_id,
            event_dicts,
            source=source,
        )
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
        "Supports pagination via after_seq, before_seq, and limit parameters."
    ),
)
async def read_events(
    session_id: str,
    request: Request,
    ctx: SessionContext = Depends(get_session_context),
    after_seq: int = Query(0, ge=0, description="Return events with seq > this value"),
    before_seq: int | None = Query(
        None,
        ge=0,
        description="Return the last events with seq < this value; requires limit",
    ),
    limit: int = Query(0, ge=0, description="Max events to return (0 = all)"),
    last_n: int = Query(
        0,
        ge=0,
        description="Return the last N matching events in chronological order",
    ),
    seqs: str | None = Query(
        None,
        description=(
            "Comma-separated exact event sequence numbers to return. Mutually exclusive "
            "with after_seq, before_seq, limit, and last_n."
        ),
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

    exact_seqs = _parse_event_seqs(seqs)

    if exact_seqs and (after_seq or before_seq is not None or limit or last_n):
        raise HTTPException(
            status_code=400,
            detail="seqs is mutually exclusive with after_seq, before_seq, limit, and last_n",
        )
    if last_n and (after_seq or before_seq is not None):
        raise HTTPException(
            status_code=400,
            detail="last_n is mutually exclusive with after_seq and before_seq",
        )
    if last_n and limit:
        raise HTTPException(
            status_code=400,
            detail="last_n and limit are mutually exclusive",
        )
    if before_seq is not None and after_seq:
        raise HTTPException(
            status_code=400,
            detail="before_seq and after_seq are mutually exclusive",
        )
    if before_seq is not None and not limit:
        raise HTTPException(
            status_code=400,
            detail="before_seq requires limit",
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

    event_types, event_sources, event_exclude_sources, event_data_sources = (
        _parse_event_filters(
            type=type,
            source=source,
            exclude_source=exclude_source,
            data_source=data_source,
        )
    )

    # Validate session exists
    _validate_session_exists(request, ctx.user_id, session_id)

    try:
        if exact_seqs:
            events = await asyncio.to_thread(
                event_store.read_seqs,
                ctx.user_id,
                session_id,
                seqs=exact_seqs,
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
        elif last_n:
            fetch_limit = last_n + 1
            events = await asyncio.to_thread(
                event_store.read_tail,
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
        elif before_seq is not None:
            fetch_limit = limit + 1
            events = await asyncio.to_thread(
                event_store.read_before,
                ctx.user_id,
                session_id,
                before_seq=before_seq,
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
            events = await asyncio.to_thread(
                event_store.read,
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
    if exact_seqs:
        has_more = False
    elif last_n and len(events) > last_n:
        has_more = True
        events = events[-last_n:]
    elif before_seq is not None and limit and len(events) > limit:
        has_more = True
        events = events[-limit:]
    elif limit and len(events) > limit:
        has_more = True
        events = events[:limit]

    try:
        availability = await asyncio.to_thread(
            event_store.availability, ctx.user_id, session_id
        )
    except Exception as e:
        logger.exception(
            "Failed to read event availability for %s/%s", ctx.user_id, session_id
        )
        raise HTTPException(
            status_code=500, detail=f"Failed to read event availability: {e}"
        )

    return EventReadResponse(
        events=events,
        last_seq=availability.last_seq,
        first_available_seq=availability.first_available_seq,
        history_gap=(
            {
                "from_seq": availability.history_gap.from_seq,
                "to_seq": availability.history_gap.to_seq,
                "reason": availability.history_gap.reason,
            }
            if availability.history_gap is not None
            else None
        ),
        has_more=has_more,
    )


@router.get(
    "/session/{session_id}/events/export",
    summary="Export session events and metadata",
    description=(
        "Export a reconstructable JSON snapshot for a single session. "
        "Session metadata, audit records, and summaries are complete; event "
        "filters apply only to the exported events array."
    ),
)
async def export_events(
    session_id: str,
    request: Request,
    ctx: SessionContext = Depends(get_session_context),
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
        description="Comma-separated payload source filter on event.data.source",
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
) -> JSONResponse:
    """Export a single session snapshot as JSON."""
    event_store = _get_event_store(request)

    if (
        min_position is not None
        and max_position is not None
        and min_position > max_position
    ):
        raise HTTPException(
            status_code=400,
            detail="min_position must be <= max_position",
        )

    event_types, event_sources, event_exclude_sources, event_data_sources = (
        _parse_event_filters(
            type=type,
            source=source,
            exclude_source=exclude_source,
            data_source=data_source,
        )
    )

    session = _get_session_or_404(ctx.user_id, session_id)
    event_last_seq = await asyncio.to_thread(
        event_store.last_seq, ctx.user_id, session_id
    )
    filters = _export_filter_metadata(
        type=type,
        source=source,
        exclude_source=exclude_source,
        data_source=data_source,
        turn_id=turn_id,
        min_position=min_position,
        max_position=max_position,
        after_ts=after_ts,
        before_ts=before_ts,
    )

    try:
        events = await asyncio.to_thread(
            event_store.read,
            ctx.user_id,
            session_id,
            after_seq=0,
            limit=0,
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
        events = [event for event in events if event.get("seq", 0) <= event_last_seq]
        audit_log = _get_export_audit_log(ctx.user_id, session_id)
        summaries = _get_export_summaries(ctx.user_id, session_id)
    except Exception as e:
        logger.exception("Failed to export events for %s/%s", ctx.user_id, session_id)
        raise HTTPException(status_code=500, detail=f"Failed to export events: {e}")

    payload = {
        "schema": "intaris.session_export.v1",
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "session_id": session_id,
        "user_id": ctx.user_id,
        "complete": not bool(filters),
        "filters": filters,
        "event_last_seq": event_last_seq,
        "event_count": len(events),
        "audit_count": len(audit_log),
        "session_summary_count": len(summaries["session_summaries"]),
        "agent_summary_count": len(summaries["agent_summaries"]),
        "consistency": "events_bounded_to_event_last_seq",
        "session": session,
        "events": events,
        "audit_log": audit_log,
        **summaries,
    }

    safe_session_id = re.sub(r"[^a-zA-Z0-9._-]+", "-", session_id).strip("-")
    filename = f"intaris-session-{safe_session_id or 'session'}-events.json"
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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
        await asyncio.to_thread(event_store.flush_session, ctx.user_id, session_id)
    except Exception as e:
        logger.exception("Failed to flush events for %s/%s", ctx.user_id, session_id)
        raise HTTPException(status_code=500, detail=f"Failed to flush events: {e}")

    return {"ok": True}
