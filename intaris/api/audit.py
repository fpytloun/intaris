"""Audit log endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from intaris.api.deps import SessionContext, get_session_context
from intaris.api.schemas import (
    AuditListResponse,
    AuditRecord,
    DecisionRequest,
    DecisionResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/audit", response_model=AuditListResponse)
async def list_audit(
    ctx: SessionContext = Depends(get_session_context),
    session_id: str | None = Query(None),
    agent_id: str | None = Query(None),
    record_type: str | None = Query(None),
    tool: str | None = Query(None),
    decision: str | None = Query(None),
    risk: str | None = Query(None),
    path: str | None = Query(None),
    from_ts: str | None = Query(None, alias="from"),
    to_ts: str | None = Query(None, alias="to"),
    resolved: bool | None = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
) -> AuditListResponse:
    """Query audit log with filters and pagination."""
    from intaris.audit import AuditStore
    from intaris.server import _get_db

    try:
        store = AuditStore(_get_db())
        result = store.query(
            user_id=ctx.user_id,
            session_id=session_id,
            agent_id=agent_id,
            record_type=record_type,
            tool=tool,
            decision=decision,
            risk=risk,
            evaluation_path=path,
            from_ts=from_ts,
            to_ts=to_ts,
            resolved=resolved,
            page=page,
            limit=limit,
        )
        return AuditListResponse(**result)
    except Exception:
        logger.exception("Error in /audit")
        raise HTTPException(
            status_code=500,
            detail="Internal error querying audit log",
        )


@router.get("/audit/{call_id}", response_model=AuditRecord)
async def get_audit_record(
    call_id: str,
    ctx: SessionContext = Depends(get_session_context),
) -> AuditRecord:
    """Get a single audit record by call_id."""
    from intaris.audit import AuditStore
    from intaris.server import _get_db

    try:
        store = AuditStore(_get_db())
        record = store.get_by_call_id(call_id, user_id=ctx.user_id)
        return AuditRecord(**record)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception:
        logger.exception("Error in /audit/{call_id}")
        raise HTTPException(
            status_code=500,
            detail="Internal error fetching audit record",
        )


@router.post("/decision", response_model=DecisionResponse)
async def resolve_decision(
    request: DecisionRequest,
    http_request: Request,
    ctx: SessionContext = Depends(get_session_context),
) -> DecisionResponse:
    """Resolve an escalated tool call.

    Called by Cognis or the Intaris UI when a user approves or denies
    an escalated tool call.
    """
    from intaris.audit import AuditStore
    from intaris.judge import resolve_with_side_effects
    from intaris.server import _get_db, _get_evaluator

    try:
        store = AuditStore(_get_db())

        # Get metrics from background worker (if available)
        bg_worker = getattr(http_request.app.state, "background_worker", None)
        metrics = getattr(bg_worker, "metrics", None) if bg_worker else None

        # Use shared resolution handler for consistent side effects
        await resolve_with_side_effects(
            call_id=request.call_id,
            user_id=ctx.user_id,
            user_decision=request.decision,
            user_note=request.note,
            resolved_by="user",
            audit_store=store,
            evaluator=_get_evaluator(),
            alignment_barrier=getattr(
                http_request.app.state, "alignment_barrier", None
            ),
            event_bus=getattr(http_request.app.state, "event_bus", None),
            notification_dispatcher=getattr(
                http_request.app.state, "notification_dispatcher", None
            ),
            metrics=metrics,
        )

        return DecisionResponse(ok=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception:
        logger.exception("Error in /decision")
        raise HTTPException(
            status_code=500,
            detail="Internal error resolving decision",
        )
