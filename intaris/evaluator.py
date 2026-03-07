"""Safety evaluator for intaris.

Orchestrates the full evaluation pipeline:
  classify → critical check → LLM evaluation → decision matrix → audit

This is the main entry point for tool call evaluation. The /evaluate
API endpoint delegates to this module.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from intaris.audit import AuditStore
from intaris.classifier import Classification, classify
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
    ):
        self._llm = llm
        self._sessions = session_store
        self._audit = audit_store

    def evaluate(
        self,
        *,
        session_id: str,
        agent_id: str | None,
        tool: str,
        args: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Evaluate a tool call for safety and intention alignment.

        This is the main entry point for the evaluation pipeline:
        1. Redact secrets from args
        2. Classify the tool call (read/write/critical)
        3. For read-only: auto-approve (fast path)
        4. For critical: auto-deny (fast path)
        5. For write: LLM safety evaluation → decision matrix
        6. Log audit record
        7. Update session counters

        Args:
            session_id: Session this call belongs to.
            agent_id: Agent making the call (optional).
            tool: Tool name (e.g., "bash", "edit", "mcp:add_memory").
            args: Tool arguments (will be redacted before storage).
            context: Optional additional context for evaluation.

        Returns:
            Dict with: call_id, decision, reasoning, risk, path, latency_ms.

        Raises:
            ValueError: If session not found.
        """
        start_time = time.monotonic()
        call_id = str(uuid.uuid4())

        # Get session for intention and policy
        session = self._sessions.get(session_id)

        # Redact secrets from args
        args_redacted = redact(args)

        # Get session policy for classifier
        session_policy = session.get("policy")

        # Step 1-2: Classify
        classification = classify(tool, args, session_policy=session_policy)

        # Step 3-4: Fast path for read-only and critical
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
        else:
            # Step 5: LLM safety evaluation
            decision = self._llm_evaluate(
                session=session,
                tool=tool,
                args_redacted=args_redacted,
                agent_id=agent_id,
                context=context,
            )

        # Calculate latency
        latency_ms = int((time.monotonic() - start_time) * 1000)

        # Step 6: Audit
        self._audit.insert(
            call_id=call_id,
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
        )

        # Step 7: Update session counters
        try:
            self._sessions.increment_counter(session_id, decision.decision)
        except ValueError:
            logger.warning("Failed to update session counter for %s", session_id)

        return {
            "call_id": call_id,
            "decision": decision.decision,
            "reasoning": decision.reasoning,
            "risk": decision.risk,
            "path": decision.path,
            "latency_ms": latency_ms,
        }

    def _llm_evaluate(
        self,
        *,
        session: dict[str, Any],
        tool: str,
        args_redacted: dict[str, Any],
        agent_id: str | None,
        context: dict[str, Any] | None = None,
    ) -> Decision:
        """Run LLM safety evaluation and apply decision matrix.

        Args:
            session: Session dict with intention, policy, stats.
            tool: Tool name.
            args_redacted: Redacted tool arguments.
            agent_id: Agent identity.
            context: Optional additional context for evaluation.

        Returns:
            Decision from the decision matrix.
        """
        # Assemble context
        session_id = session["session_id"]
        recent_history = self._audit.get_recent(session_id, limit=10)

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
            # LLM failure → treat as escalate (safe default)
            logger.exception("LLM safety evaluation failed")
            return Decision(
                decision="escalate",
                risk="high",
                reasoning="LLM evaluation failed — escalating as safe default",
                path="llm",
            )
