"""Tests for judge auto-resolution module."""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from intaris.config import JudgeConfig

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary database."""
    from intaris.config import DBConfig
    from intaris.db import Database

    db_path = str(tmp_path / "test.db")
    return Database(DBConfig(path=db_path))


@pytest.fixture
def audit_store(tmp_db):
    """Create an AuditStore with a temporary database."""
    from intaris.audit import AuditStore

    return AuditStore(tmp_db)


@pytest.fixture
def session_store(tmp_db):
    """Create a SessionStore with a temporary database."""
    from intaris.session import SessionStore

    return SessionStore(tmp_db)


@pytest.fixture
def mock_llm():
    """Create a mock LLM client."""
    llm = MagicMock()
    return llm


@pytest.fixture
def mock_evaluator():
    """Create a mock evaluator."""
    evaluator = MagicMock()
    evaluator.learn_from_approved_escalation = MagicMock()
    evaluator.get_behavioral_context = MagicMock(return_value=None)
    return evaluator


@pytest.fixture
def mock_event_bus():
    """Create a mock EventBus."""
    bus = MagicMock()
    bus.publish = MagicMock()
    return bus


@pytest.fixture
def mock_metrics():
    """Create a mock Metrics instance."""
    from intaris.background import Metrics

    return Metrics()


def _create_session(session_store, session_id="test-session", user_id="test-user"):
    """Helper to create a test session."""
    session_store.create(
        session_id=session_id,
        user_id=user_id,
        intention="Test session for unit testing",
    )


def _create_escalated_record(audit_store, call_id="test-call", user_id="test-user"):
    """Helper to create an escalated audit record."""
    return audit_store.insert(
        call_id=call_id,
        user_id=user_id,
        session_id="test-session",
        agent_id="test-agent",
        tool="bash",
        args_redacted={"command": "rm -rf /tmp/test"},
        classification="write",
        evaluation_path="llm",
        decision="escalate",
        risk="high",
        reasoning="High risk operation requires human review",
        latency_ms=100,
    )


# ── Config Tests ──────────────────────────────────────────────────────


class TestJudgeConfig:
    """Test JudgeConfig defaults and validation."""

    def test_defaults(self):
        config = JudgeConfig()
        assert config.mode == "disabled"
        assert config.notify_mode == "deny_only"

    def test_env_override(self):
        with patch.dict(
            os.environ, {"JUDGE_MODE": "auto", "JUDGE_NOTIFY_MODE": "always"}
        ):
            config = JudgeConfig()
            assert config.mode == "auto"
            assert config.notify_mode == "always"

    def test_config_validation_invalid_mode(self):
        from intaris.config import Config, LLMConfig

        config = Config(llm=LLMConfig())
        config.judge.mode = "invalid"
        # JUDGE_MODE validation runs before LLM key check
        with pytest.raises(ValueError, match="JUDGE_MODE"):
            config.validate()

    def test_config_validation_invalid_notify_mode(self):
        from intaris.config import Config, LLMConfig

        config = Config(llm=LLMConfig())
        config.judge.notify_mode = "invalid"
        # JUDGE_NOTIFY_MODE validation runs before LLM key check
        with pytest.raises(ValueError, match="JUDGE_NOTIFY_MODE"):
            config.validate()


# ── AuditStore Tests ──────────────────────────────────────────────────


class TestAuditStoreJudge:
    """Test AuditStore judge-related methods."""

    def test_resolve_with_resolved_by(self, audit_store, session_store):
        _create_session(session_store)
        _create_escalated_record(audit_store)

        result = audit_store.resolve_escalation(
            "test-call",
            "approve",
            user_note="Judge approved",
            user_id="test-user",
            resolved_by="judge",
            judge_reasoning="Clearly safe operation within scope",
        )

        assert result["user_decision"] == "approve"
        assert result["resolved_by"] == "judge"
        assert result["judge_reasoning"] == "Clearly safe operation within scope"
        assert result["resolved_at"] is not None

    def test_resolve_with_user(self, audit_store, session_store):
        _create_session(session_store)
        _create_escalated_record(audit_store)

        result = audit_store.resolve_escalation(
            "test-call",
            "deny",
            user_note="Not safe",
            user_id="test-user",
            resolved_by="user",
        )

        assert result["user_decision"] == "deny"
        assert result["resolved_by"] == "user"
        assert result["judge_reasoning"] is None

    def test_resolve_invalid_resolved_by(self, audit_store, session_store):
        _create_session(session_store)
        _create_escalated_record(audit_store)

        with pytest.raises(ValueError, match="Invalid resolved_by"):
            audit_store.resolve_escalation(
                "test-call",
                "approve",
                user_id="test-user",
                resolved_by="invalid",
            )

    def test_set_judge_reasoning(self, audit_store, session_store):
        _create_session(session_store)
        _create_escalated_record(audit_store)

        audit_store.set_judge_reasoning(
            "test-call",
            "Judge recommends deny — ambiguous scope",
            user_id="test-user",
        )

        record = audit_store.get_by_call_id("test-call", user_id="test-user")
        assert record["judge_reasoning"] == "Judge recommends deny — ambiguous scope"
        assert record["user_decision"] is None  # Not resolved

    def test_set_judge_reasoning_on_resolved_is_noop(self, audit_store, session_store):
        _create_session(session_store)
        _create_escalated_record(audit_store)

        # Resolve first
        audit_store.resolve_escalation("test-call", "approve", user_id="test-user")

        # Try to set judge reasoning on resolved record — should be no-op
        audit_store.set_judge_reasoning(
            "test-call",
            "This should not be stored",
            user_id="test-user",
        )

        record = audit_store.get_by_call_id("test-call", user_id="test-user")
        assert record["judge_reasoning"] is None  # Not set (already resolved)

    def test_resolve_already_resolved_raises(self, audit_store, session_store):
        _create_session(session_store)
        _create_escalated_record(audit_store)

        audit_store.resolve_escalation("test-call", "approve", user_id="test-user")

        with pytest.raises(ValueError, match="already resolved"):
            audit_store.resolve_escalation(
                "test-call",
                "deny",
                user_id="test-user",
                resolved_by="judge",
            )


# ── JudgeReviewer Tests ───────────────────────────────────────────────


class TestJudgeReviewer:
    """Test JudgeReviewer review and resolution logic."""

    def _make_reviewer(
        self,
        *,
        mock_llm,
        audit_store,
        session_store,
        mock_evaluator,
        mock_event_bus=None,
        mock_metrics=None,
        mode="auto",
        notify_mode="deny_only",
    ):
        from intaris.judge import JudgeReviewer

        return JudgeReviewer(
            llm=mock_llm,
            config=JudgeConfig(mode=mode, notify_mode=notify_mode),
            audit_store=audit_store,
            session_store=session_store,
            evaluator=mock_evaluator,
            alignment_barrier=None,
            event_bus=mock_event_bus,
            notification_dispatcher=None,
            metrics=mock_metrics,
        )

    def test_auto_approve(
        self,
        mock_llm,
        audit_store,
        session_store,
        mock_evaluator,
        mock_event_bus,
        mock_metrics,
    ):
        """Judge approves a clearly safe escalation in auto mode."""

        async def _test():
            _create_session(session_store)
            _create_escalated_record(audit_store)

            mock_llm.generate.return_value = json.dumps(
                {
                    "decision": "approve",
                    "reasoning": "Clearly safe operation within project scope",
                    "confidence": "high",
                }
            )

            reviewer = self._make_reviewer(
                mock_llm=mock_llm,
                audit_store=audit_store,
                session_store=session_store,
                mock_evaluator=mock_evaluator,
                mock_event_bus=mock_event_bus,
                mock_metrics=mock_metrics,
            )

            await reviewer.review_and_resolve(
                call_id="test-call",
                user_id="test-user",
                session_id="test-session",
                agent_id="test-agent",
            )

            record = audit_store.get_by_call_id("test-call", user_id="test-user")
            assert record["user_decision"] == "approve"
            assert record["resolved_by"] == "judge"
            assert "Clearly safe" in record["judge_reasoning"]
            assert mock_metrics.judge_reviews_total == 1
            assert mock_metrics.judge_approvals_total == 1
            assert mock_event_bus.publish.called

        asyncio.run(_test())

    def test_auto_deny(
        self, mock_llm, audit_store, session_store, mock_evaluator, mock_metrics
    ):
        """Judge denies an unsafe escalation in auto mode."""

        async def _test():
            _create_session(session_store)
            _create_escalated_record(audit_store)

            mock_llm.generate.return_value = json.dumps(
                {
                    "decision": "deny",
                    "reasoning": "Operation is outside project scope and risky",
                    "confidence": "high",
                }
            )

            reviewer = self._make_reviewer(
                mock_llm=mock_llm,
                audit_store=audit_store,
                session_store=session_store,
                mock_evaluator=mock_evaluator,
                mock_metrics=mock_metrics,
            )

            await reviewer.review_and_resolve(
                call_id="test-call",
                user_id="test-user",
                session_id="test-session",
            )

            record = audit_store.get_by_call_id("test-call", user_id="test-user")
            assert record["user_decision"] == "deny"
            assert record["resolved_by"] == "judge"
            assert mock_metrics.judge_denials_total == 1

        asyncio.run(_test())

    def test_auto_defer_maps_to_deny(
        self, mock_llm, audit_store, session_store, mock_evaluator, mock_metrics
    ):
        """In auto mode, defer is mapped to deny."""

        async def _test():
            _create_session(session_store)
            _create_escalated_record(audit_store)

            mock_llm.generate.return_value = json.dumps(
                {
                    "decision": "defer",
                    "reasoning": "Borderline case, needs human judgment",
                    "confidence": "medium",
                }
            )

            reviewer = self._make_reviewer(
                mock_llm=mock_llm,
                audit_store=audit_store,
                session_store=session_store,
                mock_evaluator=mock_evaluator,
                mock_metrics=mock_metrics,
            )

            await reviewer.review_and_resolve(
                call_id="test-call",
                user_id="test-user",
                session_id="test-session",
            )

            record = audit_store.get_by_call_id("test-call", user_id="test-user")
            assert record["user_decision"] == "deny"
            assert record["resolved_by"] == "judge"
            assert "auto-denied" in record["judge_reasoning"]

        asyncio.run(_test())

    def test_auto_low_confidence_maps_to_deny(
        self, mock_llm, audit_store, session_store, mock_evaluator, mock_metrics
    ):
        """In auto mode, low confidence maps to deny."""

        async def _test():
            _create_session(session_store)
            _create_escalated_record(audit_store)

            mock_llm.generate.return_value = json.dumps(
                {
                    "decision": "approve",
                    "reasoning": "Seems okay but not sure",
                    "confidence": "low",
                }
            )

            reviewer = self._make_reviewer(
                mock_llm=mock_llm,
                audit_store=audit_store,
                session_store=session_store,
                mock_evaluator=mock_evaluator,
                mock_metrics=mock_metrics,
            )

            await reviewer.review_and_resolve(
                call_id="test-call",
                user_id="test-user",
                session_id="test-session",
            )

            record = audit_store.get_by_call_id("test-call", user_id="test-user")
            assert record["user_decision"] == "deny"
            assert "auto-denied" in record["judge_reasoning"]

        asyncio.run(_test())

    def test_advisory_defer(
        self, mock_llm, audit_store, session_store, mock_evaluator, mock_metrics
    ):
        """In advisory mode, defer stores reasoning but leaves unresolved."""

        async def _test():
            _create_session(session_store)
            _create_escalated_record(audit_store)

            mock_llm.generate.return_value = json.dumps(
                {
                    "decision": "defer",
                    "reasoning": "Needs human judgment on scope",
                    "confidence": "medium",
                }
            )

            reviewer = self._make_reviewer(
                mock_llm=mock_llm,
                audit_store=audit_store,
                session_store=session_store,
                mock_evaluator=mock_evaluator,
                mock_metrics=mock_metrics,
                mode="advisory",
            )

            await reviewer.review_and_resolve(
                call_id="test-call",
                user_id="test-user",
                session_id="test-session",
            )

            record = audit_store.get_by_call_id("test-call", user_id="test-user")
            assert record["user_decision"] is None  # Not resolved
            assert record["judge_reasoning"] == "Needs human judgment on scope"
            assert mock_metrics.judge_deferrals_total == 1

        asyncio.run(_test())

    def test_advisory_approve(
        self, mock_llm, audit_store, session_store, mock_evaluator, mock_metrics
    ):
        """In advisory mode, judge can approve directly."""

        async def _test():
            _create_session(session_store)
            _create_escalated_record(audit_store)

            mock_llm.generate.return_value = json.dumps(
                {
                    "decision": "approve",
                    "reasoning": "Safe operation",
                    "confidence": "high",
                }
            )

            reviewer = self._make_reviewer(
                mock_llm=mock_llm,
                audit_store=audit_store,
                session_store=session_store,
                mock_evaluator=mock_evaluator,
                mock_metrics=mock_metrics,
                mode="advisory",
            )

            await reviewer.review_and_resolve(
                call_id="test-call",
                user_id="test-user",
                session_id="test-session",
            )

            record = audit_store.get_by_call_id("test-call", user_id="test-user")
            assert record["user_decision"] == "approve"
            assert record["resolved_by"] == "judge"

        asyncio.run(_test())

    def test_llm_failure_leaves_unresolved(
        self, mock_llm, audit_store, session_store, mock_evaluator, mock_metrics
    ):
        """Judge LLM failure leaves escalation unresolved for human."""

        async def _test():
            _create_session(session_store)
            _create_escalated_record(audit_store)

            mock_llm.generate.side_effect = Exception("LLM timeout")

            reviewer = self._make_reviewer(
                mock_llm=mock_llm,
                audit_store=audit_store,
                session_store=session_store,
                mock_evaluator=mock_evaluator,
                mock_metrics=mock_metrics,
            )

            await reviewer.review_and_resolve(
                call_id="test-call",
                user_id="test-user",
                session_id="test-session",
            )

            record = audit_store.get_by_call_id("test-call", user_id="test-user")
            assert record["user_decision"] is None  # Not resolved
            assert mock_metrics.judge_errors_total == 1

        asyncio.run(_test())

    def test_human_resolves_before_judge(
        self, mock_llm, audit_store, session_store, mock_evaluator, mock_metrics
    ):
        """Human resolves before judge — judge gracefully skips."""

        async def _test():
            _create_session(session_store)
            _create_escalated_record(audit_store)

            # Resolve as human first
            audit_store.resolve_escalation("test-call", "approve", user_id="test-user")

            mock_llm.generate.return_value = json.dumps(
                {
                    "decision": "deny",
                    "reasoning": "Would deny but human already approved",
                    "confidence": "high",
                }
            )

            reviewer = self._make_reviewer(
                mock_llm=mock_llm,
                audit_store=audit_store,
                session_store=session_store,
                mock_evaluator=mock_evaluator,
                mock_metrics=mock_metrics,
            )

            # Should not raise — gracefully handles already-resolved
            await reviewer.review_and_resolve(
                call_id="test-call",
                user_id="test-user",
                session_id="test-session",
            )

            # Record should still show human's decision
            record = audit_store.get_by_call_id("test-call", user_id="test-user")
            assert record["user_decision"] == "approve"
            assert record["resolved_by"] == "user"
            # Judge should detect already-resolved before calling LLM
            mock_llm.generate.assert_not_called()

        asyncio.run(_test())

    def test_is_enabled(self, mock_llm, audit_store, session_store, mock_evaluator):
        """Test is_enabled property."""
        reviewer_disabled = self._make_reviewer(
            mock_llm=mock_llm,
            audit_store=audit_store,
            session_store=session_store,
            mock_evaluator=mock_evaluator,
            mode="disabled",
        )
        assert not reviewer_disabled.is_enabled

        reviewer_auto = self._make_reviewer(
            mock_llm=mock_llm,
            audit_store=audit_store,
            session_store=session_store,
            mock_evaluator=mock_evaluator,
            mode="auto",
        )
        assert reviewer_auto.is_enabled


# ── Shared Resolution Handler Tests ───────────────────────────────────


class TestResolveWithSideEffects:
    """Test the shared resolve_with_side_effects function."""

    def test_basic_resolution(self, audit_store, session_store):
        """Basic resolution with all side effects."""

        async def _test():
            from intaris.judge import resolve_with_side_effects

            _create_session(session_store)
            _create_escalated_record(audit_store)

            mock_bus = MagicMock()
            mock_eval = MagicMock()

            record = await resolve_with_side_effects(
                call_id="test-call",
                user_id="test-user",
                user_decision="approve",
                user_note="Looks good",
                resolved_by="user",
                audit_store=audit_store,
                evaluator=mock_eval,
                event_bus=mock_bus,
            )

            assert record["user_decision"] == "approve"
            assert record["resolved_by"] == "user"
            mock_bus.publish.assert_called_once()
            event = mock_bus.publish.call_args[0][0]
            assert event["type"] == "decided"
            assert event["resolved_by"] == "user"
            mock_eval.learn_from_approved_escalation.assert_called_once()

        asyncio.run(_test())

    def test_deny_does_not_learn_paths(self, audit_store, session_store):
        """Deny resolution should not trigger path learning."""

        async def _test():
            from intaris.judge import resolve_with_side_effects

            _create_session(session_store)
            _create_escalated_record(audit_store)

            mock_eval = MagicMock()

            await resolve_with_side_effects(
                call_id="test-call",
                user_id="test-user",
                user_decision="deny",
                resolved_by="judge",
                judge_reasoning="Unsafe",
                audit_store=audit_store,
                evaluator=mock_eval,
            )

            mock_eval.learn_from_approved_escalation.assert_not_called()

        asyncio.run(_test())


# ── Prompt Building Tests ─────────────────────────────────────────────


class TestJudgePrompt:
    """Test judge prompt building."""

    def test_prompt_includes_boundary_tags(self):
        from intaris.judge import _build_judge_prompt

        prompt = _build_judge_prompt(
            intention="Test intention",
            policy=None,
            recent_history=[],
            session_stats={
                "total_calls": 5,
                "approved_count": 3,
                "denied_count": 1,
                "escalated_count": 1,
            },
            tool="bash",
            args_redacted={"command": "ls /tmp"},
            evaluator_reasoning="High risk operation",
            evaluator_risk="high",
            evaluation_path="llm",
            agent_id="test-agent",
        )

        # Verify boundary tags are present
        assert "⟨intention⟩" in prompt
        assert "⟨/intention⟩" in prompt
        assert "⟨tool_name⟩" in prompt
        assert "⟨tool_args⟩" in prompt
        assert "⟨context⟩" in prompt  # evaluator reasoning wrapped
        assert "⟨agent_id⟩" in prompt

    def test_prompt_includes_behavioral_context(self):
        from intaris.judge import _build_judge_prompt

        prompt = _build_judge_prompt(
            intention="Test",
            policy=None,
            recent_history=[],
            session_stats={
                "total_calls": 0,
                "approved_count": 0,
                "denied_count": 0,
                "escalated_count": 0,
            },
            tool="bash",
            args_redacted={},
            evaluator_reasoning=None,
            evaluator_risk=None,
            evaluation_path=None,
            agent_id=None,
            behavioral_context={"risk_level": 8, "context_summary": "High risk agent"},
        )

        assert "Behavioral Profile" in prompt
        assert "8/10" in prompt
        assert "High risk agent" in prompt

    def test_prompt_includes_parent_intention(self):
        from intaris.judge import _build_judge_prompt

        prompt = _build_judge_prompt(
            intention="Child intention",
            policy=None,
            recent_history=[],
            session_stats={
                "total_calls": 0,
                "approved_count": 0,
                "denied_count": 0,
                "escalated_count": 0,
            },
            tool="bash",
            args_redacted={},
            evaluator_reasoning=None,
            evaluator_risk=None,
            evaluation_path=None,
            agent_id=None,
            parent_intention="Parent intention",
        )

        assert "Parent Session Intention" in prompt
        assert "⟨parent_intention⟩" in prompt
        assert "sub-session" in prompt.lower()

    def test_system_prompt_has_anti_injection(self):
        from intaris.judge import JUDGE_SYSTEM_PROMPT
        from intaris.sanitize import ANTI_INJECTION_PREAMBLE

        formatted = JUDGE_SYSTEM_PROMPT.format(anti_injection=ANTI_INJECTION_PREAMBLE)
        assert "boundary tags" in formatted.lower() or "BOUNDARY" in formatted


# ── DB Migration Tests ────────────────────────────────────────────────


class TestDBMigration:
    """Test that new columns are created by migration."""

    def test_resolved_by_column_exists(self, tmp_db):
        with tmp_db.cursor() as cur:
            cur.execute("PRAGMA table_info(audit_log)")
            columns = {row[1] for row in cur.fetchall()}
        assert "resolved_by" in columns

    def test_judge_reasoning_column_exists(self, tmp_db):
        with tmp_db.cursor() as cur:
            cur.execute("PRAGMA table_info(audit_log)")
            columns = {row[1] for row in cur.fetchall()}
        assert "judge_reasoning" in columns
