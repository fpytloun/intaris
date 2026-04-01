"""Tests for judge auto-resolution module."""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

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

        with pytest.raises(ValueError, match="already resolved by user"):
            audit_store.resolve_escalation(
                "test-call",
                "deny",
                user_id="test-user",
                resolved_by="judge",
            )

    def test_override_judge_deny_to_approve(self, audit_store, session_store):
        """Human can override a judge-denied decision to approve."""
        _create_session(session_store)
        _create_escalated_record(audit_store, call_id="call-override-1")

        # Judge denies
        audit_store.resolve_escalation(
            "call-override-1",
            "deny",
            user_note="Judge (high confidence)",
            user_id="test-user",
            resolved_by="judge",
            judge_reasoning="Looks risky — outside project scope",
        )

        # Human overrides to approve
        updated = audit_store.resolve_escalation(
            "call-override-1",
            "approve",
            user_note="Allow writing secret for this session",
            user_id="test-user",
            resolved_by="user",
        )

        assert updated["user_decision"] == "approve"
        assert updated["resolved_by"] == "user"
        assert updated["user_note"] == "Allow writing secret for this session"
        # Judge reasoning preserved via COALESCE
        assert updated["judge_reasoning"] == "Looks risky — outside project scope"

    def test_override_judge_approve_to_deny(self, audit_store, session_store):
        """Human can override a judge approval to deny."""
        _create_session(session_store)
        _create_escalated_record(audit_store, call_id="call-override-2")

        # Judge approves
        audit_store.resolve_escalation(
            "call-override-2",
            "approve",
            user_note="Judge (high confidence)",
            user_id="test-user",
            resolved_by="judge",
            judge_reasoning="Looks safe and aligned",
        )

        # Human overrides to deny
        updated = audit_store.resolve_escalation(
            "call-override-2",
            "deny",
            user_note="Actually not safe",
            user_id="test-user",
            resolved_by="user",
        )

        assert updated["user_decision"] == "deny"
        assert updated["resolved_by"] == "user"
        assert updated["judge_reasoning"] == "Looks safe and aligned"

    def test_cannot_override_user_decision(self, audit_store, session_store):
        """Human decisions are final — cannot be overridden."""
        _create_session(session_store)
        _create_escalated_record(audit_store, call_id="call-final")

        # Human resolves
        audit_store.resolve_escalation(
            "call-final",
            "deny",
            user_note="No way",
            user_id="test-user",
            resolved_by="user",
        )

        # Another override attempt fails
        with pytest.raises(ValueError, match="already resolved by user"):
            audit_store.resolve_escalation(
                "call-final",
                "approve",
                user_note="Override attempt",
                user_id="test-user",
                resolved_by="user",
            )

    def test_escalation_retry_after_override(self, audit_store, session_store):
        """After human overrides judge denial, escalation retry finds the approval."""
        import hashlib

        _create_session(session_store)

        # Create escalated record with args_hash
        args = {"command": "kubectl apply -f secret.yaml"}
        args_hash = hashlib.sha256(
            json.dumps(args, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        audit_store.insert(
            call_id="call-retry",
            user_id="test-user",
            session_id="test-session",
            agent_id="test-agent",
            tool="bash",
            args_redacted=args,
            classification="write",
            evaluation_path="llm",
            decision="escalate",
            risk="high",
            reasoning="High risk operation",
            latency_ms=100,
            args_hash=args_hash,
        )

        # Judge denies
        audit_store.resolve_escalation(
            "call-retry",
            "deny",
            user_note="Judge (high confidence)",
            user_id="test-user",
            resolved_by="judge",
            judge_reasoning="Risky operation",
        )

        # No approval found yet (judge denied)
        result = audit_store.find_approved_escalation(
            user_id="test-user",
            tool="bash",
            args_hash=args_hash,
            cutoff="2000-01-01T00:00:00",
        )
        assert result is None

        # Human overrides to approve
        audit_store.resolve_escalation(
            "call-retry",
            "approve",
            user_note="Allow kubectl apply for this session",
            user_id="test-user",
            resolved_by="user",
        )

        # Now escalation retry should find the approval
        result = audit_store.find_approved_escalation(
            user_id="test-user",
            tool="bash",
            args_hash=args_hash,
            cutoff="2000-01-01T00:00:00",
        )
        assert result is not None
        assert result["call_id"] == "call-retry"


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

    def test_advisory_high_confidence_deny_stays_deny(
        self, mock_llm, audit_store, session_store, mock_evaluator, mock_metrics
    ):
        """In advisory mode, high-confidence deny stays as deny (obvious threat)."""

        async def _test():
            _create_session(session_store)
            _create_escalated_record(audit_store)

            mock_llm.generate.return_value = json.dumps(
                {
                    "decision": "deny",
                    "reasoning": "Destructive rm -rf / command, clearly malicious",
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
            assert record["user_decision"] == "deny"
            assert record["resolved_by"] == "judge"
            assert mock_metrics.judge_denials_total == 1

        asyncio.run(_test())

    def test_advisory_medium_confidence_deny_becomes_defer(
        self, mock_llm, audit_store, session_store, mock_evaluator, mock_metrics
    ):
        """In advisory mode, medium-confidence deny is converted to defer."""

        async def _test():
            _create_session(session_store)
            _create_escalated_record(audit_store)

            mock_llm.generate.return_value = json.dumps(
                {
                    "decision": "deny",
                    "reasoning": "Might be outside scope but not certain",
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
            assert record["user_decision"] is None  # Not resolved — deferred
            assert "original_decision=deny" in record["judge_reasoning"]
            assert mock_metrics.judge_deferrals_total == 1

        asyncio.run(_test())

    def test_advisory_low_confidence_deny_becomes_defer(
        self, mock_llm, audit_store, session_store, mock_evaluator, mock_metrics
    ):
        """In advisory mode, low-confidence deny is converted to defer."""

        async def _test():
            _create_session(session_store)
            _create_escalated_record(audit_store)

            mock_llm.generate.return_value = json.dumps(
                {
                    "decision": "deny",
                    "reasoning": "Uncertain about this operation",
                    "confidence": "low",
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
            assert record["user_decision"] is None  # Not resolved — deferred
            assert "original_decision=deny" in record["judge_reasoning"]
            assert mock_metrics.judge_deferrals_total == 1

        asyncio.run(_test())

    def test_advisory_mode_prompt_contains_advisory_rules(
        self, mock_llm, audit_store, session_store, mock_evaluator, mock_metrics
    ):
        """Advisory mode injects advisory-specific decision rules into prompt."""

        async def _test():
            _create_session(session_store)
            _create_escalated_record(audit_store)

            mock_llm.generate.return_value = json.dumps(
                {
                    "decision": "approve",
                    "reasoning": "Safe",
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

            # Verify the system prompt contains advisory-specific language
            call_args = mock_llm.generate.call_args
            messages = call_args[0][0]
            system_content = messages[0]["content"]
            assert "Advisory Mode" in system_content
            assert "Defer is your default" in system_content
            assert "unambiguously dangerous" in system_content

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

    def test_override_triggers_side_effects(self, audit_store, session_store):
        """Overriding a judge denial triggers all side effects."""

        async def _test():
            from intaris.background import Metrics
            from intaris.judge import resolve_with_side_effects

            _create_session(session_store)
            _create_escalated_record(audit_store, call_id="call-override-se")

            # Judge denies first
            audit_store.resolve_escalation(
                "call-override-se",
                "deny",
                user_note="Judge (high confidence)",
                user_id="test-user",
                resolved_by="judge",
                judge_reasoning="Risky",
            )

            mock_bus = MagicMock()
            mock_eval = MagicMock()
            metrics = Metrics()

            # Human overrides to approve
            record = await resolve_with_side_effects(
                call_id="call-override-se",
                user_id="test-user",
                user_decision="approve",
                user_note="Allow for this session",
                resolved_by="user",
                audit_store=audit_store,
                evaluator=mock_eval,
                event_bus=mock_bus,
                metrics=metrics,
            )

            assert record["user_decision"] == "approve"
            assert record["resolved_by"] == "user"
            assert record["judge_reasoning"] == "Risky"

            # EventBus published with resolved_by="user"
            mock_bus.publish.assert_called_once()
            event = mock_bus.publish.call_args[0][0]
            assert event["type"] == "decided"
            assert event["resolved_by"] == "user"
            assert event["user_decision"] == "approve"

            # Path learning triggered (approve)
            mock_eval.learn_from_approved_escalation.assert_called_once()

            # Override metric incremented
            assert metrics.judge_overrides_total == 1

        asyncio.run(_test())

    def test_override_deny_does_not_learn_paths(self, audit_store, session_store):
        """Overriding a judge approval to deny does not trigger path learning."""

        async def _test():
            from intaris.background import Metrics
            from intaris.judge import resolve_with_side_effects

            _create_session(session_store)
            _create_escalated_record(audit_store, call_id="call-override-deny")

            # Judge approves first
            audit_store.resolve_escalation(
                "call-override-deny",
                "approve",
                user_note="Judge (high confidence)",
                user_id="test-user",
                resolved_by="judge",
                judge_reasoning="Safe",
            )

            mock_eval = MagicMock()
            metrics = Metrics()

            # Human overrides to deny
            await resolve_with_side_effects(
                call_id="call-override-deny",
                user_id="test-user",
                user_decision="deny",
                user_note="Actually not safe",
                resolved_by="user",
                audit_store=audit_store,
                evaluator=mock_eval,
                metrics=metrics,
            )

            # Path learning NOT triggered (deny)
            mock_eval.learn_from_approved_escalation.assert_not_called()

            # Override metric still incremented
            assert metrics.judge_overrides_total == 1

        asyncio.run(_test())

    def test_judge_resolution_uses_judge_denial_event_type(
        self, audit_store, session_store
    ):
        """Judge deny resolution sends judge_denial event type."""

        async def _test():
            from intaris.judge import resolve_with_side_effects

            _create_session(session_store)
            _create_escalated_record(audit_store)

            mock_dispatcher = MagicMock()
            mock_dispatcher.notify = AsyncMock()

            await resolve_with_side_effects(
                call_id="test-call",
                user_id="test-user",
                user_decision="deny",
                user_note="Judge (high confidence)",
                resolved_by="judge",
                judge_reasoning="Dangerous operation",
                audit_store=audit_store,
                notification_dispatcher=mock_dispatcher,
            )

            mock_dispatcher.notify.assert_called_once()
            notification = mock_dispatcher.notify.call_args[1]["notification"]
            assert notification.event_type == "judge_denial"

        asyncio.run(_test())

    def test_judge_resolution_uses_judge_approval_event_type(
        self, audit_store, session_store
    ):
        """Judge approve resolution sends judge_approval event type."""

        async def _test():
            from intaris.judge import resolve_with_side_effects

            _create_session(session_store)
            _create_escalated_record(audit_store)

            mock_dispatcher = MagicMock()
            mock_dispatcher.notify = AsyncMock()

            await resolve_with_side_effects(
                call_id="test-call",
                user_id="test-user",
                user_decision="approve",
                user_note="Judge (high confidence)",
                resolved_by="judge",
                judge_reasoning="Safe operation",
                audit_store=audit_store,
                evaluator=MagicMock(),
                notification_dispatcher=mock_dispatcher,
            )

            mock_dispatcher.notify.assert_called_once()
            notification = mock_dispatcher.notify.call_args[1]["notification"]
            assert notification.event_type == "judge_approval"

        asyncio.run(_test())

    def test_human_resolution_uses_resolution_event_type(
        self, audit_store, session_store
    ):
        """Human resolution sends standard resolution event type."""

        async def _test():
            from intaris.judge import resolve_with_side_effects

            _create_session(session_store)
            _create_escalated_record(audit_store)

            mock_dispatcher = MagicMock()
            mock_dispatcher.notify = AsyncMock()

            await resolve_with_side_effects(
                call_id="test-call",
                user_id="test-user",
                user_decision="approve",
                user_note="Looks good",
                resolved_by="user",
                audit_store=audit_store,
                evaluator=MagicMock(),
                notification_dispatcher=mock_dispatcher,
            )

            mock_dispatcher.notify.assert_called_once()
            notification = mock_dispatcher.notify.call_args[1]["notification"]
            assert notification.event_type == "resolution"

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
        from intaris.judge import _DECISION_RULES_AUTO, JUDGE_SYSTEM_PROMPT
        from intaris.sanitize import ANTI_INJECTION_PREAMBLE

        formatted = JUDGE_SYSTEM_PROMPT.format(
            decision_rules=_DECISION_RULES_AUTO,
            anti_injection=ANTI_INJECTION_PREAMBLE,
        )
        assert "boundary tags" in formatted.lower() or "BOUNDARY" in formatted

    def test_reasoning_not_truncated_at_200(self):
        """Reasoning records longer than 200 chars appear in full (up to safety valve)."""
        from intaris.judge import _build_judge_prompt

        long_message = (
            "User message: Ok perfect. Pushed both packages. "
            "Now update docs for both projects to describe support "
            "for openclaw in client docs and how to set it up and use it. "
            "When done, commit everything, create tag and push."
        )
        assert len(long_message) > 200  # Would have been truncated before

        prompt = _build_judge_prompt(
            intention="Test",
            policy=None,
            recent_history=[
                {
                    "record_type": "reasoning",
                    "content": long_message,
                    "decision": "approve",
                }
            ],
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
        )

        # Full message should appear — not truncated at 200 chars
        assert "create tag and push" in prompt
        assert long_message in prompt

    def test_reasoning_safety_valve(self):
        """Reasoning records exceeding safety valve limit are truncated."""
        from intaris.judge import _REASONING_CONTENT_LIMIT, _build_judge_prompt

        huge_message = "x" * (_REASONING_CONTENT_LIMIT + 1000)

        prompt = _build_judge_prompt(
            intention="Test",
            policy=None,
            recent_history=[
                {
                    "record_type": "reasoning",
                    "content": huge_message,
                    "decision": "approve",
                }
            ],
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
        )

        # Should be truncated with ellipsis, not the full string
        assert huge_message not in prompt
        assert "..." in prompt

    def test_reasoning_with_string_context(self):
        """Reasoning records with string context render the context."""
        from intaris.judge import _build_judge_prompt

        prompt = _build_judge_prompt(
            intention="Test",
            policy=None,
            recent_history=[
                {
                    "record_type": "reasoning",
                    "content": "User message: ok, do it",
                    "decision": "approve",
                    "args_redacted": {
                        "context": "I propose to refactor the auth module."
                    },
                }
            ],
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
        )

        assert "ok, do it" in prompt
        assert "[context]" in prompt
        assert "refactor the auth module" in prompt

    def test_reasoning_with_dict_context(self):
        """Non-string context (dict) is rendered as JSON."""
        from intaris.judge import _build_judge_prompt

        prompt = _build_judge_prompt(
            intention="Test",
            policy=None,
            recent_history=[
                {
                    "record_type": "reasoning",
                    "content": "User message: proceed",
                    "decision": "approve",
                    "args_redacted": {"context": {"model": "gpt-4", "turn": 5}},
                }
            ],
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
        )

        assert "[context]" in prompt
        assert "gpt-4" in prompt

    def test_checkpoint_and_summary_skipped(self):
        """Checkpoint and summary records are omitted from judge history."""
        from intaris.judge import _build_judge_prompt

        prompt = _build_judge_prompt(
            intention="Test",
            policy=None,
            recent_history=[
                {
                    "record_type": "checkpoint",
                    "content": "Agent checkpoint data",
                    "decision": "approve",
                    "tool": None,
                    "args_redacted": None,
                },
                {
                    "record_type": "summary",
                    "content": "Session summary text",
                    "decision": "approve",
                    "tool": None,
                    "args_redacted": None,
                },
                {
                    "record_type": "tool_call",
                    "tool": "bash",
                    "args_redacted": {"command": "ls"},
                    "decision": "approve",
                    "reasoning": "Safe",
                },
            ],
            session_stats={
                "total_calls": 1,
                "approved_count": 1,
                "denied_count": 0,
                "escalated_count": 0,
            },
            tool="bash",
            args_redacted={},
            evaluator_reasoning=None,
            evaluator_risk=None,
            evaluation_path=None,
            agent_id=None,
        )

        # The checkpoint/summary content should NOT appear
        assert "Agent checkpoint data" not in prompt
        assert "Session summary text" not in prompt
        # But the tool_call should appear
        assert "[approve]" in prompt

    def test_parent_recent_messages_in_subsession(self):
        """Parent session reasoning records appear in sub-session judge prompt."""
        from intaris.judge import _build_judge_prompt

        prompt = _build_judge_prompt(
            intention="Child: examine auth code",
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
            parent_intention="Implement auth system",
            parent_recent_messages=[
                {
                    "record_type": "reasoning",
                    "content": "User message: implement the auth system with OAuth2",
                    "decision": "approve",
                    "args_redacted": {
                        "context": "I can help with that. Let me spawn sub-sessions."
                    },
                },
                {
                    "record_type": "reasoning",
                    "content": "User message: yes, go ahead",
                    "decision": "approve",
                    "args_redacted": None,
                },
            ],
        )

        assert "Parent Session Context" in prompt
        assert "⟨parent_context⟩" in prompt
        assert "implement the auth system with OAuth2" in prompt
        assert "yes, go ahead" in prompt
        assert "[context]" in prompt
        assert "spawn sub-sessions" in prompt

    def test_system_prompt_has_subsession_trust_model(self):
        """System prompt includes sub-session trust model guidance."""
        from intaris.judge import _DECISION_RULES_AUTO, JUDGE_SYSTEM_PROMPT
        from intaris.sanitize import ANTI_INJECTION_PREAMBLE

        formatted = JUDGE_SYSTEM_PROMPT.format(
            decision_rules=_DECISION_RULES_AUTO,
            anti_injection=ANTI_INJECTION_PREAMBLE,
        )
        assert "Sub-Session Trust Model" in formatted
        assert "parent agent" in formatted
        assert "human user" in formatted


# ── Audit get_recent Tests ────────────────────────────────────────────


class TestAuditGetRecentBefore:
    """Test audit.get_recent() with the before parameter."""

    def test_get_recent_with_before(self, audit_store, session_store):
        """Records after the before timestamp are excluded."""
        _create_session(session_store)

        # Insert 3 reasoning records
        for i in range(3):
            audit_store.insert(
                call_id=f"call-{i}",
                user_id="test-user",
                session_id="test-session",
                agent_id="test-agent",
                tool=None,
                args_redacted=None,
                classification=None,
                evaluation_path="reasoning",
                decision="approve",
                risk=None,
                reasoning=None,
                latency_ms=0,
                record_type="reasoning",
                content=f"Message {i}",
            )

        # Without before — should get all 3
        all_records = audit_store.get_recent(
            "test-session",
            user_id="test-user",
            record_type="reasoning",
        )
        assert len(all_records) == 3

        # Use a stored timestamp as the cutoff to exercise the actual filter
        mid_ts = all_records[1]["timestamp"]
        bounded = audit_store.get_recent(
            "test-session",
            user_id="test-user",
            record_type="reasoning",
            before=mid_ts,
        )
        # Inclusive: records at or before mid_ts should be returned
        assert 1 <= len(bounded) <= 3

        # Before a very early timestamp — should get none
        records = audit_store.get_recent(
            "test-session",
            user_id="test-user",
            record_type="reasoning",
            before="2000-01-01T00:00:00+00:00",
        )
        assert len(records) == 0

    def test_get_recent_before_with_record_type(self, audit_store, session_store):
        """Before parameter works together with record_type filter."""
        _create_session(session_store)

        # Insert a tool_call and a reasoning record
        audit_store.insert(
            call_id="tool-1",
            user_id="test-user",
            session_id="test-session",
            agent_id="test-agent",
            tool="bash",
            args_redacted={"command": "ls"},
            classification="read",
            evaluation_path="fast",
            decision="approve",
            risk="low",
            reasoning="Read-only",
            latency_ms=1,
        )
        audit_store.insert(
            call_id="reason-1",
            user_id="test-user",
            session_id="test-session",
            agent_id="test-agent",
            tool=None,
            args_redacted=None,
            classification=None,
            evaluation_path="reasoning",
            decision="approve",
            risk=None,
            reasoning=None,
            latency_ms=0,
            record_type="reasoning",
            content="User message: test",
        )

        # Filter by reasoning + future before — should get only reasoning
        records = audit_store.get_recent(
            "test-session",
            user_id="test-user",
            record_type="reasoning",
            before="2099-01-01T00:00:00+00:00",
        )
        assert len(records) == 1
        assert records[0]["record_type"] == "reasoning"


# ── Denial Override Tests ─────────────────────────────────────────────


def _create_denied_record(
    audit_store,
    call_id="deny-call",
    user_id="test-user",
    evaluation_path="critical",
    classification="critical",
    risk="critical",
    args_hash=None,
):
    """Helper to create a denied audit record."""
    return audit_store.insert(
        call_id=call_id,
        user_id=user_id,
        session_id="test-session",
        agent_id="test-agent",
        tool="bash",
        args_redacted={"command": "rm -rf /tmp/dangerous"},
        classification=classification,
        evaluation_path=evaluation_path,
        decision="deny",
        risk=risk,
        reasoning="Critical pattern detected in bash call",
        latency_ms=50,
        args_hash=args_hash,
    )


class TestDenialOverride:
    """Test ex-post approval/denial override for L1 denials."""

    def test_resolve_denial_to_approve(self, audit_store, session_store):
        """User can approve a previously denied tool call."""
        _create_session(session_store)
        _create_denied_record(audit_store, call_id="deny-approve-1")

        result = audit_store.resolve_escalation(
            "deny-approve-1",
            "approve",
            user_note="Allow this command for my workflow",
            user_id="test-user",
            resolved_by="user",
        )

        assert result["decision"] == "deny"  # Original decision preserved
        assert result["user_decision"] == "approve"
        assert result["resolved_by"] == "user"
        assert result["user_note"] == "Allow this command for my workflow"
        assert result["resolved_at"] is not None

    def test_resolve_denial_to_confirm_deny(self, audit_store, session_store):
        """User can confirm a denial (set user_decision to deny)."""
        _create_session(session_store)
        _create_denied_record(audit_store, call_id="deny-confirm-1")

        result = audit_store.resolve_escalation(
            "deny-confirm-1",
            "deny",
            user_note="Confirmed, this is dangerous",
            user_id="test-user",
            resolved_by="user",
        )

        assert result["decision"] == "deny"
        assert result["user_decision"] == "deny"
        assert result["resolved_by"] == "user"

    def test_cannot_override_user_resolved_denial(self, audit_store, session_store):
        """Human decisions on denials are final — cannot be overridden."""
        _create_session(session_store)
        _create_denied_record(audit_store, call_id="deny-final-1")

        # First resolution
        audit_store.resolve_escalation(
            "deny-final-1",
            "approve",
            user_note="Allow",
            user_id="test-user",
            resolved_by="user",
        )

        # Second attempt fails
        with pytest.raises(ValueError, match="already resolved by user"):
            audit_store.resolve_escalation(
                "deny-final-1",
                "deny",
                user_note="Changed my mind",
                user_id="test-user",
                resolved_by="user",
            )

    def test_approve_record_not_deny_or_escalate_fails(
        self, audit_store, session_store
    ):
        """Cannot resolve a record whose decision is 'approve'."""
        _create_session(session_store)
        audit_store.insert(
            call_id="approve-call",
            user_id="test-user",
            session_id="test-session",
            agent_id="test-agent",
            tool="bash",
            args_redacted={"command": "ls"},
            classification="read",
            evaluation_path="fast",
            decision="approve",
            risk="low",
            reasoning="Read-only",
            latency_ms=1,
        )

        with pytest.raises(ValueError, match="cannot be resolved"):
            audit_store.resolve_escalation(
                "approve-call",
                "deny",
                user_id="test-user",
                resolved_by="user",
            )

    def test_denial_override_retry_with_args_hash(self, audit_store, session_store):
        """After approving a denied call, retry finds the approval via args_hash."""
        import hashlib

        _create_session(session_store)

        args = {"command": "rm -rf /tmp/dangerous"}
        args_hash = hashlib.sha256(
            json.dumps(args, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        _create_denied_record(
            audit_store,
            call_id="deny-retry-1",
            args_hash=args_hash,
        )

        # Before override: no approval found
        result = audit_store.find_approved_escalation(
            user_id="test-user",
            tool="bash",
            args_hash=args_hash,
            cutoff="2000-01-01T00:00:00",
        )
        assert result is None

        # User approves the denial
        audit_store.resolve_escalation(
            "deny-retry-1",
            "approve",
            user_note="Allow this specific command",
            user_id="test-user",
            resolved_by="user",
        )

        # After override: approval found via args_hash
        result = audit_store.find_approved_escalation(
            user_id="test-user",
            tool="bash",
            args_hash=args_hash,
            cutoff="2000-01-01T00:00:00",
        )
        assert result is not None
        assert result["call_id"] == "deny-retry-1"

    def test_session_status_deny_resolve_succeeds_but_no_retry(
        self, audit_store, session_store
    ):
        """Session-status denials can be resolved but lack args_hash for retry."""
        _create_session(session_store)

        # Session-status deny — no args_hash
        audit_store.insert(
            call_id="status-deny-1",
            user_id="test-user",
            session_id="test-session",
            agent_id="test-agent",
            tool="bash",
            args_redacted={"command": "ls"},
            classification="write",
            evaluation_path="fast",
            decision="deny",
            risk="low",
            reasoning="Session is completed",
            latency_ms=1,
        )

        # Resolve succeeds (SQL matches decision='deny')
        result = audit_store.resolve_escalation(
            "status-deny-1",
            "approve",
            user_note="Try to unblock",
            user_id="test-user",
            resolved_by="user",
        )
        assert result["user_decision"] == "approve"

        # But retry won't find it — no args_hash
        retry = audit_store.find_approved_escalation(
            user_id="test-user",
            tool="bash",
            args_hash="any-hash",
            cutoff="2000-01-01T00:00:00",
        )
        assert retry is None

    def test_resolve_with_side_effects_denial_override(
        self, audit_store, session_store
    ):
        """Shared handler works for denial overrides with all side effects."""

        async def _test():
            from intaris.background import Metrics
            from intaris.judge import resolve_with_side_effects

            _create_session(session_store)
            _create_denied_record(audit_store, call_id="deny-se-1")

            mock_bus = MagicMock()
            mock_eval = MagicMock()
            metrics = Metrics()

            record = await resolve_with_side_effects(
                call_id="deny-se-1",
                user_id="test-user",
                user_decision="approve",
                user_note="Allow this",
                resolved_by="user",
                audit_store=audit_store,
                evaluator=mock_eval,
                event_bus=mock_bus,
                metrics=metrics,
            )

            assert record["decision"] == "deny"  # Original preserved
            assert record["user_decision"] == "approve"
            assert record["resolved_by"] == "user"

            # EventBus published with decision field
            mock_bus.publish.assert_called_once()
            event = mock_bus.publish.call_args[0][0]
            assert event["type"] == "decided"
            assert event["decision"] == "deny"
            assert event["user_decision"] == "approve"
            assert event["resolved_by"] == "user"

            # Path learning triggered
            mock_eval.learn_from_approved_escalation.assert_called_once()

            # Denial override metric incremented
            assert metrics.denial_overrides_total == 1
            # Judge override NOT incremented (this was a denial, not a judge decision)
            assert metrics.judge_overrides_total == 0

        asyncio.run(_test())

    def test_denial_confirm_does_not_learn_paths(self, audit_store, session_store):
        """Confirming a denial (user_decision=deny) does not trigger path learning."""

        async def _test():
            from intaris.judge import resolve_with_side_effects

            _create_session(session_store)
            _create_denied_record(audit_store, call_id="deny-confirm-se")

            mock_eval = MagicMock()

            await resolve_with_side_effects(
                call_id="deny-confirm-se",
                user_id="test-user",
                user_decision="deny",
                user_note="Confirmed",
                resolved_by="user",
                audit_store=audit_store,
                evaluator=mock_eval,
            )

            mock_eval.learn_from_approved_escalation.assert_not_called()

        asyncio.run(_test())

    def test_llm_deny_override_retry(self, audit_store, session_store):
        """LLM deny → user override → retry finds approval."""
        import hashlib

        _create_session(session_store)

        args = {"command": "kubectl delete namespace production"}
        args_hash = hashlib.sha256(
            json.dumps(args, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        # LLM denies (evaluation_path='llm')
        audit_store.insert(
            call_id="llm-deny-1",
            user_id="test-user",
            session_id="test-session",
            agent_id="test-agent",
            tool="bash",
            args_redacted=args,
            classification="write",
            evaluation_path="llm",
            decision="deny",
            risk="critical",
            reasoning="Critical risk — auto-denied",
            latency_ms=3500,
            args_hash=args_hash,
        )

        # User approves the LLM denial
        audit_store.resolve_escalation(
            "llm-deny-1",
            "approve",
            user_note="I need this for migration",
            user_id="test-user",
            resolved_by="user",
        )

        # Retry finds the approval
        result = audit_store.find_approved_escalation(
            user_id="test-user",
            tool="bash",
            args_hash=args_hash,
            cutoff="2000-01-01T00:00:00",
        )
        assert result is not None
        assert result["call_id"] == "llm-deny-1"


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
