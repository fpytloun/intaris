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
                    1 if injection_detected else 0,
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
    ) -> dict[str, Any]:
        """Record a user's decision on an escalated tool call.

        Args:
            call_id: The escalated call to resolve.
            user_decision: "approve" or "deny".
            user_note: Optional note from the user.
            user_id: Tenant identifier.

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
                WHERE call_id = ? AND user_id = ?
                  AND decision = 'escalate'
                  AND user_decision IS NULL
                """,
                (user_decision, user_note, now, call_id, user_id),
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


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a sqlite3.Row to a plain dict, parsing JSON fields."""
    d = dict(row)
    if d.get("args_redacted"):
        try:
            d["args_redacted"] = json.loads(d["args_redacted"])
        except (json.JSONDecodeError, TypeError):
            pass
    return d
