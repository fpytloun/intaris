"""Tests for the decision matrix."""

from __future__ import annotations

import pytest

from intaris.decision import (
    Decision,
    EvaluationResult,
    apply_decision_matrix,
    make_fast_decision,
)


class TestDecisionMatrix:
    """Test priority-ordered decision matrix."""

    def test_critical_risk_always_deny(self):
        """Priority 1: Critical risk → deny regardless of alignment."""
        result = apply_decision_matrix(
            EvaluationResult(
                aligned=True,
                risk="critical",
                reasoning="Destructive operation",
                decision="approve",  # LLM said approve, but matrix overrides
            )
        )
        assert result.decision == "deny"
        assert result.risk == "critical"

    def test_critical_risk_not_aligned(self):
        result = apply_decision_matrix(
            EvaluationResult(
                aligned=False,
                risk="critical",
                reasoning="Dangerous",
                decision="deny",
            )
        )
        assert result.decision == "deny"

    def test_aligned_low_risk_approve(self):
        """Aligned + low risk → approve."""
        result = apply_decision_matrix(
            EvaluationResult(
                aligned=True,
                risk="low",
                reasoning="Routine operation",
                decision="approve",
            )
        )
        assert result.decision == "approve"

    def test_aligned_medium_risk_approve(self):
        """Aligned + medium risk → approve."""
        result = apply_decision_matrix(
            EvaluationResult(
                aligned=True,
                risk="medium",
                reasoning="Expected modification",
                decision="approve",
            )
        )
        assert result.decision == "approve"

    def test_aligned_high_risk_escalate(self):
        """Aligned + high risk → escalate."""
        result = apply_decision_matrix(
            EvaluationResult(
                aligned=True,
                risk="high",
                reasoning="Significant impact",
                decision="approve",
            )
        )
        assert result.decision == "escalate"

    def test_not_aligned_escalate(self):
        """Not aligned → escalate regardless of risk."""
        for risk in ("low", "medium", "high"):
            result = apply_decision_matrix(
                EvaluationResult(
                    aligned=False,
                    risk=risk,
                    reasoning="Off-topic",
                    decision="approve",
                )
            )
            assert result.decision == "escalate", f"Expected escalate for risk={risk}"

    def test_llm_deny_high_risk_respected(self):
        """LLM deny + high risk → deny (honored)."""
        result = apply_decision_matrix(
            EvaluationResult(
                aligned=True,
                risk="high",
                reasoning="Dangerous operation",
                decision="deny",
            )
        )
        assert result.decision == "deny"

    def test_llm_deny_low_risk_escalates(self):
        """LLM deny + low risk → escalate (not deny)."""
        result = apply_decision_matrix(
            EvaluationResult(
                aligned=True,
                risk="low",
                reasoning="Suspicious pattern",
                decision="deny",
            )
        )
        assert result.decision == "escalate"
        assert "escalating for review" in result.reasoning.lower()

    def test_llm_deny_medium_risk_escalates(self):
        """LLM deny + medium risk → escalate (not deny)."""
        result = apply_decision_matrix(
            EvaluationResult(
                aligned=False,
                risk="medium",
                reasoning="Not aligned but medium risk",
                decision="deny",
            )
        )
        assert result.decision == "escalate"
        assert "escalating for review" in result.reasoning.lower()

    def test_reasoning_preserved(self):
        result = apply_decision_matrix(
            EvaluationResult(
                aligned=True,
                risk="low",
                reasoning="This is safe",
                decision="approve",
            )
        )
        assert "This is safe" in result.reasoning

    def test_path_is_llm(self):
        result = apply_decision_matrix(
            EvaluationResult(
                aligned=True,
                risk="low",
                reasoning="test",
                decision="approve",
            )
        )
        assert result.path == "llm"


class TestFastDecision:
    """Test fast-path decisions."""

    def test_read_approve(self):
        result = make_fast_decision("read", "Read-only tool")
        assert result.decision == "approve"
        assert result.risk == "low"
        assert result.path == "fast"

    def test_critical_deny(self):
        result = make_fast_decision("critical", "Critical pattern")
        assert result.decision == "deny"
        assert result.risk == "critical"
        assert result.path == "critical"

    def test_invalid_classification(self):
        with pytest.raises(ValueError, match="Invalid fast-path"):
            make_fast_decision("write", "Should fail")


class TestDecisionProperties:
    """Test Decision dataclass properties."""

    def test_is_approved(self):
        assert (
            Decision(
                decision="approve", risk="low", reasoning="", path="fast"
            ).is_approved
            is True
        )

    def test_is_not_approved(self):
        assert (
            Decision(decision="deny", risk="high", reasoning="", path="llm").is_approved
            is False
        )

        assert (
            Decision(
                decision="escalate", risk="high", reasoning="", path="llm"
            ).is_approved
            is False
        )
