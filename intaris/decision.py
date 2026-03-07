"""Decision matrix for intaris safety evaluation.

Implements the priority-ordered decision matrix that maps LLM evaluation
results to final decisions. Also handles standalone escalation behavior
(deny with message when no Cognis controller is available).

Decision priority (first match wins):
1. critical risk (any alignment) → AUTO-DENY
2. LLM returned "deny"          → DENY
3. aligned + low risk            → APPROVE
4. aligned + medium risk         → APPROVE
5. aligned + high risk           → ESCALATE
6. not aligned + any risk        → ESCALATE

Standalone escalation behavior:
When Intaris runs without Cognis, escalations cannot be routed to a
human approval queue. In this mode, escalations are treated as denials
with a message directing the user to the Intaris UI for review:
"Escalation requires approval — denied by default. Use Intaris UI to
review pending calls."
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class EvaluationResult:
    """Result from LLM safety evaluation."""

    aligned: bool
    risk: str  # "low", "medium", "high", "critical"
    reasoning: str
    decision: str  # "approve", "deny", "escalate"


@dataclass
class Decision:
    """Final decision after applying the decision matrix."""

    decision: str  # "approve", "deny", "escalate"
    risk: str
    reasoning: str
    path: str  # "fast", "critical", "llm"

    @property
    def is_approved(self) -> bool:
        return self.decision == "approve"


# Message used when escalation is denied in standalone mode.
STANDALONE_ESCALATION_MESSAGE = (
    "Escalation requires approval — denied by default. "
    "Use Intaris UI to review pending calls."
)


def apply_decision_matrix(evaluation: EvaluationResult) -> Decision:
    """Apply the priority-ordered decision matrix to an LLM evaluation.

    Args:
        evaluation: Result from LLM safety evaluation.

    Returns:
        Final decision with reasoning.
    """
    risk = evaluation.risk.lower()
    aligned = evaluation.aligned

    # Priority 1: Critical risk → always deny
    if risk == "critical":
        logger.info("Decision: DENY (critical risk, aligned=%s)", aligned)
        return Decision(
            decision="deny",
            risk=risk,
            reasoning=f"Critical risk — auto-denied. {evaluation.reasoning}",
            path="llm",
        )

    # Priority 6 (checked early): LLM explicitly said deny
    if evaluation.decision == "deny":
        logger.info("Decision: DENY (LLM recommended deny)")
        return Decision(
            decision="deny",
            risk=risk,
            reasoning=evaluation.reasoning,
            path="llm",
        )

    # Priority 2-3: Aligned + low/medium risk → approve
    if aligned and risk in ("low", "medium"):
        logger.info("Decision: APPROVE (aligned, %s risk)", risk)
        return Decision(
            decision="approve",
            risk=risk,
            reasoning=evaluation.reasoning,
            path="llm",
        )

    # Priority 4: Aligned + high risk → escalate
    if aligned and risk == "high":
        logger.info("Decision: ESCALATE (aligned but high risk)")
        return Decision(
            decision="escalate",
            risk=risk,
            reasoning=f"High risk — requires human review. {evaluation.reasoning}",
            path="llm",
        )

    # Priority 5: Not aligned → escalate
    if not aligned:
        logger.info("Decision: ESCALATE (not aligned, %s risk)", risk)
        return Decision(
            decision="escalate",
            risk=risk,
            reasoning=f"Not aligned with intention. {evaluation.reasoning}",
            path="llm",
        )

    # Fallback: escalate (should not reach here with valid input)
    logger.warning(
        "Decision: ESCALATE (fallback — unexpected state: "
        "aligned=%s, risk=%s, decision=%s)",
        aligned,
        risk,
        evaluation.decision,
    )
    return Decision(
        decision="escalate",
        risk=risk,
        reasoning=f"Unexpected evaluation state — escalating. {evaluation.reasoning}",
        path="llm",
    )


def make_fast_decision(
    classification: str,
    reasoning: str,
) -> Decision:
    """Create a fast-path decision (no LLM evaluation needed).

    Used for read-only (auto-approve) and critical (auto-deny) classifications.

    Args:
        classification: "read" or "critical".
        reasoning: Explanation for the decision.

    Returns:
        Decision object.
    """
    if classification == "read":
        return Decision(
            decision="approve",
            risk="low",
            reasoning=reasoning,
            path="fast",
        )
    elif classification == "critical":
        return Decision(
            decision="deny",
            risk="critical",
            reasoning=reasoning,
            path="critical",
        )
    elif classification == "escalate":
        return Decision(
            decision="escalate",
            risk="high",
            reasoning=reasoning,
            path="fast",
        )
    else:
        raise ValueError(f"Invalid fast-path classification: {classification}")
