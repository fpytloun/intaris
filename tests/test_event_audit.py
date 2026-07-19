"""Tests for the relational session-event audit projection."""

from __future__ import annotations

from intaris.audit import AuditStore
from intaris.config import DBConfig, EventStoreConfig
from intaris.db import Database
from intaris.event_audit import EventAuditStore
from intaris.events.store import EventStore
from intaris.session import SessionStore


def _database(tmp_path) -> Database:
    return Database(DBConfig(backend="sqlite", path=str(tmp_path / "audit.db")))


def _session(
    db: Database,
    *,
    user_id: str = "alice",
    session_id: str = "sess-1",
    agent_id: str = "agent-1",
) -> None:
    SessionStore(db).create(
        user_id=user_id,
        session_id=session_id,
        intention="Test audit timeline",
        agent_id=agent_id,
    )


def test_indexes_only_tool_events_idempotently(tmp_path):
    db = _database(tmp_path)
    _session(db)
    store = EventAuditStore(db)
    events = [
        {
            "seq": 1,
            "ts": "2026-07-14T10:00:00+00:00",
            "type": "message",
            "source": "cognis",
            "data": {"content": "ignored"},
        },
        {
            "seq": 2,
            "ts": "2026-07-14T10:00:01+00:00",
            "type": "tool_call",
            "source": "cognis",
            "data": {
                "name": "delegate",
                "call_id": "call-1",
                "arguments": {"task": "must not be projected"},
            },
        },
        {
            "seq": 3,
            "ts": "2026-07-14T10:00:02+00:00",
            "type": "tool_result",
            "source": "cognis",
            "data": {
                "name": "delegate",
                "call_id": "call-1",
                "is_error": False,
                "duration_ms": 25,
                "result": "must not be projected",
            },
        },
    ]

    store.index_events(user_id="alice", session_id="sess-1", events=events)
    store.index_events(user_id="alice", session_id="sess-1", events=events)

    result = store.query(user_id="alice", source="events")
    assert result["total"] == 2
    assert [item["record_type"] for item in result["items"]] == [
        "tool_result",
        "tool_call",
    ]
    assert result["items"][0]["duration_ms"] == 25
    assert result["items"][0]["is_error"] is False
    assert "arguments" not in result["items"][1]
    assert "result" not in result["items"][0]


def test_unified_query_merges_sources_and_applies_common_filters(tmp_path):
    db = _database(tmp_path)
    _session(db)
    event_store = EventAuditStore(db)
    event_store.index_events(
        user_id="alice",
        session_id="sess-1",
        events=[
            {
                "seq": 1,
                "ts": "2026-07-14T10:00:02+00:00",
                "type": "tool_call",
                "source": "cognis",
                "data": {"name": "delegate", "call_id": "event-call"},
            }
        ],
    )
    audit = AuditStore(db)
    audit.insert(
        call_id="audit-call",
        user_id="alice",
        session_id="sess-1",
        agent_id="agent-1",
        tool="bash",
        args_redacted={"command": "pwd"},
        classification="read",
        evaluation_path="fast",
        decision="approve",
        risk="low",
        reasoning="Read-only",
        latency_ms=1,
    )
    with db.cursor() as cur:
        cur.execute(
            "UPDATE audit_log SET timestamp = ? WHERE call_id = ?",
            ("2026-07-14T10:00:01+00:00", "audit-call"),
        )

    result = event_store.query(user_id="alice", source="all")
    assert result["total"] == 2
    assert [item["source"] for item in result["items"]] == ["event", "evaluation"]

    tool_result = event_store.query(user_id="alice", source="all", tool="delegate")
    assert tool_result["total"] == 1
    assert tool_result["items"][0]["source"] == "event"

    decision_result = event_store.query(
        user_id="alice", source="all", decision="approve"
    )
    assert decision_result["total"] == 1
    assert decision_result["items"][0]["source"] == "evaluation"


def test_event_query_is_tenant_and_agent_scoped(tmp_path):
    db = _database(tmp_path)
    _session(db)
    _session(db, user_id="bob", session_id="sess-2", agent_id="agent-2")
    store = EventAuditStore(db)
    event = {
        "seq": 1,
        "ts": "2026-07-14T10:00:00+00:00",
        "type": "tool_call",
        "source": "cognis",
        "data": {"name": "switch_agent_profile", "call_id": "call-1"},
    }
    store.index_events(user_id="alice", session_id="sess-1", events=[event])
    store.index_events(user_id="bob", session_id="sess-2", events=[event])

    assert store.query(user_id="alice", source="events")["total"] == 1
    assert (
        store.query(user_id="alice", source="events", agent_id="agent-2")["total"] == 0
    )


