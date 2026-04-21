"""Tests for the IntentionBarrier and generate_intention.

Tests cover:
- IntentionBarrier: trigger, wait, cancel-and-restart, timeout, metrics
- generate_intention: prompt construction, session update, edge cases
- Evaluator bootstrap: one-time intention refinement at call 10
- Session intention_source column
- Evaluator record_type filter
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from intaris.config import DBConfig
from intaris.db import Database
from intaris.evaluator import Evaluator
from intaris.intention import (
    IntentionBarrier,
    _parse_title_intention,
    generate_intention,
)
from intaris.session import SessionStore


@pytest.fixture
def db(tmp_path):
    config = DBConfig()
    config.path = str(tmp_path / "test.db")
    return Database(config)


@pytest.fixture
def session_store(db):
    return SessionStore(db)


@pytest.fixture
def mock_llm():
    """Mock LLM client that returns a canned intention."""
    llm = MagicMock()
    llm.generate.return_value = "Implementing a login feature with OAuth2"
    return llm


def _make_barrier(db, llm, timeout_ms=500, poll_timeout_ms=500):
    """Create an IntentionBarrier with given params."""
    return IntentionBarrier(
        db=db, llm=llm, timeout_ms=timeout_ms, poll_timeout_ms=poll_timeout_ms
    )


def _insert_tool_call(db, user_id, session_id, call_id="call-1"):
    """Helper to insert a tool_call audit record."""
    from intaris.audit import AuditStore

    audit = AuditStore(db)
    audit.insert(
        call_id=call_id,
        user_id=user_id,
        session_id=session_id,
        agent_id=None,
        tool="bash",
        args_redacted={"command": "ls"},
        classification="write",
        evaluation_path="llm",
        decision="approve",
        risk="low",
        reasoning="OK",
        latency_ms=10,
    )


def _insert_reasoning(db, user_id, session_id, content, call_id="call-r1"):
    """Helper to insert a reasoning audit record."""
    from intaris.audit import AuditStore

    audit = AuditStore(db)
    audit.insert(
        call_id=call_id,
        user_id=user_id,
        session_id=session_id,
        agent_id=None,
        tool=None,
        args_redacted=None,
        classification=None,
        evaluation_path="reasoning",
        decision="approve",
        risk=None,
        reasoning=None,
        latency_ms=0,
        record_type="reasoning",
        content=content,
    )


# ── generate_intention ────────────────────────────────────────────────


class TestGenerateIntention:
    """Tests for the generate_intention shared function."""

    def test_returns_intention_string(self, db, session_store, mock_llm):
        """generate_intention returns the LLM-generated intention."""
        session_store.create(
            user_id="user-1", session_id="sess-1", intention="Initial intention"
        )
        _insert_tool_call(db, "user-1", "sess-1")

        result = generate_intention(
            llm=mock_llm,
            db=db,
            session_store=session_store,
            user_id="user-1",
            session_id="sess-1",
        )

        assert result is not None
        assert "OAuth2" in result
        mock_llm.generate.assert_called_once()

    def test_updates_session_intention_and_source(self, db, session_store, mock_llm):
        """generate_intention updates the session's intention and source."""
        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")
        _insert_tool_call(db, "user-1", "sess-1")

        generate_intention(
            llm=mock_llm,
            db=db,
            session_store=session_store,
            user_id="user-1",
            session_id="sess-1",
        )

        session = session_store.get("sess-1", user_id="user-1")
        assert session["intention"] == "Implementing a login feature with OAuth2"
        assert session["intention_source"] == "user"

    def test_returns_none_for_missing_session(self, db, session_store, mock_llm):
        """generate_intention returns None if session doesn't exist."""
        result = generate_intention(
            llm=mock_llm,
            db=db,
            session_store=session_store,
            user_id="user-1",
            session_id="nonexistent",
        )
        assert result is None
        mock_llm.generate.assert_not_called()

    def test_returns_none_for_empty_history(self, db, session_store, mock_llm):
        """generate_intention returns None if no tool calls or messages."""
        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")

        result = generate_intention(
            llm=mock_llm,
            db=db,
            session_store=session_store,
            user_id="user-1",
            session_id="sess-1",
        )
        assert result is None
        mock_llm.generate.assert_not_called()

    def test_returns_none_for_short_llm_response(self, db, session_store):
        """generate_intention returns None if LLM returns too-short text."""
        llm = MagicMock()
        llm.generate.return_value = "Hi"  # Too short (< 5 chars)

        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")
        _insert_tool_call(db, "user-1", "sess-1")

        result = generate_intention(
            llm=llm,
            db=db,
            session_store=session_store,
            user_id="user-1",
            session_id="sess-1",
        )
        assert result is None

    def test_returns_none_on_llm_failure(self, db, session_store):
        """generate_intention returns None if LLM raises an exception."""
        llm = MagicMock()
        llm.generate.side_effect = RuntimeError("LLM timeout")

        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")
        _insert_tool_call(db, "user-1", "sess-1")

        result = generate_intention(
            llm=llm,
            db=db,
            session_store=session_store,
            user_id="user-1",
            session_id="sess-1",
        )
        assert result is None

    def test_preserves_long_intention(self, db, session_store):
        """generate_intention preserves full LLM output without truncation."""
        llm = MagicMock()
        llm.generate.return_value = "A" * 600

        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")
        _insert_tool_call(db, "user-1", "sess-1")

        result = generate_intention(
            llm=llm,
            db=db,
            session_store=session_store,
            user_id="user-1",
            session_id="sess-1",
        )
        assert result is not None
        assert len(result) == 600

    def test_publishes_event_bus(self, db, session_store, mock_llm):
        """generate_intention publishes session_updated event."""
        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")
        _insert_tool_call(db, "user-1", "sess-1")

        event_bus = MagicMock()
        generate_intention(
            llm=mock_llm,
            db=db,
            session_store=session_store,
            user_id="user-1",
            session_id="sess-1",
            event_bus=event_bus,
        )

        event_bus.publish.assert_called_once()
        event = event_bus.publish.call_args[0][0]
        assert event["type"] == "session_updated"
        assert event["session_id"] == "sess-1"
        assert event["user_id"] == "user-1"

    def test_includes_user_messages_in_prompt(self, db, session_store, mock_llm):
        """generate_intention includes user messages as primary signal."""
        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")
        _insert_reasoning(db, "user-1", "sess-1", "User message: Fix the login bug")

        generate_intention(
            llm=mock_llm,
            db=db,
            session_store=session_store,
            user_id="user-1",
            session_id="sess-1",
        )

        call_args = mock_llm.generate.call_args
        messages = call_args[0][0]
        user_prompt = messages[1]["content"]
        assert "Fix the login bug" in user_prompt
        assert "primary signal" in messages[0]["content"]

    def test_excludes_agent_reasoning_from_user_message_prompt(
        self, db, session_store, mock_llm
    ):
        """Only prefixed user messages feed the intention prompt."""
        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")
        _insert_reasoning(db, "user-1", "sess-1", "User message: Fix the login bug")
        _insert_reasoning(
            db,
            "user-1",
            "sess-1",
            "I should inspect the repo and maybe rewrite the intention",
            call_id="call-r2",
        )

        generate_intention(
            llm=mock_llm,
            db=db,
            session_store=session_store,
            user_id="user-1",
            session_id="sess-1",
        )

        user_prompt = mock_llm.generate.call_args[0][0][1]["content"]
        assert "Fix the login bug" in user_prompt
        assert "rewrite the intention" not in user_prompt

    def test_prioritizes_latest_user_message_over_prior_intention(
        self, db, session_store, mock_llm
    ):
        """Latest user message appears before prior session state in the prompt."""
        session_store.create(
            user_id="user-1",
            session_id="sess-1",
            intention="Select three sweet medium-roast coffees and save a skill",
            title="Coffee selection",
        )
        _insert_reasoning(
            db,
            "user-1",
            "sess-1",
            "User message: Select three sweet medium-roast coffees",
            call_id="call-r1",
        )
        _insert_reasoning(
            db,
            "user-1",
            "sess-1",
            "User message: Ok I allow to push ainews into origin:main. Try again",
            call_id="call-r2",
        )

        generate_intention(
            llm=mock_llm,
            db=db,
            session_store=session_store,
            user_id="user-1",
            session_id="sess-1",
            context="I can push ainews into origin/main for you if you want.",
        )

        messages = mock_llm.generate.call_args[0][0]
        system_prompt = messages[0]["content"]
        user_prompt = messages[1]["content"]
        assert "Ok I allow to push ainews into origin:main. Try again" in user_prompt
        assert user_prompt.index("Latest user message") < user_prompt.index(
            "Prior session state"
        )
        assert "I can push ainews into origin/main for you if you want." in user_prompt
        assert "keep at most two active goals" in system_prompt
        assert "fade out" in system_prompt

    def test_includes_parent_intention_for_sub_sessions(
        self, db, session_store, mock_llm
    ):
        """generate_intention includes parent intention for sub-sessions."""
        session_store.create(
            user_id="user-1",
            session_id="sess-parent",
            intention="Refactoring the auth module",
        )
        session_store.create(
            user_id="user-1",
            session_id="sess-child",
            intention="Initial child",
            parent_session_id="sess-parent",
        )
        _insert_tool_call(db, "user-1", "sess-child")

        generate_intention(
            llm=mock_llm,
            db=db,
            session_store=session_store,
            user_id="user-1",
            session_id="sess-child",
        )

        call_args = mock_llm.generate.call_args
        messages = call_args[0][0]
        user_prompt = messages[1]["content"]
        assert "Refactoring the auth module" in user_prompt
        assert "sub-session" in user_prompt

    def test_includes_decision_context_in_prompt(self, db, session_store, mock_llm):
        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")

        generate_intention(
            llm=mock_llm,
            db=db,
            session_store=session_store,
            user_id="user-1",
            session_id="sess-1",
            decision_context={
                "tool": "web_search",
                "args_redacted": {"query": "daily brief"},
                "user_decision": "approve",
                "user_note": "this is fine and aligned",
            },
        )

        messages = mock_llm.generate.call_args[0][0]
        user_prompt = messages[1]["content"]
        system_prompt = messages[0]["content"]
        assert "## User Decisions" in user_prompt
        assert "this is fine and aligned" in user_prompt
        assert "focus on the most recent active topic" in system_prompt

    def test_decision_context_skips_empty_history_guard(
        self, db, session_store, mock_llm
    ):
        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")

        result = generate_intention(
            llm=mock_llm,
            db=db,
            session_store=session_store,
            user_id="user-1",
            session_id="sess-1",
            decision_context={
                "tool": "web_search",
                "args_redacted": {"query": "daily brief"},
                "user_decision": "approve",
                "user_note": "approved for research",
            },
        )

        assert result is not None
        mock_llm.generate.assert_called_once()


