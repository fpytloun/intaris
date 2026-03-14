"""Unit tests for the alignment barrier (parent/child intention enforcement)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from intaris.config import DBConfig
from intaris.db import Database
from intaris.session import SessionStore

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    cfg = DBConfig()
    cfg.path = str(tmp_path / "test.db")
    return Database(cfg)


@pytest.fixture
def session_store(db):
    return SessionStore(db)


@pytest.fixture
def mock_llm():
    """Mock LLM that returns aligned=True by default."""
    import json

    llm = MagicMock()
    llm.generate.return_value = json.dumps(
        {"aligned": True, "reasoning": "Intentions are compatible"}
    )
    return llm


@pytest.fixture
def mock_llm_misaligned():
    """Mock LLM that returns aligned=False."""
    import json

    llm = MagicMock()
    llm.generate.return_value = json.dumps(
        {
            "aligned": False,
            "reasoning": "Child intention contradicts parent scope",
        }
    )
    return llm


# ── check_intention_alignment tests ───────────────────────────────────


class TestCheckIntentionAlignment:
    """Tests for the check_intention_alignment() function."""

    def test_compatible_intentions(self, mock_llm):
        """Compatible intentions return (True, reasoning)."""
        from intaris.alignment import check_intention_alignment

        aligned, reasoning = check_intention_alignment(
            llm=mock_llm,
            parent_intention="Implement user authentication with OAuth2",
            child_intention="Write unit tests for the OAuth2 module",
        )
        assert aligned is True
        assert "compatible" in reasoning.lower() or len(reasoning) > 0
        mock_llm.generate.assert_called_once()

    def test_contradictory_intentions(self, mock_llm_misaligned):
        """Contradictory intentions return (False, reasoning)."""
        from intaris.alignment import check_intention_alignment

        aligned, reasoning = check_intention_alignment(
            llm=mock_llm_misaligned,
            parent_intention="Implement user authentication with OAuth2",
            child_intention="Delete the entire project",
        )
        assert aligned is False
        assert len(reasoning) > 0

    def test_llm_failure_returns_aligned(self):
        """LLM failure defaults to aligned (fail-open)."""
        from intaris.alignment import check_intention_alignment

        llm = MagicMock()
        llm.generate.side_effect = RuntimeError("LLM service unavailable")

        aligned, reasoning = check_intention_alignment(
            llm=llm,
            parent_intention="Build a web app",
            child_intention="Destroy everything",
        )
        assert aligned is True
        assert reasoning == ""

    def test_malformed_json_returns_aligned(self):
        """Malformed LLM response defaults to aligned (fail-open)."""
        from intaris.alignment import check_intention_alignment

        llm = MagicMock()
        llm.generate.return_value = "not valid json at all"

        aligned, reasoning = check_intention_alignment(
            llm=llm,
            parent_intention="Build a web app",
            child_intention="Something unrelated",
        )
        assert aligned is True
        assert reasoning == ""


# ── AlignmentBarrier tests ────────────────────────────────────────────


def _make_barrier(db, llm, timeout_ms=5000):
    """Create an AlignmentBarrier with the given params."""
    from intaris.alignment import AlignmentBarrier

    return AlignmentBarrier(db=db, llm=llm, timeout_ms=timeout_ms)


class TestAlignmentBarrier:
    """Tests for the AlignmentBarrier class."""

    def test_wait_returns_false_when_no_pending(self, db, mock_llm):
        """wait() returns False immediately if nothing is pending."""
        barrier = _make_barrier(db, mock_llm)

        async def _test():
            result = await barrier.wait("user-1", "sess-1")
            assert result is False

        asyncio.run(_test())

    def test_trigger_and_wait_aligned(self, db, mock_llm, session_store):
        """Trigger + wait for aligned intentions → session stays active."""
        session_store.create(
            user_id="user-1",
            session_id="parent-1",
            intention="Build a web app",
        )
        session_store.create(
            user_id="user-1",
            session_id="child-1",
            intention="Write tests for the web app",
            parent_session_id="parent-1",
        )

        barrier = _make_barrier(db, mock_llm)

        async def _test():
            await barrier.trigger("user-1", "child-1")
            result = await barrier.wait("user-1", "child-1")
            assert result is True

        asyncio.run(_test())

        # Session should still be active
        session = session_store.get("child-1", user_id="user-1")
        assert session["status"] == "active"
        assert barrier.check_count == 1
        assert barrier.misaligned_count == 0

    def test_trigger_and_wait_misaligned_stores_in_memory(
        self, db, mock_llm_misaligned, session_store
    ):
        """Trigger + wait for misaligned → misalignment stored in memory, session stays active."""
        session_store.create(
            user_id="user-1",
            session_id="parent-2",
            intention="Implement user authentication",
        )
        session_store.create(
            user_id="user-1",
            session_id="child-2",
            intention="Delete the entire project",
            parent_session_id="parent-2",
        )

        barrier = _make_barrier(db, mock_llm_misaligned)

        async def _test():
            await barrier.trigger("user-1", "child-2")
            result = await barrier.wait("user-1", "child-2")
            assert result is True

        asyncio.run(_test())

        # Session should still be active (not suspended)
        session = session_store.get("child-2", user_id="user-1")
        assert session["status"] == "active"

        # Misalignment should be stored in barrier memory
        reason = barrier.is_misaligned("user-1", "child-2")
        assert reason is not None
        assert "misaligned" in reason.lower() or "contradicts" in reason.lower()
        assert barrier.check_count == 1
        assert barrier.misaligned_count == 1

    def test_no_parent_session_skips_check(self, db, mock_llm, session_store):
        """Sessions without parent_session_id skip the alignment check."""
        session_store.create(
            user_id="user-1",
            session_id="root-1",
            intention="Build something",
        )

        barrier = _make_barrier(db, mock_llm)

        async def _test():
            await barrier.trigger("user-1", "root-1")
            result = await barrier.wait("user-1", "root-1")
            assert result is True

        asyncio.run(_test())

        # LLM should NOT have been called
        mock_llm.generate.assert_not_called()
        assert barrier.check_count == 0

    def test_parent_not_found_skips_check(self, db, mock_llm, session_store):
        """If parent session doesn't exist, alignment check is skipped."""
        session_store.create(
            user_id="user-1",
            session_id="orphan-1",
            intention="Do something",
            parent_session_id="nonexistent-parent",
        )

        barrier = _make_barrier(db, mock_llm)

        async def _test():
            await barrier.trigger("user-1", "orphan-1")
            result = await barrier.wait("user-1", "orphan-1")
            assert result is True

        asyncio.run(_test())

        mock_llm.generate.assert_not_called()

    def test_timeout_fails_open(self, db, session_store):
        """Barrier timeout → fail-open (session stays active)."""
        import json
        import time

        session_store.create(
            user_id="user-1",
            session_id="parent-t",
            intention="Build something",
        )
        session_store.create(
            user_id="user-1",
            session_id="child-t",
            intention="Something else entirely",
            parent_session_id="parent-t",
        )

        # LLM that takes longer than the barrier timeout
        slow_llm = MagicMock()

        def slow_generate(*args, **kwargs):
            time.sleep(2)
            return json.dumps({"aligned": False, "reasoning": "Misaligned"})

        slow_llm.generate.side_effect = slow_generate

        barrier = _make_barrier(db, slow_llm, timeout_ms=100)

        async def _test():
            await barrier.trigger("user-1", "child-t")
            result = await barrier.wait("user-1", "child-t")
            assert result is True  # Waited (timed out)

        asyncio.run(_test())

        assert barrier.timeout_count == 1
        # Session may or may not be suspended depending on whether the
        # background task completed before we checked. The key assertion
        # is that the barrier timed out and returned True.

    def test_event_bus_publishes_on_misalignment(
        self, db, mock_llm_misaligned, session_store
    ):
        """Misalignment publishes session_alignment_failed event."""
        session_store.create(
            user_id="user-1",
            session_id="parent-ev",
            intention="Implement feature X",
        )
        session_store.create(
            user_id="user-1",
            session_id="child-ev",
            intention="Delete everything",
            parent_session_id="parent-ev",
        )

        barrier = _make_barrier(db, mock_llm_misaligned)
        mock_bus = MagicMock()
        barrier.set_event_bus(mock_bus)

        async def _test():
            await barrier.trigger("user-1", "child-ev")
            await barrier.wait("user-1", "child-ev")

        asyncio.run(_test())

        # EventBus should have been called with session_alignment_failed
        mock_bus.publish.assert_called_once()
        event = mock_bus.publish.call_args[0][0]
        assert event["type"] == "session_alignment_failed"
        assert event["session_id"] == "child-ev"
        assert event["user_id"] == "user-1"
        assert event["parent_session_id"] == "parent-ev"
        assert "reasoning" in event

    def test_metrics(self, db, mock_llm):
        """metrics() returns the expected keys."""
        barrier = _make_barrier(db, mock_llm)
        m = barrier.metrics()
        assert "wait_count" in m
        assert "timeout_count" in m
        assert "check_count" in m
        assert "misaligned_count" in m
        assert "check_errors" in m
        assert "pending" in m


