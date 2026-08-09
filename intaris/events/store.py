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
import time
from datetime import datetime, timezone
from typing import Any, Iterator

from intaris.config import EventStoreConfig
from intaris.events.backend import (
    EventBackend,
    FilesystemEventBackend,
    S3EventBackend,
    _events_to_ndjson,
)
from intaris.metrics import Histogram

logger = logging.getLogger(__name__)

# Valid canonical event types.
VALID_EVENT_TYPES = frozenset(
    {
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

        # The registry lock only protects shared maps. Backend I/O is serialized
        # per session, so a slow session cannot stall unrelated sessions.
        self._lock = threading.Lock()
        self._session_locks = tuple(threading.RLock() for _ in range(256))
        self._buffer_bytes: dict[tuple[str, str], int] = {}
        self._buffer_started_at: dict[tuple[str, str], float] = {}
        self._metrics_lock = threading.Lock()
        self._flushes_total = 0
        self._flush_failures_total = 0
        self._flushed_events_total = 0
        self._flushed_bytes_total = 0
        self._last_flush_duration_ms = 0.0
        self._flush_latency = Histogram()
        self._flush_batch_events = Histogram((1, 2, 5, 10, 25, 50, 100, 250, 500))
        self._flush_batch_bytes = Histogram(
            (1024, 4096, 16384, 65536, 262144, 1048576, 4194304)
        )
        self._reconciliation = {
            "running": False,
            "sessions_scanned_total": 0,
            "sessions_reconciled_total": 0,
            "failures_total": 0,
            "last_duration_ms": 0.0,
        }

        # EventBus reference (set after initialization via set_event_bus)
        self._event_bus: Any = None
        self._audit_store: Any = None
        self._session_store: Any = None
        self._audit_reconcile_pending: dict[tuple[str, str], int] = {}

        logger.info(
            "Event store initialized "
            "(backend=%s, flush_size=%d, flush_bytes=%d, flush_interval=%ds)",
            config.backend,
            config.flush_size,
            config.flush_bytes,
            config.flush_interval,
        )

    def _session_lock(self, key: tuple[str, str]) -> threading.RLock:
        """Return a stable striped lock without per-session registry growth."""
        return self._session_locks[hash(key) % len(self._session_locks)]

    def metrics(self) -> dict[str, Any]:
        """Return local event-store performance and buffer metrics."""
        with self._metrics_lock:
            result: dict[str, Any] = {
                "flushes_total": self._flushes_total,
                "flush_failures_total": self._flush_failures_total,
                "flushed_events_total": self._flushed_events_total,
                "flushed_bytes_total": self._flushed_bytes_total,
                "last_flush_duration_ms": self._last_flush_duration_ms,
                "flush_latency": self._flush_latency.snapshot(),
                "flush_batch_events": self._flush_batch_events.snapshot(),
                "flush_batch_bytes": self._flush_batch_bytes.snapshot(),
                "reconciliation": dict(self._reconciliation),
            }
        result["buffered_sessions"] = self.buffered_session_count
        result["buffered_events"] = self.buffered_event_count
        backend_metrics = getattr(self._backend, "metrics", None)
        if callable(backend_metrics):
            result["backend"] = backend_metrics()
        return result

    def set_event_bus(self, event_bus: Any) -> None:
        """Set the EventBus for live tailing.

        Called during lifespan initialization after both EventStore
        and EventBus are created.
        """
        self._event_bus = event_bus

    def set_audit_store(self, audit_store: Any) -> None:
        """Set the optional relational projection for auditable events."""
        self._audit_store = audit_store

    def set_session_store(self, session_store: Any) -> None:
        """Set the relational store used for durable sequence allocation."""
        self._session_store = session_store

    def reconcile_session_user_message_state(self) -> int:
        """Reconstruct relational event high-water state from event logs.

        This startup reconciliation is idempotent. It only advances relational
        values, so repeated runs cannot overwrite newer live append state.
        """
        if self._session_store is None:
            raise RuntimeError("Session store is required for event reconciliation")

        started_at = time.monotonic()
        with self._metrics_lock:
            self._reconciliation["running"] = True
        reconciled = 0
        try:
            for (
                user_id,
                session_id,
            ) in self._session_store.list_event_reconciliation_sessions():
                with self._metrics_lock:
                    self._reconciliation["sessions_scanned_total"] += 1
                try:
                    last_event_seq = self.last_seq(user_id, session_id)
                    user_events = self.read(
                        user_id,
                        session_id,
                        event_types={"user_message", "message"},
                    )
                    latest_user_message_seq = max(
                        (
                            int(event.get("seq") or 0)
                            for event in user_events
                            if self._is_user_message_event(event)
                            and int(event.get("seq") or 0) > 0
                        ),
                        default=None,
                    )
                    if last_event_seq <= 0 and latest_user_message_seq is None:
                        continue
                    self._session_store.reconcile_event_high_water(
                        session_id,
                        user_id=user_id,
                        last_event_seq=last_event_seq,
                        latest_user_message_seq=latest_user_message_seq,
                    )
                    reconciled += 1
                    with self._metrics_lock:
                        self._reconciliation["sessions_reconciled_total"] += 1
                except Exception:
                    with self._metrics_lock:
                        self._reconciliation["failures_total"] += 1
                    raise
            return reconciled
        finally:
            with self._metrics_lock:
                self._reconciliation["running"] = False
                self._reconciliation["last_duration_ms"] = round(
                    (time.monotonic() - started_at) * 1000, 3
                )

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

        key = (user_id, session_id)
        with self._session_lock(key):
            # Lazy recovery of sequence counter from backend.
            # On failure, propagate the error to avoid seq collisions
            # with existing persisted events.
            with self._lock:
                current_seq = self._seq_counters.get(key)
            if current_seq is None:
                try:
                    current_seq = self._backend.last_seq(user_id, session_id)
                    with self._lock:
                        self._seq_counters[key] = current_seq
                except Exception:
                    logger.exception(
                        "Failed to recover last_seq for %s/%s — "
                        "refusing to start from 0 (would risk seq collisions)",
                        user_id,
                        session_id,
                    )
                    raise

            if self._session_store is not None:
                assigned_seqs = self._session_store.allocate_event_sequences(
                    session_id,
                    user_id=user_id,
                    user_message_flags=[
                        self._is_user_message_event(event) for event in events
                    ],
                    minimum_last_seq=current_seq,
                )
                current_seq = assigned_seqs[-1]
                with self._lock:
                    self._seq_counters[key] = current_seq

            # Assign seq and ts to each event (copy to avoid mutating caller's dicts)
            enriched: list[dict] = []
            for index, event in enumerate(events):
                if self._session_store is None:
                    current_seq += 1
                    assigned_seqs.append(current_seq)
                seq = assigned_seqs[index]
                enriched_event = dict(event)
                enriched_event["seq"] = seq
                enriched_event["ts"] = now
                enriched_event["source"] = source
                enriched.append(enriched_event)

            # Buffer enriched copies
            with self._lock:
                buf = self._buffers.setdefault(key, [])
                if not buf:
                    self._buffer_started_at[key] = time.monotonic()
                buf.extend(enriched)
                self._buffer_bytes[key] = self._buffer_bytes.get(key, 0) + len(
                    _events_to_ndjson(enriched)
                )
                buffered_bytes = self._buffer_bytes[key]
                if self._session_store is None:
                    self._seq_counters[key] = current_seq

            # Tool events use the same batching policy as all other events.
            if (
                len(buf) >= self._config.flush_size
                or buffered_bytes >= self._config.flush_bytes
            ):
                self._flush_locked(key)

        self._reconcile_pending_audit({key})

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

    @staticmethod
    def _is_user_message_event(event: dict[str, Any]) -> bool:
        """Return whether an event carries a supported user message."""
        event_type = event.get("type")
        if event_type == "user_message":
            return True
        if event_type != "message":
            return False
        data = event.get("data")
        return isinstance(data, dict) and data.get("role") == "user"

    def read(
        self,
        user_id: str,
        session_id: str,
        after_seq: int = 0,
        limit: int = 0,
        event_types: set[str] | None = None,
        sources: set[str] | None = None,
        exclude_sources: set[str] | None = None,
        data_sources: set[str] | None = None,
        turn_id: str | None = None,
        min_position: int | None = None,
        max_position: int | None = None,
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
            data_sources: Include only events whose payload ``data.source`` is in
                this set. None = all payload sources.
            turn_id: Include only events whose payload ``data.turn_id`` matches.
            min_position: Include only events whose payload ``data.position`` is
                >= this value.
            max_position: Include only events whose payload ``data.position`` is
                <= this value.
            after_ts: Return events with ts >= this ISO 8601 timestamp.
            before_ts: Return events with ts <= this ISO 8601 timestamp.

        Returns:
            List of event dicts ordered by seq.
        """
        key = (user_id, session_id)
        with self._session_lock(key):
            with self._lock:
                buf = list(self._buffers.get(key, []))
        buffered = [event for event in buf if event.get("seq", 0) > after_seq]
        has_filters = bool(
            event_types
            or sources
            or exclude_sources
            or data_sources
            or turn_id is not None
            or min_position is not None
            or max_position is not None
            or after_ts
            or before_ts
        )
        if not limit:
            persisted = self._backend.read(user_id, session_id, after_seq, limit=0)
            return self._filter_and_deduplicate_events(
                persisted + buffered,
                event_types=event_types,
                sources=sources,
                exclude_sources=exclude_sources,
                data_sources=data_sources,
                turn_id=turn_id,
                min_position=min_position,
                max_position=max_position,
                after_ts=after_ts,
                before_ts=before_ts,
            )

        batch_limit = max(limit, 100) if has_filters else limit
        cursor = after_seq
        persisted_matches: list[dict] = []
        seen_persisted: set[int] = set()
        buffered_matches = self._filter_and_deduplicate_events(
            buffered,
            event_types=event_types,
            sources=sources,
            exclude_sources=exclude_sources,
            data_sources=data_sources,
            turn_id=turn_id,
            min_position=min_position,
            max_position=max_position,
            after_ts=after_ts,
            before_ts=before_ts,
        )
        while True:
            page = self._backend.read(user_id, session_id, cursor, limit=batch_limit)
            page_matches = self._filter_and_deduplicate_events(
                page,
                event_types=event_types,
                sources=sources,
                exclude_sources=exclude_sources,
                data_sources=data_sources,
                turn_id=turn_id,
                min_position=min_position,
                max_position=max_position,
                after_ts=after_ts,
                before_ts=before_ts,
            )
            for event in page_matches:
                seq = int(event.get("seq", 0))
                if seq not in seen_persisted and len(persisted_matches) < limit:
                    seen_persisted.add(seq)
                    persisted_matches.append(event)

            exhausted = not page or len(page) < batch_limit
            scanned_seq = int(page[-1].get("seq", cursor)) if page else cursor
            eligible_buffer = (
                buffered_matches
                if exhausted
                else [
                    event
                    for event in buffered_matches
                    if int(event.get("seq", 0)) <= scanned_seq
                ]
            )
            candidates = self._filter_and_deduplicate_events(
                persisted_matches + eligible_buffer,
                event_types=None,
                sources=None,
                exclude_sources=None,
                data_sources=None,
                turn_id=None,
                min_position=None,
                max_position=None,
                after_ts=None,
                before_ts=None,
            )
            if len(candidates) >= limit or exhausted:
                return candidates[:limit]
            cursor = scanned_seq

    def _filter_and_deduplicate_events(
        self,
        events: list[dict],
        *,
        event_types: set[str] | None,
        sources: set[str] | None,
        exclude_sources: set[str] | None,
        data_sources: set[str] | None,
        turn_id: str | None,
        min_position: int | None,
        max_position: int | None,
        after_ts: str | None,
        before_ts: str | None,
    ) -> list[dict]:
        """Order, deduplicate, and filter forward-read candidates."""
        events.sort(key=lambda event: event.get("seq", 0))
        seen: set[int] = set()
        filtered: list[dict] = []
        payload_filtering = (
            data_sources
            or turn_id is not None
            or min_position is not None
            or max_position is not None
        )
        for event in events:
            seq = int(event.get("seq", 0))
            if seq in seen:
                continue
            seen.add(seq)
            if not self._event_matches_filters(
                event,
                event_types=event_types,
                sources=sources,
                exclude_sources=exclude_sources,
                after_ts=after_ts,
                before_ts=before_ts,
            ):
                continue
            if payload_filtering and not self._event_matches_payload_filters(
                event,
                data_sources=data_sources,
                turn_id=turn_id,
                min_position=min_position,
                max_position=max_position,
            ):
                continue
            filtered.append(event)
        return filtered

    def read_seqs(
        self,
        user_id: str,
        session_id: str,
        seqs: set[int],
        event_types: set[str] | None = None,
        sources: set[str] | None = None,
        exclude_sources: set[str] | None = None,
        data_sources: set[str] | None = None,
        turn_id: str | None = None,
        min_position: int | None = None,
        max_position: int | None = None,
        after_ts: str | None = None,
        before_ts: str | None = None,
    ) -> list[dict]:
        """Read events by exact sequence numbers from storage and buffer."""
        if not seqs:
            return []

        events = self._backend.read_seqs(user_id, session_id, seqs)

        key = (user_id, session_id)
        with self._session_lock(key):
            with self._lock:
                buffer_events = list(self._buffers.get(key, []))
            for event in buffer_events:
                if event.get("seq") in seqs:
                    events.append(event)

        events.sort(key=lambda e: e.get("seq", 0))

        seen: set[int] = set()
        deduped: list[dict] = []
        for event in events:
            seq = event.get("seq", 0)
            if seq not in seen:
                seen.add(seq)
                deduped.append(event)
        events = deduped

        events = [
            event
            for event in events
            if self._event_matches_filters(
                event,
                event_types=event_types,
                sources=sources,
                exclude_sources=exclude_sources,
                after_ts=after_ts,
                before_ts=before_ts,
            )
        ]

        if (
            data_sources
            or turn_id is not None
            or min_position is not None
            or max_position is not None
        ):
            events = [
                event
                for event in events
                if self._event_matches_payload_filters(
                    event,
                    data_sources=data_sources,
                    turn_id=turn_id,
                    min_position=min_position,
                    max_position=max_position,
                )
            ]

        return events

    def read_before(
        self,
        user_id: str,
        session_id: str,
        before_seq: int,
        limit: int,
        event_types: set[str] | None = None,
        sources: set[str] | None = None,
        exclude_sources: set[str] | None = None,
        data_sources: set[str] | None = None,
        turn_id: str | None = None,
        min_position: int | None = None,
        max_position: int | None = None,
        after_ts: str | None = None,
        before_ts: str | None = None,
    ) -> list[dict]:
        """Read the last matching events with seq < before_seq in chronological order."""
        if before_seq <= 0 or limit <= 0:
            return []

        key = (user_id, session_id)
        with self._session_lock(key):
            with self._lock:
                buffer_events = list(self._buffers.get(key, []))

        payload_filtering = (
            data_sources
            or turn_id is not None
            or min_position is not None
            or max_position is not None
        )
        filtered_buffer = [
            event
            for event in buffer_events
            if event.get("seq", 0) < before_seq
            and self._event_matches_filters(
                event,
                event_types=event_types,
                sources=sources,
                exclude_sources=exclude_sources,
                after_ts=after_ts,
                before_ts=before_ts,
            )
            and (
                not payload_filtering
                or self._event_matches_payload_filters(
                    event,
                    data_sources=data_sources,
                    turn_id=turn_id,
                    min_position=min_position,
                    max_position=max_position,
                )
            )
        ]
        filtered_buffer.sort(key=lambda e: e.get("seq", 0))
        if len(filtered_buffer) >= limit:
            return filtered_buffer[-limit:]

        max_persisted_seq = min(
            before_seq - 1, self._backend.last_seq(user_id, session_id)
        )
        if max_persisted_seq <= 0:
            return filtered_buffer[-limit:]

        fetch_limit = min(max_persisted_seq, max(limit, limit + len(buffer_events)))
        previous_persisted_count = -1

        while fetch_limit > 0:
            persisted_events = self._backend.read_before(
                user_id,
                session_id,
                before_seq=before_seq,
                limit=fetch_limit,
                event_types=event_types,
                sources=sources,
                exclude_sources=exclude_sources,
                after_ts=after_ts,
                before_ts=before_ts,
            )
            raw_persisted_count = len(persisted_events)
            if payload_filtering:
                persisted_events = [
                    event
                    for event in persisted_events
                    if self._event_matches_payload_filters(
                        event,
                        data_sources=data_sources,
                        turn_id=turn_id,
                        min_position=min_position,
                        max_position=max_position,
                    )
                ]

            combined = persisted_events + filtered_buffer
            combined.sort(key=lambda e: e.get("seq", 0))

            seen: set[int] = set()
            deduped: list[dict] = []
            for event in combined:
                seq = event.get("seq", 0)
                if seq not in seen:
                    seen.add(seq)
                    deduped.append(event)

            if len(deduped) >= limit:
                return deduped[-limit:]
            if raw_persisted_count < fetch_limit:
                return deduped[-limit:]
            if raw_persisted_count == previous_persisted_count:
                return deduped[-limit:]
            if fetch_limit >= max_persisted_seq:
                return deduped[-limit:]

            previous_persisted_count = raw_persisted_count
            fetch_limit = min(fetch_limit * 2, max_persisted_seq)

        return filtered_buffer[-limit:]

    def read_tail(
        self,
        user_id: str,
        session_id: str,
        limit: int,
        event_types: set[str] | None = None,
        sources: set[str] | None = None,
        exclude_sources: set[str] | None = None,
        data_sources: set[str] | None = None,
        turn_id: str | None = None,
        min_position: int | None = None,
        max_position: int | None = None,
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

        # Payload-level filters require inspecting all candidate events because
        # the backend only filters on top-level fields.
        if (
            data_sources
            or turn_id is not None
            or min_position is not None
            or max_position is not None
        ):
            key = (user_id, session_id)
            with self._session_lock(key):
                with self._lock:
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
                and self._event_matches_payload_filters(
                    event,
                    data_sources=data_sources,
                    turn_id=turn_id,
                    min_position=min_position,
                    max_position=max_position,
                )
            ]
            filtered_buffer.sort(key=lambda e: e.get("seq", 0))
            if len(filtered_buffer) >= limit:
                return filtered_buffer[-limit:]

            max_persisted_seq = self._backend.last_seq(user_id, session_id)
            fetch_limit = max(limit, limit + len(buffer_events))
            previous_persisted_count = -1

            while max_persisted_seq > 0 and fetch_limit > 0:
                persisted_events = self._backend.read_tail(
                    user_id,
                    session_id,
                    limit=min(fetch_limit, max_persisted_seq),
                    event_types=event_types,
                    sources=sources,
                    exclude_sources=exclude_sources,
                    after_ts=after_ts,
                    before_ts=before_ts,
                )
                persisted_matches = [
                    event
                    for event in persisted_events
                    if self._event_matches_payload_filters(
                        event,
                        data_sources=data_sources,
                        turn_id=turn_id,
                        min_position=min_position,
                        max_position=max_position,
                    )
                ]
                combined = persisted_matches + filtered_buffer
                combined.sort(key=lambda e: e.get("seq", 0))

                seen: set[int] = set()
                deduped: list[dict] = []
                for event in combined:
                    seq = event.get("seq", 0)
                    if seq not in seen:
                        seen.add(seq)
                        deduped.append(event)

                if len(deduped) >= limit:
                    return deduped[-limit:]
                if len(persisted_events) < min(fetch_limit, max_persisted_seq):
                    return deduped[-limit:]
                if len(persisted_events) == previous_persisted_count:
                    return deduped[-limit:]
                if fetch_limit >= max_persisted_seq:
                    return deduped[-limit:]

                previous_persisted_count = len(persisted_events)
                fetch_limit = min(fetch_limit * 2, max_persisted_seq)

            return filtered_buffer[-limit:]

        key = (user_id, session_id)
        with self._session_lock(key):
            with self._lock:
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
        key = (user_id, session_id)
        with self._session_lock(key):
            with self._lock:
                current_seq = self._seq_counters.get(key)
            if current_seq is not None:
                return current_seq
        return self._backend.last_seq(user_id, session_id)

    def flush_session(self, user_id: str, session_id: str) -> None:
        """Flush buffered events for a specific session to storage.

        Called on session completion, termination, or suspension.
        """
        key = (user_id, session_id)
        with self._session_lock(key):
            self._flush_locked(key)
        self._reconcile_pending_audit({key})

    def flush_all(self) -> None:
        """Flush all buffered events to storage.

        Called on server shutdown (lifespan cleanup) and by the
        periodic flush background task.
        """
        with self._lock:
            keys = list(self._buffers)
        for key in keys:
            with self._session_lock(key):
                self._flush_locked(key)
        with self._lock:
            pending = set(self._audit_reconcile_pending)
        self._reconcile_pending_audit(pending)

    def flush_stale(self) -> int:
        """Flush buffers that reached the configured maximum age."""
        cutoff = time.monotonic() - self._config.flush_interval
        with self._lock:
            keys = [
                key
                for key, started_at in self._buffer_started_at.items()
                if started_at <= cutoff
            ]
        flushed = 0
        for key in keys:
            with self._session_lock(key):
                with self._lock:
                    started_at = self._buffer_started_at.get(key, float("inf"))
                    buffered = len(self._buffers.get(key, []))
                if started_at > cutoff:
                    continue
                flushed += buffered
                self._flush_locked(key)
        if keys:
            self._reconcile_pending_audit(set(keys))
        sweep_cache = getattr(self._backend, "sweep_cache", None)
        if callable(sweep_cache):
            sweep_cache()
        return flushed

    def delete_session(self, user_id: str, session_id: str) -> None:
        """Delete all events for a session (storage + buffer)."""
        key = (user_id, session_id)
        with self._session_lock(key):
            with self._lock:
                self._buffers.pop(key, None)
                self._buffer_bytes.pop(key, None)
                self._buffer_started_at.pop(key, None)
                self._seq_counters.pop(key, None)
        self._backend.delete_session(user_id, session_id)

    def delete_all_for_user(self, user_id: str) -> None:
        """Delete all events for a user (storage + buffer)."""
        with self._lock:
            keys_to_remove = [k for k in self._buffers if k[0] == user_id]
        for key in keys_to_remove:
            with self._session_lock(key):
                with self._lock:
                    self._buffers.pop(key, None)
                    self._buffer_bytes.pop(key, None)
                    self._buffer_started_at.pop(key, None)
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

    @staticmethod
    def _event_matches_payload_filters(
        event: dict,
        *,
        data_sources: set[str] | None = None,
        turn_id: str | None = None,
        min_position: int | None = None,
        max_position: int | None = None,
    ) -> bool:
        """Return True when payload metadata matches the requested filters."""
        data = event.get("data")
        if not isinstance(data, dict):
            return False

        payload_source = data.get("source")
        if data_sources and payload_source not in data_sources:
            return False

        if turn_id is not None and data.get("turn_id") != turn_id:
            return False

        if min_position is None and max_position is None:
            return True

        position = data.get("position")
        if not isinstance(position, int) or isinstance(position, bool):
            return False
        if min_position is not None and position < min_position:
            return False
        if max_position is not None and position > max_position:
            return False
        return True

    def exists(self, user_id: str, session_id: str) -> bool:
        """Check if any events exist for a session (storage or buffer)."""
        key = (user_id, session_id)
        with self._session_lock(key):
            with self._lock:
                buffered = bool(self._buffers.get(key))
            if buffered:
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
            candidates = list(self._seq_counters)
        for key in candidates:
            with self._session_lock(key):
                with self._lock:
                    if (
                        key not in active_sessions
                        and key not in self._buffers
                        and key in self._seq_counters
                    ):
                        del self._seq_counters[key]
                        removed += 1
        if removed:
            logger.debug("Swept %d stale seq counters", removed)
        return removed

    def reconcile_audit_index(self, *, batch_size: int = 1000) -> int:
        """Backfill the relational audit projection from durable event storage."""
        if self._audit_store is None:
            return 0

        indexed = 0
        for state in self._audit_store.projection_sessions():
            user_id = state["user_id"]
            session_id = state["session_id"]
            after_seq = int(state["last_seq"])
            try:
                indexed += self._reconcile_audit_session(
                    user_id=user_id,
                    session_id=session_id,
                    after_seq=after_seq,
                    batch_size=batch_size,
                )
            except Exception:
                logger.exception(
                    "Failed to reconcile historical audit events for %s/%s; will retry",
                    user_id,
                    session_id,
                )
                with self._lock:
                    self._audit_reconcile_pending[(user_id, session_id)] = after_seq
        return indexed

    def _reconcile_audit_session(
        self,
        *,
        user_id: str,
        session_id: str,
        after_seq: int,
        batch_size: int = 1000,
    ) -> int:
        indexed = 0
        while True:
            events = self._backend.read(
                user_id,
                session_id,
                after_seq=after_seq,
                limit=batch_size,
            )
            if not events:
                break
            self._audit_store.index_events(
                user_id=user_id,
                session_id=session_id,
                events=events,
            )
            indexed += sum(
                event.get("type") in {"tool_call", "tool_result"} for event in events
            )
            after_seq = int(events[-1]["seq"])
            if len(events) < batch_size:
                break
        return indexed

    def _reconcile_pending_audit(
        self, keys: set[tuple[str, str]] | None = None
    ) -> None:
        if self._audit_store is None:
            return
        with self._lock:
            target_keys = keys or set(self._audit_reconcile_pending)
            pending = {
                key: self._audit_reconcile_pending.pop(key)
                for key in target_keys
                if key in self._audit_reconcile_pending
            }
        for (user_id, session_id), after_seq in pending.items():
            try:
                self._reconcile_audit_session(
                    user_id=user_id,
                    session_id=session_id,
                    after_seq=after_seq,
                )
            except Exception:
                logger.exception(
                    "Failed to reconcile audit events for %s/%s; will retry",
                    user_id,
                    session_id,
                )
                with self._lock:
                    existing = self._audit_reconcile_pending.get(
                        (user_id, session_id), after_seq
                    )
                    self._audit_reconcile_pending[(user_id, session_id)] = min(
                        existing, after_seq
                    )

    @property
    def buffered_session_count(self) -> int:
        """Number of sessions with buffered (unflushed) events."""
        with self._lock:
            return len(self._buffers)

    @property
    def buffered_event_count(self) -> int:
        """Total number of buffered (unflushed) events across all sessions."""
        with self._lock:
            keys = list(self._buffers)
        total = 0
        for key in keys:
            with self._session_lock(key):
                with self._lock:
                    total += len(self._buffers.get(key, []))
        return total

    def _flush_locked(self, key: tuple[str, str]) -> None:
        """Flush one detached session buffer while holding its session lock."""
        with self._lock:
            buf = self._buffers.pop(key, [])
            self._buffer_bytes.pop(key, None)
            self._buffer_started_at.pop(key, None)
        if not buf:
            return
        user_id, session_id = key
        payload_bytes = len(_events_to_ndjson(buf))
        started_at = time.monotonic()
        try:
            self._backend.append(user_id, session_id, buf)
            if self._audit_store is not None:
                try:
                    checkpoint = self._audit_store.index_events(
                        user_id=user_id,
                        session_id=session_id,
                        events=buf,
                    )
                    if checkpoint < int(buf[-1]["seq"]):
                        existing = self._audit_reconcile_pending.get(key, checkpoint)
                        self._audit_reconcile_pending[key] = min(existing, checkpoint)
                except Exception:
                    logger.exception(
                        "Failed to index durable audit events for %s/%s; "
                        "startup reconciliation will retry",
                        user_id,
                        session_id,
                    )
                    self._audit_reconcile_pending[key] = 0
            logger.debug(
                "Flushed %d events for %s/%s (seq %d-%d)",
                len(buf),
                user_id,
                session_id,
                buf[0]["seq"],
                buf[-1]["seq"],
            )
            with self._metrics_lock:
                self._flushes_total += 1
                self._flushed_events_total += len(buf)
                self._flushed_bytes_total += payload_bytes
                self._last_flush_duration_ms = (time.monotonic() - started_at) * 1000
                self._flush_latency.observe(self._last_flush_duration_ms)
                self._flush_batch_events.observe(len(buf))
                self._flush_batch_bytes.observe(payload_bytes)
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
            with self._metrics_lock:
                self._flush_failures_total += 1
                self._last_flush_duration_ms = (time.monotonic() - started_at) * 1000
                self._flush_latency.observe(self._last_flush_duration_ms)
            with self._lock:
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
            with self._lock:
                self._buffers[key] = combined
                self._buffer_bytes[key] = len(_events_to_ndjson(combined))
                self._buffer_started_at.setdefault(key, time.monotonic())
