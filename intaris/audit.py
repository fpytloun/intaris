"""Audit log storage for intaris.

Records every tool call evaluation with decision, reasoning, redacted
args, and timing information. Supports filtered queries for the audit
log browser and session detail views.
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
    """Audit log CRUD backed by SQLite."""

    def __init__(self, db: Database):
        self._db = db

    def insert(
        self,
        *,
        call_id: str,
        session_id: str,
        agent_id: str | None,
        tool: str,
        args_redacted: dict[str, Any],
        classification: str,
        evaluation_path: str,
        decision: str,
        risk: str | None,
        reasoning: str | None,
        latency_ms: int,
    ) -> dict[str, Any]:
        """Insert an audit record.

        Args:
            call_id: External correlation ID for the tool call.
            session_id: Session this call belongs to.
            agent_id: Agent that made the call (optional).
            tool: Tool name (e.g., "bash", "edit", "mcp:add_memory").
            args_redacted: Tool arguments with secrets redacted.
            classification: "read" or "write".
            evaluation_path: "fast", "critical", or "llm".
            decision: "approve", "deny", or "escalate".
            risk: Risk level from LLM evaluation (null for fast path).
            reasoning: LLM reasoning or pattern match explanation.
            latency_ms: Time taken for the evaluation in milliseconds.

        Returns:
            The created audit record as a dict.
        """
        record_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        with self._db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_log
                    (id, call_id, session_id, agent_id, timestamp,
                     tool, args_redacted, classification, evaluation_path,
                     decision, risk, reasoning, latency_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    call_id,
                    session_id,
                    agent_id,
                    now,
                    tool,
                    json.dumps(args_redacted),
                    classification,
                    evaluation_path,
                    decision,
                    risk,
                    reasoning,
                    latency_ms,
                ),
            )

        return self.get_by_call_id(call_id)

    def get_by_call_id(self, call_id: str) -> dict[str, Any]:
        """Get an audit record by call_id.

        Raises:
            ValueError: If record not found.
        """
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT * FROM audit_log WHERE call_id = ?",
                (call_id,),
            )
            row = cur.fetchone()

        if row is None:
            raise ValueError(f"Audit record with call_id={call_id} not found")

        return _row_to_dict(row)

    def get_by_id(self, record_id: str) -> dict[str, Any]:
        """Get an audit record by internal ID.

        Raises:
            ValueError: If record not found.
        """
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT * FROM audit_log WHERE id = ?",
                (record_id,),
            )
            row = cur.fetchone()

        if row is None:
            raise ValueError(f"Audit record {record_id} not found")

        return _row_to_dict(row)

    def query(
        self,
        *,
        session_id: str | None = None,
        agent_id: str | None = None,
        tool: str | None = None,
        decision: str | None = None,
        risk: str | None = None,
        evaluation_path: str | None = None,
        from_ts: str | None = None,
        to_ts: str | None = None,
        page: int = 1,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Query audit records with filters and pagination.

        Returns:
            Dict with 'items', 'total', 'page', 'pages'.
        """
        conditions: list[str] = []
        params: list[Any] = []

        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if agent_id:
            conditions.append("agent_id = ?")
            params.append(agent_id)
        if tool:
            conditions.append("tool = ?")
            params.append(tool)
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
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Get recent audit records for a session.

        Used to assemble context for LLM safety evaluation.

        Returns:
            List of audit record dicts, most recent first.
        """
        with self._db.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM audit_log
                WHERE session_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (session_id, limit),
            )
            rows = cur.fetchall()

        return [_row_to_dict(row) for row in rows]

    def resolve_escalation(
        self,
        call_id: str,
        user_decision: str,
        user_note: str | None = None,
    ) -> dict[str, Any]:
        """Record a user's decision on an escalated tool call.

        Args:
            call_id: The escalated call to resolve.
            user_decision: "approve" or "deny".
            user_note: Optional note from the user.

        Returns:
            Updated audit record.

        Raises:
            ValueError: If record not found or not escalated.
        """
        if user_decision not in ("approve", "deny"):
            raise ValueError(
                f"Invalid user_decision '{user_decision}'. Must be 'approve' or 'deny'."
            )

        now = datetime.now(timezone.utc).isoformat()

        # Atomic update: conditions in WHERE prevent TOCTOU races.
        # Only updates if the record is escalated AND not yet resolved.
        with self._db.cursor() as cur:
            cur.execute(
                """
                UPDATE audit_log
                SET user_decision = ?, user_note = ?, resolved_at = ?
                WHERE call_id = ?
                  AND decision = 'escalate'
                  AND user_decision IS NULL
                """,
                (user_decision, user_note, now, call_id),
            )
            if cur.rowcount == 0:
                # Determine why the update failed for a clear error message
                record = self.get_by_call_id(call_id)
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

        return self.get_by_call_id(call_id)


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a sqlite3.Row to a plain dict, parsing JSON fields."""
    d = dict(row)
    if d.get("args_redacted"):
        try:
            d["args_redacted"] = json.loads(d["args_redacted"])
        except (json.JSONDecodeError, TypeError):
            pass
    return d
