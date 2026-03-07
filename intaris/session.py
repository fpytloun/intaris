"""Session management for intaris.

Handles session CRUD operations and counter updates for tracking
evaluation statistics per session. All operations are scoped by
user_id for multi-tenant isolation.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from intaris.db import Database

logger = logging.getLogger(__name__)


class SessionStore:
    """Session CRUD and counter management backed by SQLite.

    All operations require user_id for tenant isolation.
    """

    def __init__(self, db: Database):
        self._db = db

    def create(
        self,
        *,
        user_id: str,
        session_id: str,
        intention: str,
        details: dict[str, Any] | None = None,
        policy: dict[str, Any] | None = None,
        parent_session_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a new session.

        Args:
            user_id: Tenant identifier (owner of this session).
            session_id: Unique session identifier.
            intention: Declared purpose of the session.
            details: Optional JSON-serializable session details
                     (repo, branch, constraints, etc.).
            policy: Optional JSON-serializable session policy
                    (custom classifier rules, risk overrides).
            parent_session_id: Optional parent session for continuation chains.

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
                        (session_id, user_id, intention, details, policy,
                         last_activity_at, parent_session_id,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        user_id,
                        intention,
                        details_json,
                        policy_json,
                        now,
                        parent_session_id,
                        now,
                        now,
                    ),
                )
            except Exception as e:
                if "UNIQUE constraint" in str(e):
                    raise ValueError(f"Session {session_id} already exists") from e
                raise

        return self.get(session_id=session_id, user_id=user_id)

    def get(self, session_id: str, *, user_id: str) -> dict[str, Any]:
        """Get a session by ID, scoped to user.

        Args:
            session_id: Session to retrieve.
            user_id: Tenant identifier (must match session owner).

        Returns:
            Session as a dict.

        Raises:
            ValueError: If session not found or belongs to another user.
        """
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT * FROM sessions WHERE session_id = ? AND user_id = ?",
                (session_id, user_id),
            )
            row = cur.fetchone()

        if row is None:
            raise ValueError(f"Session {session_id} not found")

        return _row_to_dict(row)

    def update_status(self, session_id: str, status: str, *, user_id: str) -> None:
        """Update session status.

        Args:
            session_id: Session to update.
            status: New status (active, completed, suspended, terminated).
            user_id: Tenant identifier (must match session owner).

        Raises:
            ValueError: If session not found or invalid status.
        """
        valid_statuses = {"active", "idle", "completed", "suspended", "terminated"}
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
                WHERE session_id = ? AND user_id = ?
                """,
                (status, now, session_id, user_id),
            )
            if cur.rowcount == 0:
                raise ValueError(f"Session {session_id} not found")

    def increment_counter(
        self, session_id: str, decision: str, *, user_id: str
    ) -> None:
        """Increment the appropriate counter after an evaluation.

        Updates total_calls and the decision-specific counter atomically.

        Args:
            session_id: Session to update.
            decision: The evaluation decision (approve, deny, escalate).
            user_id: Tenant identifier (must match session owner).

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
                WHERE session_id = ? AND user_id = ?
                """,
                (now, session_id, user_id),
            )
            if cur.rowcount == 0:
                raise ValueError(f"Session {session_id} not found")

    def update_activity(self, session_id: str, *, user_id: str) -> None:
        """Update last_activity_at timestamp for a session.

        Called on every evaluate, reasoning, and checkpoint call to
        track session activity for idle detection.

        Args:
            session_id: Session to update.
            user_id: Tenant identifier (must match session owner).
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._db.cursor() as cur:
            cur.execute(
                """
                UPDATE sessions
                SET last_activity_at = ?, updated_at = ?
                WHERE session_id = ? AND user_id = ?
                """,
                (now, now, session_id, user_id),
            )

    def sweep_idle_sessions(self, cutoff_time: str) -> list[tuple[str, str]]:
        """Transition active sessions to idle if inactive past cutoff.

        Uses an atomic conditional UPDATE to prevent TOCTOU races
        between the sweeper and concurrent evaluate calls.

        Args:
            cutoff_time: ISO timestamp. Sessions with last_activity_at
                before this time are transitioned to idle.

        Returns:
            List of (user_id, session_id) pairs that were transitioned.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._db.cursor() as cur:
            cur.execute(
                """
                UPDATE sessions
                SET status = 'idle', updated_at = ?
                WHERE status = 'active'
                  AND last_activity_at IS NOT NULL
                  AND last_activity_at < ?
                RETURNING user_id, session_id
                """,
                (now, cutoff_time),
            )
            rows = cur.fetchall()

        return [(row[0], row[1]) for row in rows]

    def increment_summary_count(self, session_id: str, *, user_id: str) -> None:
        """Increment the summary_count for a session.

        Called after a summary is successfully generated for this session.

        Args:
            session_id: Session to update.
            user_id: Tenant identifier (must match session owner).
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._db.cursor() as cur:
            cur.execute(
                """
                UPDATE sessions
                SET summary_count = COALESCE(summary_count, 0) + 1,
                    updated_at = ?
                WHERE session_id = ? AND user_id = ?
                """,
                (now, session_id, user_id),
            )

    def list_sessions(
        self,
        *,
        user_id: str,
        status: str | None = None,
        page: int = 1,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List sessions for a user with pagination and optional status filter.

        Args:
            user_id: Tenant identifier.
            status: Optional status filter.
            page: Page number (1-indexed).
            limit: Max results per page.

        Returns:
            Dict with items, total, page, pages.
        """
        offset = (page - 1) * limit

        with self._db.cursor() as cur:
            # Count total matching sessions
            if status:
                cur.execute(
                    "SELECT COUNT(*) FROM sessions WHERE user_id = ? AND status = ?",
                    (user_id, status),
                )
            else:
                cur.execute(
                    "SELECT COUNT(*) FROM sessions WHERE user_id = ?",
                    (user_id,),
                )
            total = cur.fetchone()[0]

            # Fetch page
            if status:
                cur.execute(
                    """
                    SELECT * FROM sessions
                    WHERE user_id = ? AND status = ?
                    ORDER BY updated_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (user_id, status, limit, offset),
                )
            else:
                cur.execute(
                    """
                    SELECT * FROM sessions
                    WHERE user_id = ?
                    ORDER BY updated_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (user_id, limit, offset),
                )
            rows = cur.fetchall()

        pages = max(1, (total + limit - 1) // limit)
        return {
            "items": [_row_to_dict(row) for row in rows],
            "total": total,
            "page": page,
            "pages": pages,
        }


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