class TestAlignmentBarrierAcknowledge:
    """Tests for acknowledge(), clear_override(), clear_session(), restore_overrides()."""

    def test_acknowledge_clears_misalignment(
        self, db, mock_llm_misaligned, session_store
    ):
        """acknowledge() clears misalignment and prevents re-escalation."""
        session_store.create(
            user_id="user-1",
            session_id="parent-ack",
            intention="Build a web app",
        )
        session_store.create(
            user_id="user-1",
            session_id="child-ack",
            intention="Delete everything",
            parent_session_id="parent-ack",
        )

        barrier = _make_barrier(db, mock_llm_misaligned)

        async def _test():
            await barrier.trigger("user-1", "child-ack")
            await barrier.wait("user-1", "child-ack")

        asyncio.run(_test())

        # Misalignment should be stored
        assert barrier.is_misaligned("user-1", "child-ack") is not None

        # Acknowledge the misalignment
        barrier.acknowledge("user-1", "child-ack")

        # Misalignment should be cleared
        assert barrier.is_misaligned("user-1", "child-ack") is None

        # Verify DB persistence
        session = session_store.get("child-ack", user_id="user-1")
        assert session.get("alignment_overridden") == 1

    def test_acknowledge_prevents_re_escalation_after_recheck(
        self, db, mock_llm_misaligned, session_store
    ):
        """After acknowledge, a re-triggered misalignment check does not re-escalate."""
        session_store.create(
            user_id="user-1",
            session_id="parent-reack",
            intention="Build a web app",
        )
        session_store.create(
            user_id="user-1",
            session_id="child-reack",
            intention="Delete everything",
            parent_session_id="parent-reack",
        )

        barrier = _make_barrier(db, mock_llm_misaligned)

        async def _test():
            await barrier.trigger("user-1", "child-reack")
            await barrier.wait("user-1", "child-reack")

        asyncio.run(_test())

        # Acknowledge
        barrier.acknowledge("user-1", "child-reack")

        # Re-trigger alignment check (e.g., from intention update)
        async def _recheck():
            await barrier.trigger("user-1", "child-reack")
            await barrier.wait("user-1", "child-reack")

        asyncio.run(_recheck())

        # Should still be overridden — no re-escalation
        assert barrier.is_misaligned("user-1", "child-reack") is None

    def test_clear_override_allows_re_escalation(
        self, db, mock_llm_misaligned, session_store
    ):
        """clear_override() removes the override so re-check can re-escalate."""
        session_store.create(
            user_id="user-1",
            session_id="parent-clr",
            intention="Build a web app",
        )
        session_store.create(
            user_id="user-1",
            session_id="child-clr",
            intention="Delete everything",
            parent_session_id="parent-clr",
        )

        barrier = _make_barrier(db, mock_llm_misaligned)

        async def _test():
            await barrier.trigger("user-1", "child-clr")
            await barrier.wait("user-1", "child-clr")

        asyncio.run(_test())

        # Acknowledge, then clear override
        barrier.acknowledge("user-1", "child-clr")
        assert barrier.is_misaligned("user-1", "child-clr") is None

        barrier.clear_override("user-1", "child-clr")

        # Verify DB persistence cleared
        session = session_store.get("child-clr", user_id="user-1")
        assert session.get("alignment_overridden") == 0

        # Re-trigger — should re-escalate since override is cleared
        async def _recheck():
            await barrier.trigger("user-1", "child-clr")
            await barrier.wait("user-1", "child-clr")

        asyncio.run(_recheck())

        assert barrier.is_misaligned("user-1", "child-clr") is not None

    def test_clear_session_removes_all_state(
        self, db, mock_llm_misaligned, session_store
    ):
        """clear_session() removes both misalignment and override state."""
        session_store.create(
            user_id="user-1",
            session_id="parent-cls",
            intention="Build a web app",
        )
        session_store.create(
            user_id="user-1",
            session_id="child-cls",
            intention="Delete everything",
            parent_session_id="parent-cls",
        )

        barrier = _make_barrier(db, mock_llm_misaligned)

        async def _test():
            await barrier.trigger("user-1", "child-cls")
            await barrier.wait("user-1", "child-cls")

        asyncio.run(_test())

        # Acknowledge to add to overridden set
        barrier.acknowledge("user-1", "child-cls")

        # Clear session — should remove from both dicts
        barrier.clear_session("user-1", "child-cls")

        # Verify both are gone
        assert barrier.is_misaligned("user-1", "child-cls") is None
        m = barrier.metrics()
        assert m["overridden_sessions"] == 0

    def test_restore_overrides_from_db(self, db, session_store):
        """restore_overrides() loads acknowledged sessions from DB on startup."""
        from intaris.alignment import AlignmentBarrier

        session_store.create(
            user_id="user-1",
            session_id="parent-rst",
            intention="Build a web app",
        )
        session_store.create(
            user_id="user-1",
            session_id="child-rst",
            intention="Something else",
            parent_session_id="parent-rst",
        )

        # Manually set alignment_overridden in DB
        session_store.set_alignment_overridden(
            "child-rst", user_id="user-1", overridden=True
        )

        # Create a fresh barrier (simulating server restart)
        mock_llm = MagicMock()
        barrier = AlignmentBarrier(db=db, llm=mock_llm, timeout_ms=5000)

        count = barrier.restore_overrides()
        assert count == 1

        # The session should be in the overridden set
        assert barrier.is_misaligned("user-1", "child-rst") is None
        m = barrier.metrics()
        assert m["overridden_sessions"] == 1

    def test_no_event_published_when_overridden(
        self, db, mock_llm_misaligned, session_store
    ):
        """No event is published when misalignment is detected but user already acknowledged."""
        session_store.create(
            user_id="user-1",
            session_id="parent-noev",
            intention="Build a web app",
        )
        session_store.create(
            user_id="user-1",
            session_id="child-noev",
            intention="Delete everything",
            parent_session_id="parent-noev",
        )

        barrier = _make_barrier(db, mock_llm_misaligned)
        mock_bus = MagicMock()
        barrier.set_event_bus(mock_bus)

        # First trigger — misalignment detected, event published
        async def _first():
            await barrier.trigger("user-1", "child-noev")
            await barrier.wait("user-1", "child-noev")

        asyncio.run(_first())
        mock_bus.publish.assert_called_once()
        mock_bus.reset_mock()

        # Acknowledge
        barrier.acknowledge("user-1", "child-noev")

        # Re-trigger — misalignment detected again but overridden, no event
        async def _second():
            await barrier.trigger("user-1", "child-noev")
            await barrier.wait("user-1", "child-noev")

        asyncio.run(_second())
        mock_bus.publish.assert_not_called()


