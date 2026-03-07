"""Session intention and management endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from intaris.api.schemas import (
    IntentionRequest,
    IntentionResponse,
    SessionResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/intention", response_model=IntentionResponse)
async def declare_intention(request: IntentionRequest) -> IntentionResponse:
    """Declare a session intention.

    Creates a new session with the declared intention, optional details,
    and optional policy. Must be called before /evaluate for a session.
    """
    from intaris.server import _get_db
    from intaris.session import SessionStore

    try:
        store = SessionStore(_get_db())
        store.create(
            session_id=request.session_id,
            intention=request.intention,
            details=request.details,
            policy=request.policy,
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
async def get_session(session_id: str) -> SessionResponse:
    """Get session details including counters and status."""
    from intaris.server import _get_db
    from intaris.session import SessionStore

    try:
        store = SessionStore(_get_db())
        session = store.get(session_id)
        return SessionResponse(**session)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception:
        logger.exception("Error in /session")
        raise HTTPException(
            status_code=500,
            detail="Internal error fetching session",
        )
