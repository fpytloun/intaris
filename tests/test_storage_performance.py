"""Regression checks for linear storage work through the real spool boundary."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from intaris.config import DBConfig, EventStoreConfig
from intaris.db import Database
from intaris.event_audit import EventAuditStore
from intaris.events.store import EventStore
from intaris.session import SessionStore


@pytest.fixture
def storage(tmp_path):
    db = Database(DBConfig(path=str(tmp_path / "metadata.db")))
    sessions = SessionStore(db)
    sessions.create(user_id="tenant", session_id="session", intention="Test storage")
    config = EventStoreConfig(
        filesystem_path=str(tmp_path / "archive"),
        spool_path=str(tmp_path / "spool"),
        flush_size=2000,
    )
    store = EventStore(config)
    store.set_session_store(sessions)
    try:
        yield store, db
    finally:
        store.close()
        db.close()


def events(count):
    return [{"type": "tool_call", "data": {"tool": "read"}} for _ in range(count)]


def test_flush_projection_only_visits_new_events(storage, monkeypatch):
    store, db = storage
    audit = EventAuditStore(db)
    store.set_audit_store(audit)
    index = Mock(wraps=audit.index_events)
    monkeypatch.setattr(audit, "index_events", index)
    for _ in range(3):
        store.append("tenant", "session", events(100))
        store.flush_session("tenant", "session")
    assert [len(call.kwargs["events"]) for call in index.call_args_list] == [
        100,
        100,
        100,
    ]
    assert audit.projection_checkpoint(user_id="tenant", session_id="session") == 300


def test_projection_retry_resumes_after_last_committed_batch(storage, monkeypatch):
    store, db = storage
    audit = EventAuditStore(db)
    store.set_audit_store(audit)
    original = audit.index_events
    visited = []
    unavailable = True

    def fail_second_batch(**kwargs):
        visited.append(kwargs["events"][0]["seq"])
        if kwargs["events"][0]["seq"] > 1000 and unavailable:
            raise RuntimeError("Projection temporarily unavailable")
        return original(**kwargs)

    monkeypatch.setattr(audit, "index_events", fail_second_batch)
    store.append("tenant", "session", events(1500))
    store.flush_session("tenant", "session")
    assert audit.projection_checkpoint(user_id="tenant", session_id="session") == 1000
    unavailable = False
    store._reconcile_pending_audit()
    assert visited[0] == 1
    assert visited[1:] and set(visited[1:]) == {1001}
    assert audit.projection_checkpoint(user_id="tenant", session_id="session") == 1500


def test_stream_reads_archive_once_and_snapshots_pending(storage, monkeypatch):
    store, _ = storage
    store.append("tenant", "session", events(500))
    store.flush_session("tenant", "session")
    store.append("tenant", "session", events(2))
    archive = store._spool.backend
    stream = Mock(wraps=archive.read_stream)
    monkeypatch.setattr(archive, "read_stream", stream)
    monkeypatch.setattr(
        archive, "read", Mock(side_effect=AssertionError("Paginated archive read"))
    )
    iterator = store.read_stream("tenant", "session", after_seq=10)
    first = next(iterator)
    # Retire the pending files and admit new events during slow consumption.
    store.flush_session("tenant", "session")
    store.append("tenant", "session", events(3))
    result = [first, *iterator]
    assert [event["seq"] for event in result] == list(range(11, 503))
    stream.assert_called_once_with("tenant", "session", 10)


def test_lazy_stream_failure_fences_admission(storage, monkeypatch):
    store, _ = storage
    store.append("tenant", "session", events(2))
    store.flush_session("tenant", "session")
    archive = store._spool.backend
    original = archive.read_stream

    def broken_stream(*args):
        yield next(original(*args))
        raise OSError("Archive read interrupted")

    monkeypatch.setattr(archive, "read_stream", broken_stream)
    with pytest.raises(OSError, match="interrupted"):
        list(store.read_stream("tenant", "session"))
    assert not store._spool.metrics()["write_admission"]


def test_filtered_forward_read_does_not_restart_chunk_decoder(storage, monkeypatch):
    store, _ = storage
    rows = events(500)
    rows[-1] = {"type": "tool_result", "data": {"result": "last"}}
    store.append("tenant", "session", rows)
    store.flush_all()
    archive = store._spool.backend
    stream = Mock(wraps=archive.read_stream)
    monkeypatch.setattr(archive, "read_stream", stream)
    monkeypatch.setattr(
        archive, "read", Mock(side_effect=AssertionError("Repeated filtered read"))
    )
    result = store.read("tenant", "session", limit=1, event_types={"tool_result"})
    assert [event["seq"] for event in result] == [500]
    stream.assert_called_once_with("tenant", "session", 0)


def test_truncated_frame_cannot_advance_projection_checkpoint(storage):
    store, db = storage
    store.append("tenant", "session", events(1500))
    store.flush_all()
    chunk = next(store._spool.backend._base_path.rglob("*.zst"))
    payload = chunk.read_bytes()
    chunk.write_bytes(payload[:-1])
    audit = EventAuditStore(db)
    store.set_audit_store(audit)
    try:
        assert store.reconcile_audit_index() == 0
        assert audit.projection_checkpoint(user_id="tenant", session_id="session") == 0
    finally:
        chunk.write_bytes(payload)