class TestEvaluatorDecisionContext:
    def test_llm_evaluate_passes_user_decisions_to_prompt(self, db, session_store):
        from intaris.audit import AuditStore

        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")
        audit = AuditStore(db)
        audit.insert(
            call_id="call-1",
            user_id="user-1",
            session_id="sess-1",
            agent_id=None,
            tool="web_search",
            args_redacted={"query": "daily brief"},
            classification="write",
            evaluation_path="llm",
            decision="escalate",
            risk="high",
            reasoning="Needs review",
            latency_ms=10,
        )
        audit.resolve_escalation(
            "call-1",
            "approve",
            user_note="this is fine and aligned",
            user_id="user-1",
            resolved_by="user",
        )

        llm = MagicMock()
        llm.generate.return_value = (
            '{"aligned": true, "risk": "low", '
            '"reasoning": "Allowed", "decision": "approve"}'
        )
        evaluator = Evaluator(
            llm=llm,
            session_store=session_store,
            audit_store=audit,
            db=db,
        )

        with patch("intaris.evaluator.build_evaluation_user_prompt") as build_prompt:
            build_prompt.return_value = "prompt"
            session = session_store.get("sess-1", user_id="user-1")
            evaluator._llm_evaluate(
                session=session,
                tool="web_search",
                args_redacted={"query": "weather"},
                agent_id=None,
            )

        assert build_prompt.call_args.kwargs["user_decisions"]
        assert build_prompt.call_args.kwargs["user_decisions"][0]["user_note"] == (
            "this is fine and aligned"
        )

    def test_llm_evaluate_honors_final_human_same_tool_approval(
        self, db, session_store
    ):
        from intaris.audit import AuditStore

        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")
        audit = AuditStore(db)
        audit.insert(
            call_id="call-1",
            user_id="user-1",
            session_id="sess-1",
            agent_id=None,
            tool="mcptodoist_find-projects",
            args_redacted={"query": "cognis"},
            classification="write",
            evaluation_path="llm",
            decision="escalate",
            risk="low",
            reasoning="Needs review",
            latency_ms=10,
        )
        audit.resolve_escalation(
            "call-1",
            "approve",
            user_note="Todoist project lookup is fine in this session",
            user_id="user-1",
            resolved_by="user",
        )

        llm = MagicMock()
        llm.generate.return_value = (
            '{"aligned": false, "risk": "low", '
            '"reasoning": "Not aligned with intention but previously approved", '
            '"decision": "approve"}'
        )
        evaluator = Evaluator(
            llm=llm,
            session_store=session_store,
            audit_store=audit,
            db=db,
        )

        session = session_store.get("sess-1", user_id="user-1")
        decision = evaluator._llm_evaluate(
            session=session,
            tool="mcptodoist_find-projects",
            args_redacted={"query": "intaris"},
            agent_id=None,
        )

        assert decision.decision == "approve"
        assert "authoritative precedent" in decision.reasoning

    def test_llm_evaluate_honors_cross_tool_family_approval(self, db, session_store):
        from intaris.audit import AuditStore

        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")
        audit = AuditStore(db)
        audit.insert(
            call_id="call-1",
            user_id="user-1",
            session_id="sess-1",
            agent_id=None,
            tool="web_search",
            args_redacted={"query": "todoist docs"},
            classification="write",
            evaluation_path="llm",
            decision="escalate",
            risk="low",
            reasoning="Needs review",
            latency_ms=10,
        )
        audit.resolve_escalation(
            "call-1",
            "approve",
            user_note="Web lookup is fine for this session",
            user_id="user-1",
            resolved_by="user",
        )

        llm = MagicMock()
        llm.generate.return_value = (
            '{"aligned": false, "risk": "low", '
            '"reasoning": "Fetching docs looks off-topic", '
            '"decision": "approve"}'
        )
        evaluator = Evaluator(
            llm=llm,
            session_store=session_store,
            audit_store=audit,
            db=db,
        )

        session = session_store.get("sess-1", user_id="user-1")
        decision = evaluator._llm_evaluate(
            session=session,
            tool="web_fetch",
            args_redacted={"url": "https://todoist.com/help"},
            agent_id=None,
        )

        assert decision.decision == "approve"
        assert "Prior approved tool: web_search" in decision.reasoning

    def test_llm_evaluate_respects_newer_same_tool_human_denial(
        self, db, session_store
    ):
        from intaris.audit import AuditStore

        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")
        audit = AuditStore(db)
        audit.insert(
            call_id="call-1",
            user_id="user-1",
            session_id="sess-1",
            agent_id=None,
            tool="mcptodoist_find-projects",
            args_redacted={"query": "cognis"},
            classification="write",
            evaluation_path="llm",
            decision="escalate",
            risk="low",
            reasoning="Needs review",
            latency_ms=10,
        )
        audit.resolve_escalation(
            "call-1",
            "deny",
            user_note="Do not do this now",
            user_id="user-1",
            resolved_by="user",
        )

        llm = MagicMock()
        llm.generate.return_value = (
            '{"aligned": false, "risk": "low", '
            '"reasoning": "Not aligned with intention", '
            '"decision": "approve"}'
        )
        evaluator = Evaluator(
            llm=llm,
            session_store=session_store,
            audit_store=audit,
            db=db,
        )

        session = session_store.get("sess-1", user_id="user-1")
        decision = evaluator._llm_evaluate(
            session=session,
            tool="mcptodoist_find-projects",
            args_redacted={"query": "intaris"},
            agent_id=None,
        )

        assert decision.decision == "escalate"

    def test_llm_evaluate_respects_cross_tool_family_denial(self, db, session_store):
        from intaris.audit import AuditStore

        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")
        audit = AuditStore(db)
        audit.insert(
            call_id="call-1",
            user_id="user-1",
            session_id="sess-1",
            agent_id=None,
            tool="mcptodoist_find-projects",
            args_redacted={"query": "cognis"},
            classification="write",
            evaluation_path="llm",
            decision="escalate",
            risk="low",
            reasoning="Needs review",
            latency_ms=10,
        )
        audit.resolve_escalation(
            "call-1",
            "deny",
            user_note="Do not do Todoist lookups now",
            user_id="user-1",
            resolved_by="user",
        )

        llm = MagicMock()
        llm.generate.return_value = (
            '{"aligned": false, "risk": "low", '
            '"reasoning": "Not aligned with intention", '
            '"decision": "approve"}'
        )
        evaluator = Evaluator(
            llm=llm,
            session_store=session_store,
            audit_store=audit,
            db=db,
        )

        session = session_store.get("sess-1", user_id="user-1")
        decision = evaluator._llm_evaluate(
            session=session,
            tool="mcptodoist_find-sections",
            args_redacted={"projectId": "abc123"},
            agent_id=None,
        )

        assert decision.decision == "escalate"

    def test_strips_quotes_from_llm_response(self, db, session_store):
        """generate_intention strips surrounding quotes from LLM output."""
        llm = MagicMock()
        llm.generate.return_value = '"Building a REST API for user management"'

        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")
        _insert_tool_call(db, "user-1", "sess-1")

        result = generate_intention(
            llm=llm,
            db=db,
            session_store=session_store,
            user_id="user-1",
            session_id="sess-1",
        )
        assert result is not None
        assert not result.startswith('"')
        assert not result.endswith('"')

    def test_prompt_enforces_english_output(self, db, session_store, mock_llm):
        """System prompt must instruct LLM to generate intention in English."""
        session_store.create(
            user_id="user-1", session_id="sess-1", intention="Initial intention"
        )
        _insert_tool_call(db, "user-1", "sess-1")

        generate_intention(
            llm=mock_llm,
            db=db,
            session_store=session_store,
            user_id="user-1",
            session_id="sess-1",
        )

        call_args = mock_llm.generate.call_args
        messages = call_args[0][0]
        system_prompt = messages[0]["content"]
        assert "English" in system_prompt
        assert "regardless" in system_prompt.lower()

    def test_czech_messages_still_produce_english_prompt_instruction(
        self, db, session_store, mock_llm
    ):
        """Regression: Czech user messages must not suppress English enforcement.

        When user messages are in Czech, the system prompt must still
        contain the English language instruction.
        """
        session_store.create(
            user_id="user-1",
            session_id="sess-1",
            intention="Oprava přihlašovacího formuláře",
        )
        _insert_reasoning(
            db,
            "user-1",
            "sess-1",
            "User message: Oprav chybu v přihlašovacím formuláři",
            call_id="call-cz1",
        )
        _insert_reasoning(
            db,
            "user-1",
            "sess-1",
            "User message: Přidej validaci emailu",
            call_id="call-cz2",
        )
        _insert_tool_call(db, "user-1", "sess-1")

        generate_intention(
            llm=mock_llm,
            db=db,
            session_store=session_store,
            user_id="user-1",
            session_id="sess-1",
        )

        call_args = mock_llm.generate.call_args
        messages = call_args[0][0]
        system_prompt = messages[0]["content"]
        # English enforcement must be present even with Czech context
        assert "Always write the intention in English" in system_prompt


