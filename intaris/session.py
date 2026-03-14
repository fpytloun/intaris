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
        agent_id: str | None = None,
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
            agent_id: Optional agent identifier (from X-Agent-Id header).

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
                         last_activity_at, parent_session_id, agent_id,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        user_id,
                        intention,
                        details_json,
                        policy_json,
                        now,
                        parent_session_id,
                        agent_id,
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

    def update_status(
        self,
        session_id: str,
        status: str,
        *,
        user_id: str,
        status_reason: str | None = None,
    ) -> None:
        """Update session status.

        Args:
            session_id: Session to update.
            status: New status (active, completed, suspended, terminated).
            user_id: Tenant identifier (must match session owner).
            status_reason: Optional reason for the status change. Cleared
                automatically when status transitions to ``active``
                (reactivation clears the reason).

        Raises:
            ValueError: If session not found or invalid status.
        """
        valid_statuses = {"active", "idle", "completed", "suspended", "terminated"}
        if status not in valid_statuses:
            raise ValueError(
                f"Invalid status '{status}'. Must be one of: {valid_statuses}"
            )

        # Clear status_reason on reactivation (user override)
        if status == "active":
            status_reason = None

        now = datetime.now(timezone.utc).isoformat()
        with self._db.cursor() as cur:
            cur.execute(
                """
                UPDATE sessions
                SET status = ?, status_reason = ?, updated_at = ?
                WHERE session_id = ? AND user_id = ?
                """,
                (status, status_reason, now, session_id, user_id),
            )
            if cur.rowcount == 0:
                raise ValueError(f"Session {session_id} not found")

    def update_session(
        self,
        session_id: str,
        *,
        user_id: str,
        intention: str | None = None,
        details: dict[str, Any] | None = None,
        intention_source: str | None = None,
    ) -> dict[str, Any]:
        """Update session intention and/or details.

        Only updates fields that are provided (not None).

        Args:
            session_id: Session to update.
            user_id: Tenant identifier (must match session owner).
            intention: New intention text (optional).
            details: New details dict (optional, replaces existing).
            intention_source: How the intention was set: "initial",
                "user" (from user message), or "bootstrap" (one-time
                refinement from tool patterns). Optional.

        Returns:
            Updated session as a dict.

        Raises:
            ValueError: If session not found.
        """
        if intention is None and details is None and intention_source is None:
            return self.get(session_id, user_id=user_id)

        now = datetime.now(timezone.utc).isoformat()
        sets = ["updated_at = ?"]
        params: list[Any] = [now]

        if intention is not None:
            sets.append("intention = ?")
            params.append(intention)
        if details is not None:
            sets.append("details = ?")
            params.append(json.dumps(details))
        if intention_source is not None:
            sets.append("intention_source = ?")
            params.append(intention_source)

        params.extend([session_id, user_id])

        with self._db.cursor() as cur:
            cur.execute(
                f"UPDATE sessions SET {', '.join(sets)} "
                f"WHERE session_id = ? AND user_id = ?",
                params,
            )
            if cur.rowcount == 0:
                raise ValueError(f"Session {session_id} not found")

        return self.get(session_id, user_id=user_id)

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

    def sweep_child_sessions(self, cutoff_time: str) -> list[tuple[str, str]]:
        """Auto-complete idle child sessions that have been idle past cutoff.

        Child sessions (those with parent_session_id set) are completed
        faster than parent sessions as a defense-in-depth measure for
        sub-agent sessions that don't signal completion.

        Args:
            cutoff_time: ISO timestamp. Idle child sessions with
                last_activity_at before this time are completed.

        Returns:
            List of (user_id, session_id) pairs that were completed.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._db.cursor() as cur:
            cur.execute(
                """
                UPDATE sessions
                SET status = 'completed', updated_at = ?
                WHERE status = 'idle'
                  AND parent_session_id IS NOT NULL
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

    def set_alignment_overridden(
        self,
        session_id: str,
        *,
        user_id: str,
        overridden: bool,
    ) -> None:
        """Set or clear the alignment_overridden flag on a session.

        Called when a user acknowledges (approves) an alignment escalation,
        or when the child session's intention changes (clears the flag so
        the alignment barrier re-checks).

        Args:
            session_id: Session to update.
            user_id: Tenant identifier (must match session owner).
            overridden: True to mark as overridden, False to clear.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._db.cursor() as cur:
            cur.execute(
                """
                UPDATE sessions
                SET alignment_overridden = ?, updated_at = ?
                WHERE session_id = ? AND user_id = ?
                """,
                (1 if overridden else 0, now, session_id, user_id),
            )

    def get_active_child_sessions(
        self,
        *,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get active child sessions that need alignment re-checking.

        Returns child sessions (parent_session_id IS NOT NULL) that are
        active and have NOT been alignment-overridden by the user. Used
        on startup to re-trigger alignment checks after server restart.

        Args:
            user_id: Optional tenant filter. If None, returns for all users.

        Returns:
            List of dicts with user_id and session_id.
        """
        conditions = [
            "parent_session_id IS NOT NULL",
            "status = 'active'",
            "COALESCE(alignment_overridden, 0) = 0",
        ]
        params: list[Any] = []

        if user_id is not None:
            conditions.append("user_id = ?")
            params.append(user_id)

        where = " AND ".join(conditions)
        with self._db.cursor() as cur:
            cur.execute(
                f"SELECT user_id, session_id FROM sessions WHERE {where}",
                params,
            )
            rows = cur.fetchall()

        return [{"user_id": row[0], "session_id": row[1]} for row in rows]

    def list_sessions(
        self,
        *,
        user_id: str,
        status: str | None = None,
        search: str | None = None,
        agent_id: str | None = None,
        parent_session_id: str | None = None,
        page: int = 1,
        limit: int = 50,
        tree: bool = False,
    ) -> dict[str, Any]:
        """List sessions for a user with pagination and optional filters.

        Args:
            user_id: Tenant identifier.
            status: Optional status filter (exact match).
            search: Optional text search on session_id and intention.
            agent_id: Optional agent_id filter (exact match).
            parent_session_id: Optional filter for child sessions of a
                given parent (exact match).
            page: Page number (1-indexed).
            limit: Max results per page.
            tree: When True, enables tree-aware filtering:
                - Status filter applies only to root sessions
                - Search matches roots and children, but always returns
                  the full tree (parent + all children) for any match
                - Pagination counts only root sessions
                - All children of paginated roots are included

        Returns:
            Dict with items, total, page, pages.
        """
        if tree and not parent_session_id:
            return self._list_sessions_tree(
                user_id=user_id,
                status=status,
                search=search,
                agent_id=agent_id,
                page=page,
                limit=limit,
            )

        offset = (page - 1) * limit

        # Build dynamic WHERE clause
        conditions = ["user_id = ?"]
        params: list[Any] = [user_id]

        if status:
            conditions.append("status = ?")
            params.append(status)

        if agent_id:
            conditions.append("agent_id = ?")
            params.append(agent_id)

        if parent_session_id:
            conditions.append("parent_session_id = ?")
            params.append(parent_session_id)

        if search:
            conditions.append(
                "(session_id LIKE ? ESCAPE '\\' OR intention LIKE ? ESCAPE '\\')"
            )
            # Escape LIKE metacharacters so literal %, _ in search work correctly
            escaped = (
                search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            like_pattern = f"%{escaped}%"
            params.extend([like_pattern, like_pattern])

        where = " AND ".join(conditions)

        with self._db.cursor() as cur:
            # Count total matching sessions
            cur.execute(f"SELECT COUNT(*) FROM sessions WHERE {where}", params)
            total = cur.fetchone()[0]

            # Fetch page
            cur.execute(
                f"""
                SELECT * FROM sessions
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            )
            rows = cur.fetchall()

        pages = max(1, (total + limit - 1) // limit)
        return {
            "items": [_row_to_dict(row) for row in rows],
            "total": total,
            "page": page,
            "pages": pages,
        }

    def _list_sessions_tree(
        self,
        *,
        user_id: str,
        status: str | None = None,
        search: str | None = None,
        agent_id: str | None = None,
        page: int = 1,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Tree-aware session listing.

        Filters and pagination apply to root sessions only. All children
        of visible roots are included regardless of their status. When
        search matches a child, its parent is also included.
        """
        offset = (page - 1) * limit

        # Base conditions shared by all queries
        base_cond = "user_id = ?"
        base_params: list[Any] = [user_id]
        if agent_id:
            base_cond += " AND agent_id = ?"
            base_params.append(agent_id)

        with self._db.cursor() as cur:
            # -- Step 1: Find root sessions matching status + search --------
            root_conditions = [base_cond, "parent_session_id IS NULL"]
            root_params: list[Any] = list(base_params)

            if status:
                root_conditions.append("status = ?")
                root_params.append(status)

            if search:
                root_conditions.append(
                    "(session_id LIKE ? ESCAPE '\\' OR intention LIKE ? ESCAPE '\\')"
                )
                escaped = (
                    search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                )
                like = f"%{escaped}%"
                root_params.extend([like, like])

            root_where = " AND ".join(root_conditions)

            # -- Step 2: If searching, also find parents of matching
            #    children (include parent even if it doesn't match filters)
            if search:
                escaped = (
                    search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                )
                like = f"%{escaped}%"
                # Collect root IDs from both sources using UNION
                root_id_sql = f"""
                    SELECT session_id FROM sessions
                    WHERE {root_where}
                    UNION
                    SELECT DISTINCT parent_session_id FROM sessions
                    WHERE {base_cond}
                      AND parent_session_id IS NOT NULL
                      AND (session_id LIKE ? ESCAPE '\\' OR intention LIKE ? ESCAPE '\\')
                """
                root_id_params = [
                    *root_params,
                    *base_params,
                    like,
                    like,
                ]
            else:
                root_id_sql = f"SELECT session_id FROM sessions WHERE {root_where}"
                root_id_params = list(root_params)

            # -- Step 3: Count visible roots for pagination ----------------
            cur.execute(
                f"SELECT COUNT(*) FROM ({root_id_sql})",
                root_id_params,
            )
            total = cur.fetchone()[0]

            # -- Step 4: Paginate root IDs ---------------------------------
            paginated_root_sql = f"""
                SELECT s.* FROM sessions s
                INNER JOIN ({root_id_sql}) AS roots
                    ON s.session_id = roots.session_id
                ORDER BY s.last_activity_at DESC, s.created_at DESC
                LIMIT ? OFFSET ?
            """
            paginated_root_params = [*root_id_params, limit, offset]
            cur.execute(paginated_root_sql, paginated_root_params)
            root_rows = cur.fetchall()

            # -- Step 5: Fetch ALL children of paginated roots -------------
            root_ids = [dict(r)["session_id"] for r in root_rows]
            child_rows: list[Any] = []
            if root_ids:
                placeholders = ",".join("?" * len(root_ids))
                cur.execute(
                    f"""
                    SELECT * FROM sessions
                    WHERE {base_cond}
                      AND parent_session_id IN ({placeholders})
                    ORDER BY created_at DESC
                    """,
                    [*base_params, *root_ids],
                )
                child_rows = cur.fetchall()

        pages = max(1, (total + limit - 1) // limit)
        items = [_row_to_dict(r) for r in root_rows] + [
            _row_to_dict(r) for r in child_rows
        ]
        return {
            "items": items,
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
