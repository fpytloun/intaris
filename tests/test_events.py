"""Tests for the session event recording system.

Tests cover:
- Backend helpers (chunk filename, ndjson serialization, path validation)
- FilesystemEventBackend (append, read, read_stream, last_seq, delete, exists)
- EventStore (append, read, flush, seq assignment, buffer management, EventBus)
- EventStoreConfig validation
"""

from __future__ import annotations

import json
import threading

import pytest

from intaris.config import EventStoreConfig
from intaris.events.backend import (
    FilesystemEventBackend,
    _chunk_filename,
    _events_to_ndjson,
    _ndjson_to_events,
    _parse_chunk_filename,
    _validate_path_component,
)
from intaris.events.store import VALID_EVENT_TYPES, EventStore


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def fs_config(tmp_path):
    """EventStoreConfig pointing at a temp directory."""
    return EventStoreConfig(
        enabled=True,
        backend="filesystem",
        filesystem_path=str(tmp_path / "events"),
        flush_size=5,
        flush_interval=30,
    )


@pytest.fixture
def backend(fs_config):
    """FilesystemEventBackend instance."""
    return FilesystemEventBackend(fs_config)


@pytest.fixture
def store(fs_config):
    """EventStore with filesystem backend and small flush_size for testing."""
    return EventStore(fs_config)


# ── Backend helpers ───────────────────────────────────────────────────


class TestChunkFilename:
    """Tests for chunk filename generation and parsing."""

    def test_generate_basic(self):
        assert _chunk_filename(1, 100) == "seq_000001_000100.ndjson"

    def test_generate_large_numbers(self):
        assert _chunk_filename(1000000, 2000000) == "seq_1000000_2000000.ndjson"

    def test_generate_single_event(self):
        assert _chunk_filename(42, 42) == "seq_000042_000042.ndjson"

    def test_parse_valid(self):
        assert _parse_chunk_filename("seq_000001_000100.ndjson") == (1, 100)

    def test_parse_large_numbers(self):
        assert _parse_chunk_filename("seq_1000000_2000000.ndjson") == (1000000, 2000000)

    def test_parse_invalid_returns_none(self):
        assert _parse_chunk_filename("not_a_chunk.txt") is None
        assert _parse_chunk_filename("seq_abc_def.ndjson") is None
        assert _parse_chunk_filename("") is None

    def test_roundtrip(self):
        filename = _chunk_filename(7, 42)
        parsed = _parse_chunk_filename(filename)
        assert parsed == (7, 42)


class TestNdjsonSerialization:
    """Tests for ndjson serialization/deserialization."""

    def test_single_event(self):
        events = [{"seq": 1, "type": "message", "data": {"text": "hello"}}]
        data = _events_to_ndjson(events)
        result = _ndjson_to_events(data)
        assert result == events

    def test_multiple_events(self):
        events = [
            {"seq": 1, "type": "message"},
            {"seq": 2, "type": "tool_call"},
            {"seq": 3, "type": "evaluation"},
        ]
        data = _events_to_ndjson(events)
        result = _ndjson_to_events(data)
        assert result == events

    def test_empty_list(self):
        data = _events_to_ndjson([])
        # Empty list produces just a newline
        result = _ndjson_to_events(data)
        assert result == []

    def test_preserves_nested_data(self):
        events = [
            {
                "seq": 1,
                "type": "tool_call",
                "data": {"args": {"path": "/foo/bar", "nested": {"a": [1, 2, 3]}}},
            }
        ]
        data = _events_to_ndjson(events)
        result = _ndjson_to_events(data)
        assert result[0]["data"]["args"]["nested"]["a"] == [1, 2, 3]

    def test_handles_unicode(self):
        events = [{"seq": 1, "type": "message", "data": {"text": "héllo wörld 🌍"}}]
        data = _events_to_ndjson(events)
        result = _ndjson_to_events(data)
        assert result[0]["data"]["text"] == "héllo wörld 🌍"


class TestPathValidation:
    """Tests for path component validation."""

    def test_valid_components(self):
        _validate_path_component("alice", "user_id")
        _validate_path_component("sess-123", "session_id")
        _validate_path_component("user@example.com", "user_id")
        _validate_path_component("a.b.c", "user_id")
        _validate_path_component("user:agent", "user_id")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            _validate_path_component("", "user_id")

    def test_too_long_raises(self):
        with pytest.raises(ValueError, match="too long"):
            _validate_path_component("a" * 257, "user_id")

    def test_path_traversal_raises(self):
        with pytest.raises(ValueError, match="must not contain"):
            _validate_path_component("../etc/passwd", "user_id")

    def test_invalid_chars_raises(self):
        with pytest.raises(ValueError, match="invalid characters"):
            _validate_path_component("user id", "user_id")  # space
        with pytest.raises(ValueError, match="invalid characters"):
            _validate_path_component("user\x00id", "user_id")  # null byte