# ── IntentionBarrier ──────────────────────────────────────────────────


class TestIntentionBarrier:
    """Tests for IntentionBarrier async coordination.

    Uses asyncio.run() for each test since pytest-asyncio is not a
    project dependency.
    """

    def test_wait_returns_false_when_no_pending(self, db, mock_llm):
        """wait() returns False immediately when nothing is pending."""
        barrier = _make_barrier(db, mock_llm)

        async def _test():
            result = await barrier.wait("user-1", "sess-1")
            assert result is False
            assert barrier.wait_count == 0

        asyncio.run(_test())

    def test_trigger_and_wait(self, db, session_store, mock_llm):
        """trigger() starts update, wait() blocks until complete."""
        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")
        _insert_tool_call(db, "user-1", "sess-1")

        barrier = _make_barrier(db, mock_llm)

        async def _test():
            await barrier.trigger("user-1", "sess-1")
            result = await barrier.wait("user-1", "sess-1")
            assert result is True
            assert barrier.wait_count == 1
            assert barrier.update_count == 1

        asyncio.run(_test())

    def test_wait_after_completion_returns_false(self, db, session_store, mock_llm):
        """wait() returns False if the update already completed and cleaned up."""
        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")
        _insert_tool_call(db, "user-1", "sess-1")

        barrier = _make_barrier(db, mock_llm)

        async def _test():
            await barrier.trigger("user-1", "sess-1")
            await barrier.wait("user-1", "sess-1")
            # Give cleanup a chance
            await asyncio.sleep(0.05)
            # Second wait should return False (entry cleaned up)
            result = await barrier.wait("user-1", "sess-1")
            assert result is False

        asyncio.run(_test())

    def test_timeout_increments_counter(self, db, session_store):
        """wait() times out and increments timeout_count."""
        import time as time_mod

        slow_llm = MagicMock()

        def slow_generate(*args, **kwargs):
            time_mod.sleep(0.3)  # Longer than 100ms timeout
            return "Updated intention"

        slow_llm.generate.side_effect = slow_generate

        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")
        _insert_tool_call(db, "user-1", "sess-1")

        barrier = _make_barrier(db, slow_llm, timeout_ms=100)

        async def _test():
            await barrier.trigger("user-1", "sess-1")
            result = await barrier.wait("user-1", "sess-1")
            assert result is True
            assert barrier.timeout_count == 1
            assert barrier.wait_count == 1

        asyncio.run(_test())

    def test_cancel_and_restart(self, db, session_store, mock_llm):
        """Second trigger cancels the first and starts a new update."""
        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")
        _insert_tool_call(db, "user-1", "sess-1")

        barrier = _make_barrier(db, mock_llm)

        async def _test():
            await barrier.trigger("user-1", "sess-1")
            await barrier.trigger("user-1", "sess-1")
            result = await barrier.wait("user-1", "sess-1")
            assert result is True

        asyncio.run(_test())

    def test_trigger_from_decision_registers_pending_refresh(
        self, db, session_store, mock_llm
    ):
        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")
        barrier = _make_barrier(db, mock_llm)

        async def _test():
            await barrier.trigger_from_decision(
                "user-1",
                "sess-1",
                tool="web_search",
                args_redacted={"query": "daily brief"},
                user_note="approved for this session",
            )
            assert ("user-1", "sess-1") in barrier._pending
            result = await barrier.wait("user-1", "sess-1")
            assert result is True

        asyncio.run(_test())

    def test_concurrent_waiters(self, db, session_store, mock_llm):
        """Multiple waiters are all unblocked when update completes."""
        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")
        _insert_tool_call(db, "user-1", "sess-1")

        barrier = _make_barrier(db, mock_llm)

        async def _test():
            await barrier.trigger("user-1", "sess-1")
            results = await asyncio.gather(
                barrier.wait("user-1", "sess-1"),
                barrier.wait("user-1", "sess-1"),
            )
            assert all(r is True for r in results)
            assert barrier.wait_count == 2

        asyncio.run(_test())

    def test_metrics_initial(self, db, mock_llm):
        """metrics() returns correct initial counters."""
        barrier = _make_barrier(db, mock_llm)
        metrics = barrier.metrics()
        assert metrics["wait_count"] == 0
        assert metrics["timeout_count"] == 0
        assert metrics["update_count"] == 0
        assert metrics["update_errors"] == 0
        assert metrics["pending"] == 0

    def test_llm_failure_does_not_increment_update_count(self, db, session_store):
        """LLM failure in generate_intention results in no update_count."""
        error_llm = MagicMock()
        error_llm.generate.side_effect = RuntimeError("LLM down")

        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")
        _insert_tool_call(db, "user-1", "sess-1")

        barrier = _make_barrier(db, error_llm)

        async def _test():
            await barrier.trigger("user-1", "sess-1")
            await barrier.wait("user-1", "sess-1")
            # generate_intention catches the error internally and returns None,
            # so update_count stays 0 (no successful update)
            assert barrier.update_count == 0
            # Waiters are still unblocked (barrier completes regardless)

        asyncio.run(_test())

    def test_executor_error_increments_error_counter(self, db, session_store):
        """Errors escaping generate_intention increment update_errors."""
        # Simulate an error that escapes generate_intention by making
        # the SessionStore constructor fail (before generate_intention runs)
        mock_llm = MagicMock()

        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")
        _insert_tool_call(db, "user-1", "sess-1")

        barrier = _make_barrier(db, mock_llm)

        async def _test():
            # Patch SessionStore to raise during _run
            with patch(
                "intaris.intention.SessionStore",
                side_effect=RuntimeError("DB gone"),
            ):
                await barrier.trigger("user-1", "sess-1")
                await barrier.wait("user-1", "sess-1")

            assert barrier.update_errors == 1
            assert barrier.update_count == 0

        asyncio.run(_test())

    def test_set_event_bus(self, db, mock_llm):
        """set_event_bus stores reference for event publishing."""
        barrier = _make_barrier(db, mock_llm)
        event_bus = MagicMock()
        barrier.set_event_bus(event_bus)
        assert barrier._event_bus is event_bus

    def test_independent_sessions(self, db, session_store, mock_llm):
        """Different sessions have independent barriers."""
        session_store.create(
            user_id="user-1", session_id="sess-1", intention="Session 1"
        )
        session_store.create(
            user_id="user-1", session_id="sess-2", intention="Session 2"
        )
        _insert_tool_call(db, "user-1", "sess-1", call_id="call-s1")
        _insert_tool_call(db, "user-1", "sess-2", call_id="call-s2")

        barrier = _make_barrier(db, mock_llm)

        async def _test():
            # Trigger only sess-1
            await barrier.trigger("user-1", "sess-1")
            # sess-2 should not be pending
            result = await barrier.wait("user-1", "sess-2")
            assert result is False
            # sess-1 should be pending
            result = await barrier.wait("user-1", "sess-1")
            assert result is True

        asyncio.run(_test())


