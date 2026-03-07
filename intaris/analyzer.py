"""Behavioral analysis engine for intaris.

Provides session summary generation (L2) and cross-session behavioral
analysis (L3). Uses the analysis LLM client for structured output.

Phase 1: Stub implementations that log and return empty results.
Phase 3: Full LLM-based analysis with prompt templates and token budgets.
"""

from __future__ import annotations

import logging
from typing import Any

from intaris.db import Database

logger = logging.getLogger(__name__)


async def generate_summary(
    db: Database,
    llm: Any | None,
    task: dict[str, Any],
) -> dict[str, Any]:
    """Generate an Intaris session summary for an activity window.

    Analyzes audit trail records within the window to produce a
    structured summary with intent alignment and risk indicators.

    Phase 1 stub: logs the task and returns an empty result.
    Phase 3 will implement full LLM-based summary generation.

    Args:
        db: Database instance.
        llm: LLM client for analysis (None in Phase 1).
        task: Task dict with payload containing session_id, trigger, etc.

    Returns:
        Result dict (empty in Phase 1).
    """
    user_id = task.get("user_id", "unknown")
    session_id = task.get("session_id", "unknown")
    payload = task.get("payload") or {}
    trigger = payload.get("trigger", "unknown")

    logger.info(
        "Summary generation stub: user=%s session=%s trigger=%s "
        "(full implementation in Phase 3)",
        user_id,
        session_id,
        trigger,
    )

    return {"status": "stub", "message": "Summary generation not yet implemented"}


async def run_analysis(
    db: Database,
    llm: Any | None,
    task: dict[str, Any],
) -> dict[str, Any]:
    """Run cross-session behavioral analysis for a user.

    Examines session summaries across a lookback window to detect
    patterns invisible at the individual session level.

    Phase 1 stub: logs the task and returns an empty result.
    Phase 4 will implement full LLM-based cross-session analysis.

    Args:
        db: Database instance.
        llm: LLM client for analysis (None in Phase 1).
        task: Task dict with payload containing lookback_days, etc.

    Returns:
        Result dict (empty in Phase 1).
    """
    user_id = task.get("user_id", "unknown")
    payload = task.get("payload") or {}
    triggered_by = payload.get("triggered_by", "unknown")

    logger.info(
        "Cross-session analysis stub: user=%s triggered_by=%s "
        "(full implementation in Phase 4)",
        user_id,
        triggered_by,
    )

    return {"status": "stub", "message": "Analysis not yet implemented"}
