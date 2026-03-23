"""Audit log storage for intaris.

Records every tool call evaluation with decision, reasoning, redacted
args, and timing information. Supports filtered queries for the audit
log browser and session detail views. All operations are scoped by
user_id for multi-tenant isolation.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from intaris.db import Database

logger = logging.getLogger(__name__)


class AuditStore:
    """Audit log CRUD backed by SQLite.

    All operations require user_id for tenant isolation.
    """

    VALID_RECORD_TYPES = {"tool_call", "reasoning", "checkpoint", "summary"}

    def __init__(self, db: Database):
        self._db = db

    def insert(
        self,
        *,
        call_id: str,
        user_id: str,
        session_id: str,
        agent_id: str | None,
        tool: str | None,
        args_redacted: dict[str, Any] | None,
        classification: str | None,
        evaluation_path: str,
        decision: str,
        risk: str | None,
        reasoning: str | None,
        latency_ms: int,
        record_type: str = "tool_call",
        content: str | None = None,
        args_hash: str | None = None,
        profile_version: int | None = None,
        intention: str | None = None,
        injection_detected: bool = False,
    ) -> dict[str, Any]:
        """Insert an audit record.

        Args:
            call_id: External correlation ID for the tool call.
            user_id: Tenant identifier.
            session_id: Session this call belongs to.
            agent_id: Agent that made the call (optional).
            tool: Tool name (e.g., "bash", "edit"). Null for reasoning checkpoints.
            args_redacted: Tool arguments with secrets redacted. Null for reasoning.
            classification: "read" or "write". Null for reasoning checkpoints.
            evaluation_path: "fast", "critical", "llm", or "reasoning".
            decision: "approve", "deny", or "escalate".
            risk: Risk level from LLM evaluation (null for fast path).
            reasoning: LLM reasoning or pattern match explanation.
            latency_ms: Time taken for the evaluation in milliseconds.
            record_type: Record type: "tool_call", "reasoning", "checkpoint",
                or "summary".
            content: Reasoning or checkpoint text (null for tool_call records).
            args_hash: SHA-256 hash of canonical args for escalation retry.
            profile_version: Behavioral profile version at time of evaluation.
            intention: Session intention at time of evaluation (for tracking).
            injection_detected: Whether prompt injection patterns were detected
                in the tool args or session intention.

        Returns:
            The created audit record as a dict.
        """
        if record_type not in self.VALID_RECORD_TYPES:
            raise ValueError(
                f"Invalid record_type '{record_type}'. "
                f"Must be one of: {self.VALID_RECORD_TYPES}"
            )

        record_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        with self._db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_log
                    (id, call_id, record_type, user_id, session_id, agent_id,
                     timestamp, tool, args_redacted, content, classification,
                     evaluation_path, decision, risk, reasoning, latency_ms,
                     args_hash, profile_version, intention, injection_detected)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    call_id,
                    record_type,
                    user_id,
                    session_id,
                    agent_id,
                    now,
                    tool,
                    json.dumps(args_redacted) if args_redacted is not None else None,
                    content,
                    classification,
                    evaluation_path,
                    decision,
                    risk,
                    reasoning,
                    latency_ms,
                    args_hash,
                    profile_version,
                    intention,
                    injection_detected,
                ),
            )

        return self.get_by_call_id(call_id, user_id=user_id)

    def get_by_call_id(self, call_id: str, *, user_id: str) -> dict[str, Any]:
        """Get an audit record by call_id, scoped to user.

        Raises:
            ValueError: If record not found.
        """
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT * FROM audit_log WHERE call_id = ? AND user_id = ?",
                (call_id, user_id),
            )
            row = cur.fetchone()

        if row is None:
            raise ValueError(f"Audit record with call_id={call_id} not found")

        return _row_to_dict(row)

    def get_by_id(self, record_id: str, *, user_id: str) -> dict[str, Any]:
        """Get an audit record by internal ID, scoped to user.

        Raises:
            ValueError: If record not found.
        """
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT * FROM audit_log WHERE id = ? AND user_id = ?",
                (record_id, user_id),
            )
            row = cur.fetchone()

        if row is None:
            raise ValueError(f"Audit record {record_id} not found")

        return _row_to_dict(row)

    def query(
        self,
        *,
        user_id: str,
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
        page: int = 1,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Query audit records with filters and pagination.

        Args:
            user_id: Tenant identifier (always required).
            record_type: Filter by record type (tool_call, reasoning, checkpoint).
            resolved: Filter by resolution status. ``True`` returns only
                records with a user_decision set, ``False`` returns only
                unresolved records (user_decision IS NULL). ``None`` (default)
                returns all records regardless of resolution status.

        Returns:
            Dict with 'items', 'total', 'page', 'pages'.
        """
        conditions: list[str] = ["user_id = ?"]
        params: list[Any] = [user_id]

        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if agent_id:
            conditions.append("agent_id = ?")
            params.append(agent_id)
        if record_type:
            conditions.append("record_type = ?")
            params.append(record_type)
        if tool:
            if self._db.backend == "postgresql":
                conditions.append("tool ILIKE ?")
            else:
                conditions.append("tool LIKE ? COLLATE NOCASE")
            params.append(f"%{tool}%")
        if decision:
            conditions.append("decision = ?")
            params.append(decision)
        if risk:
            conditions.append("risk = ?")
            params.append(risk)
        if evaluation_path:
            conditions.append("evaluation_path = ?")
            params.append(evaluation_path)
        if from_ts:
            conditions.append("timestamp >= ?")
            params.append(from_ts)
        if to_ts:
            conditions.append("timestamp <= ?")
            params.append(to_ts)
        if resolved is True:
            conditions.append("user_decision IS NOT NULL")
        elif resolved is False:
            conditions.append("user_decision IS NULL")

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        with self._db.cursor() as cur:
            # Count total matching records
            cur.execute(f"SELECT COUNT(*) FROM audit_log {where}", params)
            total = cur.fetchone()[0]

            # Fetch page
            offset = (page - 1) * limit
            cur.execute(
                f"""
                SELECT * FROM audit_log
                {where}
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
                """,
                params + [limit, offset],
            )
            rows = cur.fetchall()

        pages = max(1, (total + limit - 1) // limit)
        return {
            "items": [_row_to_dict(row) for row in rows],
            "total": total,
            "page": page,
            "pages": pages,
        }

    def get_recent(
        self,
        session_id: str,
        *,
        user_id: str,
        limit: int = 10,
        record_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get recent audit records for a session.

        Used to assemble context for LLM safety evaluation.

        Args:
            session_id: Session to query.
            user_id: Tenant identifier.
            limit: Max records to return.
            record_type: Filter by record type (e.g., "tool_call", "reasoning").

        Returns:
            List of audit record dicts, most recent first.
        """
        if record_type:
            if record_type not in self.VALID_RECORD_TYPES:
                raise ValueError(
                    f"Invalid record_type '{record_type}'. "
                    f"Must be one of: {self.VALID_RECORD_TYPES}"
                )
            sql = """
                SELECT * FROM audit_log
                WHERE session_id = ? AND user_id = ? AND record_type = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """
            params: tuple[Any, ...] = (session_id, user_id, record_type, limit)
        else:
            sql = """
                SELECT * FROM audit_log
                WHERE session_id = ? AND user_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """
            params = (session_id, user_id, limit)

        with self._db.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        return [_row_to_dict(row) for row in rows]

    def find_approved_escalation(
        self,
        *,
        user_id: str,
        tool: str,
        args_hash: str,
        cutoff: str,
    ) -> dict[str, Any] | None:
        """Find a prior approved escalation for the same tool+args.

        Used by the evaluator for escalation retry: if the same tool+args
        combination was approved within the TTL window, the approval can
        be reused.

        Args:
            user_id: Tenant identifier.
            tool: Tool name.
            args_hash: SHA-256 hash of canonical args.
            cutoff: ISO timestamp — only consider approvals after this time.

        Returns:
            Audit record dict if a matching approval is found, else None.
        """
        with self._db.cursor() as cur:
            cur.execute(
                """
                SELECT call_id, reasoning FROM audit_log
                WHERE user_id = ? AND tool = ?
                  AND args_hash = ? AND user_decision = 'approve'
                  AND resolved_at >= ?
                ORDER BY resolved_at DESC
                LIMIT 1
                """,
                (user_id, tool, args_hash, cutoff),
            )
            row = cur.fetchone()

        if row is None:
            return None
        return dict(row)

    def resolve_escalation(
        self,
        call_id: str,
        user_decision: str,
        user_note: str | None = None,
        *,
        user_id: str,
        resolved_by: str = "user",
        judge_reasoning: str | None = None,
    ) -> dict[str, Any]:
        """Record a decision on an escalated tool call.

        Args:
            call_id: The escalated call to resolve.
            user_decision: "approve" or "deny".
            user_note: Optional note from the resolver.
            user_id: Tenant identifier.
            resolved_by: Who resolved: "user" (human) or "judge" (LLM judge).
            judge_reasoning: Judge's reasoning (stored when judge resolves).

        Returns:
            Updated audit record.

        Raises:
            ValueError: If record not found or not escalated.
        """
        if user_decision not in ("approve", "deny"):
            raise ValueError(
                f"Invalid user_decision '{user_decision}'. Must be 'approve' or 'deny'."
            )
        if resolved_by not in ("user", "judge"):
            raise ValueError(
                f"Invalid resolved_by '{resolved_by}'. Must be 'user' or 'judge'."
            )

        now = datetime.now(timezone.utc).isoformat()

        # Atomic update: conditions in WHERE prevent TOCTOU races.
        # Only updates if the record is escalated AND not yet resolved.
        with self._db.cursor() as cur:
            cur.execute(
                """
                UPDATE audit_log
                SET user_decision = ?, user_note = ?, resolved_at = ?,
                    resolved_by = ?, judge_reasoning = ?
                WHERE call_id = ? AND user_id = ?
                  AND decision = 'escalate'
                  AND user_decision IS NULL
                """,
                (
                    user_decision,
                    user_note,
                    now,
                    resolved_by,
                    judge_reasoning,
                    call_id,
                    user_id,
                ),
            )
            if cur.rowcount == 0:
                # Determine why the update failed for a clear error message
                record = self.get_by_call_id(call_id, user_id=user_id)
                if record["decision"] != "escalate":
                    raise ValueError(
                        f"Call {call_id} is not escalated "
                        f"(decision={record['decision']})"
                    )
                if record.get("user_decision"):
                    raise ValueError(
                        f"Call {call_id} already resolved "
                        f"(user_decision={record['user_decision']})"
                    )
                # Shouldn't reach here, but raise generic error
                raise ValueError(f"Failed to resolve escalation for {call_id}")

        return self.get_by_call_id(call_id, user_id=user_id)

    def set_judge_reasoning(
        self,
        call_id: str,
        judge_reasoning: str,
        *,
        user_id: str,
    ) -> None:
        """Store judge reasoning on an unresolved escalation.

        Used in advisory mode when the judge defers to a human. The
        reasoning is stored so the human can see the judge's analysis
        when reviewing the escalation.

        Idempotent: multiple calls overwrite the previous reasoning.
        Only writes to unresolved escalations — if the escalation was
        resolved between the judge's check and this write, the WHERE
        clause prevents stale writes (rowcount=0, silently ignored).

        Args:
            call_id: The escalated call.
            judge_reasoning: Judge's reasoning text.
            user_id: Tenant identifier.
        """
        with self._db.cursor() as cur:
            cur.execute(
                """
                UPDATE audit_log
                SET judge_reasoning = ?
                WHERE call_id = ? AND user_id = ?
                  AND decision = 'escalate'
                  AND user_decision IS NULL
                """,
                (judge_reasoning, call_id, user_id),
            )

    def get_window(
        self,
        session_id: str,
        *,
        user_id: str,
        from_ts: str,
        to_ts: str,
        record_types: set[str] | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Get audit records within a time window for a session.

        Optimized for L2 session summary generation — fetches records
        within a specific time range, optionally filtered by record type.

        Args:
            session_id: Session to query.
            user_id: Tenant identifier.
            from_ts: Window start (inclusive) as ISO timestamp.
            to_ts: Window end (inclusive) as ISO timestamp.
            record_types: Optional set of record types to include
                (e.g., {"tool_call", "reasoning"}). None = all types.
            limit: Max records to return.

        Returns:
            List of audit record dicts, ordered by timestamp ASC
            (chronological order for analysis).
        """
        conditions = [
            "session_id = ?",
            "user_id = ?",
            "timestamp >= ?",
            "timestamp <= ?",
        ]
        params: list[Any] = [session_id, user_id, from_ts, to_ts]

        if record_types:
            valid = record_types & self.VALID_RECORD_TYPES
            if not valid:
                return []
            placeholders = ", ".join("?" for _ in valid)
            conditions.append(f"record_type IN ({placeholders})")
            params.extend(sorted(valid))

        where = "WHERE " + " AND ".join(conditions)
        limit_clause = f"LIMIT {limit}" if limit > 0 else ""

        with self._db.cursor() as cur:
            cur.execute(
                f"""
                SELECT * FROM audit_log
                {where}
                ORDER BY timestamp ASC
                {limit_clause}
                """,
                params,
            )
            rows = cur.fetchall()

        return [_row_to_dict(row) for row in rows]

    def get_session_stats(
        self,
        session_id: str,
        *,
        user_id: str,
        from_ts: str | None = None,
        to_ts: str | None = None,
    ) -> dict[str, int]:
        """Get aggregated stats for a session within an optional time window.

        Returns counts of approved, denied, escalated tool calls.

        Args:
            session_id: Session to query.
            user_id: Tenant identifier.
            from_ts: Optional window start (inclusive).
            to_ts: Optional window end (inclusive).

        Returns:
            Dict with approved_count, denied_count, escalated_count, total.
        """
        conditions = [
            "session_id = ?",
            "user_id = ?",
            "record_type = 'tool_call'",
        ]
        params: list[Any] = [session_id, user_id]

        if from_ts:
            conditions.append("timestamp >= ?")
            params.append(from_ts)
        if to_ts:
            conditions.append("timestamp <= ?")
            params.append(to_ts)

        where = "WHERE " + " AND ".join(conditions)

        with self._db.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN decision = 'approve' THEN 1 ELSE 0 END) as approved,
                    SUM(CASE WHEN decision = 'deny' THEN 1 ELSE 0 END) as denied,
                    SUM(CASE WHEN decision = 'escalate' THEN 1 ELSE 0 END) as escalated
                FROM audit_log
                {where}
                """,
                params,
            )
            row = cur.fetchone()

        if row is None:
            return {
                "approved_count": 0,
                "denied_count": 0,
                "escalated_count": 0,
                "total": 0,
            }
        return {
            "approved_count": row["approved"] or 0,
            "denied_count": row["denied"] or 0,
            "escalated_count": row["escalated"] or 0,
            "total": row["total"] or 0,
        }


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a sqlite3.Row to a plain dict, parsing JSON fields."""
    d = dict(row)
    if d.get("args_redacted"):
        try:
            d["args_redacted"] = json.loads(d["args_redacted"])
        except (json.JSONDecodeError, TypeError):
            pass
    return d
