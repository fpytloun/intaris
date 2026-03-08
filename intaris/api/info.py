"""Identity, stats, and configuration endpoints for the management UI."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

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
) -> dict:
    """Return aggregated statistics for the dashboard.

    Computes session counts, evaluation totals, decision distribution,
    pending approvals, and the list of known users (for impersonation).
    """
    from intaris.server import _get_db

    try:
        db = _get_db()

        # Session counts by status
        with db.cursor() as cur:
            cur.execute(
                "SELECT status, COUNT(*) FROM sessions "
                "WHERE user_id = ? GROUP BY status",
                (ctx.user_id,),
            )
            status_counts = dict(cur.fetchall())

        total_sessions = sum(status_counts.values())

        # Evaluation totals and decision distribution
        with db.cursor() as cur:
            cur.execute(
                "SELECT decision, COUNT(*) FROM audit_log "
                "WHERE user_id = ? GROUP BY decision",
                (ctx.user_id,),
            )
            decision_counts = dict(cur.fetchall())

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
                "AND user_decision IS NULL",
                (ctx.user_id,),
            )
            pending_approvals = cur.fetchone()[0]

        # Average latency
        with db.cursor() as cur:
            cur.execute(
                "SELECT AVG(latency_ms) FROM audit_log WHERE user_id = ?",
                (ctx.user_id,),
            )
            avg_latency = cur.fetchone()[0]
            avg_latency_ms = round(avg_latency, 1) if avg_latency else 0.0

        # Known users (for impersonation dropdown).
        # Only return the full list when user switching is allowed
        # (unbound API key). Bound users only see their own ID to
        # prevent cross-tenant user enumeration.
        if not ctx.user_bound:
            with db.cursor() as cur:
                cur.execute("SELECT DISTINCT user_id FROM sessions ORDER BY user_id")
                users = [row[0] for row in cur.fetchall()]
        else:
            users = [ctx.user_id]

        # Risk distribution (for pie chart)
        with db.cursor() as cur:
            cur.execute(
                "SELECT risk, COUNT(*) FROM audit_log "
                "WHERE user_id = ? AND risk IS NOT NULL "
                "GROUP BY risk",
                (ctx.user_id,),
            )
            risk_distribution = dict(cur.fetchall())

        # Evaluation path distribution (for pie chart)
        with db.cursor() as cur:
            cur.execute(
                "SELECT evaluation_path, COUNT(*) FROM audit_log "
                "WHERE user_id = ? GROUP BY evaluation_path",
                (ctx.user_id,),
            )
            path_distribution = dict(cur.fetchall())

        # Classification distribution (for pie chart)
        with db.cursor() as cur:
            cur.execute(
                "SELECT classification, COUNT(*) FROM audit_log "
                "WHERE user_id = ? AND classification IS NOT NULL "
                "GROUP BY classification",
                (ctx.user_id,),
            )
            classification_distribution = dict(cur.fetchall())

        # Top tools (for bar chart, top 10)
        with db.cursor() as cur:
            cur.execute(
                "SELECT tool, COUNT(*) as cnt FROM audit_log "
                "WHERE user_id = ? AND tool IS NOT NULL "
                "GROUP BY tool ORDER BY cnt DESC LIMIT 10",
                (ctx.user_id,),
            )
            top_tools = [{"tool": row[0], "count": row[1]} for row in cur.fetchall()]

        # Activity timeline (evaluations per hour, last 24h)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        with db.cursor() as cur:
            cur.execute(
                "SELECT strftime('%Y-%m-%dT%H:00', timestamp) as hour, "
                "COUNT(*) as cnt FROM audit_log "
                "WHERE user_id = ? AND timestamp >= ? "
                "GROUP BY hour ORDER BY hour",
                (ctx.user_id, cutoff),
            )
            activity_timeline = [
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
                    "WHERE user_id = ? AND enabled = 1",
                    (ctx.user_id,),
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
            "users": users,
            "mcp": mcp_stats,
        }
    except Exception:
        logger.exception("Error in /stats")
        raise HTTPException(
            status_code=500,
            detail="Internal error computing stats",
        )


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

        return {
            "version": __version__,
            "llm": {
                "model": cfg.llm.model,
                "base_url": llm_base_url_display,
                "temperature": cfg.llm.temperature,
                "reasoning_effort": cfg.llm.reasoning_effort,
                "timeout_ms": cfg.llm.timeout_ms,
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
        }
    except Exception:
        logger.exception("Error in /config")
        raise HTTPException(
            status_code=500,
            detail="Internal error fetching config",
        )