class TestAlignmentEscalation:
    """Tests for the evaluator returning escalate for misaligned sessions."""

    def test_evaluator_returns_escalate_for_misaligned(self, db, session_store):
        """Evaluator returns decision=escalate with path=alignment for misaligned sessions."""
        from intaris.audit import AuditStore
        from intaris.evaluator import Evaluator

        session_store.create(
            user_id="user-1",
            session_id="parent-esc",
            intention="Build feature X",
        )
        session_store.create(
            user_id="user-1",
            session_id="child-esc",
            intention="Delete everything",
            parent_session_id="parent-esc",
        )

        audit_store = AuditStore(db)
        mock_llm = MagicMock()

        # Create a barrier with misalignment pre-loaded
        from intaris.alignment import AlignmentBarrier

        barrier = AlignmentBarrier(db=db, llm=mock_llm, timeout_ms=5000)
        # Manually inject misalignment
        barrier._misaligned[("user-1", "child-esc")] = (
            "Child session misaligned with parent: intentions conflict"
        )

        evaluator = Evaluator(
            llm=mock_llm,
            session_store=session_store,
            audit_store=audit_store,
            alignment_barrier=barrier,
        )

        result = evaluator.evaluate(
            user_id="user-1",
            session_id="child-esc",
            agent_id=None,
            tool="bash",
            args={"command": "rm -rf /"},
        )

        assert result["decision"] == "escalate"
        assert result["path"] == "alignment"
        assert result["risk"] == "high"
        assert "misaligned" in result["reasoning"].lower()

    def test_evaluator_normal_after_acknowledge(self, db, session_store):
        """After acknowledge, evaluator proceeds with normal evaluation."""
        from intaris.audit import AuditStore
        from intaris.evaluator import Evaluator

        session_store.create(
            user_id="user-1",
            session_id="parent-norm",
            intention="Build feature X",
        )
        session_store.create(
            user_id="user-1",
            session_id="child-norm",
            intention="Help with feature X",
            parent_session_id="parent-norm",
        )

        audit_store = AuditStore(db)
        mock_llm = MagicMock()

        from intaris.alignment import AlignmentBarrier

        barrier = AlignmentBarrier(db=db, llm=mock_llm, timeout_ms=5000)
        # Inject misalignment then acknowledge
        barrier._misaligned[("user-1", "child-norm")] = "Some misalignment"
        barrier.acknowledge("user-1", "child-norm")

        evaluator = Evaluator(
            llm=mock_llm,
            session_store=session_store,
            audit_store=audit_store,
            alignment_barrier=barrier,
        )

        # Read-only tool → fast-path approve (no LLM needed)
        result = evaluator.evaluate(
            user_id="user-1",
            session_id="child-norm",
            agent_id=None,
            tool="read",
            args={"path": "/tmp/test.txt"},
        )

        assert result["decision"] == "approve"
        assert result["path"] == "fast"


