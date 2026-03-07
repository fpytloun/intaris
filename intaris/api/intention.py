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
    """
    from intaris.server import _get_db
    from intaris.session import SessionStore

    try:
        store = SessionStore(_get_db())
        store.create(
            user_id=ctx.user_id,
            session_id=request.session_id,
            intention=request.intention,
            details=request.details,
            policy=request.policy,
            parent_session_id=request.parent_session_id,
        )

        # Publish session_created event
        event_bus = getattr(http_request.app.state, "event_bus", None)
        if event_bus is not None:
            event_bus.publish(
                {
                    "type": "session_created",
                    "session_id": request.session_id,
                    "user_id": ctx.user_id,
                    "intention": request.intention,
                    "status": "active",
                    "parent_session_id": request.parent_session_id,
                    "details": request.details,
                }
            )

        return IntentionResponse(ok=True)
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
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
) -> SessionListResponse:
    """List sessions with optional status filter and pagination."""
    from intaris.server import _get_db
    from intaris.session import SessionStore

    try:
        store = SessionStore(_get_db())
        result = store.list_sessions(
            user_id=ctx.user_id,
            status=status,
            page=page,
            limit=limit,
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
