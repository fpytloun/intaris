"""High-level event store with write buffering and EventBus integration.

The EventStore is the primary interface for session recording. It:
- Assigns monotonic sequence numbers per session
- Buffers events in memory for chunk consolidation
- Publishes events to EventBus for live WebSocket tailing
- Flushes deterministically on threshold, timer, session end, and shutdown

Event format (each ndjson line):
  {"seq": 1, "ts": "2026-03-12T10:00:00.123Z", "type": "message",
   "source": "opencode", "data": {...}}
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Iterator

from intaris.config import EventStoreConfig
from intaris.events.backend import (
    EventBackend,
    FilesystemEventBackend,
    S3EventBackend,
)

logger = logging.getLogger(__name__)

# Valid canonical event types.
VALID_EVENT_TYPES = frozenset(
    {
        "message",
        "user_message",
        "assistant_message",
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
)


class EventStore:
    """High-level event store combining backend + buffer + EventBus.

    Thread-safe via a single lock protecting the write buffer and
    sequence counters. Reads go directly to the backend (no lock needed
    for append-only storage).
    """

    def __init__(self, config: EventStoreConfig) -> None:
        self._config = config

        # Initialize backend
        if config.backend == "s3":
            self._backend: EventBackend = S3EventBackend(config)
        elif config.backend == "filesystem":
            self._backend = FilesystemEventBackend(config)
        else:
            raise ValueError(f"Unsupported event store backend: {config.backend}")

        # Write buffer: (user_id, session_id) → list of events
        self._buffers: dict[tuple[str, str], list[dict]] = {}

        # Sequence counters: (user_id, session_id) → last assigned seq
        self._seq_counters: dict[tuple[str, str], int] = {}

        # Lock protecting buffers and seq counters
        self._lock = threading.Lock()

        # EventBus reference (set after initialization via set_event_bus)
        self._event_bus: Any = None

        logger.info(
            "Event store initialized (backend=%s, flush_size=%d, flush_interval=%ds)",
            config.backend,
            config.flush_size,
            config.flush_interval,
        )

    def set_event_bus(self, event_bus: Any) -> None:
        """Set the EventBus for live tailing.

        Called during lifespan initialization after both EventStore
        and EventBus are created.
        """
        self._event_bus = event_bus

    def append(
        self,
        user_id: str,
        session_id: str,
        events: list[dict[str, Any]],
        source: str = "intaris",
    ) -> list[int]:
        """Append events to a session's event log.

        Assigns sequence numbers and server timestamps, publishes to
        EventBus for live tailing, and buffers for storage. Flushes
        to backend when buffer reaches flush_size.

        Args:
            user_id: Tenant identifier.
            session_id: Session identifier.
            events: List of event dicts. Must have ``type`` and ``data`` fields.
            source: Event source identifier (e.g., "opencode", "intaris").

        Returns:
            List of assigned sequence numbers.
        """
        if not events:
            return []

        now = datetime.now(timezone.utc).isoformat()
        assigned_seqs: list[int] = []

        with self._lock:
            key = (user_id, session_id)

            # Lazy recovery of sequence counter from backend.
            # On failure, propagate the error to avoid seq collisions
            # with existing persisted events.
            if key not in self._seq_counters:
                try:
                    self._seq_counters[key] = self._backend.last_seq(
                        user_id, session_id
                    )
                except Exception:
                    logger.exception(
                        "Failed to recover last_seq for %s/%s — "
                        "refusing to start from 0 (would risk seq collisions)",
                        user_id,
                        session_id,
                    )
                    raise

            # Assign seq and ts to each event (copy to avoid mutating caller's dicts)
            enriched: list[dict] = []
            for event in events:
                self._seq_counters[key] += 1
                seq = self._seq_counters[key]
                enriched_event = dict(event)
                enriched_event["seq"] = seq
                enriched_event["ts"] = now
                enriched_event["source"] = source
                enriched.append(enriched_event)
                assigned_seqs.append(seq)

            # Buffer enriched copies
            buf = self._buffers.setdefault(key, [])
            buf.extend(enriched)

            # Flush if buffer exceeds threshold
            if len(buf) >= self._config.flush_size:
                self._flush_locked(key)

        # Publish to EventBus for live tailing (outside lock).
        # Uses enriched copies (with seq/ts/source) rather than caller's dicts.
        if self._event_bus is not None:
            for event in enriched:
                self._event_bus.publish(
                    {
                        "type": "session_event",
                        "user_id": user_id,
                        "session_id": session_id,
                        "event": event,
                    }
                )

        return assigned_seqs

    def read(
        self,
        user_id: str,
        session_id: str,
        after_seq: int = 0,
        limit: int = 0,
        event_types: set[str] | None = None,
        sources: set[str] | None = None,
        exclude_sources: set[str] | None = None,
        after_ts: str | None = None,
        before_ts: str | None = None,
    ) -> list[dict]:
        """Read events from storage and buffer.

        Combines persisted events from the backend with any buffered
        (unflushed) events. Results are ordered by seq.

        Args:
            after_seq: Return events with seq > this value.
            limit: Max events to return. 0 = all.
            event_types: Filter by event type. None = all types.
            sources: Include only events from these sources. None = all.
            exclude_sources: Exclude events from these sources. None = no
                exclusion. Applied after ``sources`` include filter.
            after_ts: Return events with ts >= this ISO 8601 timestamp.
            before_ts: Return events with ts <= this ISO 8601 timestamp.

        Returns:
            List of event dicts ordered by seq.
        """
        # Read from backend (persisted chunks)
        events = self._backend.read(user_id, session_id, after_seq, limit=0)

        # Add buffered (unflushed) events
        with self._lock:
            key = (user_id, session_id)
            buf = self._buffers.get(key, [])
            for event in buf:
                if event.get("seq", 0) > after_seq:
                    events.append(event)

        # Sort by seq (buffer events may interleave with backend events
        # if a flush happened between backend read and buffer read)
        events.sort(key=lambda e: e.get("seq", 0))

        # Deduplicate by seq (in case of overlap)
        seen: set[int] = set()
        deduped: list[dict] = []
        for event in events:
            seq = event.get("seq", 0)
            if seq not in seen:
                seen.add(seq)
                deduped.append(event)
        events = deduped

        # Filter by timestamp range (ISO 8601 strings compare lexicographically)
        if after_ts:
            events = [e for e in events if e.get("ts", "") >= after_ts]
        if before_ts:
            events = [e for e in events if e.get("ts", "") <= before_ts]

        # Filter by event type
        if event_types:
            events = [e for e in events if e.get("type") in event_types]

        # Filter by source (include / exclude)
        if sources:
            events = [e for e in events if e.get("source") in sources]
        if exclude_sources:
            events = [e for e in events if e.get("source") not in exclude_sources]

        # Apply limit
        if limit:
            events = events[:limit]

        return events

    def read_tail(
        self,
        user_id: str,
        session_id: str,
        limit: int,
        event_types: set[str] | None = None,
        sources: set[str] | None = None,
        exclude_sources: set[str] | None = None,
        after_ts: str | None = None,
        before_ts: str | None = None,
    ) -> list[dict]:
        """Read the last matching events in chronological order.

        Buffered events are considered first. Persisted tail reads request
        ``limit + buffered_matches`` items to tolerate overlap if a flush
        happens while the read is in progress.
        """
        if limit <= 0:
            return []

        with self._lock:
            key = (user_id, session_id)
            buffer_events = list(self._buffers.get(key, []))

        filtered_buffer = [
            event
            for event in buffer_events
            if self._event_matches_filters(
                event,
                event_types=event_types,
                sources=sources,
                exclude_sources=exclude_sources,
                after_ts=after_ts,
                before_ts=before_ts,
            )
        ]

        if len(filtered_buffer) >= limit:
            filtered_buffer.sort(key=lambda e: e.get("seq", 0))
            return filtered_buffer[-limit:]

        persisted_events = self._backend.read_tail(
            user_id,
            session_id,
            limit=limit + len(filtered_buffer),
            event_types=event_types,
            sources=sources,
            exclude_sources=exclude_sources,
            after_ts=after_ts,
            before_ts=before_ts,
        )

        events = persisted_events + filtered_buffer
        events.sort(key=lambda e: e.get("seq", 0))

        seen: set[int] = set()
        deduped: list[dict] = []
        for event in events:
            seq = event.get("seq", 0)
            if seq not in seen:
                seen.add(seq)
                deduped.append(event)

        return deduped[-limit:]

    def read_stream(
        self,
        user_id: str,
        session_id: str,
        after_seq: int = 0,
    ) -> Iterator[dict]:
        """Stream events from storage (backend only, no buffer).

        For large sessions where loading all events into memory is
        impractical. Does not include buffered events — call flush_session
        first if you need complete data.
        """
        yield from self._backend.read_stream(user_id, session_id, after_seq)

    def last_seq(self, user_id: str, session_id: str) -> int:
        """Get the last sequence number (from buffer or backend)."""
        with self._lock:
            key = (user_id, session_id)
            if key in self._seq_counters:
                return self._seq_counters[key]
        return self._backend.last_seq(user_id, session_id)

    def flush_session(self, user_id: str, session_id: str) -> None:
        """Flush buffered events for a specific session to storage.

        Called on session completion, termination, or suspension.
        """
        with self._lock:
            self._flush_locked((user_id, session_id))

    def flush_all(self) -> None:
        """Flush all buffered events to storage.

        Called on server shutdown (lifespan cleanup) and by the
        periodic flush background task.
        """
        with self._lock:
            for key in list(self._buffers.keys()):
                self._flush_locked(key)

    def delete_session(self, user_id: str, session_id: str) -> None:
        """Delete all events for a session (storage + buffer)."""
        with self._lock:
            key = (user_id, session_id)
            self._buffers.pop(key, None)
            self._seq_counters.pop(key, None)
        self._backend.delete_session(user_id, session_id)

    def delete_all_for_user(self, user_id: str) -> None:
        """Delete all events for a user (storage + buffer)."""
        with self._lock:
            keys_to_remove = [k for k in self._buffers if k[0] == user_id]
            for key in keys_to_remove:
                self._buffers.pop(key, None)
                self._seq_counters.pop(key, None)
        self._backend.delete_all_for_user(user_id)

    @staticmethod
    def _event_matches_filters(
        event: dict,
        *,
        event_types: set[str] | None = None,
        sources: set[str] | None = None,
        exclude_sources: set[str] | None = None,
        after_ts: str | None = None,
        before_ts: str | None = None,
    ) -> bool:
        """Return True when the event matches the requested read filters."""
        event_ts = event.get("ts", "")
        if after_ts and event_ts < after_ts:
            return False
        if before_ts and event_ts > before_ts:
            return False
        if event_types and event.get("type") not in event_types:
            return False
        if sources and event.get("source") not in sources:
            return False
        if exclude_sources and event.get("source") in exclude_sources:
            return False
        return True

    def exists(self, user_id: str, session_id: str) -> bool:
        """Check if any events exist for a session (storage or buffer)."""
        with self._lock:
            key = (user_id, session_id)
            if self._buffers.get(key):
                return True
        return self._backend.exists(user_id, session_id)

    def sweep_seq_counters(self, active_sessions: set[tuple[str, str]]) -> int:
        """Remove seq counters for sessions that are no longer active.

        Called periodically by the background worker to prevent unbounded
        growth of ``_seq_counters``. Sessions with buffered events are
        never swept (they still need their counter).

        Args:
            active_sessions: Set of (user_id, session_id) tuples for
                sessions that are still active/idle.

        Returns:
            Number of counters removed.
        """
        removed = 0
        with self._lock:
            stale = [
                k
                for k in self._seq_counters
                if k not in active_sessions and k not in self._buffers
            ]
            for k in stale:
                del self._seq_counters[k]
                removed += 1
        if removed:
            logger.debug("Swept %d stale seq counters", removed)
        return removed

    @property
    def buffered_session_count(self) -> int:
        """Number of sessions with buffered (unflushed) events."""
        with self._lock:
            return len(self._buffers)

    @property
    def buffered_event_count(self) -> int:
        """Total number of buffered (unflushed) events across all sessions."""
        with self._lock:
            return sum(len(buf) for buf in self._buffers.values())

    def _flush_locked(self, key: tuple[str, str]) -> None:
        """Flush buffer for a session to the backend. Must hold self._lock."""
        buf = self._buffers.pop(key, [])
        if not buf:
            return
        user_id, session_id = key
        try:
            self._backend.append(user_id, session_id, buf)
            logger.debug(
                "Flushed %d events for %s/%s (seq %d-%d)",
                len(buf),
                user_id,
                session_id,
                buf[0]["seq"],
                buf[-1]["seq"],
            )
        except Exception:
            # Put events back in buffer on failure so they aren't lost.
            # Cap total buffer size to prevent OOM on persistent failure.
            _MAX_BUFFER_PER_SESSION = 10000
            logger.exception(
                "Failed to flush %d events for %s/%s, re-buffering",
                len(buf),
                user_id,
                session_id,
            )
            existing = self._buffers.get(key, [])
            combined = buf + existing
            if len(combined) > _MAX_BUFFER_PER_SESSION:
                dropped = len(combined) - _MAX_BUFFER_PER_SESSION
                logger.error(
                    "Buffer overflow for %s/%s, dropping %d oldest events",
                    user_id,
                    session_id,
                    dropped,
                )
                combined = combined[-_MAX_BUFFER_PER_SESSION:]
            self._buffers[key] = combined
