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
from intaris.intention import IntentionBarrier, generate_intention
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


def _make_barrier(db, llm, timeout_ms=500):
    """Create an IntentionBarrier with given params."""
    return IntentionBarrier(db=db, llm=llm, timeout_ms=timeout_ms)


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

    def test_truncates_long_intention(self, db, session_store):
        """generate_intention truncates intention to 500 chars."""
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
        assert len(result) == 500

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
        assert "most important signal" in messages[0]["content"]

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