# ── IntentionBarrier Arrival Wait ─────────────────────────────────────


class TestIntentionBarrierArrivalWait:
    """Tests for the arrival-wait mechanism (server-side timestamp).

    When /evaluate arrives before /reasoning, the barrier uses an
    asyncio.Event to wait for trigger() to be called, avoiding the
    race condition where the evaluator proceeds with a stale intention.

    The arrival wait is now triggered by the server-side
    ``_last_user_message_time`` timestamp (set by ``trigger()``) rather
    than a client-supplied ``intention_pending`` hint.
    """

    def test_arrival_hit_trigger_arrives_in_time(self, db, session_store, mock_llm):
        """Recent user message timestamp + trigger arrives → waits for completion."""
        import time

        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")
        _insert_tool_call(db, "user-1", "sess-1")

        barrier = _make_barrier(db, mock_llm, poll_timeout_ms=2000)

        async def _test():
            # Simulate: a prior trigger set the timestamp (as happens in
            # the /reasoning endpoint), then another trigger arrives
            # while wait() is checking the timestamp.
            barrier._last_user_message_time[("user-1", "sess-1")] = time.monotonic()

            async def delayed_trigger():
                await asyncio.sleep(0.1)
                await barrier.trigger("user-1", "sess-1")

            asyncio.create_task(delayed_trigger())

            result = await barrier.wait("user-1", "sess-1")
            assert result is True
            assert barrier.arrival_wait_count == 1
            assert barrier.arrival_hit_count == 1
            assert barrier.arrival_timeout_count == 0
            assert barrier.wait_count == 1  # Also waited for completion

        asyncio.run(_test())

    def test_arrival_timeout_reasoning_never_arrives(self, db, mock_llm):
        """Recent timestamp but /reasoning never arrives → timeout."""
        import time

        barrier = _make_barrier(db, mock_llm, poll_timeout_ms=200)

        async def _test():
            # Set a recent timestamp to simulate the race condition
            barrier._last_user_message_time[("user-1", "sess-1")] = time.monotonic()

            result = await barrier.wait("user-1", "sess-1")
            # Should return False — no entry was ever created
            assert result is False
            assert barrier.arrival_wait_count == 1
            assert barrier.arrival_timeout_count == 1
            assert barrier.arrival_hit_count == 0
            assert barrier.wait_count == 0  # Never reached barrier wait

        asyncio.run(_test())

    def test_no_timestamp_unchanged_behavior(self, db, mock_llm):
        """No recent user message timestamp → existing behavior unchanged."""
        barrier = _make_barrier(db, mock_llm)

        async def _test():
            # No trigger, no timestamp → returns False immediately
            result = await barrier.wait("user-1", "sess-1")
            assert result is False
            assert barrier.arrival_wait_count == 0
            assert barrier.wait_count == 0

        asyncio.run(_test())

    def test_stale_timestamp_skips_arrival_wait(self, db, mock_llm):
        """Old timestamp (beyond poll_timeout) → no arrival wait."""
        import time

        barrier = _make_barrier(db, mock_llm, poll_timeout_ms=200)

        async def _test():
            # Set a timestamp far in the past (beyond poll_timeout)
            barrier._last_user_message_time[("user-1", "sess-1")] = (
                time.monotonic() - 60.0
            )

            result = await barrier.wait("user-1", "sess-1")
            assert result is False
            assert barrier.arrival_wait_count == 0
            assert barrier.wait_count == 0

        asyncio.run(_test())

    def test_concurrent_waiters_share_arrival_event(self, db, session_store, mock_llm):
        """Multiple evaluate calls with recent timestamp share one arrival event."""
        import time

        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")
        _insert_tool_call(db, "user-1", "sess-1")

        barrier = _make_barrier(db, mock_llm, poll_timeout_ms=2000)

        async def _test():
            barrier._last_user_message_time[("user-1", "sess-1")] = time.monotonic()

            # Both evaluate calls arrive before /reasoning
            async def delayed_trigger():
                await asyncio.sleep(0.1)
                await barrier.trigger("user-1", "sess-1")

            asyncio.create_task(delayed_trigger())

            # First waiter creates the arrival event, second reuses it
            # (or creates a new one if the first already consumed it).
            # Both should eventually succeed.
            results = await asyncio.gather(
                barrier.wait("user-1", "sess-1"),
                barrier.wait("user-1", "sess-1"),
            )
            # At least one should have waited and succeeded
            assert any(r is True for r in results)
            assert barrier.arrival_wait_count >= 1

        asyncio.run(_test())

    def test_arrival_event_cleanup_on_timeout(self, db, mock_llm):
        """Arrival event is cleaned up after timeout."""
        import time

        barrier = _make_barrier(db, mock_llm, poll_timeout_ms=100)

        async def _test():
            barrier._last_user_message_time[("user-1", "sess-1")] = time.monotonic()
            await barrier.wait("user-1", "sess-1")
            # Arrival event should be cleaned up
            assert ("user-1", "sess-1") not in barrier._arrival_events

        asyncio.run(_test())

    def test_arrival_event_cleanup_on_hit(self, db, session_store, mock_llm):
        """Arrival event is cleaned up after trigger arrives."""
        import time

        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")
        _insert_tool_call(db, "user-1", "sess-1")

        barrier = _make_barrier(db, mock_llm, poll_timeout_ms=2000)

        async def _test():
            barrier._last_user_message_time[("user-1", "sess-1")] = time.monotonic()

            async def delayed_trigger():
                await asyncio.sleep(0.05)
                await barrier.trigger("user-1", "sess-1")

            asyncio.create_task(delayed_trigger())

            await barrier.wait("user-1", "sess-1")
            # Arrival event should be cleaned up
            assert ("user-1", "sess-1") not in barrier._arrival_events

        asyncio.run(_test())

    def test_timestamp_cleared_after_run_completes(self, db, session_store, mock_llm):
        """_last_user_message_time is cleared when the barrier task completes."""
        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")
        _insert_tool_call(db, "user-1", "sess-1")

        barrier = _make_barrier(db, mock_llm)

        async def _test():
            await barrier.trigger("user-1", "sess-1")
            # Wait for the barrier to complete
            await barrier.wait("user-1", "sess-1")
            # Give the finally block a tick to clean up
            await asyncio.sleep(0.05)
            assert ("user-1", "sess-1") not in barrier._last_user_message_time

        asyncio.run(_test())

    def test_intention_pending_param_ignored(self, db, mock_llm):
        """Deprecated intention_pending param is accepted but ignored."""
        barrier = _make_barrier(db, mock_llm)

        async def _test():
            # intention_pending=True without a recent timestamp should NOT
            # trigger arrival wait (old behavior would have waited).
            result = await barrier.wait("user-1", "sess-1", intention_pending=True)
            assert result is False
            assert barrier.arrival_wait_count == 0

        asyncio.run(_test())

    def test_trigger_already_arrived_skips_arrival_wait(
        self, db, session_store, mock_llm
    ):
        """If trigger() already ran before wait(), no arrival wait needed."""
        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")
        _insert_tool_call(db, "user-1", "sess-1")

        barrier = _make_barrier(db, mock_llm, poll_timeout_ms=2000)

        async def _test():
            # /reasoning arrives first (normal case)
            await barrier.trigger("user-1", "sess-1")
            # /evaluate arrives with intention_pending=True
            result = await barrier.wait("user-1", "sess-1", intention_pending=True)
            assert result is True
            # Should NOT have entered arrival wait (entry already existed)
            assert barrier.arrival_wait_count == 0
            assert barrier.wait_count == 1

        asyncio.run(_test())

    def test_metrics_include_arrival_counters(self, db, mock_llm):
        """metrics() includes the new arrival-wait counters."""
        barrier = _make_barrier(db, mock_llm)
        metrics = barrier.metrics()
        assert "arrival_wait_count" in metrics
        assert "arrival_hit_count" in metrics
        assert "arrival_timeout_count" in metrics
        assert metrics["arrival_wait_count"] == 0
        assert metrics["arrival_hit_count"] == 0
        assert metrics["arrival_timeout_count"] == 0