def test_timestamp_filters_normalize_offsets_for_sqlite(tmp_path):
    db = _database(tmp_path)
    _session(db)
    store = EventAuditStore(db)
    store.index_events(
        user_id="alice",
        session_id="sess-1",
        events=[
            {
                "seq": 1,
                "ts": "2026-07-14T10:00:00+00:00",
                "type": "tool_call",
                "source": "cognis",
                "data": {"name": "delegate", "call_id": "call-1"},
            }
        ],
    )

    equal_offset = store.query(
        user_id="alice",
        source="events",
        from_ts="2026-07-14T11:00:00+01:00",
    )
    before = store.query(
        user_id="alice",
        source="events",
        to_ts="2026-07-14T09:59:59Z",
    )

    assert equal_offset["total"] == 1
    assert before["total"] == 0


def test_combined_pagination_is_deterministic(tmp_path):
    db = _database(tmp_path)
    _session(db)
    store = EventAuditStore(db)
    store.index_events(
        user_id="alice",
        session_id="sess-1",
        events=[
            {
                "seq": seq,
                "ts": "2026-07-14T10:00:00+00:00",
                "type": "tool_call",
                "source": "cognis",
                "data": {"name": "delegate", "call_id": f"call-{seq}"},
            }
            for seq in range(1, 11)
        ],
    )

    first = store.query(user_id="alice", source="events", page=1, limit=3)
    second = store.query(user_id="alice", source="events", page=2, limit=3)

    assert first["pages"] == 4
    assert [item["seq"] for item in first["items"]] == [10, 9, 8]
    assert [item["seq"] for item in second["items"]] == [7, 6, 5]


def test_reconciles_historical_durable_events(tmp_path):
    db = _database(tmp_path)
    _session(db)
    config = EventStoreConfig(
        enabled=True,
        backend="filesystem",
        filesystem_path=str(tmp_path / "events"),
        flush_size=100,
        flush_interval=30,
    )
    original = EventStore(config)
    original.append(
        "alice",
        "sess-1",
        [
            {"type": "message", "data": {"content": "before"}},
            {
                "type": "tool_call",
                "data": {"name": "delegate", "call_id": "historical-call"},
            },
        ],
        source="cognis",
    )
    original.flush_all()

    audit = EventAuditStore(db)
    restarted = EventStore(config)
    restarted.set_audit_store(audit)

    assert restarted.reconcile_audit_index() == 1
    assert restarted.reconcile_audit_index() == 0
    result = audit.query(user_id="alice", source="events")
    assert result["total"] == 1
    assert result["items"][0]["call_id"] == "historical-call"


def test_projection_failure_is_repaired_from_durable_events(tmp_path):
    db = _database(tmp_path)
    _session(db)
    config = EventStoreConfig(
        enabled=True,
        backend="filesystem",
        filesystem_path=str(tmp_path / "events"),
        flush_size=100,
        flush_interval=30,
    )

    class FailingProjection:
        def index_events(self, **kwargs):
            raise RuntimeError("database unavailable")

    store = EventStore(config)
    store.set_audit_store(FailingProjection())
    store.append(
        "alice",
        "sess-1",
        [
            {
                "type": "tool_call",
                "data": {"name": "delegate", "call_id": "repair-call"},
            }
        ],
        source="cognis",
    )
    assert len(EventStore(config).read("alice", "sess-1")) == 1

    audit = EventAuditStore(db)
    store.set_audit_store(audit)
    store.flush_all()
    assert audit.query(user_id="alice", source="events")["total"] == 1


def test_mixed_source_equal_timestamp_pages_do_not_overlap(tmp_path):
    db = _database(tmp_path)
    _session(db)
    store = EventAuditStore(db)
    timestamp = "2026-07-14T10:00:00+00:00"
    store.index_events(
        user_id="alice",
        session_id="sess-1",
        events=[
            {
                "seq": seq,
                "ts": timestamp,
                "type": "tool_call",
                "source": "cognis",
                "data": {"name": "delegate", "call_id": f"event-{seq}"},
            }
            for seq in range(1, 3)
        ],
    )
    audit = AuditStore(db)
    for index in range(2):
        audit.insert(
            call_id=f"audit-{index}",
            user_id="alice",
            session_id="sess-1",
            agent_id="agent-1",
            tool="bash",
            args_redacted={},
            classification="read",
            evaluation_path="fast",
            decision="approve",
            risk="low",
            reasoning="Read-only",
            latency_ms=1,
        )
    with db.cursor() as cur:
        cur.execute("UPDATE audit_log SET timestamp = ?", (timestamp,))

    first = store.query(user_id="alice", source="all", page=1, limit=2)
    second = store.query(user_id="alice", source="all", page=2, limit=2)

    first_ids = {item["id"] for item in first["items"]}
    second_ids = {item["id"] for item in second["items"]}
    assert len(first_ids | second_ids) == 4
    assert first_ids.isdisjoint(second_ids)