# ── Parent lifecycle cascade tests ────────────────────────────────────


class TestParentLifecycleCascade:
    """Tests for the evaluator's parent lifecycle cascade behavior.

    When a parent session is terminated/suspended, child sessions should
    be auto-suspended on the next evaluate call.
    """

    def test_child_suspended_when_parent_terminated(self, db, session_store):
        """Evaluating a child session when parent is terminated → auto-suspend."""
        from unittest.mock import MagicMock

        from intaris.audit import AuditStore
        from intaris.evaluator import Evaluator

        session_store.create(
            user_id="user-1",
            session_id="parent-lc",
            intention="Build feature X",
        )
        session_store.create(
            user_id="user-1",
            session_id="child-lc",
            intention="Help with feature X",
            parent_session_id="parent-lc",
        )

        # Terminate the parent
        session_store.update_status("parent-lc", "terminated", user_id="user-1")

        audit_store = AuditStore(db)
        mock_llm = MagicMock()
        evaluator = Evaluator(
            llm=mock_llm,
            session_store=session_store,
            audit_store=audit_store,
        )

        result = evaluator.evaluate(
            user_id="user-1",
            session_id="child-lc",
            agent_id=None,
            tool="read",
            args={"path": "/tmp/test.txt"},
        )

        assert result["decision"] == "deny"
        assert result["session_status"] == "suspended"
        assert "Parent session is terminated" in result["status_reason"]

        # Verify the child session was actually suspended in the DB
        child = session_store.get("child-lc", user_id="user-1")
        assert child["status"] == "suspended"
        assert "Parent session is terminated" in child["status_reason"]

    def test_child_suspended_when_parent_suspended(self, db, session_store):
        """Evaluating a child session when parent is suspended → auto-suspend."""
        from unittest.mock import MagicMock

        from intaris.audit import AuditStore
        from intaris.evaluator import Evaluator

        session_store.create(
            user_id="user-1",
            session_id="parent-lc2",
            intention="Build feature Y",
        )
        session_store.create(
            user_id="user-1",
            session_id="child-lc2",
            intention="Help with feature Y",
            parent_session_id="parent-lc2",
        )

        # Suspend the parent
        session_store.update_status("parent-lc2", "suspended", user_id="user-1")

        audit_store = AuditStore(db)
        mock_llm = MagicMock()
        evaluator = Evaluator(
            llm=mock_llm,
            session_store=session_store,
            audit_store=audit_store,
        )

        result = evaluator.evaluate(
            user_id="user-1",
            session_id="child-lc2",
            agent_id=None,
            tool="bash",
            args={"command": "ls"},
        )

        assert result["decision"] == "deny"
        assert result["session_status"] == "suspended"
        assert "Parent session is suspended" in result["status_reason"]

    def test_child_normal_when_parent_active(self, db, session_store):
        """Evaluating a child session with active parent → normal evaluation."""
        from unittest.mock import MagicMock

        from intaris.audit import AuditStore
        from intaris.evaluator import Evaluator

        session_store.create(
            user_id="user-1",
            session_id="parent-lc3",
            intention="Build feature Z",
        )
        session_store.create(
            user_id="user-1",
            session_id="child-lc3",
            intention="Help with feature Z",
            parent_session_id="parent-lc3",
        )

        audit_store = AuditStore(db)
        mock_llm = MagicMock()
        evaluator = Evaluator(
            llm=mock_llm,
            session_store=session_store,
            audit_store=audit_store,
        )

        # Read-only tool → fast-path approve (no LLM needed)
        result = evaluator.evaluate(
            user_id="user-1",
            session_id="child-lc3",
            agent_id=None,
            tool="read",
            args={"path": "/tmp/test.txt"},
        )

        assert result["decision"] == "approve"
        assert result["path"] == "fast"
        # No session_status in the response for normal evaluations
        assert result.get("session_status") is None