# ── Evaluator Bootstrap ───────────────────────────────────────────────


class TestEvaluatorBootstrap:
    """Tests for the one-time intention bootstrap in the evaluator."""

    def test_bootstrap_enqueues_at_call_10(self, db, session_store):
        """Evaluator enqueues bootstrap task at call 10 when source is initial."""
        from intaris.audit import AuditStore
        from intaris.background import TaskQueue
        from intaris.evaluator import Evaluator

        llm = MagicMock()
        llm.generate.return_value = (
            '{"aligned": true, "risk": "low", '
            '"reasoning": "Safe", "decision": "approve"}'
        )

        session_store.create(
            user_id="user-1", session_id="sess-1", intention="Initial session"
        )

        # Set total_calls to 9 (next call will be the 10th)
        with db.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET total_calls = 9 WHERE session_id = 'sess-1'"
            )

        evaluator = Evaluator(
            llm=llm,
            session_store=session_store,
            audit_store=AuditStore(db),
            db=db,
            analysis_config=MagicMock(enabled=True),
        )

        evaluator.evaluate(
            user_id="user-1",
            session_id="sess-1",
            agent_id=None,
            tool="read",
            args={"path": "/tmp/test"},
        )

        # Check that a bootstrap task was enqueued
        tq = TaskQueue(db)
        task = tq.claim_next()
        assert task is not None
        assert task["task_type"] == "intention_update"

    def test_bootstrap_skipped_when_source_is_user(self, db, session_store):
        """Evaluator does NOT bootstrap when intention_source is 'user'."""
        from intaris.audit import AuditStore
        from intaris.background import TaskQueue
        from intaris.evaluator import Evaluator

        llm = MagicMock()
        llm.generate.return_value = (
            '{"aligned": true, "risk": "low", '
            '"reasoning": "Safe", "decision": "approve"}'
        )

        session_store.create(
            user_id="user-1", session_id="sess-1", intention="User-set intention"
        )

        # Set total_calls to 9 and intention_source to 'user'
        with db.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET total_calls = 9, intention_source = 'user' "
                "WHERE session_id = 'sess-1'"
            )

        evaluator = Evaluator(
            llm=llm,
            session_store=session_store,
            audit_store=AuditStore(db),
            db=db,
            analysis_config=MagicMock(enabled=True),
        )

        evaluator.evaluate(
            user_id="user-1",
            session_id="sess-1",
            agent_id=None,
            tool="read",
            args={"path": "/tmp/test"},
        )

        # No bootstrap task should be enqueued
        tq = TaskQueue(db)
        task = tq.claim_next()
        assert task is None

    def test_bootstrap_skipped_at_other_call_counts(self, db, session_store):
        """Evaluator does NOT bootstrap at call counts other than 10."""
        from intaris.audit import AuditStore
        from intaris.background import TaskQueue
        from intaris.evaluator import Evaluator

        llm = MagicMock()
        llm.generate.return_value = (
            '{"aligned": true, "risk": "low", '
            '"reasoning": "Safe", "decision": "approve"}'
        )

        session_store.create(
            user_id="user-1", session_id="sess-1", intention="Initial session"
        )

        # Set total_calls to 5 (not 9, so next call is 6th not 10th)
        with db.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET total_calls = 5 WHERE session_id = 'sess-1'"
            )

        evaluator = Evaluator(
            llm=llm,
            session_store=session_store,
            audit_store=AuditStore(db),
            db=db,
            analysis_config=MagicMock(enabled=True),
        )

        evaluator.evaluate(
            user_id="user-1",
            session_id="sess-1",
            agent_id=None,
            tool="read",
            args={"path": "/tmp/test"},
        )

        tq = TaskQueue(db)
        task = tq.claim_next()
        assert task is None


