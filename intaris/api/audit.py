"""Audit log endpoints."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from datetime import timezone as tz

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
    from intaris.server import _get_db

    try:
        store = AuditStore(_get_db())
        store.resolve_escalation(
            call_id=request.call_id,
            user_decision=request.decision,
            user_note=request.note,
            user_id=ctx.user_id,
        )

        # Look up the audit record for EventBus and notifications
        record = None
        try:
            record = store.get_by_call_id(request.call_id, user_id=ctx.user_id)
        except ValueError:
            pass  # Record not found — skip event and notification

        # Acknowledge alignment override when user approves an alignment
        # escalation. This prevents re-escalation for subsequent tool calls
        # in the same session. The per-call LLM evaluation still injects
        # parent_intention as defense-in-depth.
        if (
            record is not None
            and request.decision == "approve"
            and record.get("evaluation_path") == "alignment"
        ):
            alignment_barrier = getattr(
                http_request.app.state, "alignment_barrier", None
            )
            if alignment_barrier is not None:
                session_id = record.get("session_id")
                if session_id:
                    alignment_barrier.acknowledge(ctx.user_id, session_id)

        # Learn path prefixes from user-approved escalations so subsequent
        # reads to the same or sibling directories are fast-pathed.
        if record is not None and request.decision == "approve":
            try:
                from intaris.server import _get_evaluator

                evaluator = _get_evaluator()
                evaluator.learn_from_approved_escalation(record)
            except Exception:
                logger.debug("Could not learn path prefix from approval", exc_info=True)

        # Publish event to EventBus
        event_bus = getattr(http_request.app.state, "event_bus", None)
        if event_bus is not None and record is not None:
            event_bus.publish(
                {
                    "type": "decided",
                    "call_id": request.call_id,
                    "session_id": record.get("session_id"),
                    "user_id": ctx.user_id,
                    "user_decision": request.decision,
                    "user_note": request.note,
                }
            )

        # Fire-and-forget resolution notification to user's channels
        dispatcher = getattr(http_request.app.state, "notification_dispatcher", None)
        if dispatcher is not None and record is not None:
            from intaris.notifications.providers import Notification

            notification = Notification(
                event_type="resolution",
                call_id=request.call_id,
                session_id=record.get("session_id", ""),
                user_id=ctx.user_id,
                agent_id=record.get("agent_id"),
                tool=record.get("tool"),
                args_redacted=None,
                risk=record.get("risk"),
                reasoning=None,
                ui_url=None,
                approve_url=None,
                deny_url=None,
                timestamp=datetime.now(tz.utc).isoformat(),
                user_decision=request.decision,
                user_note=request.note,
            )
            asyncio.create_task(
                dispatcher.notify(
                    user_id=ctx.user_id,
                    notification=notification,
                )
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