# ── IntentionBarrier → AlignmentBarrier chain test ────────────────────


class TestBarrierChaining:
    """Test that IntentionBarrier chains to AlignmentBarrier for child sessions."""

    def test_intention_update_triggers_alignment_recheck(self, db, session_store):
        """After IntentionBarrier updates a child's intention,
        the AlignmentBarrier is triggered for re-check."""

        from intaris.intention import IntentionBarrier

        session_store.create(
            user_id="user-1",
            session_id="parent-chain",
            intention="Build authentication module",
        )
        session_store.create(
            user_id="user-1",
            session_id="child-chain",
            intention="Explore auth libraries",
            parent_session_id="parent-chain",
        )

        # Insert some audit records so generate_intention has data
        from intaris.audit import AuditStore

        audit_store = AuditStore(db)
        audit_store.insert(
            call_id="chain-1",
            user_id="user-1",
            session_id="child-chain",
            agent_id=None,
            tool="read",
            args_redacted={"path": "/tmp/test"},
            classification="read",
            evaluation_path="fast",
            decision="approve",
            risk="low",
            reasoning=None,
            latency_ms=5,
            record_type="reasoning",
            content="User message: I want to research JWT libraries",
        )

        # Create a mock LLM for intention generation
        intention_llm = MagicMock()
        intention_llm.generate.return_value = (
            "Researching JWT authentication libraries for the auth module"
        )

        # Create a mock alignment barrier
        mock_alignment_barrier = MagicMock()
        mock_alignment_triggered = False

        async def mock_trigger(user_id, session_id):
            nonlocal mock_alignment_triggered
            mock_alignment_triggered = True

        mock_alignment_barrier.trigger = mock_trigger

        barrier = IntentionBarrier(db=db, llm=intention_llm, timeout_ms=5000)
        barrier.set_alignment_barrier(mock_alignment_barrier)

        async def _test():
            await barrier.trigger("user-1", "child-chain")
            await barrier.wait("user-1", "child-chain")

        asyncio.run(_test())

        # The alignment barrier should have been triggered
        assert mock_alignment_triggered, (
            "AlignmentBarrier.trigger() was not called after "
            "IntentionBarrier updated a child session's intention"
        )