# ── Session intention_source Column ───────────────────────────────────


class TestIntentionSourceColumn:
    """Tests for the intention_source column on sessions."""

    def test_default_intention_source(self, session_store):
        """New sessions have intention_source='initial'."""
        session = session_store.create(
            user_id="user-1", session_id="sess-1", intention="Test"
        )
        assert session.get("intention_source") == "initial"

    def test_update_intention_source(self, session_store):
        """update_session can set intention_source."""
        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")

        session_store.update_session(
            "sess-1",
            user_id="user-1",
            intention="Updated by user",
            intention_source="user",
        )

        session = session_store.get("sess-1", user_id="user-1")
        assert session["intention_source"] == "user"
        assert session["intention"] == "Updated by user"

    def test_intention_source_preserved_on_other_updates(self, session_store):
        """Updating intention without intention_source preserves existing."""
        session_store.create(user_id="user-1", session_id="sess-1", intention="Initial")

        session_store.update_session(
            "sess-1",
            user_id="user-1",
            intention="First update",
            intention_source="user",
        )

        session_store.update_session(
            "sess-1",
            user_id="user-1",
            intention="Second update",
        )

        session = session_store.get("sess-1", user_id="user-1")
        assert session["intention_source"] == "user"


# ── Evaluator get_recent Filter ──────────────────────────────────────


