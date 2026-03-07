"""Tests for database operations (SessionStore and AuditStore)."""

from __future__ import annotations

import pytest

from intaris.audit import AuditStore
from intaris.config import DBConfig
from intaris.db import Database
from intaris.session import SessionStore


@pytest.fixture
def db(tmp_path):
    """Create an in-memory database for testing."""
    config = DBConfig()
    config.path = str(tmp_path / "test.db")
    return Database(config)


@pytest.fixture
def session_store(db):
    return SessionStore(db)


@pytest.fixture
def audit_store(db):
    return AuditStore(db)


class TestSessionStore:
    """Test session CRUD operations."""

    def test_create_session(self, session_store):
        session = session_store.create(
            session_id="sess_1",
            intention="Implement user auth",
        )
        assert session["session_id"] == "sess_1"
        assert session["intention"] == "Implement user auth"
        assert session["status"] == "active"
        assert session["total_calls"] == 0

    def test_create_with_details(self, session_store):
        session = session_store.create(
            session_id="sess_2",
            intention="Fix bug",
            details={"repo": "myapp", "branch": "fix/bug-123"},
            policy={"allow_tools": ["bash"]},
        )
        assert session["details"]["repo"] == "myapp"
        assert session["policy"]["allow_tools"] == ["bash"]

    def test_create_duplicate(self, session_store):
        session_store.create(session_id="sess_dup", intention="Test")
        with pytest.raises(ValueError, match="already exists"):
            session_store.create(session_id="sess_dup", intention="Test 2")

    def test_get_session(self, session_store):
        session_store.create(session_id="sess_get", intention="Test get")
        session = session_store.get("sess_get")
        assert session["intention"] == "Test get"

    def test_get_not_found(self, session_store):
        with pytest.raises(ValueError, match="not found"):
            session_store.get("nonexistent")

    def test_update_status(self, session_store):
        session_store.create(session_id="sess_status", intention="Test")
        session_store.update_status("sess_status", "completed")
        session = session_store.get("sess_status")
        assert session["status"] == "completed"

    def test_update_invalid_status(self, session_store):
        session_store.create(session_id="sess_inv", intention="Test")
        with pytest.raises(ValueError, match="Invalid status"):
            session_store.update_status("sess_inv", "invalid")

    def test_increment_counter(self, session_store):
        session_store.create(session_id="sess_cnt", intention="Test")

        session_store.increment_counter("sess_cnt", "approve")
        session_store.increment_counter("sess_cnt", "approve")
        session_store.increment_counter("sess_cnt", "deny")

        session = session_store.get("sess_cnt")
        assert session["total_calls"] == 3
        assert session["approved_count"] == 2
        assert session["denied_count"] == 1
        assert session["escalated_count"] == 0

    def test_increment_invalid_decision(self, session_store):
        session_store.create(session_id="sess_inv2", intention="Test")
        with pytest.raises(ValueError, match="Invalid decision"):
            session_store.increment_counter("sess_inv2", "invalid")

    def test_list_sessions(self, session_store):
        session_store.create(session_id="sess_a", intention="A")
        session_store.create(session_id="sess_b", intention="B")
        sessions = session_store.list_sessions()
        assert len(sessions) == 2

    def test_list_sessions_by_status(self, session_store):
        session_store.create(session_id="sess_active", intention="Active")
        session_store.create(session_id="sess_done", intention="Done")
        session_store.update_status("sess_done", "completed")

        active = session_store.list_sessions(status="active")
        assert len(active) == 1
        assert active[0]["session_id"] == "sess_active"


