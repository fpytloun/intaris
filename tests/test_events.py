"""Tests for the session event recording system.

Tests cover:
- Backend helpers (chunk filename, ndjson serialization, path validation)
- FilesystemEventBackend (append, read, read_stream, last_seq, delete, exists)
- EventStore (append, read, flush, seq assignment, buffer management, EventBus)
- EventStoreConfig validation
"""

from __future__ import annotations

import io
import threading
import time
from pathlib import Path

import pytest

from intaris.config import EventStoreConfig
from intaris.events.backend import (
    FilesystemEventBackend,
    S3EventBackend,
    _chunk_filename,
    _events_to_ndjson,
    _ndjson_to_events,
    _parse_chunk_filename,
    _validate_path_component,
)
from intaris.events.cache import FilesystemEventChunkCache, NullEventChunkCache
from intaris.events.store import VALID_EVENT_TYPES, EventStore, _merge_seq_ranges
from intaris.metrics import Histogram

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
        _validate_path_component("user+test@example.com", "user_id")
        _validate_path_component("customer/department=shipping@example.com", "user_id")
        _validate_path_component("o'hara!#$%&'*+=?^_`{|}~@example.com", "user_id")
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

    def test_null_byte_raises(self):
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

    def test_available_seq_ranges_uses_chunk_metadata(self, backend):
        backend.append(
            "alice",
            "sess1",
            [{"seq": 5, "ts": "t", "type": "message", "data": {}}],
        )
        backend.append(
            "alice",
            "sess1",
            [
                {"seq": 8, "ts": "t", "type": "message", "data": {}},
                {"seq": 9, "ts": "t", "type": "message", "data": {}},
            ],
        )

        assert backend.available_seq_ranges("alice", "sess1") == [(5, 5), (8, 9)]

    def test_append_and_read_with_full_email_characters(self, backend, fs_config):
        events = [
            {"seq": 1, "ts": "2026-01-01T00:00:00Z", "type": "message", "data": {}}
        ]
        user_id = "customer/department=shipping+ops@example.com"
        session_id = "sess:weird/part"

        backend.append(user_id, session_id, events)

        assert backend.read(user_id, session_id) == events
        base_path = Path(fs_config.filesystem_path)
        encoded_user = "customer%2Fdepartment%3Dshipping%2Bops%40example.com"
        encoded_session = "sess%3Aweird%2Fpart"
        assert (base_path / encoded_user / encoded_session).exists()

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

    def test_read_tail(self, backend):
        events = [
            {"seq": i, "ts": f"t-{i}", "type": "message", "data": {}}
            for i in range(1, 6)
        ]
        backend.append("alice", "sess1", events)

        result = backend.read_tail("alice", "sess1", limit=2)
        assert [e["seq"] for e in result] == [4, 5]

    def test_read_before_seq(self, backend):
        events = [
            {"seq": i, "ts": f"t-{i}", "type": "message", "data": {}}
            for i in range(1, 6)
        ]
        backend.append("alice", "sess1", events)

        result = backend.read_before("alice", "sess1", before_seq=5, limit=2)
        assert [e["seq"] for e in result] == [3, 4]

    def test_read_before_seq_with_filters(self, backend):
        backend.append(
            "alice",
            "sess1",
            [
                {
                    "seq": 1,
                    "ts": "t-1",
                    "type": "message",
                    "source": "client",
                    "data": {},
                },
                {
                    "seq": 2,
                    "ts": "t-2",
                    "type": "evaluation",
                    "source": "intaris",
                    "data": {},
                },
                {
                    "seq": 3,
                    "ts": "t-3",
                    "type": "message",
                    "source": "client",
                    "data": {},
                },
                {
                    "seq": 4,
                    "ts": "t-4",
                    "type": "tool_call",
                    "source": "client",
                    "data": {},
                },
                {
                    "seq": 5,
                    "ts": "t-5",
                    "type": "message",
                    "source": "client",
                    "data": {},
                },
            ],
        )

        result = backend.read_before(
            "alice",
            "sess1",
            before_seq=5,
            limit=2,
            event_types={"message"},
            sources={"client"},
        )
        assert [e["seq"] for e in result] == [1, 3]

    def test_read_tail_with_filters(self, backend):
        backend.append(
            "alice",
            "sess1",
            [
                {
                    "seq": 1,
                    "ts": "t-1",
                    "type": "message",
                    "source": "client",
                    "data": {},
                },
                {
                    "seq": 2,
                    "ts": "t-2",
                    "type": "evaluation",
                    "source": "intaris",
                    "data": {},
                },
                {
                    "seq": 3,
                    "ts": "t-3",
                    "type": "message",
                    "source": "client",
                    "data": {},
                },
                {
                    "seq": 4,
                    "ts": "t-4",
                    "type": "tool_call",
                    "source": "client",
                    "data": {},
                },
                {
                    "seq": 5,
                    "ts": "t-5",
                    "type": "message",
                    "source": "client",
                    "data": {},
                },
            ],
        )

        result = backend.read_tail(
            "alice",
            "sess1",
            limit=2,
            event_types={"message"},
            sources={"client"},
        )
        assert [e["seq"] for e in result] == [3, 5]

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

    def test_read_before_seq_across_chunks(self, backend):
        """Backward reading works across multiple chunk files."""
        chunk1 = [
            {"seq": 1, "ts": "t", "type": "message", "data": {}},
            {"seq": 2, "ts": "t", "type": "message", "data": {}},
        ]
        chunk2 = [
            {"seq": 3, "ts": "t", "type": "message", "data": {}},
            {"seq": 4, "ts": "t", "type": "message", "data": {}},
        ]
        chunk3 = [
            {"seq": 5, "ts": "t", "type": "message", "data": {}},
            {"seq": 6, "ts": "t", "type": "message", "data": {}},
        ]
        backend.append("alice", "sess1", chunk1)
        backend.append("alice", "sess1", chunk2)
        backend.append("alice", "sess1", chunk3)

        result = backend.read_before("alice", "sess1", before_seq=5, limit=3)
        assert [e["seq"] for e in result] == [2, 3, 4]

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

    def test_read_seqs_returns_exact_matches_across_chunks(self, backend):
        """Exact seq reads return only requested events from overlapping chunks."""
        chunk1 = [
            {"seq": i, "ts": "t", "type": "message", "data": {"index": i}}
            for i in range(1, 4)
        ]
        chunk2 = [
            {"seq": i, "ts": "t", "type": "message", "data": {"index": i}}
            for i in range(4, 7)
        ]
        backend.append("alice", "sess1", chunk1)
        backend.append("alice", "sess1", chunk2)

        result = backend.read_seqs("alice", "sess1", {2, 5, 99})

        assert [event["seq"] for event in result] == [2, 5]

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

    @pytest.mark.parametrize(
        ("durable_last_seq", "chunks", "first_available_seq", "expected_gap"),
        [
            (0, [], None, None),
            (4, [(1, 2), (3, 4)], 1, None),
            (6, [(3, 4), (5, 6)], 3, (1, 2, "retention")),
            (6, [(1, 2), (5, 6)], 1, (3, 4, "internal_gap")),
            (6, [(1, 4)], 1, (5, 6, "internal_gap")),
            (42, [], None, (1, 42, "retention")),
        ],
    )
    def test_availability_semantics(
        self,
        store,
        durable_last_seq,
        chunks,
        first_available_seq,
        expected_gap,
    ):
        for start_seq, end_seq in chunks:
            store._backend.append(
                "alice",
                "sess1",
                [
                    {"seq": seq, "ts": "t", "type": "message", "data": {}}
                    for seq in range(start_seq, end_seq + 1)
                ],
            )

        availability = store.availability(
            "alice", "sess1", durable_last_seq=durable_last_seq
        )

        assert availability.last_seq == durable_last_seq
        assert availability.first_available_seq == first_available_seq
        if expected_gap is None:
            assert availability.history_gap is None
        else:
            assert availability.history_gap is not None
            assert (
                availability.history_gap.from_seq,
                availability.history_gap.to_seq,
                availability.history_gap.reason,
            ) == expected_gap

    def test_availability_includes_buffered_events_without_suffix_gap(self, store):
        store.append(
            "alice",
            "sess1",
            [{"type": "message", "data": {}} for _ in range(3)],
        )

        availability = store.availability(
            "alice", "sess1", durable_last_seq=store.last_seq("alice", "sess1")
        )

        assert availability.last_seq == 3
        assert availability.first_available_seq == 1
        assert availability.history_gap is None

    def test_merge_seq_ranges_normalizes_adjacent_and_overlapping_ranges(self):
        assert _merge_seq_ranges([(5, 7), (1, 3), (3, 5), (10, 10)]) == [
            (1, 7),
            (10, 10),
        ]

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

    def test_unfiltered_read_pushes_limit_to_backend(self, store):
        store.append(
            "alice",
            "sess1",
            [{"type": "message", "data": {}} for _ in range(5)],
        )
        observed_limits = []
        original_read = store._backend.read

        def recording_read(user_id, session_id, after_seq=0, limit=0):
            observed_limits.append(limit)
            return original_read(user_id, session_id, after_seq, limit)

        store._backend.read = recording_read
        events = store.read("alice", "sess1", limit=2)

        assert len(events) == 2
        assert observed_limits == [2]

    def test_filtered_read_pages_until_limit_is_satisfied(self, tmp_path):
        config = EventStoreConfig(
            backend="filesystem",
            filesystem_path=str(tmp_path / "events"),
            flush_size=500,
            flush_interval=30,
        )
        store = EventStore(config)
        events = [
            {
                "type": "evaluation" if index == 150 else "message",
                "data": {"index": index},
            }
            for index in range(1, 206)
        ]
        store.append("alice", "sess1", events)
        store.flush_all()
        observed_limits = []
        original_read = store._backend.read

        def recording_read(user_id, session_id, after_seq=0, limit=0):
            observed_limits.append(limit)
            return original_read(user_id, session_id, after_seq, limit)

        store._backend.read = recording_read
        result = store.read(
            "alice",
            "sess1",
            limit=1,
            event_types={"evaluation"},
        )

        assert [event["data"]["index"] for event in result] == [150]
        assert observed_limits == [100, 100]

    def test_filtered_read_prefers_earlier_persisted_match_over_buffer(self, tmp_path):
        config = EventStoreConfig(
            backend="filesystem",
            filesystem_path=str(tmp_path / "events"),
            flush_size=500,
            flush_interval=30,
        )
        store = EventStore(config)
        store.append(
            "alice",
            "sess1",
            [
                {
                    "type": "evaluation" if index == 150 else "message",
                    "data": {"index": index},
                }
                for index in range(1, 206)
            ],
        )
        store.flush_all()
        store.append(
            "alice",
            "sess1",
            [{"type": "evaluation", "data": {"index": 206}}],
        )

        result = store.read(
            "alice",
            "sess1",
            limit=1,
            event_types={"evaluation"},
        )

        assert [event["data"]["index"] for event in result] == [150]

    def test_sparse_filtered_read_processes_each_page_once(self, tmp_path):
        config = EventStoreConfig(
            backend="filesystem",
            filesystem_path=str(tmp_path / "events"),
            flush_size=500,
            flush_interval=30,
        )
        store = EventStore(config)
        store.append(
            "alice",
            "sess1",
            [{"type": "message", "data": {"index": index}} for index in range(1, 251)],
        )
        store.flush_all()
        observed_after_seq = []
        original_read = store._backend.read

        def recording_read(user_id, session_id, after_seq=0, limit=0):
            observed_after_seq.append(after_seq)
            return original_read(user_id, session_id, after_seq, limit)

        store._backend.read = recording_read
        result = store.read(
            "alice",
            "sess1",
            limit=1,
            event_types={"evaluation"},
        )

        assert result == []
        assert observed_after_seq == [0, 100, 200]

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

    def test_read_seqs_combines_persisted_and_buffered_events(self, store):
        store.append(
            "alice",
            "sess1",
            [
                {"type": "message", "data": {"content": "one"}},
                {"type": "tool_call", "data": {}},
                {"type": "message", "data": {"content": "three"}},
                {"type": "evaluation", "data": {}},
                {"type": "message", "data": {"content": "five"}},
            ],
        )
        # flush_size=5 flushes seq 1-5; seq 6 remains buffered.
        store.append(
            "alice",
            "sess1",
            [{"type": "assistant_message", "data": {"content": "six"}}],
        )

        events = store.read_seqs(
            "alice",
            "sess1",
            {1, 4, 6},
            event_types={"message", "assistant_message"},
        )

        assert [event["seq"] for event in events] == [1, 6]

    def test_read_with_payload_source_filter(self, store):
        store.append(
            "alice",
            "sess1",
            [
                {
                    "type": "system_message",
                    "data": {"source": "identity", "turn_id": "turn-1", "position": 0},
                },
                {
                    "type": "developer_message",
                    "data": {
                        "source": "memory_search",
                        "turn_id": "turn-1",
                        "position": 1,
                    },
                },
                {
                    "type": "context_snapshot",
                    "data": {"source": "bootstrap", "entries": []},
                },
            ],
            source="cognis",
        )

        events = store.read("alice", "sess1", data_sources={"memory_search"})
        assert [event["type"] for event in events] == ["developer_message"]
        assert events[0]["data"]["source"] == "memory_search"

    def test_read_with_turn_and_position_filters(self, store):
        store.append(
            "alice",
            "sess1",
            [
                {
                    "type": "system_message",
                    "data": {"source": "identity", "turn_id": "turn-1", "position": 0},
                },
                {
                    "type": "developer_message",
                    "data": {
                        "source": "memory_search",
                        "turn_id": "turn-1",
                        "position": 2,
                    },
                },
                {
                    "type": "assistant_message",
                    "data": {"content": "hi", "turn_id": "turn-2", "position": 0},
                },
            ],
            source="cognis",
        )

        events = store.read(
            "alice",
            "sess1",
            turn_id="turn-1",
            min_position=1,
            max_position=3,
        )
        assert [event["type"] for event in events] == ["developer_message"]

    def test_read_tail_with_payload_filters(self, store):
        store.append(
            "alice",
            "sess1",
            [
                {
                    "type": "system_message",
                    "data": {"source": "identity", "turn_id": "turn-1", "position": 0},
                },
                {
                    "type": "developer_message",
                    "data": {
                        "source": "memory_search",
                        "turn_id": "turn-1",
                        "position": 1,
                    },
                },
                {
                    "type": "developer_message",
                    "data": {
                        "source": "memory_search",
                        "turn_id": "turn-2",
                        "position": 0,
                    },
                },
            ],
            source="cognis",
        )

        events = store.read_tail(
            "alice",
            "sess1",
            limit=1,
            data_sources={"memory_search"},
            turn_id="turn-2",
        )
        assert [event["seq"] for event in events] == [3]

    def test_read_tail_with_payload_filters_across_persisted_and_buffer(self, store):
        store.append(
            "alice",
            "sess1",
            [
                {
                    "type": "system_message",
                    "data": {"source": "identity", "turn_id": "turn-0", "position": 0},
                },
                {
                    "type": "developer_message",
                    "data": {
                        "source": "memory_search",
                        "turn_id": "turn-1",
                        "position": 0,
                    },
                },
                {
                    "type": "assistant_message",
                    "data": {"content": "hello", "turn_id": "turn-1", "position": 1},
                },
                {
                    "type": "developer_message",
                    "data": {
                        "source": "memory_search",
                        "turn_id": "turn-2",
                        "position": 0,
                    },
                },
                {
                    "type": "context_snapshot",
                    "data": {"source": "bootstrap", "entries": []},
                },
            ],
            source="cognis",
        )
        store.append(
            "alice",
            "sess1",
            [
                {
                    "type": "developer_message",
                    "data": {
                        "source": "memory_search",
                        "turn_id": "turn-3",
                        "position": 0,
                    },
                },
                {
                    "type": "assistant_message",
                    "data": {"content": "done", "turn_id": "turn-3", "position": 1},
                },
            ],
            source="cognis",
        )

        events = store.read_tail(
            "alice",
            "sess1",
            limit=2,
            data_sources={"memory_search"},
        )
        assert [event["seq"] for event in events] == [4, 6]

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

    def test_tool_events_use_normal_batching(self, store):
        """Tool events remain buffered until a configured threshold."""
        store.append(
            "alice",
            "sess1",
            [
                {"type": "tool_call", "data": {}},
                {"type": "tool_result", "data": {}},
            ],
        )

        assert store.buffered_event_count == 2
        assert not store._backend.exists("alice", "sess1")

    def test_auto_flush_on_byte_threshold(self, tmp_path):
        config = EventStoreConfig(
            backend="filesystem",
            filesystem_path=str(tmp_path / "events"),
            flush_size=100,
            flush_bytes=100,
            flush_interval=30,
        )
        store = EventStore(config)

        store.append(
            "alice",
            "sess1",
            [{"type": "message", "data": {"content": "x" * 200}}],
        )

        assert store.buffered_event_count == 0
        assert store._backend.exists("alice", "sess1")

    def test_flush_stale_only_flushes_expired_buffers(self, store):
        store.append("alice", "old", [{"type": "message", "data": {}}])
        store.append("alice", "new", [{"type": "message", "data": {}}])
        store._buffer_started_at[("alice", "old")] = (
            time.monotonic() - store._config.flush_interval - 1
        )

        flushed = store.flush_stale()

        assert flushed == 1
        assert store.read("alice", "old")
        assert store.buffered_event_count == 1
        assert store.read("alice", "new")

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

    def test_read_tail_from_buffer(self, store):
        store.append(
            "alice",
            "sess1",
            [{"type": "message", "data": {}} for _ in range(3)],
        )

        events = store.read_tail("alice", "sess1", limit=2)
        assert [e["seq"] for e in events] == [2, 3]

    def test_read_before_seq_from_buffer(self, store):
        store.append(
            "alice",
            "sess1",
            [{"type": "message", "data": {}} for _ in range(4)],
        )

        events = store.read_before("alice", "sess1", before_seq=4, limit=2)
        assert [e["seq"] for e in events] == [2, 3]

    def test_read_tail_combines_backend_and_buffer(self, store):
        store.append(
            "alice", "sess1", [{"type": "message", "data": {}} for _ in range(5)]
        )
        store.append(
            "alice", "sess1", [{"type": "tool_call", "data": {}} for _ in range(2)]
        )

        events = store.read_tail("alice", "sess1", limit=4)
        assert [e["seq"] for e in events] == [4, 5, 6, 7]

    def test_read_before_seq_combines_backend_and_buffer(self, store):
        store.append(
            "alice", "sess1", [{"type": "message", "data": {}} for _ in range(5)]
        )
        store.append(
            "alice", "sess1", [{"type": "tool_call", "data": {}} for _ in range(2)]
        )

        events = store.read_before("alice", "sess1", before_seq=7, limit=3)
        assert [e["seq"] for e in events] == [4, 5, 6]

    def test_read_tail_with_filters(self, store):
        store.append(
            "alice",
            "sess1",
            [
                {"type": "message", "data": {}},
                {"type": "evaluation", "data": {}},
                {"type": "message", "data": {}},
                {"type": "tool_call", "data": {}},
                {"type": "message", "data": {}},
            ],
        )

        events = store.read_tail("alice", "sess1", limit=2, event_types={"message"})
        assert [e["seq"] for e in events] == [3, 5]

    def test_read_before_seq_with_payload_filters_across_persisted_and_buffer(
        self, store
    ):
        store.append(
            "alice",
            "sess1",
            [
                {
                    "type": "system_message",
                    "data": {"source": "identity", "turn_id": "turn-0", "position": 0},
                },
                {
                    "type": "developer_message",
                    "data": {
                        "source": "memory_search",
                        "turn_id": "turn-1",
                        "position": 0,
                    },
                },
                {
                    "type": "assistant_message",
                    "data": {"content": "hello", "turn_id": "turn-1", "position": 1},
                },
                {
                    "type": "developer_message",
                    "data": {
                        "source": "memory_search",
                        "turn_id": "turn-2",
                        "position": 0,
                    },
                },
                {
                    "type": "context_snapshot",
                    "data": {"source": "bootstrap", "entries": []},
                },
            ],
            source="cognis",
        )
        store.append(
            "alice",
            "sess1",
            [
                {
                    "type": "developer_message",
                    "data": {
                        "source": "memory_search",
                        "turn_id": "turn-3",
                        "position": 0,
                    },
                },
                {
                    "type": "assistant_message",
                    "data": {"content": "done", "turn_id": "turn-3", "position": 1},
                },
            ],
            source="cognis",
        )

        events = store.read_before(
            "alice",
            "sess1",
            before_seq=7,
            limit=2,
            data_sources={"memory_search"},
        )
        assert [event["seq"] for event in events] == [4, 6]

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

    def test_read_with_exclude_source_filter(self, store):
        """Exclude source filter removes events from specified sources."""
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

        # Exclude intaris — should return opencode + client
        events = store.read("alice", "sess1", exclude_sources={"intaris"})
        assert len(events) == 2
        sources = {e["source"] for e in events}
        assert "intaris" not in sources
        assert sources == {"opencode", "client"}

    def test_read_with_exclude_and_include_source(self, store):
        """Include and exclude source filters can be combined."""
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

        # Include opencode+intaris, then exclude intaris → only opencode
        events = store.read(
            "alice",
            "sess1",
            sources={"opencode", "intaris"},
            exclude_sources={"intaris"},
        )
        assert len(events) == 1
        assert events[0]["source"] == "opencode"

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

    def test_slow_flush_does_not_block_other_sessions(self, tmp_path):
        config = EventStoreConfig(
            backend="filesystem",
            filesystem_path=str(tmp_path / "events"),
            flush_size=1,
            flush_interval=30,
        )
        store = EventStore(config)
        original_append = store._backend.append
        blocked = threading.Event()
        release = threading.Event()

        def blocking_append(user_id, session_id, events):
            if session_id == "slow":
                blocked.set()
                release.wait(timeout=2)
            original_append(user_id, session_id, events)

        store._backend.append = blocking_append
        slow = threading.Thread(
            target=store.append,
            args=("alice", "slow", [{"type": "message", "data": {}}]),
        )
        slow.start()
        assert blocked.wait(timeout=1)

        fast = threading.Thread(
            target=store.append,
            args=("alice", "fast", [{"type": "message", "data": {}}]),
        )
        fast.start()
        fast.join(timeout=1)

        release.set()
        slow.join(timeout=1)
        assert not fast.is_alive()
        assert store.last_seq("alice", "fast") == 1

    def test_buffer_registry_is_safe_during_concurrent_session_creation(
        self, fs_config
    ):
        store = EventStore(fs_config)
        errors = []
        finished = threading.Event()

        def create_sessions():
            try:
                for index in range(100):
                    store.append(
                        "alice",
                        f"session-{index}",
                        [{"type": "message", "data": {}}],
                    )
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()

        creator = threading.Thread(target=create_sessions)
        creator.start()
        while not finished.is_set():
            store.flush_stale()
            store.metrics()
        creator.join(timeout=2)
        store.flush_all()

        assert not errors
        assert store.buffered_event_count == 0