class TestEvaluatorRecordTypeFilter:
    """Tests for evaluator filtering get_recent to tool_call only."""

    def test_llm_evaluate_uses_tool_call_filter(self, db, session_store):
        """Evaluator passes record_type='tool_call' to get_recent."""
        from intaris.audit import AuditStore
        from intaris.evaluator import Evaluator

        llm = MagicMock()
        llm.generate.return_value = (
            '{"aligned": true, "risk": "low", '
            '"reasoning": "Safe", "decision": "approve"}'
        )

        session_store.create(
            user_id="user-1", session_id="sess-1", intention="Test session"
        )

        audit = AuditStore(db)

        # Insert a reasoning record (should be excluded from eval context)
        _insert_reasoning(db, "user-1", "sess-1", "User message: Fix the bug")

        # Insert a tool_call record (should be included)
        _insert_tool_call(db, "user-1", "sess-1")

        evaluator = Evaluator(
            llm=llm,
            session_store=session_store,
            audit_store=audit,
            db=db,
        )

        # Patch get_recent to verify the filter
        original_get_recent = audit.get_recent
        calls = []

        def tracking_get_recent(*args, **kwargs):
            calls.append(kwargs)
            return original_get_recent(*args, **kwargs)

        with patch.object(audit, "get_recent", side_effect=tracking_get_recent):
            evaluator.evaluate(
                user_id="user-1",
                session_id="sess-1",
                agent_id=None,
                tool="bash",
                args={"command": "npm install express"},
            )

        # Verify get_recent was called with record_type="tool_call"
        assert len(calls) >= 1
        assert calls[0].get("record_type") == "tool_call"


