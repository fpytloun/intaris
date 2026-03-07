"""Session management for intaris.

Handles session CRUD operations and counter updates for tracking
evaluation statistics per session.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from intaris.db import Database

logger = logging.getLogger(__name__)


class SessionStore:
    """Session CRUD and counter management backed by SQLite."""

    def __init__(self, db: Database):
        self._db = db

    def create(
        self,
        session_id: str,
        intention: str,
        details: dict[str, Any] | None = None,
        policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a new session.

        Args:
            session_id: Unique session identifier.
            intention: Declared purpose of the session.
            details: Optional JSON-serializable session details
                     (repo, branch, constraints, etc.).
            policy: Optional JSON-serializable session policy
                    (custom classifier rules, risk overrides).

        Returns:
            The created session as a dict.

        Raises:
            ValueError: If session_id already exists.
        """
        now = datetime.now(timezone.utc).isoformat()
        details_json = json.dumps(details) if details else None
        policy_json = json.dumps(policy) if policy else None

        with self._db.cursor() as cur:
            try:
                cur.execute(
                    """
                    INSERT INTO sessions
                        (session_id, intention, details, policy,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (session_id, intention, details_json, policy_json, now, now),
                )
            except Exception as e:
                if "UNIQUE constraint" in str(e):
                    raise ValueError(f"Session {session_id} already exists") from e
                raise

        return self.get(session_id)

    def get(self, session_id: str) -> dict[str, Any]:
        """Get a session by ID.

        Returns:
            Session as a dict.

        Raises:
            ValueError: If session not found.
        """
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            )
            row = cur.fetchone()

        if row is None:
            raise ValueError(f"Session {session_id} not found")

        return _row_to_dict(row)

    def update_status(self, session_id: str, status: str) -> None:
        """Update session status.

        Args:
            session_id: Session to update.
            status: New status (active, completed, suspended, terminated).

        Raises:
            ValueError: If session not found or invalid status.
        """
        valid_statuses = {"active", "completed", "suspended", "terminated"}
        if status not in valid_statuses:
            raise ValueError(
                f"Invalid status '{status}'. Must be one of: {valid_statuses}"
            )

        now = datetime.now(timezone.utc).isoformat()
        with self._db.cursor() as cur:
            cur.execute(
                """
                UPDATE sessions
                SET status = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (status, now, session_id),
            )
            if cur.rowcount == 0:
                raise ValueError(f"Session {session_id} not found")

    def increment_counter(self, session_id: str, decision: str) -> None:
        """Increment the appropriate counter after an evaluation.

        Updates total_calls and the decision-specific counter atomically.

        Args:
            session_id: Session to update.
            decision: The evaluation decision (approve, deny, escalate).

        Raises:
            ValueError: If session not found or invalid decision.
        """
        counter_map = {
            "approve": "approved_count",
            "deny": "denied_count",
            "escalate": "escalated_count",
        }
        counter = counter_map.get(decision)
        if counter is None:
            raise ValueError(
                f"Invalid decision '{decision}'. "
                f"Must be one of: {set(counter_map.keys())}"
            )

        now = datetime.now(timezone.utc).isoformat()
        with self._db.cursor() as cur:
            cur.execute(
                f"""
                UPDATE sessions
                SET total_calls = total_calls + 1,
                    {counter} = {counter} + 1,
                    updated_at = ?
                WHERE session_id = ?
                """,
                (now, session_id),
            )
            if cur.rowcount == 0:
                raise ValueError(f"Session {session_id} not found")

    def list_sessions(
        self,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List sessions, optionally filtered by status.

        Returns:
            List of session dicts, ordered by most recent first.
        """
        with self._db.cursor() as cur:
            if status:
                cur.execute(
                    """
                    SELECT * FROM sessions
                    WHERE status = ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (status, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT * FROM sessions
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            rows = cur.fetchall()

        return [_row_to_dict(row) for row in rows]


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a sqlite3.Row to a plain dict, parsing JSON fields."""
    d = dict(row)
    for json_field in ("details", "policy"):
        if d.get(json_field):
            try:
                d[json_field] = json.loads(d[json_field])
            except (json.JSONDecodeError, TypeError):
                pass
    return d