# ── FilesystemEventBackend ────────────────────────────────────────────


class TestFilesystemEventBackend:
    """Tests for the filesystem storage backend."""

    def test_append_and_read(self, backend):
        events = [
            {"seq": 1, "ts": "2026-01-01T00:00:00Z", "type": "message", "data": {}},
            {"seq": 2, "ts": "2026-01-01T00:00:01Z", "type": "tool_call", "data": {}},
        ]
        backend.append("alice", "sess1", events)

        result = backend.read("alice", "sess1")
        assert len(result) == 2
        assert result[0]["seq"] == 1
        assert result[1]["seq"] == 2

    def test_read_empty_session(self, backend):
        result = backend.read("alice", "nonexistent")
        assert result == []

    def test_read_after_seq(self, backend):
        events = [
            {"seq": 1, "ts": "t", "type": "message", "data": {}},
            {"seq": 2, "ts": "t", "type": "message", "data": {}},
            {"seq": 3, "ts": "t", "type": "message", "data": {}},
        ]
        backend.append("alice", "sess1", events)

        result = backend.read("alice", "sess1", after_seq=1)
        assert len(result) == 2
        assert result[0]["seq"] == 2
        assert result[1]["seq"] == 3

    def test_read_with_limit(self, backend):
        events = [
            {"seq": i, "ts": "t", "type": "message", "data": {}} for i in range(1, 11)
        ]
        backend.append("alice", "sess1", events)

        result = backend.read("alice", "sess1", limit=3)
        assert len(result) == 3
        assert result[0]["seq"] == 1
        assert result[2]["seq"] == 3

    def test_read_after_seq_with_limit(self, backend):
        events = [
            {"seq": i, "ts": "t", "type": "message", "data": {}} for i in range(1, 11)
        ]
        backend.append("alice", "sess1", events)

        result = backend.read("alice", "sess1", after_seq=5, limit=2)
        assert len(result) == 2
        assert result[0]["seq"] == 6
        assert result[1]["seq"] == 7

    def test_read_across_chunks(self, backend):
        """Reading works across multiple chunk files."""
        chunk1 = [
            {"seq": 1, "ts": "t", "type": "message", "data": {}},
            {"seq": 2, "ts": "t", "type": "message", "data": {}},
        ]
        chunk2 = [
            {"seq": 3, "ts": "t", "type": "message", "data": {}},
            {"seq": 4, "ts": "t", "type": "message", "data": {}},
        ]
        backend.append("alice", "sess1", chunk1)
        backend.append("alice", "sess1", chunk2)

        result = backend.read("alice", "sess1")
        assert len(result) == 4
        assert [e["seq"] for e in result] == [1, 2, 3, 4]

    def test_read_after_seq_skips_entire_chunks(self, backend):
        """Chunks entirely before after_seq are skipped."""
        chunk1 = [
            {"seq": i, "ts": "t", "type": "message", "data": {}} for i in range(1, 4)
        ]
        chunk2 = [
            {"seq": i, "ts": "t", "type": "message", "data": {}} for i in range(4, 7)
        ]
        backend.append("alice", "sess1", chunk1)
        backend.append("alice", "sess1", chunk2)

        result = backend.read("alice", "sess1", after_seq=3)
        assert len(result) == 3
        assert result[0]["seq"] == 4

    def test_read_stream(self, backend):
        events = [
            {"seq": i, "ts": "t", "type": "message", "data": {}} for i in range(1, 6)
        ]
        backend.append("alice", "sess1", events)

        result = list(backend.read_stream("alice", "sess1"))
        assert len(result) == 5
        assert [e["seq"] for e in result] == [1, 2, 3, 4, 5]

    def test_read_stream_after_seq(self, backend):
        events = [
            {"seq": i, "ts": "t", "type": "message", "data": {}} for i in range(1, 6)
        ]
        backend.append("alice", "sess1", events)

        result = list(backend.read_stream("alice", "sess1", after_seq=3))
        assert len(result) == 2
        assert result[0]["seq"] == 4

    def test_last_seq_empty(self, backend):
        assert backend.last_seq("alice", "nonexistent") == 0

    def test_last_seq_with_data(self, backend):
        events = [
            {"seq": 1, "ts": "t", "type": "message", "data": {}},
            {"seq": 2, "ts": "t", "type": "message", "data": {}},
        ]
        backend.append("alice", "sess1", events)
        assert backend.last_seq("alice", "sess1") == 2

    def test_last_seq_multiple_chunks(self, backend):
        backend.append(
            "alice", "sess1", [{"seq": 1, "ts": "t", "type": "message", "data": {}}]
        )
        backend.append(
            "alice", "sess1", [{"seq": 5, "ts": "t", "type": "message", "data": {}}]
        )
        assert backend.last_seq("alice", "sess1") == 5

    def test_exists_false(self, backend):
        assert backend.exists("alice", "nonexistent") is False

    def test_exists_true(self, backend):
        events = [{"seq": 1, "ts": "t", "type": "message", "data": {}}]
        backend.append("alice", "sess1", events)
        assert backend.exists("alice", "sess1") is True

    def test_delete_session(self, backend):
        events = [{"seq": 1, "ts": "t", "type": "message", "data": {}}]
        backend.append("alice", "sess1", events)
        assert backend.exists("alice", "sess1") is True

        backend.delete_session("alice", "sess1")
        assert backend.exists("alice", "sess1") is False
        assert backend.read("alice", "sess1") == []

    def test_delete_session_nonexistent_is_safe(self, backend):
        # Should not raise
        backend.delete_session("alice", "nonexistent")

    def test_delete_all_for_user(self, backend):
        backend.append(
            "alice", "sess1", [{"seq": 1, "ts": "t", "type": "message", "data": {}}]
        )
        backend.append(
            "alice", "sess2", [{"seq": 1, "ts": "t", "type": "message", "data": {}}]
        )
        backend.append(
            "bob", "sess1", [{"seq": 1, "ts": "t", "type": "message", "data": {}}]
        )

        backend.delete_all_for_user("alice")

        assert backend.exists("alice", "sess1") is False
        assert backend.exists("alice", "sess2") is False
        assert backend.exists("bob", "sess1") is True

    def test_append_empty_is_noop(self, backend):
        backend.append("alice", "sess1", [])
        assert backend.exists("alice", "sess1") is False

    def test_path_traversal_blocked(self, backend):
        with pytest.raises(ValueError, match="must not contain"):
            backend.append(
                "../etc",
                "sess1",
                [{"seq": 1, "ts": "t", "type": "message", "data": {}}],
            )

    def test_tenant_isolation(self, backend):
        """Alice cannot read Bob's events."""
        backend.append(
            "alice",
            "sess1",
            [{"seq": 1, "ts": "t", "type": "message", "data": {"user": "alice"}}],
        )
        backend.append(
            "bob",
            "sess1",
            [{"seq": 1, "ts": "t", "type": "message", "data": {"user": "bob"}}],
        )

        alice_events = backend.read("alice", "sess1")
        bob_events = backend.read("bob", "sess1")

        assert len(alice_events) == 1
        assert alice_events[0]["data"]["user"] == "alice"
        assert len(bob_events) == 1
        assert bob_events[0]["data"]["user"] == "bob"


