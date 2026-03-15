"""Session intention and management endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from intaris.api.deps import SessionContext, get_session_context
from intaris.api.schemas import (
    IntentionRequest,
    IntentionResponse,
    SessionListResponse,
    SessionResponse,
    SessionUpdateRequest,
    StatusUpdateRequest,
    StatusUpdateResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/intention", response_model=IntentionResponse)
async def declare_intention(
    request: IntentionRequest,
    http_request: Request,
    ctx: SessionContext = Depends(get_session_context),
) -> IntentionResponse:
    """Declare a session intention.

    Creates a new session with the declared intention, optional details,
    and optional policy. Must be called before /evaluate for a session.

    For child sessions (parent_session_id set), validates the parent
    exists and triggers an async alignment check. The first /evaluate
    call will wait for the alignment check to complete.
    """
    from intaris.server import _get_db
    from intaris.session import SessionStore

    try:
        store = SessionStore(_get_db())

        # Validate parent session exists and belongs to the same user
        if request.parent_session_id:
            try:
                store.get(request.parent_session_id, user_id=ctx.user_id)
            except ValueError:
                raise HTTPException(
                    status_code=404,
                    detail=(f"Parent session {request.parent_session_id} not found"),
                )

        store.create(
            user_id=ctx.user_id,
            session_id=request.session_id,
            intention=request.intention,
            details=request.details,
            policy=request.policy,
            parent_session_id=request.parent_session_id,
            agent_id=ctx.agent_id,
        )

        # Publish session_created event
        event_bus = getattr(http_request.app.state, "event_bus", None)
        if event_bus is not None:
            event_bus.publish(
                {
                    "type": "session_created",
                    "session_id": request.session_id,
                    "user_id": ctx.user_id,
                    "agent_id": ctx.agent_id,
                    "intention": request.intention,
                    "status": "active",
                    "parent_session_id": request.parent_session_id,
                    "details": request.details,
                }
            )

        # Auto-append lifecycle event to event store (session recording)
        event_store = getattr(http_request.app.state, "event_store", None)
        if event_store is not None:
            try:
                event_store.append(
                    ctx.user_id,
                    request.session_id,
                    [
                        {
                            "type": "lifecycle",
                            "data": {
                                "event": "session_created",
                                "status": "active",
                                "intention": request.intention,
                                "agent_id": ctx.agent_id,
                                "parent_session_id": request.parent_session_id,
                                "details": request.details,
                            },
                        }
                    ],
                    source="intaris",
                )
            except Exception:
                logger.debug(
                    "Failed to auto-append session_created event", exc_info=True
                )

        # Trigger async alignment check for child sessions.
        # The check runs in the background; the first /evaluate call
        # waits for it via the alignment barrier.
        if request.parent_session_id:
            alignment_barrier = getattr(
                http_request.app.state, "alignment_barrier", None
            )
            if alignment_barrier is not None:
                await alignment_barrier.trigger(ctx.user_id, request.session_id)

        return IntentionResponse(ok=True)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception:
        logger.exception("Error in /intention")
        raise HTTPException(
            status_code=500,
            detail="Internal error creating session",
        )


@router.get("/session/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: str,
    ctx: SessionContext = Depends(get_session_context),
) -> SessionResponse:
    """Get session details including counters and status."""
    from intaris.server import _get_db
    from intaris.session import SessionStore

    try:
        store = SessionStore(_get_db())
        session = store.get(session_id, user_id=ctx.user_id)
        return SessionResponse(**session)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception:
        logger.exception("Error in /session")
        raise HTTPException(
            status_code=500,
            detail="Internal error fetching session",
        )


@router.get("/sessions", response_model=SessionListResponse)
async def list_sessions(
    ctx: SessionContext = Depends(get_session_context),
    status: str | None = Query(None),
    agent_id: str | None = Query(None, description="Filter by agent_id"),
    parent_session_id: str | None = Query(
        None, description="Filter child sessions by parent"
    ),
    q: str | None = Query(None, description="Search session_id and intention"),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    tree: bool = Query(
        False,
        description="Tree-aware filtering: status/search filter roots only, "
        "include all children, paginate by root count",
    ),
) -> SessionListResponse:
    """List sessions with optional status filter, search, and pagination."""
    from intaris.server import _get_db
    from intaris.session import SessionStore

    try:
        store = SessionStore(_get_db())
        result = store.list_sessions(
            user_id=ctx.user_id,
            status=status,
            agent_id=agent_id,
            parent_session_id=parent_session_id,
            search=q,
            page=page,
            limit=limit,
            tree=tree,
        )
        return SessionListResponse(**result)
    except Exception:
        logger.exception("Error in /sessions")
        raise HTTPException(
            status_code=500,
            detail="Internal error listing sessions",
        )


@router.patch("/session/{session_id}", response_model=SessionResponse)
async def update_session(
    session_id: str,
    request: SessionUpdateRequest,
    http_request: Request,
    ctx: SessionContext = Depends(get_session_context),
) -> SessionResponse:
    """Update session intention and/or details."""
    from intaris.server import _get_db
    from intaris.session import SessionStore

    try:
        store = SessionStore(_get_db())
        session = store.update_session(
            session_id,
            user_id=ctx.user_id,
            intention=request.intention,
            details=request.details,
        )

        # Publish session_updated event
        event_bus = getattr(http_request.app.state, "event_bus", None)
        if event_bus is not None:
            event_bus.publish(
                {
                    "type": "session_updated",
                    "session_id": session_id,
                    "user_id": ctx.user_id,
                    "intention": session.get("intention"),
                    "details": session.get("details"),
                }
            )

        # Re-check alignment for child sessions when intention changes.
        # Clear the override flag first so the new intention is re-evaluated
        # (AGENTS.md: "Intention changes clear the override flag").
        if request.intention and session.get("parent_session_id"):
            alignment_barrier = getattr(
                http_request.app.state, "alignment_barrier", None
            )
            if alignment_barrier is not None:
                alignment_barrier.clear_override(ctx.user_id, session_id)
                await alignment_barrier.trigger(ctx.user_id, session_id)

        return SessionResponse(**session)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception:
        logger.exception("Error in PATCH /session")
        raise HTTPException(
            status_code=500,
            detail="Internal error updating session",
        )


@router.patch("/session/{session_id}/status", response_model=StatusUpdateResponse)
async def update_session_status(
    session_id: str,
    request: StatusUpdateRequest,
    http_request: Request,
    ctx: SessionContext = Depends(get_session_context),
) -> StatusUpdateResponse:
    """Update session status.

    Verifies session ownership before allowing the update.
    """
    from intaris.server import _get_db
    from intaris.session import SessionStore

    try:
        store = SessionStore(_get_db())
        store.update_status(session_id, request.status, user_id=ctx.user_id)

        # Publish session_status_changed event
        event_bus = getattr(http_request.app.state, "event_bus", None)
        if event_bus is not None:
            event_bus.publish(
                {
                    "type": "session_status_changed",
                    "session_id": session_id,
                    "user_id": ctx.user_id,
                    "status": request.status,
                }
            )

        # Auto-append lifecycle event to event store (session recording)
        event_store = getattr(http_request.app.state, "event_store", None)
        if event_store is not None:
            try:
                event_store.append(
                    ctx.user_id,
                    session_id,
                    [
                        {
                            "type": "lifecycle",
                            "data": {
                                "event": "session_status_changed",
                                "status": request.status,
                            },
                        }
                    ],
                    source="intaris",
                )
            except Exception:
                logger.debug(
                    "Failed to auto-append status_changed event", exc_info=True
                )

            # Flush event buffer on session completion/termination/suspension
            if request.status in ("completed", "terminated", "suspended"):
                try:
                    event_store.flush_session(ctx.user_id, session_id)
                except Exception:
                    logger.debug(
                        "Failed to flush events on session %s",
                        request.status,
                        exc_info=True,
                    )

        # Always enqueue a close summary on session completion/termination.
        # No cancel_duplicate or recently_completed guard — close summaries
        # must always run to trigger compaction. Duplicates are harmless
        # (compaction uses supersede semantics).
        if request.status in ("completed", "terminated"):
            bg_worker = getattr(http_request.app.state, "background_worker", None)
            if bg_worker is not None and bg_worker.analyzer_ready:
                try:
                    tq = bg_worker._task_queue
                    tq.enqueue(
                        "summary",
                        ctx.user_id,
                        session_id=session_id,
                        payload={"trigger": "close"},
                        priority=2,
                    )
                except Exception:
                    logger.debug("Failed to enqueue close summary", exc_info=True)

        return StatusUpdateResponse(ok=True)
    except ValueError as e:
        detail = str(e)
        if "not found" in detail:
            raise HTTPException(status_code=404, detail=detail) from e
        raise HTTPException(status_code=400, detail=detail) from e
    except Exception:
        logger.exception("Error in /session/{session_id}/status")
        raise HTTPException(
            status_code=500,
            detail="Internal error updating session status",
        )
