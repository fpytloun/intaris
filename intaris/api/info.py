"""Identity, stats, and configuration endpoints for the management UI."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from intaris.api.deps import SessionContext, get_session_context

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/whoami")
async def whoami() -> dict:
    """Return the authenticated user's identity.

    Used by the management UI to verify the API key and determine
    whether user switching is allowed.

    Unlike other endpoints, this does NOT require user_id — wildcard
    API keys and no-auth mode may not have a user bound yet. The UI
    uses this to decide whether to show the user switcher.
    """
    from intaris.server import _session_agent_id, _session_user_bound, _session_user_id

    return {
        "user_id": _session_user_id.get(),
        "agent_id": _session_agent_id.get(),
        "can_switch_user": not _session_user_bound.get(),
    }


@router.get("/stats")
async def stats(
    request: Request,
    ctx: SessionContext = Depends(get_session_context),
    agent_id: str | None = Query(None, description="Filter by agent_id"),
) -> dict:
    """Return aggregated statistics for the dashboard.

    Computes session counts, evaluation totals, decision distribution,
    pending approvals, and the list of known users/agents.

    When agent_id is provided, all audit_log and session queries are
    filtered to that agent.
    """
    from intaris.server import _get_db

    try:
        db = _get_db()

        # Build reusable WHERE fragments for agent_id filtering
        audit_agent_cond = ""
        audit_agent_params: tuple[str, ...] = ()
        session_agent_cond = ""
        session_agent_params: tuple[str, ...] = ()
        if agent_id:
            audit_agent_cond = " AND agent_id = ?"
            audit_agent_params = (agent_id,)
            session_agent_cond = " AND agent_id = ?"
            session_agent_params = (agent_id,)

        # Session counts by status
        with db.cursor() as cur:
            cur.execute(
                "SELECT status, COUNT(*) FROM sessions "
                f"WHERE user_id = ?{session_agent_cond} GROUP BY status",
                (ctx.user_id, *session_agent_params),
            )
            status_counts = {row[0]: row[1] for row in cur.fetchall()}

        total_sessions = sum(status_counts.values())

        # Evaluation totals and decision distribution
        with db.cursor() as cur:
            cur.execute(
                "SELECT decision, COUNT(*) FROM audit_log "
                f"WHERE user_id = ?{audit_agent_cond} GROUP BY decision",
                (ctx.user_id, *audit_agent_params),
            )
            decision_counts = {row[0]: row[1] for row in cur.fetchall()}

        total_evaluations = sum(decision_counts.values())
        approved = decision_counts.get("approve", 0)
        approval_rate = (
            round(approved / total_evaluations * 100, 1)
            if total_evaluations > 0
            else 0.0
        )

        # Pending approvals (escalated + unresolved)
        with db.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM audit_log "
                "WHERE user_id = ? AND decision = 'escalate' "
                f"AND user_decision IS NULL{audit_agent_cond}",
                (ctx.user_id, *audit_agent_params),
            )
            pending_approvals = cur.fetchone()[0]

        # Average latency
        with db.cursor() as cur:
            cur.execute(
                f"SELECT AVG(latency_ms) FROM audit_log "
                f"WHERE user_id = ?{audit_agent_cond}",
                (ctx.user_id, *audit_agent_params),
            )
            avg_latency = cur.fetchone()[0]
            avg_latency_ms = round(avg_latency, 1) if avg_latency else 0.0

        # Known users (for impersonation dropdown).
        if not ctx.user_bound:
            with db.cursor() as cur:
                cur.execute("SELECT DISTINCT user_id FROM sessions ORDER BY user_id")
                users = [row[0] for row in cur.fetchall()]
        else:
            users = [ctx.user_id]

        # Known agents (for agent filter dropdown)
        with db.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT agent_id FROM audit_log "
                "WHERE user_id = ? AND agent_id IS NOT NULL "
                "ORDER BY agent_id",
                (ctx.user_id,),
            )
            agents = [row[0] for row in cur.fetchall()]

        # Risk distribution (for pie chart)
        with db.cursor() as cur:
            cur.execute(
                "SELECT risk, COUNT(*) FROM audit_log "
                f"WHERE user_id = ? AND risk IS NOT NULL{audit_agent_cond} "
                "GROUP BY risk",
                (ctx.user_id, *audit_agent_params),
            )
            risk_distribution = {row[0]: row[1] for row in cur.fetchall()}

        # Evaluation path distribution (for pie chart)
        with db.cursor() as cur:
            cur.execute(
                "SELECT evaluation_path, COUNT(*) FROM audit_log "
                f"WHERE user_id = ?{audit_agent_cond} GROUP BY evaluation_path",
                (ctx.user_id, *audit_agent_params),
            )
            path_distribution = {row[0]: row[1] for row in cur.fetchall()}

        # Classification distribution (for pie chart)
        with db.cursor() as cur:
            cur.execute(
                "SELECT classification, COUNT(*) FROM audit_log "
                f"WHERE user_id = ? AND classification IS NOT NULL"
                f"{audit_agent_cond} "
                "GROUP BY classification",
                (ctx.user_id, *audit_agent_params),
            )
            classification_distribution = {row[0]: row[1] for row in cur.fetchall()}

        # Top tools (for bar chart, top 10)
        with db.cursor() as cur:
            cur.execute(
                "SELECT tool, COUNT(*) as cnt FROM audit_log "
                f"WHERE user_id = ? AND tool IS NOT NULL{audit_agent_cond} "
                "GROUP BY tool ORDER BY cnt DESC LIMIT 10",
                (ctx.user_id, *audit_agent_params),
            )
            top_tools = [{"tool": row[0], "count": row[1]} for row in cur.fetchall()]

        # Activity timeline (evaluations per hour, last 24h)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        # SQLite uses strftime(); PostgreSQL uses to_char().
        if db.backend == "postgresql":
            hour_expr = "to_char(timestamp, 'YYYY-MM-DD\"T\"HH24:00')"
        else:
            hour_expr = "strftime('%Y-%m-%dT%H:00', timestamp)"
        with db.cursor() as cur:
            cur.execute(
                f"SELECT {hour_expr} as hour, "
                "COUNT(*) as cnt FROM audit_log "
                f"WHERE user_id = ? AND timestamp >= ?{audit_agent_cond} "
                "GROUP BY hour ORDER BY hour",
                (ctx.user_id, cutoff, *audit_agent_params),
            )
            activity_timeline = [
                {"hour": row[0], "count": row[1]} for row in cur.fetchall()
            ]

        # Sessions timeline (unique active sessions per hour, last 24h)
        with db.cursor() as cur:
            cur.execute(
                f"SELECT {hour_expr} as hour, "
                "COUNT(DISTINCT session_id) as cnt FROM audit_log "
                f"WHERE user_id = ? AND timestamp >= ?{audit_agent_cond} "
                "GROUP BY hour ORDER BY hour",
                (ctx.user_id, cutoff, *audit_agent_params),
            )
            sessions_timeline = [
                {"hour": row[0], "count": row[1]} for row in cur.fetchall()
            ]

        # MCP proxy stats
        mcp_stats = {}
        try:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM mcp_servers WHERE user_id = ?",
                    (ctx.user_id,),
                )
                mcp_stats["total_servers"] = cur.fetchone()[0]

            with db.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM mcp_servers "
                    "WHERE user_id = ? AND enabled = ?",
                    (ctx.user_id, True),
                )
                mcp_stats["enabled_servers"] = cur.fetchone()[0]

            mcp_proxy = getattr(
                getattr(request, "app", None),
                "state",
                None,
            )
            mcp_proxy = getattr(mcp_proxy, "mcp_proxy", None)
            if mcp_proxy is not None:
                mcp_stats["active_sessions"] = mcp_proxy.active_sessions
                mcp_stats["active_connections"] = mcp_proxy.connection_count
        except Exception:
            pass  # MCP tables may not exist in older DBs.

        # Behavioral analysis stats
        analysis_stats = _get_analysis_stats(db, ctx.user_id, agent_id, request)

        return {
            "total_sessions": total_sessions,
            "sessions_by_status": status_counts,
            "total_evaluations": total_evaluations,
            "decisions": decision_counts,
            "approval_rate": approval_rate,
            "pending_approvals": pending_approvals,
            "avg_latency_ms": avg_latency_ms,
            "risk_distribution": risk_distribution,
            "path_distribution": path_distribution,
            "classification_distribution": classification_distribution,
            "top_tools": top_tools,
            "activity_timeline": activity_timeline,
            "sessions_timeline": sessions_timeline,
            "users": users,
            "agents": agents,
            "mcp": mcp_stats,
            "analysis": analysis_stats,
        }
    except Exception:
        logger.exception("Error in /stats")
        raise HTTPException(
            status_code=500,
            detail="Internal error computing stats",
        )


def _get_analysis_stats(
    db: Any,
    user_id: str,
    agent_id: str | None,
    request: Request,
) -> dict:
    """Compute behavioral analysis stats for the dashboard.

    Returns profile risk level, summary/analysis counts, and queue depth.
    """
    from typing import Any as _Any

    stats: dict[str, _Any] = {
        "behavioral_risk_level": 1,
        "profile_version": 0,
        "total_summaries": 0,
        "total_analyses": 0,
        "last_analysis_at": None,
        "pending_tasks": 0,
    }

    try:
        # Get behavioral profile for the agent (or user-level)
        with db.cursor() as cur:
            if agent_id:
                cur.execute(
                    "SELECT risk_level, profile_version FROM behavioral_profiles "
                    "WHERE user_id = ? AND agent_id = ?",
                    (user_id, agent_id),
                )
            else:
                # Match /profile ordering: most recently updated profile
                cur.execute(
                    "SELECT risk_level, profile_version FROM behavioral_profiles "
                    "WHERE user_id = ? "
                    "ORDER BY updated_at DESC "
                    "LIMIT 1",
                    (user_id,),
                )
            row = cur.fetchone()
            if row:
                stats["behavioral_risk_level"] = row["risk_level"]
                stats["profile_version"] = row["profile_version"]

        # Total summaries
        with db.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM session_summaries WHERE user_id = ?",
                (user_id,),
            )
            stats["total_summaries"] = cur.fetchone()[0]

        # Total analyses and last analysis timestamp
        agent_cond = ""
        agent_params: tuple[str, ...] = ()
        if agent_id:
            agent_cond = " AND agent_id = ?"
            agent_params = (agent_id,)

        with db.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*), MAX(created_at) FROM behavioral_analyses "
                f"WHERE user_id = ?{agent_cond}",
                (user_id, *agent_params),
            )
            row = cur.fetchone()
            if row:
                stats["total_analyses"] = row[0] or 0
                stats["last_analysis_at"] = row[1]

        # Latest analysis findings (compact: category + severity only)
        with db.cursor() as cur:
            cur.execute(
                "SELECT findings, risk_level FROM behavioral_analyses "
                f"WHERE user_id = ?{agent_cond} "
                "ORDER BY created_at DESC LIMIT 1",
                (user_id, *agent_params),
            )
            row = cur.fetchone()
            if row and row["findings"]:
                try:
                    raw = row["findings"]
                    findings = json.loads(raw) if isinstance(raw, str) else raw
                    if isinstance(findings, list):
                        stats["latest_risk_level"] = row["risk_level"]
                        stats["latest_findings"] = [
                            {
                                "category": f.get("category", "?"),
                                "severity": f.get("severity", 1),
                            }
                            for f in findings
                        ]
                except (json.JSONDecodeError, TypeError):
                    pass

        # Pending tasks in queue
        bg_worker = getattr(
            getattr(request, "app", None),
            "state",
            None,
        )
        bg_worker = getattr(bg_worker, "background_worker", None)
        if bg_worker is not None:
            stats["pending_tasks"] = bg_worker.metrics.task_queue_depth
            # Judge metrics
            stats["judge_reviews_total"] = bg_worker.metrics.judge_reviews_total
            stats["judge_approvals_total"] = bg_worker.metrics.judge_approvals_total
            stats["judge_denials_total"] = bg_worker.metrics.judge_denials_total
            stats["judge_deferrals_total"] = bg_worker.metrics.judge_deferrals_total
            stats["judge_errors_total"] = bg_worker.metrics.judge_errors_total
    except Exception:
        logger.debug("Failed to compute analysis stats", exc_info=True)

    return stats


@router.get("/config")
async def config(
    request: Request,
    ctx: SessionContext = Depends(get_session_context),
) -> dict:
    """Return non-sensitive server configuration for the Settings tab.

    Explicitly excludes API keys, webhook secrets, and internal URLs.
    The LLM base URL is masked to prevent topology leakage.
    """
    from intaris import __version__
    from intaris.server import _get_config

    try:
        cfg = _get_config()

        # Mask LLM base URL — show "openai" for default, "custom" otherwise
        default_openai = "https://api.openai.com/v1"
        llm_base_url_display = (
            "openai" if cfg.llm.base_url == default_openai else "custom"
        )
        analysis_llm_base_url_display = (
            "openai" if cfg.llm_analysis.base_url == default_openai else "custom"
        )

        return {
            "version": __version__,
            "llm": {
                "model": cfg.llm.model,
                "base_url": llm_base_url_display,
                "temperature": cfg.llm.temperature,
                "reasoning_effort": cfg.llm.reasoning_effort,
                "timeout_ms": cfg.llm.timeout_ms,
            },
            "llm_analysis": {
                "model": cfg.llm_analysis.model,
                "base_url": analysis_llm_base_url_display,
                "temperature": cfg.llm_analysis.temperature,
                "reasoning_effort": cfg.llm_analysis.reasoning_effort,
                "timeout_ms": cfg.llm_analysis.timeout_ms,
            },
            "llm_l3_analysis": {
                "model": cfg.llm_l3_analysis.model,
                "reasoning_effort": cfg.llm_l3_analysis.reasoning_effort,
                "timeout_ms": cfg.llm_l3_analysis.timeout_ms,
            },
            "llm_judge": {
                "model": cfg.llm_judge.model,
                "reasoning_effort": cfg.llm_judge.reasoning_effort,
                "timeout_ms": cfg.llm_judge.timeout_ms,
            },
            "judge": {
                "mode": cfg.judge.mode,
                "notify_mode": cfg.judge.notify_mode,
            },
            "analysis": {
                "enabled": cfg.analysis.enabled,
                "session_idle_timeout_min": cfg.analysis.session_idle_timeout_min,
                "summary_volume_threshold": cfg.analysis.summary_volume_threshold,
                "analysis_interval_min": cfg.analysis.analysis_interval_min,
                "lookback_days": cfg.analysis.lookback_days,
                "worker_count": cfg.analysis.worker_count,
            },
            "rate_limit": cfg.server.rate_limit,
            "webhook_configured": bool(cfg.webhook.url),
            "auth_configured": bool(cfg.server.api_keys or cfg.server.api_key),
            "mcp": {
                "config_file": bool(cfg.mcp.config_file),
                "allow_stdio": cfg.mcp.allow_stdio,
                "encryption_configured": bool(cfg.mcp.encryption_key),
                "upstream_timeout_ms": cfg.mcp.upstream_timeout_ms,
            },
            "event_store": {
                "enabled": cfg.event_store.enabled,
                "backend": cfg.event_store.backend,
                "flush_size": cfg.event_store.flush_size,
                "flush_interval": cfg.event_store.flush_interval,
            },
            "notification": {
                "action_ttl_minutes": cfg.notification.action_ttl_minutes,
                "encryption_configured": bool(cfg.mcp.encryption_key),
            },
            "db": {
                "backend": cfg.db.backend,
            },
        }
    except Exception:
        logger.exception("Error in /config")
        raise HTTPException(
            status_code=500,
            detail="Internal error fetching config",
        )
