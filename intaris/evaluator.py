"""Safety evaluator for intaris.

Orchestrates the full evaluation pipeline:
  classify → critical check → LLM evaluation → decision matrix → audit

This is the main entry point for tool call evaluation. The /evaluate
API endpoint delegates to this module.

For MCP proxy calls, the evaluator also supports:
- Tool preference overrides (auto-approve, escalate, deny)
- Escalation retry: reuses a prior approval if the same tool+args
  combination was approved within the last 10 minutes.
- args_hash storage for escalation retry lookups.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from typing import Any

from intaris.audit import AuditStore
from intaris.classifier import Classification, classify
from intaris.config import AnalysisConfig
from intaris.db import Database
from intaris.decision import (
    Decision,
    EvaluationResult,
    apply_decision_matrix,
    make_fast_decision,
)
from intaris.llm import LLMClient, parse_json_response
from intaris.prompts import (
    SAFETY_EVALUATION_SCHEMA,
    SAFETY_EVALUATION_SYSTEM_PROMPT,
    build_evaluation_user_prompt,
)
from intaris.redactor import redact
from intaris.session import SessionStore

# Escalation retry: reuse approval if same tool+args approved within this window.
_ESCALATION_RETRY_TTL_MINUTES = 10

logger = logging.getLogger(__name__)


class Evaluator:
    """Orchestrates tool call safety evaluation.

    Combines classification, LLM evaluation, decision matrix, and
    audit logging into a single pipeline.
    """

    def __init__(
        self,
        *,
        llm: LLMClient,
        session_store: SessionStore,
        audit_store: AuditStore,
        db: Database | None = None,
        analysis_config: AnalysisConfig | None = None,
    ):
        self._llm = llm
        self._sessions = session_store
        self._audit = audit_store
        self._db = db
        self._analysis_config = analysis_config
        self._analysis_enabled = analysis_config is not None and analysis_config.enabled

    def evaluate(
        self,
        *,
        user_id: str,
        session_id: str,
        agent_id: str | None,
        tool: str,
        args: dict[str, Any],
        context: dict[str, Any] | None = None,
        tool_preferences: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Evaluate a tool call for safety and intention alignment.

        This is the main entry point for the evaluation pipeline:
        1. Redact secrets from args
        2. Classify the tool call (read/write/critical/escalate)
        3. For read-only: auto-approve (fast path)
        4. For critical: auto-deny (fast path)
        5. For escalate: check retry cache, else escalate (fast path)
        6. For write: LLM safety evaluation → decision matrix
        7. Log audit record (with args_hash for escalation retry)
        8. Update session counters

        Args:
            user_id: Tenant identifier.
            session_id: Session this call belongs to.
            agent_id: Agent making the call (optional).
            tool: Tool name (e.g., "bash", "edit", "mcp:add_memory").
            args: Tool arguments (will be redacted before storage).
            context: Optional additional context for evaluation.
            tool_preferences: Optional per-tool preference overrides
                mapping 'server:tool' → preference string. Used by
                the MCP proxy to pass user-configured tool policies.

        Returns:
            Dict with: call_id, decision, reasoning, risk, path, latency_ms.

        Raises:
            ValueError: If session not found.
        """
        start_time = time.monotonic()
        call_id = str(uuid.uuid4())

        # Get session for intention and policy (verifies ownership)
        session = self._sessions.get(session_id, user_id=user_id)

        # Check session status — deny evaluation for inactive sessions
        session_status = session.get("status", "active")

        # Auto-resume idle sessions silently (behavioral guardrails)
        if session_status == "idle":
            try:
                self._sessions.update_status(session_id, "active", user_id=user_id)
                session_status = "active"
                logger.debug("Auto-resumed idle session %s", session_id)
            except ValueError:
                pass

        if session_status in ("completed", "suspended", "terminated"):
            latency_ms = int((time.monotonic() - start_time) * 1000)
            self._audit.insert(
                call_id=call_id,
                user_id=user_id,
                session_id=session_id,
                agent_id=agent_id,
                tool=tool,
                args_redacted=redact(args),
                classification="write",
                evaluation_path="fast",
                decision="deny",
                risk="low",
                reasoning=f"Session is {session_status} — evaluation denied",
                latency_ms=latency_ms,
            )
            try:
                self._sessions.increment_counter(session_id, "deny", user_id=user_id)
            except ValueError:
                pass
            return {
                "call_id": call_id,
                "decision": "deny",
                "reasoning": f"Session is {session_status} — evaluation denied",
                "risk": "low",
                "path": "fast",
                "latency_ms": latency_ms,
                "args_redacted": redact(args),
                "classification": "write",
            }

        # Update session activity timestamp (for idle detection)
        try:
            self._sessions.update_activity(session_id, user_id=user_id)
        except Exception:
            logger.debug("Failed to update session activity", exc_info=True)

        # Lookup behavioral profile for context injection
        profile_version: int | None = None
        if self._analysis_enabled and self._db is not None:
            profile = self._get_behavioral_context(user_id)
            if profile:
                profile_version = profile.get("profile_version")
                # Inject context for high/critical risk profiles
                if profile.get("risk_level") in ("high", "critical"):
                    context = dict(context) if context else {}
                    context["behavioral_alert"] = profile.get("context_summary", "")

        # Redact secrets from args
        args_redacted = redact(args)

        # Compute args_hash for escalation retry lookups
        args_hash = _compute_args_hash(args)

        # Get session policy for classifier
        session_policy = session.get("policy")

        # Resolve parent intention for sub-sessions (intention chain).
        # Sub-agent tool calls must be aligned with BOTH the parent
        # session's intention and their own. This prevents sub-agents
        # from escaping the parent's guardrails by declaring a narrow
        # intention that doesn't cover what they're actually doing.
        parent_intention: str | None = None
        parent_session_id = session.get("parent_session_id")
        if parent_session_id:
            try:
                parent_session = self._sessions.get(parent_session_id, user_id=user_id)
                parent_intention = parent_session.get("intention")
            except ValueError:
                logger.debug(
                    "Parent session %s not found for sub-session %s",
                    parent_session_id,
                    session_id,
                )

        # Step 1-2: Classify (with tool preferences for MCP proxy)
        classification = classify(
            tool,
            args,
            session_policy=session_policy,
            tool_preferences=tool_preferences,
        )

        # Step 3-5: Fast paths for read-only, critical, and escalate
        if classification == Classification.READ:
            decision = make_fast_decision(
                "read",
                f"Read-only tool call: {tool}",
            )
        elif classification == Classification.CRITICAL:
            decision = make_fast_decision(
                "critical",
                f"Critical pattern detected in {tool} call",
            )
        elif classification == Classification.ESCALATE:
            # Check escalation retry: reuse prior approval if same
            # tool+args was approved within the TTL window.
            retry_decision = self._check_escalation_retry(
                user_id=user_id,
                tool=tool,
                args_hash=args_hash,
            )
            if retry_decision is not None:
                decision = retry_decision
            else:
                decision = make_fast_decision(
                    "escalate",
                    f"Tool preference requires escalation for {tool}",
                )
        else:
            # Step 6: LLM safety evaluation
            decision = self._llm_evaluate(
                session=session,
                tool=tool,
                args_redacted=args_redacted,
                agent_id=agent_id,
                context=context,
                parent_intention=parent_intention,
            )

        # Calculate latency
        latency_ms = int((time.monotonic() - start_time) * 1000)

        # Step 7: Audit (with args_hash for escalation retry, profile_version)
        self._audit.insert(
            call_id=call_id,
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
            tool=tool,
            args_redacted=args_redacted,
            classification=classification.value,
            evaluation_path=decision.path,
            decision=decision.decision,
            risk=decision.risk,
            reasoning=decision.reasoning,
            latency_ms=latency_ms,
            args_hash=args_hash,
            profile_version=profile_version,
            intention=session.get("intention"),
        )

        # Step 8: Update session counters
        try:
            self._sessions.increment_counter(
                session_id, decision.decision, user_id=user_id
            )
        except ValueError:
            logger.warning("Failed to update session counter for %s", session_id)

        # Enqueue periodic intention update task
        # Triggers every 20 calls starting at call 10 to refine intention
        # as the session evolves. User messages also trigger updates
        # via POST /reasoning (see api/analysis.py).
        total = session.get("total_calls", 0)
        if total >= 9 and total % 20 == 9 and self._analysis_enabled:
            try:
                from intaris.background import TaskQueue

                if self._db is not None:
                    tq = TaskQueue(self._db)
                    if not tq.recently_completed(
                        "intention_update", user_id, session_id, 60
                    ) and not tq.cancel_duplicate(
                        "intention_update", user_id, session_id
                    ):
                        tq.enqueue(
                            "intention_update",
                            user_id,
                            session_id=session_id,
                            payload={"trigger": "auto"},
                            priority=2,
                        )
            except Exception:
                logger.debug("Failed to enqueue intention update", exc_info=True)

        return {
            "call_id": call_id,
            "decision": decision.decision,
            "reasoning": decision.reasoning,
            "risk": decision.risk,
            "path": decision.path,
            "latency_ms": latency_ms,
            "args_redacted": args_redacted,
            "classification": classification.value,
        }

    def _get_behavioral_context(self, user_id: str) -> dict[str, Any] | None:
        """Fast DB lookup of pre-computed behavioral profile.

        Returns the profile dict if found, None otherwise.
        This is a ~1ms read that does not impact the evaluate hot path.

        Args:
            user_id: Tenant identifier.

        Returns:
            Profile dict with risk_level, context_summary, profile_version,
            or None if no profile exists.
        """
        if self._db is None:
            return None

        try:
            with self._db.cursor() as cur:
                cur.execute(
                    "SELECT risk_level, context_summary, profile_version "
                    "FROM behavioral_profiles WHERE user_id = ?",
                    (user_id,),
                )
                row = cur.fetchone()
            return dict(row) if row else None
        except Exception:
            logger.debug("Failed to lookup behavioral profile", exc_info=True)
            return None

    def _check_escalation_retry(
        self,
        *,
        user_id: str,
        tool: str,
        args_hash: str,
    ) -> Decision | None:
        """Check if a prior escalation for the same tool+args was approved.

        Looks for an audit record within the retry TTL window where:
        - Same user, tool, and args_hash (session-independent so approvals
          survive MCP proxy reconnects)
        - The escalation was resolved with user_decision='approve'

        Returns:
            Decision to approve (reusing prior approval), or None if no
            valid prior approval found.
        """
        from datetime import datetime, timedelta, timezone

        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(minutes=_ESCALATION_RETRY_TTL_MINUTES)
        ).isoformat()

        row = self._audit.find_approved_escalation(
            user_id=user_id,
            tool=tool,
            args_hash=args_hash,
            cutoff=cutoff,
        )

        if row is not None:
            prior_call_id = row["call_id"]
            logger.info(
                "Escalation retry: reusing approval from %s for %s",
                prior_call_id,
                tool,
            )
            return Decision(
                decision="approve",
                risk="low",
                reasoning=(
                    f"Reusing prior approval (call {prior_call_id}) — "
                    f"same tool and arguments approved within "
                    f"{_ESCALATION_RETRY_TTL_MINUTES} minutes"
                ),
                path="fast",
            )

        return None

    def _llm_evaluate(
        self,
        *,
        session: dict[str, Any],
        tool: str,
        args_redacted: dict[str, Any],
        agent_id: str | None,
        context: dict[str, Any] | None = None,
        parent_intention: str | None = None,
    ) -> Decision:
        """Run LLM safety evaluation and apply decision matrix.

        Args:
            session: Session dict with intention, policy, stats.
            tool: Tool name.
            args_redacted: Redacted tool arguments.
            agent_id: Agent identity.
            context: Optional additional context for evaluation.
            parent_intention: Parent session intention for sub-sessions.

        Returns:
            Decision from the decision matrix.
        """
        # Assemble context
        session_id = session["session_id"]
        user_id = session["user_id"]
        recent_history = self._audit.get_recent(session_id, user_id=user_id, limit=10)

        user_prompt = build_evaluation_user_prompt(
            intention=session["intention"],
            policy=session.get("policy"),
            recent_history=recent_history,
            session_stats={
                "total_calls": session.get("total_calls", 0),
                "approved_count": session.get("approved_count", 0),
                "denied_count": session.get("denied_count", 0),
                "escalated_count": session.get("escalated_count", 0),
            },
            tool=tool,
            args=args_redacted,
            agent_id=agent_id,
            context=context,
            parent_intention=parent_intention,
        )

        messages = [
            {"role": "system", "content": SAFETY_EVALUATION_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        try:
            raw = self._llm.generate(
                messages,
                json_schema=SAFETY_EVALUATION_SCHEMA,
                max_tokens=1024,
            )
            result = parse_json_response(raw)

            evaluation = EvaluationResult(
                aligned=bool(result.get("aligned", False)),
                risk=str(result.get("risk", "high")),
                reasoning=str(result.get("reasoning", "No reasoning provided")),
                decision=str(result.get("decision", "escalate")),
            )

            return apply_decision_matrix(evaluation)

        except Exception:
            # LLM failure → propagate as exception. The API endpoint
            # catches this and returns 500, letting the client retry
            # with exponential backoff. Making a safety decision
            # (approve/deny/escalate) on an infra failure is wrong.
            logger.exception("LLM safety evaluation failed")
            raise


def _compute_args_hash(args: dict[str, Any]) -> str:
    """Compute a deterministic SHA-256 hash of tool arguments.

    Used for escalation retry: if the same tool+args combination was
    previously approved, the approval can be reused within the TTL window.

    Falls back to hashing the repr() if args contain non-JSON-serializable
    values (e.g., bytes, datetime objects).

    Args:
        args: Tool arguments dict.

    Returns:
        Hex-encoded SHA-256 hash string.
    """
    try:
        canonical = json.dumps(args, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        canonical = repr(sorted(args.items()))
    return hashlib.sha256(canonical.encode()).hexdigest()