class TestEventStoreConfig:
    """Tests for EventStoreConfig."""

    def test_defaults(self):
        config = EventStoreConfig()
        assert config.enabled is True
        assert config.backend == "filesystem"
        assert config.flush_size == 100
        assert config.flush_bytes == 4 * 1024 * 1024
        assert config.flush_interval == 30
        assert config.s3_chunk_cache_ttl == 300.0
        assert config.event_cache_backend == "filesystem"
        assert config.event_cache_max_bytes == 10 * 1024**3
        assert config.event_cache_ttl_seconds == 7 * 24 * 3600

    def test_unsupported_backend_raises(self, tmp_path):
        config = EventStoreConfig(
            backend="redis",
            filesystem_path=str(tmp_path / "events"),
        )
        with pytest.raises(ValueError, match="Unsupported event store backend"):
            EventStore(config)

    def test_filesystem_event_backend_does_not_create_chunk_cache(self, tmp_path):
        cache_path = tmp_path / "event-cache"
        EventStore(
            EventStoreConfig(
                backend="filesystem",
                filesystem_path=str(tmp_path / "events"),
                event_cache_path=str(cache_path),
            )
        )

        assert not cache_path.exists()


class TestS3EventBackendCache:
    """Tests for the bounded local S3 metadata caches."""

    class FakeClientError(Exception):
        def __init__(self, status_code):
            self.response = {
                "Error": {"Code": "PreconditionFailed"},
                "ResponseMetadata": {"HTTPStatusCode": status_code},
            }
            super().__init__("S3 client error")

    class FakeClient:
        def __init__(self):
            self.list_calls = 0
            self.objects = []
            self.put_calls = []
            self.etag = "etag-remote"
            self.events = [{"seq": 1, "ts": "t", "type": "message", "data": {}}]

        def list_objects_v2(self, **kwargs):
            self.list_calls += 1
            prefix = kwargs["Prefix"]
            contents = [
                {"Key": key, "ETag": f'"{self.etag}"'}
                for key in self.objects
                if key.startswith(prefix)
            ]
            return {"Contents": contents, "IsTruncated": False}

        def put_object(self, **kwargs):
            self.put_calls.append(kwargs)
            self.objects.append(kwargs["Key"])
            return {"ETag": '"etag-written"'}

        def get_object(self, **kwargs):
            return {"Body": io.BytesIO(_events_to_ndjson(self.events))}

    @staticmethod
    def backend(client, event_cache=None):
        backend = S3EventBackend.__new__(S3EventBackend)
        backend._client = client
        backend._client_error_type = TestS3EventBackendCache.FakeClientError
        backend._endpoint_identity = "https://s3.test"
        backend._bucket = "events"
        backend._chunk_cache_ttl = 60.0
        backend._cache_lock = threading.Lock()
        backend._chunk_cache = {}
        backend._object_versions = {}
        backend._write_prefix_cache = {}
        backend._chunk_cache_hits = 0
        backend._chunk_cache_misses = 0
        backend._list_requests = 0
        backend._get_requests = 0
        backend._put_requests = 0
        backend._bytes_read = 0
        backend._bytes_written = 0
        backend._operation_latency = {
            "list": Histogram(),
            "get": Histogram(),
            "put": Histogram(),
        }
        backend._cache_max_entries = 4096
        backend._event_cache = event_cache or NullEventChunkCache()
        return backend

    def test_chunk_listing_is_cached(self):
        client = self.FakeClient()
        client.objects = ["events/alice/session/seq_000001_000002.ndjson"]
        backend = self.backend(client)

        first = backend._list_chunks("alice", "session")
        second = backend._list_chunks("alice", "session")

        assert first == second
        assert client.list_calls == 1
        assert backend.metrics()["chunk_cache_hits_total"] == 1

    def test_available_seq_ranges_uses_cached_index_without_get(self):
        client = self.FakeClient()
        client.objects = [
            "events/alice/session/seq_000003_000004.ndjson",
            "events/alice/session/seq_000007_000009.ndjson",
        ]
        backend = self.backend(client)

        assert backend.available_seq_ranges("alice", "session") == [(3, 4), (7, 9)]
        assert backend.available_seq_ranges("alice", "session") == [(3, 4), (7, 9)]
        assert client.list_calls == 1
        assert backend.metrics()["get_requests_total"] == 0

    def test_availability_refreshes_stale_index_that_trails_high_water(self, store):
        client = self.FakeClient()
        reader_backend = self.backend(client)
        writer_backend = self.backend(client)
        store._backend = reader_backend

        assert reader_backend.available_seq_ranges("alice", "session") == []
        writer_backend.append(
            "alice",
            "session",
            [
                {"seq": 1, "ts": "t", "type": "message", "data": {}},
                {"seq": 2, "ts": "t", "type": "message", "data": {}},
            ],
        )

        availability = store.availability("alice", "session", durable_last_seq=2)

        assert availability.last_seq == 2
        assert availability.first_available_seq == 1
        assert availability.history_gap is None
        assert reader_backend.metrics()["get_requests_total"] == 0
        assert writer_backend.metrics()["get_requests_total"] == 0

    @pytest.mark.parametrize(
        ("objects", "durable_last_seq", "first_available_seq", "expected_gap"),
        [
            (
                ["events/alice/session/seq_000003_000006.ndjson"],
                6,
                3,
                (1, 2, "retention"),
            ),
            (
                [
                    "events/alice/session/seq_000001_000002.ndjson",
                    "events/alice/session/seq_000005_000006.ndjson",
                ],
                6,
                1,
                (3, 4, "internal_gap"),
            ),
            ([], 42, None, (1, 42, "retention")),
        ],
    )
    def test_s3_ranges_drive_availability_semantics(
        self,
        store,
        objects,
        durable_last_seq,
        first_available_seq,
        expected_gap,
    ):
        client = self.FakeClient()
        client.objects = objects
        store._backend = self.backend(client)

        availability = store.availability(
            "alice", "session", durable_last_seq=durable_last_seq
        )

        assert availability.first_available_seq == first_available_seq
        assert availability.history_gap is not None
        assert (
            availability.history_gap.from_seq,
            availability.history_gap.to_seq,
            availability.history_gap.reason,
        ) == expected_gap
        assert store._backend.metrics()["get_requests_total"] == 0

    def test_append_reuses_discovered_prefix(self):
        client = self.FakeClient()
        backend = self.backend(client)
        events = [{"seq": 1, "ts": "t", "type": "message", "data": {}}]

        backend.append("alice", "session", events)
        backend.append(
            "alice",
            "session",
            [{"seq": 2, "ts": "t", "type": "message", "data": {}}],
        )

        assert client.list_calls == 1
        assert len(client.objects) == 2
        assert all(call["IfNoneMatch"] == "*" for call in client.put_calls)
        metrics = backend.metrics()
        assert metrics["put_requests_total"] == 2
        assert metrics["bytes_written_total"] > 0
        assert metrics["operation_latency"]["put"]["count"] == 2

    def test_append_caches_body_under_its_own_put_version(self):
        client = self.FakeClient()
        backend = self.backend(client)
        cached_keys = []

        class RecordingCache(NullEventChunkCache):
            def put(self, key, value):
                cached_keys.append(key)

        backend._event_cache = RecordingCache()
        original_cache_key = backend._object_cache_key

        def race_with_metadata_refresh(key, *, version=None):
            backend._object_versions[key] = "etag-replaced"
            return original_cache_key(key, version=version)

        backend._object_cache_key = race_with_metadata_refresh
        backend.append(
            "alice",
            "session",
            [{"seq": 1, "ts": "t", "type": "message", "data": {}}],
        )

        assert cached_keys[0].endswith("\0etag-written")

    def test_matching_preexisting_immutable_chunk_is_idempotent(self):
        client = self.FakeClient()

        def precondition_failed(**kwargs):
            raise self.FakeClientError(412)

        client.put_object = precondition_failed
        backend = self.backend(client)

        backend.append(
            "alice",
            "session",
            [{"seq": 1, "ts": "t", "type": "message", "data": {}}],
        )

        assert backend.metrics()["get_requests_total"] == 1

    def test_mismatching_preexisting_immutable_chunk_is_rejected(self):
        client = self.FakeClient()
        client.events = [
            {
                "seq": 1,
                "ts": "different",
                "type": "message",
                "data": {"content": "collision"},
            }
        ]

        def precondition_failed(**kwargs):
            raise self.FakeClientError(412)

        client.put_object = precondition_failed
        backend = self.backend(client)

        with pytest.raises(RuntimeError, match="Immutable event chunk collision"):
            backend.append(
                "alice",
                "session",
                [{"seq": 1, "ts": "t", "type": "message", "data": {}}],
            )

    def test_stream_read_records_get_metrics(self):
        client = self.FakeClient()
        client.objects = ["events/alice/session/seq_000001_000001.ndjson"]
        backend = self.backend(client)

        assert list(backend.read_stream("alice", "session"))[0]["seq"] == 1

        metrics = backend.metrics()
        assert metrics["get_requests_total"] == 1
        assert metrics["bytes_read_total"] > 0
        assert metrics["operation_latency"]["get"]["count"] == 1

    def test_repeated_chunk_read_uses_filesystem_cache(self, tmp_path):
        client = self.FakeClient()
        client.objects = ["events/alice/session/seq_000001_000001.ndjson"]
        event_cache = FilesystemEventChunkCache(
            str(tmp_path / "cache"),
            max_bytes=1024 * 1024,
            ttl_seconds=3600,
            touch_interval_seconds=3600,
            sweep_interval_seconds=60,
        )
        backend = self.backend(client, event_cache)

        assert backend.read("alice", "session")[0]["seq"] == 1
        assert backend.read("alice", "session")[0]["seq"] == 1

        metrics = backend.metrics()
        assert metrics["get_requests_total"] == 1
        assert metrics["event_cache"]["hits_total"] == 1

    def test_corrupt_cached_chunk_is_reloaded_from_s3(self, tmp_path):
        client = self.FakeClient()
        client.objects = ["events/alice/session/seq_000001_000001.ndjson"]
        event_cache = FilesystemEventChunkCache(
            str(tmp_path / "cache"),
            max_bytes=1024 * 1024,
            ttl_seconds=3600,
            touch_interval_seconds=3600,
            sweep_interval_seconds=60,
        )
        backend = self.backend(client, event_cache)
        assert backend.read("alice", "session")[0]["seq"] == 1
        object_key = client.objects[0]
        digest = event_cache._digest_key(backend._object_cache_key(object_key))
        event_cache._path_for_digest(digest).write_bytes(b"corrupt")

        assert backend.read("alice", "session")[0]["seq"] == 1

        metrics = backend.metrics()
        assert metrics["get_requests_total"] == 2
        assert metrics["event_cache"]["corruption_recoveries_total"] == 1

    def test_recreated_object_uses_new_etag_cache_identity(self, tmp_path):
        client = self.FakeClient()
        client.objects = ["events/alice/session/seq_000001_000001.ndjson"]
        event_cache = FilesystemEventChunkCache(
            str(tmp_path / "cache"),
            max_bytes=1024 * 1024,
            ttl_seconds=3600,
            touch_interval_seconds=3600,
            sweep_interval_seconds=60,
        )
        backend = self.backend(client, event_cache)
        assert backend.read("alice", "session")[0]["data"] == {}

        client.etag = "etag-recreated"
        client.events = [
            {
                "seq": 1,
                "ts": "t2",
                "type": "message",
                "data": {"content": "new"},
            }
        ]
        cache_key = ("alice", "session")
        cached_at, chunks = backend._chunk_cache[cache_key]
        backend._chunk_cache[cache_key] = (cached_at - 61, chunks)

        result = backend.read("alice", "session")

        assert result[0]["data"] == {"content": "new"}
        assert backend.metrics()["get_requests_total"] == 2

    def test_failed_put_records_attempt_without_written_bytes(self):
        client = self.FakeClient()

        def fail_put(**kwargs):
            raise RuntimeError("S3 unavailable")

        client.put_object = fail_put
        backend = self.backend(client)

        with pytest.raises(RuntimeError, match="S3 unavailable"):
            backend.append(
                "alice",
                "session",
                [{"seq": 1, "ts": "t", "type": "message", "data": {}}],
            )

        metrics = backend.metrics()
        assert metrics["put_requests_total"] == 1
        assert metrics["bytes_written_total"] == 0
        assert metrics["operation_latency"]["put"]["count"] == 1

    def test_local_append_does_not_extend_stale_chunk_cache(self):
        client = self.FakeClient()
        backend = self.backend(client)
        client.objects = ["events/alice/session/seq_000001_000001.ndjson"]
        assert len(backend._list_chunks("alice", "session")) == 1
        cache_key = ("alice", "session")
        cached_at, chunks = backend._chunk_cache[cache_key]
        backend._chunk_cache[cache_key] = (cached_at - 61, chunks)
        client.objects.append("events/alice/session/seq_000002_000002.ndjson")

        backend.append(
            "alice",
            "session",
            [{"seq": 3, "ts": "t", "type": "message", "data": {}}],
        )
        refreshed = backend._list_chunks("alice", "session")

        assert [chunk[:2] for chunk in refreshed] == [(1, 1), (2, 2), (3, 3)]


class TestValidEventTypes:
    """Tests for the canonical event types set."""

    def test_expected_types(self):
        expected = {
            "message",
            "system_message",
            "developer_message",
            "user_message",
            "assistant_message",
            "assistant_thinking",
            "context_snapshot",
            "tool_call",
            "tool_result",
            "evaluation",
            "delegation",
            "compaction_summary",
            "part",
            "lifecycle",
            "checkpoint",
            "reasoning",
            "transcript",
        }
        assert VALID_EVENT_TYPES == expected

    def test_is_frozenset(self):
        assert isinstance(VALID_EVENT_TYPES, frozenset)
