"""POST /evaluate endpoint for tool call safety evaluation."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from datetime import timezone as tz

from fastapi import APIRouter, Depends, HTTPException, Request

from intaris.api.deps import SessionContext, get_session_context
from intaris.api.schemas import EvaluateRequest, EvaluateResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/evaluate", response_model=EvaluateResponse)
async def evaluate(
    request: EvaluateRequest,
    http_request: Request,
    ctx: SessionContext = Depends(get_session_context),
) -> EvaluateResponse:
    """Evaluate a tool call for safety and intention alignment.

    Runs the full evaluation pipeline:
    1. Rate limit check
    2. Classify (read-only allowlist -> auto-approve)
    3. Critical pattern check (-> auto-deny)
    4. LLM safety evaluation (-> decision matrix)
    5. Audit logging
    6. Session counter update
    7. Webhook notification (async, fire-and-forget) on escalation
    8. EventBus publish

    Returns the decision with reasoning, risk level, and latency.
    """
    from intaris.server import _get_evaluator

    # Rate limit check
    rate_limiter = getattr(http_request.app.state, "rate_limiter", None)
    if rate_limiter is not None:
        if not rate_limiter.check_and_record(ctx.user_id, request.session_id):
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded: max evaluations per session per minute",
            )

    # Wait for any pending intention update (barrier pattern).
    # If a user message triggered an intention update via POST /reasoning,
    # we wait for it to complete before evaluating. This ensures the
    # evaluator sees the freshest user-stated intention.
    #
    # When intention_pending=True, the client signals that a user message
    # was just sent to /reasoning. If the /reasoning call hasn't arrived
    # yet (race condition), the barrier waits for it using an asyncio.Event
    # before proceeding with the standard barrier wait.
    barrier = getattr(http_request.app.state, "intention_barrier", None)
    if barrier is not None:
        await barrier.wait(
            ctx.user_id,
            request.session_id,
            intention_pending=request.intention_pending,
        )

    # Wait for any pending alignment check (barrier pattern).
    # If a child session was just created or its intention was updated,
    # the alignment check runs async and we wait for it here. This
    # ensures no tool calls execute before alignment is verified.
    alignment_barrier = getattr(http_request.app.state, "alignment_barrier", None)
    if alignment_barrier is not None:
        await alignment_barrier.wait(ctx.user_id, request.session_id)

    try:
        evaluator = _get_evaluator()
        # agent_id: request body overrides header if provided
        agent_id = request.agent_id or ctx.agent_id
        result = evaluator.evaluate(
            user_id=ctx.user_id,
            session_id=request.session_id,
            agent_id=agent_id,
            tool=request.tool,
            args=request.args,
            context=request.context,
        )

        # Fire-and-forget webhook on escalation (Cognis integration)
        webhook = getattr(http_request.app.state, "webhook", None)
        if (
            webhook is not None
            and webhook.is_configured()
            and result.get("decision") == "escalate"
        ):
            asyncio.create_task(
                webhook.send_escalation(
                    call_id=result["call_id"],
                    session_id=request.session_id,
                    user_id=ctx.user_id,
                    agent_id=agent_id,
                    tool=request.tool,
                    args_redacted=result.get("args_redacted"),
                    risk=result.get("risk"),
                    reasoning=result.get("reasoning"),
                )
            )

        # Fire-and-forget per-user notifications on escalation
        dispatcher = getattr(http_request.app.state, "notification_dispatcher", None)
        if dispatcher is not None and result.get("decision") == "escalate":
            from intaris.notifications.providers import Notification

            notification = Notification(
                event_type="escalation",
                call_id=result["call_id"],
                session_id=request.session_id,
                user_id=ctx.user_id,
                agent_id=agent_id,
                tool=request.tool,
                args_redacted=result.get("args_redacted"),
                risk=result.get("risk"),
                reasoning=result.get("reasoning"),
                ui_url=None,  # Set by dispatcher
                approve_url=None,  # Set by dispatcher
                deny_url=None,  # Set by dispatcher
                timestamp=datetime.now(tz.utc).isoformat(),
            )
            asyncio.create_task(
                dispatcher.notify(
                    user_id=ctx.user_id,
                    notification=notification,
                )
            )

        # Fire-and-forget notification on session suspension
        if dispatcher is not None and result.get("session_status") == "suspended":
            from intaris.notifications.providers import Notification

            notification = Notification(
                event_type="session_suspended",
                call_id=result["call_id"],
                session_id=request.session_id,
                user_id=ctx.user_id,
                agent_id=agent_id,
                tool=request.tool,
                args_redacted=result.get("args_redacted"),
                risk=result.get("risk"),
                reasoning=result.get("status_reason") or result.get("reasoning"),
                ui_url=None,
                approve_url=None,
                deny_url=None,
                timestamp=datetime.now(tz.utc).isoformat(),
            )
            asyncio.create_task(
                dispatcher.notify(
                    user_id=ctx.user_id,
                    notification=notification,
                )
            )

        # Fire-and-forget notification on denial
        if dispatcher is not None and result.get("decision") == "deny":
            from intaris.notifications.providers import Notification

            notification = Notification(
                event_type="denial",
                call_id=result["call_id"],
                session_id=request.session_id,
                user_id=ctx.user_id,
                agent_id=agent_id,
                tool=request.tool,
                args_redacted=result.get("args_redacted"),
                risk=result.get("risk"),
                reasoning=result.get("reasoning"),
                ui_url=None,
                approve_url=None,
                deny_url=None,
                timestamp=datetime.now(tz.utc).isoformat(),
            )
            asyncio.create_task(
                dispatcher.notify(
                    user_id=ctx.user_id,
                    notification=notification,
                )
            )

        # Publish event to EventBus
        event_bus = getattr(http_request.app.state, "event_bus", None)
        if event_bus is not None:
            event_bus.publish(
                {
                    "type": "evaluated",
                    "call_id": result["call_id"],
                    "session_id": request.session_id,
                    "user_id": ctx.user_id,
                    "agent_id": agent_id,
                    "decision": result["decision"],
                    "risk": result.get("risk"),
                    "path": result["path"],
                    "latency_ms": result["latency_ms"],
                    "tool": request.tool,
                    "record_type": "tool_call",
                    "classification": result.get("classification"),
                    "timestamp": datetime.now(tz.utc).isoformat(),
                }
            )

        # Auto-append evaluation event to event store (session recording)
        event_store = getattr(http_request.app.state, "event_store", None)
        if event_store is not None:
            try:
                event_store.append(
                    ctx.user_id,
                    request.session_id,
                    [
                        {
                            "type": "evaluation",
                            "data": {
                                "call_id": result["call_id"],
                                "tool": request.tool,
                                "args_redacted": result.get("args_redacted"),
                                "classification": result.get("classification"),
                                "decision": result["decision"],
                                "risk": result.get("risk"),
                                "reasoning": result.get("reasoning"),
                                "path": result["path"],
                                "latency_ms": result["latency_ms"],
                                "agent_id": agent_id,
                            },
                        }
                    ],
                    source="intaris",
                )
            except Exception:
                logger.debug("Failed to auto-append evaluation event", exc_info=True)

        return EvaluateResponse(**result)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception:
        logger.exception("Error in /evaluate")
        raise HTTPException(
            status_code=500,
            detail="Internal error during evaluation",
        )