# ── _parse_title_intention tests ──────────────────────────────────────


class TestParseTitleIntention:
    """Tests for _parse_title_intention JSON parsing helper."""

    def test_valid_json(self):
        raw = '{"title": "Fix login bug", "intention": "User is debugging a login issue."}'
        title, intention = _parse_title_intention(raw)
        assert title == "Fix login bug"
        assert intention == "User is debugging a login issue."

    def test_valid_json_with_whitespace(self):
        raw = '  \n  {"title": "Deploy app", "intention": "Deploying the application to production."}\n  '
        title, intention = _parse_title_intention(raw)
        assert title == "Deploy app"
        assert intention == "Deploying the application to production."

    def test_json_with_markdown_fences(self):
        raw = '```json\n{"title": "Setup CI", "intention": "Configuring CI pipeline."}\n```'
        title, intention = _parse_title_intention(raw)
        assert title == "Setup CI"
        assert intention == "Configuring CI pipeline."

    def test_markdown_fences_no_lang(self):
        raw = '```\n{"title": "Auth flow", "intention": "Implementing OAuth2 authentication."}\n```'
        title, intention = _parse_title_intention(raw)
        assert title == "Auth flow"
        assert intention == "Implementing OAuth2 authentication."

    def test_fallback_plain_text(self):
        raw = "User is debugging a login issue in the Flask application."
        title, intention = _parse_title_intention(raw, current_title="Old Title")
        assert title == "Old Title"
        assert intention == raw

    def test_fallback_preserves_none_title(self):
        raw = "User is working on the project."
        title, intention = _parse_title_intention(raw)
        assert title is None
        assert intention == raw

    def test_empty_title_becomes_none(self):
        raw = '{"title": "", "intention": "User is doing something."}'
        title, intention = _parse_title_intention(raw)
        assert title is None
        assert intention == "User is doing something."

    def test_missing_title_key(self):
        raw = '{"intention": "User is doing something."}'
        title, intention = _parse_title_intention(raw)
        assert title is None
        assert intention == "User is doing something."

    def test_too_short_intention_returns_empty(self):
        raw = '{"title": "Test", "intention": "Hi"}'
        title, intention = _parse_title_intention(raw, current_title="Old")
        # intention < 5 chars returns empty string (causes generate_intention
        # to return None and skip the update) instead of storing raw JSON
        assert title == "Old"
        assert intention == ""

    def test_title_truncated_to_120(self):
        long_title = "A" * 200
        raw = f'{{"title": "{long_title}", "intention": "User is doing something."}}'
        title, intention = _parse_title_intention(raw)
        assert title is not None
        assert len(title) == 120
        assert intention == "User is doing something."

    def test_quoted_plain_text_stripped(self):
        raw = '"User is debugging an issue."'
        title, intention = _parse_title_intention(raw)
        assert title is None
        assert intention == "User is debugging an issue."

    def test_invalid_json(self):
        raw = '{"title": "Broken'
        title, intention = _parse_title_intention(raw, current_title="Keep This")
        assert title == "Keep This"
        assert '{"title": "Broken' in intention