# ── EventStore ────────────────────────────────────────────────────────


class TestEventStore:
    """Tests for the high-level EventStore."""

    def test_append_assigns_seq(self, store):
        seqs = store.append(
            "alice",
            "sess1",
            [
                {"type": "message", "data": {}},
                {"type": "tool_call", "data": {}},
            ],
        )
        assert seqs == [1, 2]

    def test_append_assigns_ts(self, store):
        store.append("alice", "sess1", [{"type": "message", "data": {}}])
        events = store.read("alice", "sess1")
        assert "ts" in events[0]
        assert events[0]["ts"].endswith("Z") or "+" in events[0]["ts"]

    def test_append_assigns_source(self, store):
        store.append(
            "alice", "sess1", [{"type": "message", "data": {}}], source="opencode"
        )
        events = store.read("alice", "sess1")
        assert events[0]["source"] == "opencode"

    def test_append_default_source(self, store):
        store.append("alice", "sess1", [{"type": "message", "data": {}}])
        events = store.read("alice", "sess1")
        assert events[0]["source"] == "intaris"

    def test_append_empty_returns_empty(self, store):
        seqs = store.append("alice", "sess1", [])
        assert seqs == []

    def test_seq_monotonic_across_appends(self, store):
        seqs1 = store.append("alice", "sess1", [{"type": "message", "data": {}}])
        seqs2 = store.append("alice", "sess1", [{"type": "message", "data": {}}])
        assert seqs1 == [1]
        assert seqs2 == [2]

    def test_seq_independent_per_session(self, store):
        seqs1 = store.append("alice", "sess1", [{"type": "message", "data": {}}])
        seqs2 = store.append("alice", "sess2", [{"type": "message", "data": {}}])
        assert seqs1 == [1]
        assert seqs2 == [1]

    def test_read_returns_buffered_events(self, store):
        """Events are readable before flush (from buffer)."""
        store.append(
            "alice",
            "sess1",
            [
                {"type": "message", "data": {"text": "hello"}},
            ],
        )
        # flush_size=5, so this is still buffered
        events = store.read("alice", "sess1")
        assert len(events) == 1
        assert events[0]["data"]["text"] == "hello"

    def test_read_after_seq(self, store):
        store.append(
            "alice",
            "sess1",
            [
                {"type": "message", "data": {}},
                {"type": "tool_call", "data": {}},
                {"type": "evaluation", "data": {}},
            ],
        )
        events = store.read("alice", "sess1", after_seq=1)
        assert len(events) == 2
        assert events[0]["seq"] == 2

    def test_read_with_limit(self, store):
        store.append(
            "alice",
            "sess1",
            [
                {"type": "message", "data": {}},
                {"type": "tool_call", "data": {}},
                {"type": "evaluation", "data": {}},
            ],
        )
        events = store.read("alice", "sess1", limit=2)
        assert len(events) == 2

    def test_read_with_type_filter(self, store):
        store.append(
            "alice",
            "sess1",
            [
                {"type": "message", "data": {}},
                {"type": "tool_call", "data": {}},
                {"type": "evaluation", "data": {}},
                {"type": "message", "data": {}},
            ],
        )
        events = store.read("alice", "sess1", event_types={"message"})
        assert len(events) == 2
        assert all(e["type"] == "message" for e in events)

    def test_read_with_multiple_type_filter(self, store):
        store.append(
            "alice",
            "sess1",
            [
                {"type": "message", "data": {}},
                {"type": "tool_call", "data": {}},
                {"type": "evaluation", "data": {}},
            ],
        )
        events = store.read("alice", "sess1", event_types={"message", "evaluation"})
        assert len(events) == 2

    def test_auto_flush_on_threshold(self, store):
        """Buffer is flushed when flush_size is reached."""
        # flush_size=5, append 5 events to trigger flush
        store.append(
            "alice", "sess1", [{"type": "message", "data": {}} for _ in range(5)]
        )
        # Buffer should be empty after flush
        assert store.buffered_event_count == 0
        # Events should still be readable (from backend)
        events = store.read("alice", "sess1")
        assert len(events) == 5

    def test_manual_flush(self, store):
        store.append(
            "alice",
            "sess1",
            [
                {"type": "message", "data": {}},
                {"type": "message", "data": {}},
            ],
        )
        assert store.buffered_event_count == 2

        store.flush_session("alice", "sess1")
        assert store.buffered_event_count == 0

        # Events still readable from backend
        events = store.read("alice", "sess1")
        assert len(events) == 2

    def test_flush_all(self, store):
        store.append("alice", "sess1", [{"type": "message", "data": {}}])
        store.append("alice", "sess2", [{"type": "message", "data": {}}])
        store.append("bob", "sess1", [{"type": "message", "data": {}}])

        assert store.buffered_session_count == 3
        store.flush_all()
        assert store.buffered_session_count == 0
        assert store.buffered_event_count == 0

    def test_read_combines_backend_and_buffer(self, store):
        """Read returns events from both flushed backend and unflushed buffer."""
        # Append 5 events to trigger flush (flush_size=5)
        store.append(
            "alice", "sess1", [{"type": "message", "data": {}} for _ in range(5)]
        )
        # Append 2 more (still in buffer)
        store.append(
            "alice", "sess1", [{"type": "tool_call", "data": {}} for _ in range(2)]
        )

        events = store.read("alice", "sess1")
        assert len(events) == 7
        assert [e["seq"] for e in events] == [1, 2, 3, 4, 5, 6, 7]

    def test_read_deduplicates(self, store):
        """Events are not duplicated when present in both backend and buffer."""
        # This tests the dedup logic in read()
        store.append(
            "alice", "sess1", [{"type": "message", "data": {}} for _ in range(5)]
        )
        # After auto-flush, buffer is empty. Append more.
        store.append(
            "alice", "sess1", [{"type": "message", "data": {}} for _ in range(3)]
        )

        events = store.read("alice", "sess1")
        seqs = [e["seq"] for e in events]
        # No duplicates
        assert len(seqs) == len(set(seqs))
        assert seqs == [1, 2, 3, 4, 5, 6, 7, 8]

    def test_last_seq_from_buffer(self, store):
        store.append("alice", "sess1", [{"type": "message", "data": {}}])
        assert store.last_seq("alice", "sess1") == 1

    def test_last_seq_from_backend(self, store):
        store.append(
            "alice", "sess1", [{"type": "message", "data": {}} for _ in range(5)]
        )
        # After auto-flush, seq counter is in memory
        assert store.last_seq("alice", "sess1") == 5

    def test_last_seq_empty(self, store):
        assert store.last_seq("alice", "nonexistent") == 0

    def test_seq_recovery_from_backend(self, fs_config):
        """Sequence counter is recovered from backend on first append."""
        # Create a store and write some events
        store1 = EventStore(fs_config)
        store1.append(
            "alice", "sess1", [{"type": "message", "data": {}} for _ in range(5)]
        )
        store1.flush_all()

        # Create a new store (simulating restart)
        store2 = EventStore(fs_config)
        seqs = store2.append("alice", "sess1", [{"type": "message", "data": {}}])
        # Should continue from 5, not start at 1
        assert seqs == [6]

    def test_delete_session(self, store):
        store.append("alice", "sess1", [{"type": "message", "data": {}}])
        store.flush_session("alice", "sess1")
        store.append("alice", "sess1", [{"type": "message", "data": {}}])

        store.delete_session("alice", "sess1")

        assert store.exists("alice", "sess1") is False
        assert store.read("alice", "sess1") == []
        assert store.buffered_event_count == 0

    def test_delete_all_for_user(self, store):
        store.append("alice", "sess1", [{"type": "message", "data": {}}])
        store.append("alice", "sess2", [{"type": "message", "data": {}}])
        store.append("bob", "sess1", [{"type": "message", "data": {}}])
        store.flush_all()

        store.delete_all_for_user("alice")

        assert store.exists("alice", "sess1") is False
        assert store.exists("alice", "sess2") is False
        assert store.exists("bob", "sess1") is True

    def test_exists_with_buffer_only(self, store):
        """exists() returns True even for unflushed events."""
        store.append("alice", "sess1", [{"type": "message", "data": {}}])
        assert store.exists("alice", "sess1") is True

    def test_exists_with_backend_only(self, store):
        store.append(
            "alice", "sess1", [{"type": "message", "data": {}} for _ in range(5)]
        )
        # Auto-flushed, buffer empty
        assert store.exists("alice", "sess1") is True

    def test_exists_false(self, store):
        assert store.exists("alice", "nonexistent") is False

    def test_buffered_counts(self, store):
        assert store.buffered_session_count == 0
        assert store.buffered_event_count == 0

        store.append("alice", "sess1", [{"type": "message", "data": {}}])
        store.append(
            "alice",
            "sess2",
            [
                {"type": "message", "data": {}},
                {"type": "message", "data": {}},
            ],
        )

        assert store.buffered_session_count == 2
        assert store.buffered_event_count == 3

    def test_flush_failure_rebuffers(self, fs_config, tmp_path):
        """If backend.append fails, events are put back in the buffer."""
        store = EventStore(fs_config)

        # Append events
        store.append("alice", "sess1", [{"type": "message", "data": {}}])
        assert store.buffered_event_count == 1

        # Make the backend fail by removing the base directory
        import shutil

        events_dir = tmp_path / "events"
        shutil.rmtree(events_dir)
        # Make it a file so mkdir fails
        events_dir.write_text("block")

        # Flush should fail but events should be re-buffered
        store.flush_session("alice", "sess1")
        assert store.buffered_event_count == 1

    def test_eventbus_publish(self, store):
        """EventStore publishes session_event to EventBus on append."""
        published = []

        class MockEventBus:
            def publish(self, event):
                published.append(event)

        store.set_event_bus(MockEventBus())
        store.append(
            "alice",
            "sess1",
            [
                {"type": "message", "data": {"text": "hello"}},
            ],
        )

        assert len(published) == 1
        assert published[0]["type"] == "session_event"
        assert published[0]["user_id"] == "alice"
        assert published[0]["session_id"] == "sess1"
        assert published[0]["event"]["type"] == "message"
        assert published[0]["event"]["data"]["text"] == "hello"

    def test_eventbus_publish_multiple(self, store):
        """Each event in a batch is published individually."""
        published = []

        class MockEventBus:
            def publish(self, event):
                published.append(event)

        store.set_event_bus(MockEventBus())
        store.append(
            "alice",
            "sess1",
            [
                {"type": "message", "data": {}},
                {"type": "tool_call", "data": {}},
            ],
        )

        assert len(published) == 2
        assert published[0]["event"]["seq"] == 1
        assert published[1]["event"]["seq"] == 2

    def test_eventbus_not_set(self, store):
        """Append works fine without EventBus."""
        seqs = store.append("alice", "sess1", [{"type": "message", "data": {}}])
        assert seqs == [1]

    def test_read_with_source_filter(self, store):
        """Source filter returns only events from matching sources."""
        store.append(
            "alice",
            "sess1",
            [
                {"type": "message", "data": {}},
                {"type": "tool_call", "data": {}},
            ],
            source="opencode",
        )
        store.append(
            "alice",
            "sess1",
            [
                {"type": "evaluation", "data": {}},
            ],
            source="intaris",
        )

        # Filter to opencode only
        events = store.read("alice", "sess1", sources={"opencode"})
        assert len(events) == 2
        assert all(e["source"] == "opencode" for e in events)

        # Filter to intaris only
        events = store.read("alice", "sess1", sources={"intaris"})
        assert len(events) == 1
        assert events[0]["source"] == "intaris"

        # No filter returns all
        events = store.read("alice", "sess1")
        assert len(events) == 3

    def test_read_with_multiple_source_filter(self, store):
        """Multiple sources in filter returns events from any of them."""
        store.append(
            "alice",
            "sess1",
            [{"type": "message", "data": {}}],
            source="opencode",
        )
        store.append(
            "alice",
            "sess1",
            [{"type": "evaluation", "data": {}}],
            source="intaris",
        )
        store.append(
            "alice",
            "sess1",
            [{"type": "tool_call", "data": {}}],
            source="client",
        )

        events = store.read("alice", "sess1", sources={"opencode", "client"})
        assert len(events) == 2
        sources = {e["source"] for e in events}
        assert sources == {"opencode", "client"}

    def test_read_with_source_and_type_filter(self, store):
        """Source and type filters can be combined."""
        store.append(
            "alice",
            "sess1",
            [
                {"type": "message", "data": {}},
                {"type": "tool_call", "data": {}},
            ],
            source="opencode",
        )
        store.append(
            "alice",
            "sess1",
            [
                {"type": "message", "data": {}},
            ],
            source="intaris",
        )

        # Filter to opencode messages only
        events = store.read(
            "alice", "sess1", event_types={"message"}, sources={"opencode"}
        )
        assert len(events) == 1
        assert events[0]["source"] == "opencode"
        assert events[0]["type"] == "message"

    def test_thread_safety(self, store):
        """Concurrent appends from multiple threads produce unique seqs."""
        results = []
        errors = []

        def append_events(thread_id):
            try:
                seqs = store.append(
                    "alice",
                    "sess1",
                    [
                        {"type": "message", "data": {"thread": thread_id}}
                        for _ in range(10)
                    ],
                )
                results.extend(seqs)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=append_events, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(results) == 50
        # All seqs should be unique
        assert len(set(results)) == 50
        # All seqs should be in range 1..50
        assert sorted(results) == list(range(1, 51))


class TestEventStoreConfig:
    """Tests for EventStoreConfig."""

    def test_defaults(self):
        config = EventStoreConfig()
        assert config.enabled is True
        assert config.backend == "filesystem"
        assert config.flush_size == 100
        assert config.flush_interval == 30

    def test_unsupported_backend_raises(self, tmp_path):
        config = EventStoreConfig(
            backend="redis",
            filesystem_path=str(tmp_path / "events"),
        )
        with pytest.raises(ValueError, match="Unsupported event store backend"):
            EventStore(config)


class TestValidEventTypes:
    """Tests for the canonical event types set."""

    def test_expected_types(self):
        expected = {
            "message",
            "tool_call",
            "tool_result",
            "evaluation",
            "part",
            "lifecycle",
            "checkpoint",
            "reasoning",
            "transcript",
        }
        assert VALID_EVENT_TYPES == expected

    def test_is_frozenset(self):
        assert isinstance(VALID_EVENT_TYPES, frozenset)
