"""Behavioral analysis API endpoints.

Provides endpoints for:
- L1 Data Collection: POST /reasoning, POST /checkpoint
- L2 Session Analysis: agent summaries, summary triggers, summary retrieval
- L3 Behavioral Analysis: analysis triggers, analysis listing, profiles
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from intaris.api.deps import SessionContext, get_session_context
from intaris.api.schemas import (
    AgentSummaryRecord,
    AgentSummaryRequest,
    AgentSummaryResponse,
    AnalysisListResponse,
    AnalysisRecord,
    AnalysisTriggerResponse,
    BackfillSummariesResponse,
    CheckpointRequest,
    CheckpointResponse,
    ProfileResponse,
    ReasoningRequest,
    ReasoningResponse,
    SessionSummariesResponse,
    SessionSummaryRecord,
    SummaryTriggerResponse,
    TaskStatusResponse,
)
from intaris.sanitize import _INJECTION_PATTERNS as _SANITIZE_PATTERNS

logger = logging.getLogger(__name__)

_BACKFILL_SESSION_LIMIT = 1000
"""Maximum number of sessions to enqueue per backfill request."""

router = APIRouter()

# ── Text Sanitization ─────────────────────────────────────────────────

# Injection patterns used for stripping. Reuses the comprehensive
# pattern set from sanitize.py (which covers chat templates, role
# impersonation, instruction overrides, boundary tag escapes, etc.)
# rather than maintaining a separate subset here.


def _sanitize_agent_text(text: str) -> str:
    """Strip known prompt injection patterns from agent-reported text.

    Defense-in-depth: agent text is never included in analysis prompts,
    but we sanitize on storage as an additional safety layer.

    Uses the comprehensive pattern set from ``intaris.sanitize`` to
    detect and strip injection attempts.

    Args:
        text: Raw agent-reported text.

    Returns:
        Sanitized text with injection patterns removed.
    """
    sanitized = text
    for category, pattern in _SANITIZE_PATTERNS:
        if pattern.search(sanitized):
            logger.warning(
                "Sanitized injection pattern from agent text: %s (%s)",
                pattern.pattern,
                category,
            )
            sanitized = pattern.sub("", sanitized)
    return sanitized.strip()


# ── L1 Data Collection ────────────────────────────────────────────────


@router.post("/reasoning", response_model=ReasoningResponse)
async def submit_reasoning(
    request: ReasoningRequest,
    http_request: Request,
    ctx: SessionContext = Depends(get_session_context),
) -> ReasoningResponse:
    """Submit agent reasoning for audit trail.

    Stores the reasoning text as a record_type="reasoning" entry in
    the audit log. Text is sanitized to strip injection patterns.
    Not included in safety evaluation prompts. User messages (prefixed
    with "User message:") may be included in intention refinement
    prompts as they represent the user's own words, not agent text.
    """
    from intaris.server import _get_db

    # Rate limit check (shares budget with /evaluate)
    rate_limiter = getattr(http_request.app.state, "rate_limiter", None)
    if rate_limiter is not None:
        if not rate_limiter.check_and_record(ctx.user_id, request.session_id):
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded",
            )

    try:
        from intaris.audit import AuditStore
        from intaris.session import SessionStore

        db = _get_db()
        session_store = SessionStore(db)
        audit_store = AuditStore(db)

        # Verify session exists and belongs to user
        session_store.get(request.session_id, user_id=ctx.user_id)

        # Sanitize and store
        sanitized = _sanitize_agent_text(request.content)
        call_id = str(uuid.uuid4())

        audit_store.insert(
            call_id=call_id,
            user_id=ctx.user_id,
            session_id=request.session_id,
            agent_id=ctx.agent_id,
            tool=None,
            args_redacted=None,
            classification=None,
            evaluation_path="reasoning",
            decision="approve",
            risk=None,
            reasoning=None,
            latency_ms=0,
            record_type="reasoning",
            content=sanitized,
        )

        # Update session activity
        session_store.update_activity(request.session_id, user_id=ctx.user_id)

        # Trigger immediate intention update from user message via the
        # IntentionBarrier. The barrier runs the LLM call as an async
        # task; the next POST /evaluate will wait for it to complete.
        # Only triggers for user messages, not agent reasoning.
        if sanitized.startswith("User message:"):
            logger.info(
                "Received user message for %s/%s (context_len=%d)",
                ctx.user_id,
                request.session_id,
                len(request.context or ""),
            )
            barrier = getattr(http_request.app.state, "intention_barrier", None)
            if barrier is not None:
                try:
                    await barrier.trigger(
                        ctx.user_id,
                        request.session_id,
                        context=request.context,
                    )
                except Exception:
                    logger.debug(
                        "Failed to trigger intention barrier",
                        exc_info=True,
                    )

        # Auto-append to event store (session recording)
        event_store = getattr(http_request.app.state, "event_store", None)
        if event_store is not None:
            try:
                event_store.append(
                    ctx.user_id,
                    request.session_id,
                    [
                        {
                            "type": "reasoning",
                            "data": {
                                "call_id": call_id,
                                "content": sanitized,
                                "record_type": "reasoning",
                            },
                        }
                    ],
                    source="intaris",
                )
            except Exception:
                logger.debug("Failed to auto-append reasoning event", exc_info=True)

        return ReasoningResponse(ok=True, call_id=call_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception:
        logger.exception("Error in /reasoning")
        raise HTTPException(
            status_code=500,
            detail="Internal error storing reasoning",
        )


@router.post("/checkpoint", response_model=CheckpointResponse)
async def submit_checkpoint(
    request: CheckpointRequest,
    http_request: Request,
    ctx: SessionContext = Depends(get_session_context),
) -> CheckpointResponse:
    """Submit agent behavioral checkpoint for audit trail.

    Stores the checkpoint text as a record_type="checkpoint" entry in
    the audit log. Text is sanitized to strip injection patterns.
    """
    from intaris.server import _get_db

    # Rate limit check
    rate_limiter = getattr(http_request.app.state, "rate_limiter", None)
    if rate_limiter is not None:
        if not rate_limiter.check_and_record(ctx.user_id, request.session_id):
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded",
            )

    try:
        from intaris.audit import AuditStore
        from intaris.session import SessionStore

        db = _get_db()
        session_store = SessionStore(db)
        audit_store = AuditStore(db)

        # Verify session exists and belongs to user
        session_store.get(request.session_id, user_id=ctx.user_id)

        # Sanitize and store
        sanitized = _sanitize_agent_text(request.content)
        call_id = str(uuid.uuid4())

        audit_store.insert(
            call_id=call_id,
            user_id=ctx.user_id,
            session_id=request.session_id,
            agent_id=ctx.agent_id,
            tool=None,
            args_redacted=None,
            classification=None,
            evaluation_path="checkpoint",
            decision="approve",
            risk=None,
            reasoning=None,
            latency_ms=0,
            record_type="checkpoint",
            content=sanitized,
        )

        # Update session activity
        session_store.update_activity(request.session_id, user_id=ctx.user_id)

        # Auto-append to event store (session recording)
        event_store = getattr(http_request.app.state, "event_store", None)
        if event_store is not None:
            try:
                event_store.append(
                    ctx.user_id,
                    request.session_id,
                    [
                        {
                            "type": "checkpoint",
                            "data": {
                                "call_id": call_id,
                                "content": sanitized,
                                "record_type": "checkpoint",
                            },
                        }
                    ],
                    source="intaris",
                )
            except Exception:
                logger.debug("Failed to auto-append checkpoint event", exc_info=True)

        return CheckpointResponse(ok=True, call_id=call_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception:
        logger.exception("Error in /checkpoint")
        raise HTTPException(
            status_code=500,
            detail="Internal error storing checkpoint",
        )


# ── L2 Session Analysis ──────────────────────────────────────────────


@router.post(
    "/session/{session_id}/agent-summary",
    response_model=AgentSummaryResponse,
)
async def submit_agent_summary(
    session_id: str,
    request: AgentSummaryRequest,
    ctx: SessionContext = Depends(get_session_context),
) -> AgentSummaryResponse:
    """Submit agent-reported session summary.

    Stored in a separate table (agent_summaries) — NEVER mixed into
    Intaris analysis prompts. Used for post-hoc comparison only.
    """
    from intaris.server import _get_db

    try:
        from intaris.session import SessionStore

        db = _get_db()
        session_store = SessionStore(db)

        # Verify session exists and belongs to user
        session_store.get(session_id, user_id=ctx.user_id)

        # Sanitize and store
        sanitized = _sanitize_agent_text(request.summary)
        summary_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        with db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO agent_summaries
                    (id, user_id, session_id, summary, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (summary_id, ctx.user_id, session_id, sanitized, now),
            )

        return AgentSummaryResponse(ok=True)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception:
        logger.exception("Error in /session/%s/agent-summary", session_id)
        raise HTTPException(
            status_code=500,
            detail="Internal error storing agent summary",
        )


@router.post(
    "/session/{session_id}/summary/trigger",
    response_model=SummaryTriggerResponse,
)
async def trigger_summary(
    session_id: str,
    http_request: Request,
    ctx: SessionContext = Depends(get_session_context),
) -> SummaryTriggerResponse:
    """Manually trigger Intaris summary generation for a session.

    Enqueues a summary task in the background task queue.
    """
    from intaris.server import _get_db

    try:
        from intaris.session import SessionStore

        db = _get_db()
        session_store = SessionStore(db)

        # Verify session exists and belongs to user
        session_store.get(session_id, user_id=ctx.user_id)

        # Get background worker from app state
        worker = getattr(http_request.app.state, "background_worker", None)
        if worker is None or not worker._config.enabled:
            raise HTTPException(
                status_code=404,
                detail="Behavioral analysis is not enabled",
            )

        # Enqueue summary task (skip if duplicate pending)
        task_queue = worker._task_queue
        if task_queue.cancel_duplicate("summary", ctx.user_id, session_id):
            return SummaryTriggerResponse(ok=True, task_id=None)

        task_id = task_queue.enqueue(
            "summary",
            ctx.user_id,
            session_id=session_id,
            payload={"trigger": "manual"},
            priority=1,
        )

        return SummaryTriggerResponse(ok=True, task_id=task_id)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception:
        logger.exception("Error in /session/%s/summary/trigger", session_id)
        raise HTTPException(
            status_code=500,
            detail="Internal error triggering summary",
        )


@router.get(
    "/session/{session_id}/summary",
    response_model=SessionSummariesResponse,
)
async def get_session_summaries(
    session_id: str,
    ctx: SessionContext = Depends(get_session_context),
) -> SessionSummariesResponse:
    """Get all summaries for a session.

    Returns both Intaris-generated and agent-reported summaries,
    clearly labeled by source.
    """
    from intaris.server import _get_db

    try:
        from intaris.session import SessionStore

        db = _get_db()
        session_store = SessionStore(db)

        # Verify session exists and belongs to user
        session_store.get(session_id, user_id=ctx.user_id)

        with db.cursor() as cur:
            # Intaris-generated summaries (compacted first, then by time)
            cur.execute(
                """
                SELECT * FROM session_summaries
                WHERE user_id = ? AND session_id = ?
                ORDER BY
                    CASE WHEN summary_type = 'compacted' THEN 0 ELSE 1 END,
                    created_at DESC
                """,
                (ctx.user_id, session_id),
            )
            intaris_rows = cur.fetchall()

            # Agent-reported summaries
            cur.execute(
                """
                SELECT * FROM agent_summaries
                WHERE user_id = ? AND session_id = ?
                ORDER BY created_at DESC
                """,
                (ctx.user_id, session_id),
            )
            agent_rows = cur.fetchall()

        intaris_summaries = []
        for row in intaris_rows:
            d = dict(row)
            # Parse JSON fields
            for field in ("tools_used", "risk_indicators"):
                if d.get(field):
                    try:
                        d[field] = json.loads(d[field])
                    except (json.JSONDecodeError, TypeError):
                        pass
            intaris_summaries.append(SessionSummaryRecord(**d))

        agent_summaries = [AgentSummaryRecord(**dict(row)) for row in agent_rows]

        return SessionSummariesResponse(
            intaris_summaries=intaris_summaries,
            agent_summaries=agent_summaries,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception:
        logger.exception("Error in /session/%s/summary", session_id)
        raise HTTPException(
            status_code=500,
            detail="Internal error fetching summaries",
        )


# ── L3 Behavioral Analysis ───────────────────────────────────────────


@router.post("/analysis/trigger", response_model=AnalysisTriggerResponse)
async def trigger_analysis(
    http_request: Request,
    ctx: SessionContext = Depends(get_session_context),
    agent_id: str | None = Query(None, description="Agent to analyze"),
) -> AnalysisTriggerResponse:
    """Manually trigger cross-session behavioral analysis.

    Enqueues an analysis task in the background task queue.
    Optionally scoped to a specific agent_id.
    """
    worker = getattr(http_request.app.state, "background_worker", None)
    if worker is None or not worker._config.enabled:
        raise HTTPException(
            status_code=404,
            detail="Behavioral analysis is not enabled",
        )

    try:
        from intaris.server import _get_db

        task_queue = worker._task_queue

        if agent_id:
            # Single agent — check for duplicate, enqueue one task
            if task_queue.cancel_duplicate("analysis", ctx.user_id):
                return AnalysisTriggerResponse(ok=True, task_id=None)
            task_id = task_queue.enqueue(
                "analysis",
                ctx.user_id,
                payload={
                    "triggered_by": "manual",
                    "agent_id": agent_id,
                },
            )
            return AnalysisTriggerResponse(ok=True, task_id=task_id)

        # All agents — enumerate distinct agents for this user and
        # enqueue a separate analysis task for each, matching the
        # periodic scheduler pattern in BackgroundWorker.
        db = _get_db()
        with db.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT COALESCE(agent_id, '') as agent_id "
                "FROM sessions WHERE user_id = ?",
                (ctx.user_id,),
            )
            agents = [
                row["agent_id"] if isinstance(row, dict) else row[0]
                for row in cur.fetchall()
            ]

        # Per-agent dedup: check each agent individually so a running
        # task for one agent doesn't block others from being enqueued.
        enqueued = 0
        for aid in agents:
            if not task_queue.cancel_duplicate("analysis", ctx.user_id):
                task_queue.enqueue(
                    "analysis",
                    ctx.user_id,
                    payload={
                        "triggered_by": "manual",
                        "agent_id": aid,
                    },
                )
                enqueued += 1

        logger.info(
            "Analysis triggered for all agents: user=%s agents=%d enqueued=%d",
            ctx.user_id,
            len(agents),
            enqueued,
        )
        return AnalysisTriggerResponse(ok=True, task_id=None)
    except Exception:
        logger.exception("Error in /analysis/trigger")
        raise HTTPException(
            status_code=500,
            detail="Internal error triggering analysis",
        )


@router.post("/summaries/backfill", response_model=BackfillSummariesResponse)
async def backfill_summaries(
    http_request: Request,
    ctx: SessionContext = Depends(get_session_context),
    lookback_days: int = Query(7, ge=1, le=365, description="Days to look back"),
    force: bool = Query(
        False, description="Include sessions that already have summaries"
    ),
    agent_id: str | None = Query(None, description="Filter by agent ID"),
) -> BackfillSummariesResponse:
    """Backfill session summaries for recent sessions.

    Enqueues summary tasks for sessions within the lookback window that
    are missing summaries. When *force* is set, also re-evaluates
    sessions that already have summaries.
    """
    from intaris.server import _get_db

    worker = getattr(http_request.app.state, "background_worker", None)
    if worker is None or not worker._config.enabled:
        raise HTTPException(
            status_code=404,
            detail="Behavioral analysis is not enabled",
        )

    try:
        db = _get_db()
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=lookback_days)
        ).isoformat()

        # Build query — find sessions needing summaries.
        # Unlike _startup_catchup (which skips active sessions), the
        # manual backfill includes ALL statuses since the user
        # explicitly requested it. We always fetch all sessions and
        # filter by summary_count in Python so that `skipped` reflects
        # sessions that already have summaries.
        conditions = ["user_id = ?", "last_activity_at >= ?"]
        params: list[str | int] = [ctx.user_id, cutoff]

        if agent_id:
            conditions.append("agent_id = ?")
            params.append(agent_id)

        where = " AND ".join(conditions)
        with db.cursor() as cur:
            cur.execute(
                f"SELECT user_id, session_id, summary_count "
                f"FROM sessions WHERE {where} LIMIT ?",
                (*params, _BACKFILL_SESSION_LIMIT),
            )
            rows = cur.fetchall()

        task_queue = worker._task_queue
        enqueued = 0
        skipped = 0

        for row in rows:
            uid = row["user_id"] if isinstance(row, dict) else row[0]
            sid = row["session_id"] if isinstance(row, dict) else row[1]
            sc = row["summary_count"] if isinstance(row, dict) else row[2]

            if not force and (sc or 0) > 0:
                skipped += 1
                continue

            if not task_queue.cancel_duplicate("summary", uid, sid):
                task_queue.enqueue(
                    "summary",
                    uid,
                    session_id=sid,
                    payload={"trigger": "manual"},
                    priority=1,
                )
                enqueued += 1

        logger.info(
            "Summary backfill: user=%s agent=%s lookback=%dd force=%s "
            "enqueued=%d skipped=%d",
            ctx.user_id,
            agent_id or "all",
            lookback_days,
            force,
            enqueued,
            skipped,
        )

        return BackfillSummariesResponse(enqueued=enqueued, skipped=skipped)
    except Exception:
        logger.exception("Error in /summaries/backfill")
        raise HTTPException(
            status_code=500,
            detail="Internal error during summary backfill",
        )


@router.get("/tasks/status", response_model=TaskStatusResponse)
async def get_task_status(
    ctx: SessionContext = Depends(get_session_context),
    task_type: str | None = Query(None, description="Filter: summary, analysis"),
    session_id: str | None = Query(None, description="Filter by session"),
    since: datetime | None = Query(
        None, description="ISO 8601 cutoff (only count tasks created after this)"
    ),
) -> TaskStatusResponse:
    """Get task queue status counts for the current user.

    Returns counts grouped by status (pending, running, completed,
    failed, cancelled). Optionally filtered by task type, session,
    and creation time.
    """
    from intaris.server import _get_db

    try:
        db = _get_db()
        # Default to 24h window if no 'since' provided
        cutoff = (
            since.isoformat()
            if since
            else (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        )

        conditions = ["user_id = ?", "created_at >= ?"]
        params: list[str] = [ctx.user_id, cutoff]

        if task_type:
            conditions.append("task_type = ?")
            params.append(task_type)

        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)

        where = " AND ".join(conditions)
        with db.cursor() as cur:
            cur.execute(
                f"SELECT status, COUNT(*) as cnt FROM analysis_tasks "
                f"WHERE {where} GROUP BY status",
                tuple(params),
            )
            rows = cur.fetchall()

        counts: dict[str, int] = {}
        for row in rows:
            status = row["status"] if isinstance(row, dict) else row[0]
            cnt = row["cnt"] if isinstance(row, dict) else row[1]
            counts[status] = cnt

        return TaskStatusResponse(
            pending=counts.get("pending", 0),
            running=counts.get("running", 0),
            completed=counts.get("completed", 0),
            failed=counts.get("failed", 0),
            cancelled=counts.get("cancelled", 0),
        )
    except Exception:
        logger.exception("Error in /tasks/status")
        raise HTTPException(
            status_code=500,
            detail="Internal error querying task status",
        )


@router.get("/analysis", response_model=AnalysisListResponse)
async def list_analyses(
    ctx: SessionContext = Depends(get_session_context),
    agent_id: str | None = Query(None, description="Filter by agent_id"),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
) -> AnalysisListResponse:
    """List behavioral analyses for the current user.

    Optionally filtered by agent_id.
    """
    from intaris.server import _get_db

    try:
        db = _get_db()
        offset = (page - 1) * limit

        agent_cond = ""
        agent_params: tuple[str, ...] = ()
        if agent_id:
            agent_cond = " AND agent_id = ?"
            agent_params = (agent_id,)

        with db.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM behavioral_analyses "
                f"WHERE user_id = ?{agent_cond}",
                (ctx.user_id, *agent_params),
            )
            total = cur.fetchone()[0]

            cur.execute(
                f"""
                SELECT * FROM behavioral_analyses
                WHERE user_id = ?{agent_cond}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (ctx.user_id, *agent_params, limit, offset),
            )
            rows = cur.fetchall()

        items = []
        for row in rows:
            d = dict(row)
            for field in ("sessions_scope", "findings", "recommendations"):
                if d.get(field):
                    try:
                        d[field] = json.loads(d[field])
                    except (json.JSONDecodeError, TypeError):
                        pass
            items.append(AnalysisRecord(**d))

        pages = max(1, (total + limit - 1) // limit)
        return AnalysisListResponse(items=items, total=total, page=page, pages=pages)
    except Exception:
        logger.exception("Error in /analysis")
        raise HTTPException(
            status_code=500,
            detail="Internal error listing analyses",
        )


@router.get("/profile", response_model=ProfileResponse)
async def get_profile(
    ctx: SessionContext = Depends(get_session_context),
    agent_id: str | None = Query(None, description="Agent to get profile for"),
) -> ProfileResponse:
    """Get the behavioral risk profile for the current user.

    Optionally scoped to a specific agent_id. Without agent_id, returns
    the highest-risk profile across all agents.

    Note: the user_bound restriction was removed because the management
    UI uses single shared API keys (INTARIS_API_KEY) which are not
    user-bound. The /stats endpoint already exposes the same risk data
    without this restriction, so blocking /profile provided no security
    benefit. Agents connecting via the evaluate pipeline do not call
    this endpoint.
    """
    from intaris.server import _get_db

    try:
        db = _get_db()

        with db.cursor() as cur:
            if agent_id:
                cur.execute(
                    "SELECT * FROM behavioral_profiles "
                    "WHERE user_id = ? AND agent_id = ?",
                    (ctx.user_id, agent_id),
                )
            else:
                # Return the highest-risk profile across all agents
                cur.execute(
                    "SELECT * FROM behavioral_profiles "
                    "WHERE user_id = ? "
                    "ORDER BY risk_level DESC "
                    "LIMIT 1",
                    (ctx.user_id,),
                )
            row = cur.fetchone()

        if row is None:
            # No profile yet — return defaults
            return ProfileResponse(user_id=ctx.user_id)

        d = dict(row)
        if d.get("active_alerts"):
            try:
                d["active_alerts"] = json.loads(d["active_alerts"])
            except (json.JSONDecodeError, TypeError):
                pass

        return ProfileResponse(**d)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Error in /profile")
        raise HTTPException(
            status_code=500,
            detail="Internal error fetching profile",
        )
