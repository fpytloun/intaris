"""Relational projection and unified querying for auditable session events."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from intaris.audit import AuditStore
from intaris.db import Database

AUDIT_EVENT_TYPES = frozenset({"tool_call", "tool_result"})
AUDIT_SOURCES = frozenset({"all", "evaluations", "events"})


def _first_string(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _timestamp_key(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


def _normalize_timestamp_filter(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid audit timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _timeline_sort_key(item: dict[str, Any]) -> tuple[str, int, str, int]:
    if item.get("source") == "event":
        return (
            _timestamp_key(item.get("timestamp")),
            1,
            str(item.get("session_id") or ""),
            int(item.get("seq") or 0),
        )
    return (
        _timestamp_key(item.get("timestamp")),
        0,
        str(item.get("id") or ""),
        0,
    )


class EventAuditStore:
    """Index selected session events and query the unified audit timeline."""

    def __init__(self, db: Database):
        self._db = db

    def index_events(
        self,
        *,
        user_id: str,
        session_id: str,
        events: list[dict[str, Any]],
    ) -> int:
        """Idempotently project tool events after sequence assignment."""
        ordered_events = sorted(
            (event for event in events if isinstance(event.get("seq"), int)),
            key=lambda event: event["seq"],
        )
        if not ordered_events:
            return 0

        rows: list[tuple[Any, ...]] = []
        for event in ordered_events:
            event_type = event.get("type")
            if event_type not in AUDIT_EVENT_TYPES:
                continue
            data = event.get("data")
            if not isinstance(data, dict):
                data = {}
            seq = event.get("seq")
            timestamp = event.get("ts")
            if not isinstance(seq, int) or not timestamp:
                continue
            is_error = data.get("is_error")
            rows.append(
                (
                    user_id,
                    session_id,
                    seq,
                    timestamp,
                    event_type,
                    str(event.get("source") or "unknown"),
                    _first_string(data, "name", "tool", "tool_name"),
                    _first_string(data, "call_id", "tool_call_id"),
                    _first_string(data, "audit_call_id"),
                    bool(is_error) if isinstance(is_error, bool) else None,
                    data.get("duration_ms")
                    if isinstance(data.get("duration_ms"), int)
                    else None,
                )
            )

        with self._db.cursor() as cur:
            for row in rows:
                cur.execute(
                    """
                    INSERT INTO audit_event_index
                        (user_id, session_id, seq, timestamp, event_type,
                         event_source, tool, call_id, audit_call_id, is_error,
                         duration_ms)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (user_id, session_id, seq) DO NOTHING
                    """,
                    row,
                )
            cur.execute(
                """
                SELECT last_seq FROM audit_event_projection_state
                WHERE user_id = ? AND session_id = ?
                """,
                (user_id, session_id),
            )
            state = cur.fetchone()
            last_seq = int(state["last_seq"]) if state is not None else 0
            checkpoint = last_seq
            new_events = [
                event for event in ordered_events if int(event["seq"]) > last_seq
            ]
            if new_events and int(new_events[0]["seq"]) == last_seq + 1:
                expected = list(range(last_seq + 1, int(new_events[-1]["seq"]) + 1))
                actual = [int(event["seq"]) for event in new_events]
                if actual == expected:
                    cur.execute(
                        """
                        INSERT INTO audit_event_projection_state
                            (user_id, session_id, last_seq, updated_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT (user_id, session_id) DO UPDATE
                        SET last_seq = excluded.last_seq,
                            updated_at = excluded.updated_at
                        """,
                        (
                            user_id,
                            session_id,
                            actual[-1],
                            datetime.now(timezone.utc).isoformat(),
                        ),
                    )
                    checkpoint = actual[-1]
        return checkpoint

    def projection_sessions(self) -> list[dict[str, Any]]:
        """Return all sessions and their durable projection checkpoints."""
        with self._db.cursor() as cur:
            cur.execute(
                """
                SELECT s.user_id, s.session_id, COALESCE(p.last_seq, 0) AS last_seq
                FROM sessions s
                LEFT JOIN audit_event_projection_state p
                  ON p.user_id = s.user_id AND p.session_id = s.session_id
                ORDER BY s.user_id, s.session_id
                """
            )
            return [dict(row) for row in cur.fetchall()]

    def query(
        self,
        *,
        user_id: str,
        source: str = "evaluations",
        session_id: str | None = None,
        agent_id: str | None = None,
        record_type: str | None = None,
        tool: str | None = None,
        decision: str | None = None,
        risk: str | None = None,
        evaluation_path: str | None = None,
        from_ts: str | None = None,
        to_ts: str | None = None,
        resolved: bool | None = None,
        event_type: str | None = None,
        event_source: str | None = None,
        page: int = 1,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Return a deterministically ordered page from both audit sources."""
        if source not in AUDIT_SOURCES:
            raise ValueError(
                f"Invalid audit source '{source}'. Must be one of: {AUDIT_SOURCES}"
            )
        if event_type and event_type not in AUDIT_EVENT_TYPES:
            raise ValueError(
                f"Invalid audit event type '{event_type}'. "
                f"Must be one of: {AUDIT_EVENT_TYPES}"
            )

        from_ts = _normalize_timestamp_filter(from_ts)
        to_ts = _normalize_timestamp_filter(to_ts)
        offset = (page - 1) * limit
        branch_limit = offset + limit
        items: list[dict[str, Any]] = []
        total = 0

        decision_only_filters = any(
            value is not None
            for value in (
                record_type,
                decision,
                risk,
                evaluation_path,
                resolved,
            )
        )
        event_only_filters = event_type is not None or event_source is not None

        if source in {"all", "evaluations"} and not event_only_filters:
            audit_result = AuditStore(self._db).query(
                user_id=user_id,
                session_id=session_id,
                agent_id=agent_id,
                record_type=record_type,
                tool=tool,
                decision=decision,
                risk=risk,
                evaluation_path=evaluation_path,
                from_ts=from_ts,
                to_ts=to_ts,
                resolved=resolved,
                page=1,
                limit=branch_limit,
            )
            total += audit_result["total"]
            for item in audit_result["items"]:
                item["source"] = "evaluation"
                items.append(item)

        if source in {"all", "events"} and not decision_only_filters:
            event_result = self._query_events(
                user_id=user_id,
                session_id=session_id,
                agent_id=agent_id,
                tool=tool,
                event_type=event_type,
                event_source=event_source,
                from_ts=from_ts,
                to_ts=to_ts,
                limit=branch_limit,
            )
            total += event_result["total"]
            items.extend(event_result["items"])

        items.sort(key=_timeline_sort_key, reverse=True)
        items = items[offset : offset + limit]
        return {
            "items": items,
            "total": total,
            "page": page,
            "pages": max(1, (total + limit - 1) // limit),
        }

    def _query_events(
        self,
        *,
        user_id: str,
        session_id: str | None,
        agent_id: str | None,
        tool: str | None,
        event_type: str | None,
        event_source: str | None,
        from_ts: str | None,
        to_ts: str | None,
        limit: int,
    ) -> dict[str, Any]:
        conditions = ["e.user_id = ?"]
        params: list[Any] = [user_id]
        if session_id:
            conditions.append("e.session_id = ?")
            params.append(session_id)
        if agent_id:
            conditions.append("s.agent_id = ?")
            params.append(agent_id)
        if tool:
            operator = "ILIKE" if self._db.backend == "postgresql" else "LIKE"
            collate = "" if self._db.backend == "postgresql" else " COLLATE NOCASE"
            conditions.append(f"e.tool {operator} ?{collate}")
            params.append(f"%{tool}%")
        if event_type:
            conditions.append("e.event_type = ?")
            params.append(event_type)
        if event_source:
            conditions.append("e.event_source = ?")
            params.append(event_source)
        if from_ts:
            conditions.append("e.timestamp >= ?")
            params.append(from_ts)
        if to_ts:
            conditions.append("e.timestamp <= ?")
            params.append(to_ts)

        where = " AND ".join(conditions)
        with self._db.cursor() as cur:
            cur.execute(
                f"""
                SELECT COUNT(*)
                FROM audit_event_index e
                JOIN sessions s
                  ON s.user_id = e.user_id AND s.session_id = e.session_id
                WHERE {where}
                """,
                params,
            )
            total = cur.fetchone()[0]
            cur.execute(
                f"""
                SELECT e.*, s.agent_id
                FROM audit_event_index e
                JOIN sessions s
                  ON s.user_id = e.user_id AND s.session_id = e.session_id
                WHERE {where}
                ORDER BY e.timestamp DESC, e.session_id DESC, e.seq DESC
                LIMIT ?
                """,
                [*params, limit],
            )
            rows = [dict(row) for row in cur.fetchall()]

        items = []
        for row in rows:
            row_id = f"event:{row['session_id']}:{row['seq']}"
            items.append(
                {
                    "id": row_id,
                    "call_id": row.get("call_id"),
                    "record_type": row["event_type"],
                    "source": "event",
                    "user_id": row["user_id"],
                    "session_id": row["session_id"],
                    "agent_id": row.get("agent_id"),
                    "timestamp": row["timestamp"],
                    "tool": row.get("tool"),
                    "event_source": row["event_source"],
                    "seq": row["seq"],
                    "is_error": (
                        bool(row["is_error"])
                        if row.get("is_error") is not None
                        else None
                    ),
                    "duration_ms": row.get("duration_ms"),
                    "audit_call_id": row.get("audit_call_id"),
                }
            )
        return {"items": items, "total": total}