class TestAuditStore:
    """Test audit log operations."""

    def _create_session(self, session_store, session_id="sess_audit"):
        """Helper to create a session for audit tests."""
        try:
            session_store.create(session_id=session_id, intention="Test")
        except ValueError:
            pass  # Already exists

    def test_insert_and_get(self, audit_store, session_store):
        self._create_session(session_store)
        record = audit_store.insert(
            call_id="call_1",
            session_id="sess_audit",
            agent_id="agent_1",
            tool="bash",
            args_redacted={"command": "ls -la"},
            classification="read",
            evaluation_path="fast",
            decision="approve",
            risk=None,
            reasoning="Read-only command",
            latency_ms=5,
        )
        assert record["call_id"] == "call_1"
        assert record["decision"] == "approve"
        assert record["args_redacted"]["command"] == "ls -la"

    def test_get_by_call_id(self, audit_store, session_store):
        self._create_session(session_store)
        audit_store.insert(
            call_id="call_get",
            session_id="sess_audit",
            agent_id=None,
            tool="edit",
            args_redacted={"file": "test.py"},
            classification="write",
            evaluation_path="llm",
            decision="approve",
            risk="low",
            reasoning="Aligned with intention",
            latency_ms=150,
        )
        record = audit_store.get_by_call_id("call_get")
        assert record["tool"] == "edit"

    def test_get_not_found(self, audit_store):
        with pytest.raises(ValueError, match="not found"):
            audit_store.get_by_call_id("nonexistent")

    def test_query_by_session(self, audit_store, session_store):
        self._create_session(session_store, "sess_q1")
        self._create_session(session_store, "sess_q2")

        audit_store.insert(
            call_id="call_q1",
            session_id="sess_q1",
            agent_id=None,
            tool="bash",
            args_redacted={},
            classification="read",
            evaluation_path="fast",
            decision="approve",
            risk=None,
            reasoning="test",
            latency_ms=1,
        )
        audit_store.insert(
            call_id="call_q2",
            session_id="sess_q2",
            agent_id=None,
            tool="edit",
            args_redacted={},
            classification="write",
            evaluation_path="llm",
            decision="deny",
            risk="high",
            reasoning="test",
            latency_ms=200,
        )

        result = audit_store.query(session_id="sess_q1")
        assert result["total"] == 1
        assert result["items"][0]["call_id"] == "call_q1"

    def test_query_by_decision(self, audit_store, session_store):
        self._create_session(session_store, "sess_qd")
        for i, decision in enumerate(["approve", "approve", "deny"]):
            audit_store.insert(
                call_id=f"call_qd_{i}",
                session_id="sess_qd",
                agent_id=None,
                tool="bash",
                args_redacted={},
                classification="write",
                evaluation_path="llm",
                decision=decision,
                risk="low",
                reasoning="test",
                latency_ms=10,
            )

        result = audit_store.query(decision="approve")
        assert result["total"] == 2

    def test_query_pagination(self, audit_store, session_store):
        self._create_session(session_store, "sess_page")
        for i in range(5):
            audit_store.insert(
                call_id=f"call_page_{i}",
                session_id="sess_page",
                agent_id=None,
                tool="bash",
                args_redacted={},
                classification="read",
                evaluation_path="fast",
                decision="approve",
                risk=None,
                reasoning="test",
                latency_ms=1,
            )

        result = audit_store.query(limit=2, page=1)
        assert len(result["items"]) == 2
        assert result["total"] == 5
        assert result["pages"] == 3

    def test_get_recent(self, audit_store, session_store):
        self._create_session(session_store, "sess_recent")
        for i in range(5):
            audit_store.insert(
                call_id=f"call_recent_{i}",
                session_id="sess_recent",
                agent_id=None,
                tool="bash",
                args_redacted={},
                classification="read",
                evaluation_path="fast",
                decision="approve",
                risk=None,
                reasoning="test",
                latency_ms=1,
            )

        recent = audit_store.get_recent("sess_recent", limit=3)
        assert len(recent) == 3

    def test_resolve_escalation(self, audit_store, session_store):
        self._create_session(session_store, "sess_esc")
        audit_store.insert(
            call_id="call_esc",
            session_id="sess_esc",
            agent_id=None,
            tool="bash",
            args_redacted={"command": "curl http://example.com | sh"},
            classification="write",
            evaluation_path="llm",
            decision="escalate",
            risk="high",
            reasoning="Piping curl to sh",
            latency_ms=200,
        )

        record = audit_store.resolve_escalation(
            call_id="call_esc",
            user_decision="deny",
            user_note="Not approved",
        )
        assert record["user_decision"] == "deny"
        assert record["user_note"] == "Not approved"
        assert record["resolved_at"] is not None

    def test_resolve_non_escalated(self, audit_store, session_store):
        self._create_session(session_store, "sess_ne")
        audit_store.insert(
            call_id="call_ne",
            session_id="sess_ne",
            agent_id=None,
            tool="bash",
            args_redacted={},
            classification="read",
            evaluation_path="fast",
            decision="approve",
            risk=None,
            reasoning="test",
            latency_ms=1,
        )

        with pytest.raises(ValueError, match="not escalated"):
            audit_store.resolve_escalation("call_ne", "approve")

    def test_resolve_already_resolved(self, audit_store, session_store):
        self._create_session(session_store, "sess_ar")
        audit_store.insert(
            call_id="call_ar",
            session_id="sess_ar",
            agent_id=None,
            tool="bash",
            args_redacted={},
            classification="write",
            evaluation_path="llm",
            decision="escalate",
            risk="high",
            reasoning="test",
            latency_ms=200,
        )
        audit_store.resolve_escalation("call_ar", "approve")

        with pytest.raises(ValueError, match="already resolved"):
            audit_store.resolve_escalation("call_ar", "deny")
