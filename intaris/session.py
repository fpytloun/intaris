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
        title: str | None = None,
        expected_policy_revision: int | None = None,
        analytics_enabled: bool = True,
        guardrails_enabled: bool = True,
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
            title: Optional short topic label for the session.

        Returns:
            The created session as a dict.

        Raises:
            ValueError: If session_id already exists.
        """
        from intaris.policy_activation import HybridError, prepare_policy

        if expected_policy_revision not in (None, 0):
            raise HybridError(
                "stale_policy_revision", "New session requires revision 0", 409
            )
        from intaris.policy_inheritance import (
            authorization_identity,
            inherit_policy,
            source_policy,
        )

        local_policy = source_policy(policy)
        parent = (
            self.refresh_inherited_policy(parent_session_id, user_id=user_id)
            if parent_session_id
            else None
        )
        policy = prepare_policy(
            inherit_policy(parent["policy"], local_policy) if parent else local_policy,
            tenant=user_id,
            scope=details,
        )
        now = datetime.now(timezone.utc).isoformat()
        details_json = json.dumps(details) if details else None
        policy_json = json.dumps(policy) if policy else None

        with self._db.cursor() as cur:
            try:
                if parent:
                    self._guard_parent(cur, parent, user_id)
                cur.execute(
                    """
                    INSERT INTO sessions
                        (session_id, user_id, intention, details, policy,
                         last_activity_at, parent_session_id, agent_id,
                           title, created_at, updated_at, alignment_state, original_assignment,
                           policy_source, parent_policy_hash, analytics_enabled, guardrails_enabled)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        title,
                        now,
                        now,
                        "pending"
                        if parent_session_id
                        and guardrails_enabled
                        and (policy or {}).get("evaluation_policy_version")
                        == "hybrid_v1"
                        else "ready",
                        intention,
                        json.dumps(local_policy),
                        authorization_identity(parent) if parent else None,
                        analytics_enabled,
                        guardrails_enabled,
                    ),
                )
            except Exception as e:
                err_str = str(e)
                # SQLite: "UNIQUE constraint failed: ..."
                # PostgreSQL: psycopg2.errors.UniqueViolation (class 23505)
                if "UNIQUE constraint" in err_str or "duplicate key" in err_str:
                    raise ValueError(f"Session {session_id} already exists") from e
                raise

        return self.get(session_id=session_id, user_id=user_id)

    def _guard_parent(self, cur, parent, user_id):
        from intaris.policy_activation import HybridError
        from intaris.policy_inheritance import validate_parent_chain

        validate_parent_chain(cur, parent, postgresql=self._db.backend == "postgresql")
        cur.execute(
            "SELECT policy_revision, state_revision, status FROM sessions WHERE session_id = ? AND user_id = ?"
            + (" FOR UPDATE" if self._db.backend == "postgresql" else ""),
            (parent["session_id"], user_id),
        )
        row = cur.fetchone()
        if (
            row is None
            or row["policy_revision"] != parent["policy_revision"]
            or row["state_revision"] != parent["state_revision"]
        ):
            raise HybridError(
                "policy_state_changed",
                "Parent changed during policy activation",
                409,
                retryable=True,
            )
        if row["status"] in {"suspended", "terminated"}:
            raise HybridError(
                "parent_session_inactive",
                "Parent session is suspended or terminated",
                403,
            )

    def refresh_inherited_policy(self, session_id, *, user_id, _seen=None):
        """Re-activate stale inherited sources before evaluation, without new verdicts."""
        from intaris.policy_activation import HybridError
        from intaris.policy_inheritance import authorization_identity

        seen = set(_seen or ())
        if session_id in seen or len(seen) >= 32:
            raise HybridError("invalid_parent_chain", "Invalid session ancestry", 409)
        seen.add(session_id)
        current = self.get(session_id, user_id=user_id)
        parent_id = current.get("parent_session_id")
        if not parent_id:
            return current
        parent = self.refresh_inherited_policy(parent_id, user_id=user_id, _seen=seen)
        if parent["status"] in {"suspended", "terminated"}:
            raise HybridError(
                "parent_session_inactive",
                "Parent session is suspended or terminated",
                403,
            )
        identity = authorization_identity(parent)
        if current.get("parent_policy_hash") != identity:
            from intaris.policy_inheritance import source_policy

            local = current.get("policy_source")
            local = json.loads(local) if local else source_policy(current.get("policy"))
            return self.update_session(session_id, user_id=user_id, policy=local)
        return current

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
                SET status = ?, status_reason = ?, updated_at = ?,
                    state_revision = state_revision + 1
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
        policy: dict[str, Any] | None = None,
        intention_source: str | None = None,
        title: str | None = None,
        expected_policy_revision: int | None = None,
        analytics_enabled: bool | None = None,
        guardrails_enabled: bool | None = None,
    ) -> dict[str, Any]:
        """Update session intention, title, and/or details.

        Only updates fields that are provided (not None).

        Args:
            session_id: Session to update.
            user_id: Tenant identifier (must match session owner).
            intention: New intention text (optional).
            details: New details dict (optional, replaces existing).
            intention_source: How the intention was set: "initial",
                "user" (from user message), or "bootstrap" (one-time
                refinement from tool patterns). Optional.
            title: Short topic label for the session (optional).

        Returns:
            Updated session as a dict.

        Raises:
            ValueError: If session not found.
        """
        from intaris.policy_activation import HybridError, prepare_policy

        current = None
        parent = None
        local_policy = None
        parent_identity = None
        captured_revision = None
        captured_state = None
        if policy is not None or expected_policy_revision is not None:
            current = self.get(session_id, user_id=user_id)
            captured_revision = current["policy_revision"]
            captured_state = current["state_revision"]
            if (
                expected_policy_revision is not None
                and expected_policy_revision != captured_revision
            ):
                raise HybridError(
                    "stale_policy_revision", "Policy revision changed", 409
                )
        if (
            intention is None
            and details is None
            and policy is None
            and intention_source is None
            and title is None
            and analytics_enabled is None
            and guardrails_enabled is None
        ):
            return self.get(session_id, user_id=user_id)

        if policy is not None:
            assert current is not None
            from intaris.policy_inheritance import (
                authorization_identity,
                inherit_policy,
                source_policy,
            )

            local_policy = source_policy(policy)
            if current.get("parent_session_id"):
                parent = self.refresh_inherited_policy(
                    current["parent_session_id"], user_id=user_id
                )
                parent_identity = authorization_identity(parent)
                policy = inherit_policy(parent.get("policy"), local_policy)
            policy = prepare_policy(
                policy,
                tenant=user_id,
                scope=details if details is not None else current.get("details"),
                existing_version=(current.get("policy") or {}).get(
                    "evaluation_policy_version", "legacy"
                ),
            )
        # Runtime executor refreshes may resend the exact same policy/details
        # immediately after approval. Do not turn that no-op into a new scope
        # revision and invalidate the exact-action receipt. Real changes keep
        # the existing guarded write/invalidation path.
        if current is None:
            current = self.get(session_id, user_id=user_id)
        if (
            (not parent or current.get("parent_policy_hash") == parent_identity)
            and (
                not parent
                or local_policy is None
                or json.loads(current.get("policy_source") or "{}") == local_policy
            )
            and all(
                value is None or value == current.get(key)
                for key, value in (
                    ("intention", intention),
                    ("policy", policy),
                    ("details", details),
                    ("intention_source", intention_source),
                    ("title", title),
                    ("analytics_enabled", analytics_enabled),
                    ("guardrails_enabled", guardrails_enabled),
                )
            )
        ):
            return current
        now = datetime.now(timezone.utc).isoformat()
        sets = ["updated_at = ?"]
        if any(
            value is not None
            for value in (intention, policy, details, intention_source)
        ):
            sets.append("state_revision = state_revision + 1")
        params: list[Any] = [now]

        if intention is not None:
            sets.append("intention = ?")
            params.append(intention)
        if details is not None:
            sets.append("details = ?")
            params.append(json.dumps(details))
        if policy is not None:
            sets.append("policy = ?")
            sets.append("policy_revision = policy_revision + 1")
            params.append(json.dumps(policy))
            sets.extend(["policy_source = ?", "parent_policy_hash = ?"])
            params.extend([json.dumps(local_policy), parent_identity])
        if intention_source is not None:
            sets.append("intention_source = ?")
            params.append(intention_source)
        if title is not None:
            sets.append("title = ?")
            params.append(title)
        for field, value in (
            ("analytics_enabled", analytics_enabled),
            ("guardrails_enabled", guardrails_enabled),
        ):
            if value is not None:
                revision = field.replace("_enabled", "_revision")
                sets.append(
                    f"{revision} = {revision} + CASE WHEN {field} = ? THEN 0 ELSE 1 END"
                )
                params.append(value)
                sets.append(f"{field} = ?")
                params.append(value)
        if guardrails_enabled is not None:
            if not any(
                value is not None
                for value in (intention, policy, details, intention_source)
            ):
                sets.append(
                    "state_revision = state_revision + CASE WHEN guardrails_enabled = ? THEN 0 ELSE 1 END"
                )
                params.append(guardrails_enabled)
            sets.append(
                "alignment_state = CASE WHEN guardrails_enabled = ? THEN alignment_state ELSE ? END"
            )
            params.extend(
                [
                    guardrails_enabled,
                    "pending"
                    if guardrails_enabled and current.get("parent_session_id")
                    else "ready",
                ]
            )
            sets.append(
                "alignment_reason = CASE WHEN guardrails_enabled = ? THEN alignment_reason ELSE NULL END"
            )
            params.append(guardrails_enabled)

        params.extend([session_id, user_id])
        revision_clause = ""
        if captured_revision is not None:
            revision_clause = " AND policy_revision = ? AND state_revision = ?"
            params.extend([captured_revision, captured_state])

        with self._db.cursor() as cur:
            if parent:
                self._guard_parent(cur, parent, user_id)
            cur.execute(
                f"UPDATE sessions SET {', '.join(sets)} "
                f"WHERE session_id = ? AND user_id = ?{revision_clause}",
                params,
            )
            if cur.rowcount == 0:
                if captured_revision is not None:
                    raise HybridError(
                        "stale_policy_revision",
                        "Policy changed during compilation",
                        409,
                    )
                raise ValueError(f"Session {session_id} not found")
            # Revoke claims in the same transaction as opt-out. An already
            # dispatched call may finish, but its writes must fail their fence.
            for enabled, task_type in (
                (analytics_enabled, "summary"),
                (guardrails_enabled, "intention_update"),
            ):
                if enabled is False:
                    cur.execute(
                        "UPDATE analysis_tasks SET status = 'cancelled', claim_token = NULL, "
                        "completed_at = ?, result = ? WHERE user_id = ? AND session_id = ? "
                        "AND task_type = ? AND status IN ('pending', 'running')",
                        (
                            now,
                            json.dumps(
                                {
                                    "status": "skipped",
                                    "reason": "analytics_disabled"
                                    if task_type == "summary"
                                    else "guardrails_disabled",
                                }
                            ),
                            user_id,
                            session_id,
                            task_type,
                        ),
                    )

            if (
                policy is not None
                and policy.get("evaluation_policy_version") != "hybrid_v1"
            ):
                from intaris.workflow_scope import registry_revision

                if registry_revision(cur, user_id, session_id):
                    raise HybridError(
                        "workflow_step_conflict",
                        "Declared workflow scope requires the hybrid policy engine",
                        409,
                    )

        return self.get(session_id, user_id=user_id)

    def set_alignment_state(
        self,
        session_id: str,
        *,
        user_id: str,
        state: str,
        expected_state_revision: int | None = None,
        reason: str | None = None,
    ) -> bool:
        if state not in {"ready", "pending", "misaligned", "error"}:
            raise ValueError("Invalid alignment state")
        condition = " AND guardrails_enabled = TRUE"
        if expected_state_revision is not None:
            condition += " AND state_revision = ?"
        params = [state, reason, session_id, user_id]
        if expected_state_revision is not None:
            params.append(expected_state_revision)
        with self._db.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET alignment_state = ?, alignment_reason = ?, "
                "state_revision = state_revision + 1 "
                f"WHERE session_id = ? AND user_id = ?{condition}",
                params,
            )
            return cur.rowcount == 1

    def allocate_event_sequences(
        self,
        session_id: str,
        *,
        user_id: str,
        user_message_flags: list[bool],
        minimum_last_seq: int = 0,
        latest_user_evidence: dict[str, Any] | None = None,
        spool_reservation: dict[str, str] | None = None,
    ) -> list[int]:
        """Allocate event sequences and advance durable user-message state."""
        if not user_message_flags:
            return []
        count = len(user_message_flags)
        last_user_offset = max(
            (
                index
                for index, is_user_message in enumerate(user_message_flags, start=1)
                if is_user_message
            ),
            default=0,
        )
        base_sql = (
            "CASE WHEN COALESCE(last_event_seq, 0) < ? "
            "THEN ? ELSE COALESCE(last_event_seq, 0) END"
        )
        returning = (
            " RETURNING last_event_seq" if self._db.backend == "postgresql" else ""
        )
        with self._db.cursor() as cur:
            if spool_reservation is not None:
                reservation = spool_reservation
                cur.execute(
                    "INSERT INTO event_spool_reservations "
                    "(user_id, session_id, batch_id, digest, spool_id, event_ts, "
                    "first_seq, last_seq) VALUES (?, ?, ?, ?, ?, ?, 0, 0) "
                    "ON CONFLICT (user_id, session_id, batch_id) DO NOTHING",
                    (
                        user_id,
                        session_id,
                        reservation["batch_id"],
                        reservation["digest"],
                        reservation["spool_id"],
                        reservation["ts"],
                    ),
                )
                if cur.rowcount != 1:
                    cur.execute(
                        "SELECT * FROM event_spool_reservations "
                        "WHERE user_id = ? AND session_id = ? AND batch_id = ?",
                        (user_id, session_id, reservation["batch_id"]),
                    )
                    existing = dict(cur.fetchone())
                    if existing["digest"] != reservation["digest"]:
                        raise ValueError("Event batch idempotency payload mismatch")
                    if existing["event_ts"] != reservation["ts"]:
                        raise ValueError("Event batch reservation timestamp mismatch")
                    if existing["last_seq"] - existing["first_seq"] + 1 != count:
                        raise ValueError("Event batch reservation size mismatch")
                    return list(range(existing["first_seq"], existing["last_seq"] + 1))
            cur.execute(
                f"""
                UPDATE sessions
                SET latest_user_message_seq = CASE
                        WHEN ? > 0 THEN {base_sql} + ?
                        ELSE latest_user_message_seq
                    END,
                    user_message_observed = CASE
                        WHEN ? > 0 THEN TRUE
                        ELSE user_message_observed
                    END,
                    last_event_seq = {base_sql} + ?,
                    state_revision = state_revision + CASE WHEN ? > 0 THEN 1 ELSE 0 END,
                    latest_user_evidence = COALESCE(?, latest_user_evidence)
                WHERE session_id = ? AND user_id = ?{returning}
                """,
                (
                    last_user_offset,
                    minimum_last_seq,
                    minimum_last_seq,
                    last_user_offset,
                    last_user_offset,
                    minimum_last_seq,
                    minimum_last_seq,
                    count,
                    last_user_offset,
                    json.dumps(latest_user_evidence)
                    if latest_user_evidence is not None
                    else None,
                    session_id,
                    user_id,
                ),
            )
            if cur.rowcount == 0:
                raise ValueError(f"Session {session_id} not found")
            if self._db.backend != "postgresql":
                cur.execute(
                    "SELECT last_event_seq FROM sessions "
                    "WHERE session_id = ? AND user_id = ?",
                    (session_id, user_id),
                )
            row = cur.fetchone()
            if spool_reservation is not None:
                end = int(row["last_event_seq"])
                cur.execute(
                    "UPDATE event_spool_reservations SET first_seq = ?, last_seq = ? "
                    "WHERE user_id = ? AND session_id = ? AND batch_id = ?",
                    (
                        end - count + 1,
                        end,
                        user_id,
                        session_id,
                        spool_reservation["batch_id"],
                    ),
                )
        last_seq = int(row["last_event_seq"])
        return list(range(last_seq - count + 1, last_seq + 1))

    def get_spool_reservation(
        self, user_id: str, session_id: str, batch_id: str
    ) -> dict[str, Any] | None:
        """Read an assignment without advancing sequence or user-message state."""
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT * FROM event_spool_reservations "
                "WHERE user_id = ? AND session_id = ? AND batch_id = ?",
                (user_id, session_id, batch_id),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def pending_spool_reservations(self) -> list[dict[str, Any]]:
        """Return pending reservation metadata for startup reconciliation."""
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT * FROM event_spool_reservations WHERE archived = 0 "
                "ORDER BY user_id, session_id, first_seq"
            )
            return [dict(row) for row in cur.fetchall()]

    def archive_spool_reservations(
        self, user_id: str, session_id: str, batch_ids: list[str]
    ) -> None:
        """Retain retry receipts after their exact payloads have been archived."""
        with self._db.cursor() as cur:
            for batch_id in batch_ids:
                cur.execute(
                    "UPDATE event_spool_reservations SET archived = 1 "
                    "WHERE user_id = ? AND session_id = ? AND batch_id = ?",
                    (user_id, session_id, batch_id),
                )

    def invalidate_spool_reservations(self, user_id: str, session_id: str) -> None:
        """Keep retry identities but prevent explicit deletion being replayed."""
        with self._db.cursor() as cur:
            cur.execute(
                "UPDATE event_spool_reservations SET archived = 2 "
                "WHERE user_id = ? AND session_id = ?",
                (user_id, session_id),
            )

    def observe_user_message(
        self,
        session_id: str,
        *,
        user_id: str,
        event_seq: int,
    ) -> None:
        """Advance durable user-message state for a pre-migration event."""
        with self._db.cursor() as cur:
            cur.execute(
                """
                UPDATE sessions
                SET latest_user_message_seq = CASE
                        WHEN latest_user_message_seq IS NULL
                          OR latest_user_message_seq < ?
                        THEN ?
                        ELSE latest_user_message_seq
                    END,
                    last_event_seq = CASE
                        WHEN COALESCE(last_event_seq, 0) < ?
                        THEN ?
                        ELSE last_event_seq
                    END,
                    user_message_observed = TRUE,
                    state_revision = state_revision + CASE
                        WHEN latest_user_message_seq IS NULL OR latest_user_message_seq < ?
                        THEN 1 ELSE 0 END
                WHERE session_id = ? AND user_id = ?
                """,
                (
                    event_seq,
                    event_seq,
                    event_seq,
                    event_seq,
                    event_seq,
                    session_id,
                    user_id,
                ),
            )
            if cur.rowcount == 0:
                raise ValueError(f"Session {session_id} not found")

    def mark_user_message_observed(
        self,
        session_id: str,
        *,
        user_id: str,
    ) -> None:
        """Permanently suppress bootstrap for a direct user message."""
        with self._db.cursor() as cur:
            cur.execute(
                """
                UPDATE sessions
                SET user_message_observed = TRUE, state_revision = state_revision + 1
                WHERE session_id = ? AND user_id = ?
                """,
                (session_id, user_id),
            )
            if cur.rowcount == 0:
                raise ValueError(f"Session {session_id} not found")

    def list_event_reconciliation_sessions(self) -> list[tuple[str, str]]:
        """List session identities that can have authoritative event logs."""
        with self._db.cursor() as cur:
            cur.execute("SELECT user_id, session_id FROM sessions")
            rows = cur.fetchall()
        return [(str(row["user_id"]), str(row["session_id"])) for row in rows]

    def reconcile_event_high_water(
        self,
        session_id: str,
        *,
        user_id: str,
        last_event_seq: int,
        latest_user_message_seq: int | None,
    ) -> None:
        """Merge high-water state reconstructed from historical event logs."""
        with self._db.cursor() as cur:
            cur.execute(
                """
                UPDATE sessions
                SET last_event_seq = CASE
                        WHEN COALESCE(last_event_seq, 0) < ?
                        THEN ?
                        ELSE last_event_seq
                    END,
                    latest_user_message_seq = CASE
                        WHEN ? IS NULL THEN latest_user_message_seq
                        WHEN latest_user_message_seq IS NULL
                          OR latest_user_message_seq < ?
                        THEN ?
                        ELSE latest_user_message_seq
                    END,
                    user_message_observed = CASE
                        WHEN ? IS NOT NULL THEN TRUE
                        ELSE user_message_observed
                    END
                WHERE session_id = ? AND user_id = ?
                """,
                (
                    last_event_seq,
                    last_event_seq,
                    latest_user_message_seq,
                    latest_user_message_seq,
                    latest_user_message_seq,
                    latest_user_message_seq,
                    session_id,
                    user_id,
                ),
            )
            if cur.rowcount == 0:
                raise ValueError(f"Session {session_id} not found")

    def update_generated_intention(
        self,
        session_id: str,
        *,
        user_id: str,
        intention: str,
        intention_source: str,
        title: str | None,
        expected_user_message_seq: int | None = None,
        guard_user_message_seq: bool = False,
        require_no_user_message: bool = False,
        intention_profile: dict[str, Any] | None = None,
        expected_state_revision: int | None = None,
        initialize_title: bool = False,
        expected_guardrails_revision: int | None = None,
    ) -> bool:
        """Persist a generated intention only while its durable guard holds."""
        now = datetime.now(timezone.utc).isoformat()
        conditions = ["session_id = ?", "user_id = ?", "guardrails_enabled = TRUE"]
        params: list[Any] = [
            intention,
            intention_source,
            title,
            now,
            json.dumps(intention_profile, ensure_ascii=False)
            if intention_profile is not None
            else None,
        ]
        condition_params: list[Any] = [session_id, user_id]
        if expected_guardrails_revision is not None:
            conditions.append("guardrails_revision = ?")
            condition_params.append(expected_guardrails_revision)
        if guard_user_message_seq:
            if expected_user_message_seq is None:
                conditions.append("latest_user_message_seq IS NULL")
            else:
                conditions.append("latest_user_message_seq = ?")
                condition_params.append(expected_user_message_seq)
        if require_no_user_message:
            conditions.append("user_message_observed = FALSE")
            conditions.append("intention_source = 'initial'")
        if expected_state_revision is not None:
            conditions.append("state_revision = ?")
            condition_params.append(expected_state_revision)
        params.extend(condition_params)
        title_assignment = (
            "CASE WHEN title IS NULL OR title = '' THEN COALESCE(?, title) ELSE title END"
            if initialize_title
            else "COALESCE(?, title)"
        )
        with self._db.cursor() as cur:
            cur.execute(
                f"""
                UPDATE sessions
                SET intention = ?, intention_source = ?, state_revision = state_revision + 1,
                    title = {title_assignment}, updated_at = ?, intention_profile = ?
                WHERE """
                + " AND ".join(conditions),
                params,
            )
            return cur.rowcount == 1

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

        Sessions that have active or idle child sessions are excluded —
        a parent should stay active as long as any of its children are
        still running.

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
                SET status = 'idle', updated_at = ?, state_revision = state_revision + 1
                WHERE status = 'active'
                  AND last_activity_at IS NOT NULL
                  AND last_activity_at < ?
                  AND NOT EXISTS (
                    SELECT 1 FROM sessions AS child
                    WHERE child.parent_session_id = sessions.session_id
                      AND child.user_id = sessions.user_id
                      AND child.status IN ('active', 'idle')
                  )
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
                SET status = 'completed', updated_at = ?, state_revision = state_revision + 1
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
        from intaris.session_modes import analytics_write

        with analytics_write(self._db) as cur:
            cur.execute(
                """
                UPDATE sessions
                SET summary_count = COALESCE(summary_count, 0) + 1,
                    last_summary_attempt_at = ?,
                    updated_at = ?
                WHERE session_id = ? AND user_id = ?
                """,
                (now, now, session_id, user_id),
            )

    def mark_summary_attempt(self, session_id: str, *, user_id: str) -> None:
        """Record that summary generation was attempted for a session.

        Used when a summary task is skipped due to insufficient data so
        startup catch-up and parent orchestration do not retry the same
        stale no-data window until new session activity arrives.
        """

        now = datetime.now(timezone.utc).isoformat()
        from intaris.session_modes import analytics_write

        with analytics_write(self._db) as cur:
            cur.execute(
                """
                UPDATE sessions
                SET last_summary_attempt_at = ?, updated_at = ?
                WHERE session_id = ? AND user_id = ?
                """,
                (now, now, session_id, user_id),
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
                SET alignment_overridden = ?, updated_at = ?,
                    state_revision = state_revision + CASE
                        WHEN alignment_overridden != ? OR (? AND alignment_state != 'ready')
                        THEN 1 ELSE 0 END,
                    alignment_state = CASE WHEN ? THEN 'ready' ELSE alignment_state END
                WHERE session_id = ? AND user_id = ?
                """,
                (
                    overridden,
                    now,
                    overridden,
                    overridden,
                    overridden,
                    session_id,
                    user_id,
                ),
            )

    def get_active_child_sessions(
        self,
        *,
        user_id: str | None = None,
        parent_session_id: str | None = None,
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
            "(alignment_overridden IS NULL OR alignment_overridden = ?)",
        ]
        params: list[Any] = [False]

        if user_id is not None:
            conditions.append("user_id = ?")
            params.append(user_id)
        if parent_session_id is not None:
            conditions.append("parent_session_id = ?")
            params.append(parent_session_id)

        where = " AND ".join(conditions)
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT user_id, session_id, alignment_state FROM sessions "
                f"WHERE {where}",
                params,
            )
            rows = cur.fetchall()

        return [
            {"user_id": row[0], "session_id": row[1], "alignment_state": row[2]}
            for row in rows
        ]

    # Allowed sort columns — maps API name to SQL expression.
    _SORT_COLUMNS: dict[str, str] = {
        "created_at": "sessions.created_at",
        "last_activity_at": "sessions.last_activity_at",
        "alignment": "sessions.last_alignment",
    }

    def list_sessions(
        self,
        *,
        user_id: str,
        status: str | None = None,
        search: str | None = None,
        agent_id: str | None = None,
        parent_session_id: str | None = None,
        alignment: str | None = None,
        min_risk: int | None = None,
        risk_category: str | None = None,
        sort: str | None = None,
        sort_dir: str | None = None,
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
            alignment: Optional alignment filter — matches sessions whose
                latest summary has this ``intent_alignment`` value
                (e.g. ``aligned``, ``partially_aligned``, ``not_aligned``).
                Uses a subquery against ``session_summaries``.
            min_risk: Optional minimum risk severity (1-10).  Matches
                sessions that have at least one risk indicator with
                severity >= this value in any summary.
            risk_category: Optional risk indicator category filter
                (e.g. ``intent_drift``).  Matches sessions that have
                a risk indicator with this category in any summary.
            sort: Sort column: ``created_at`` (default),
                ``last_activity_at``, ``alignment``.
            sort_dir: Sort direction: ``desc`` (default), ``asc``.
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
                alignment=alignment,
                min_risk=min_risk,
                risk_category=risk_category,
                sort=sort,
                sort_dir=sort_dir,
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

        if alignment:
            # Match the LATEST summary per session (compacted supersedes
            # window; ROW_NUMBER by created_at DESC picks the most recent).
            conditions.append(
                """session_id IN (
                    SELECT session_id FROM (
                        SELECT session_id, intent_alignment,
                               ROW_NUMBER() OVER (
                                   PARTITION BY session_id
                                   ORDER BY created_at DESC
                               ) AS rn
                        FROM session_summaries
                        WHERE user_id = ?
                    ) WHERE rn = 1 AND intent_alignment = ?
                )"""
            )
            params.extend([user_id, alignment])

        if min_risk is not None:
            conditions.append(
                """session_id IN (
                    SELECT DISTINCT s2.session_id
                    FROM session_summaries s2, json_each(s2.risk_indicators) AS ri
                    WHERE s2.user_id = ?
                      AND json_extract(ri.value, '$.severity') >= ?
                )"""
            )
            params.extend([user_id, min_risk])

        if risk_category:
            conditions.append(
                """session_id IN (
                    SELECT DISTINCT s3.session_id
                    FROM session_summaries s3, json_each(s3.risk_indicators) AS ri
                    WHERE s3.user_id = ?
                      AND json_extract(ri.value, '$.indicator') = ?
                )"""
            )
            params.extend([user_id, risk_category])

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

        # Resolve sort order
        order_col = self._SORT_COLUMNS.get(sort or "", "sessions.created_at")
        order_dir = "ASC" if sort_dir == "asc" else "DESC"

        with self._db.cursor() as cur:
            # Count total matching sessions
            cur.execute(f"SELECT COUNT(*) FROM sessions WHERE {where}", params)
            total = cur.fetchone()[0]

            # Fetch page
            cur.execute(
                f"""
                SELECT * FROM sessions
                WHERE {where}
                ORDER BY {order_col} {order_dir}
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
        alignment: str | None = None,
        min_risk: int | None = None,
        risk_category: str | None = None,
        sort: str | None = None,
        sort_dir: str | None = None,
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

            if alignment:
                root_conditions.append(
                    """session_id IN (
                        SELECT session_id FROM (
                            SELECT session_id, intent_alignment,
                                   ROW_NUMBER() OVER (
                                       PARTITION BY session_id
                                       ORDER BY created_at DESC
                                   ) AS rn
                            FROM session_summaries
                            WHERE user_id = ?
                        ) WHERE rn = 1 AND intent_alignment = ?
                    )"""
                )
                root_params.extend([user_id, alignment])

            if min_risk is not None:
                root_conditions.append(
                    """session_id IN (
                        SELECT DISTINCT s2.session_id
                        FROM session_summaries s2,
                             json_each(s2.risk_indicators) AS ri
                        WHERE s2.user_id = ?
                          AND json_extract(ri.value, '$.severity') >= ?
                    )"""
                )
                root_params.extend([user_id, min_risk])

            if risk_category:
                root_conditions.append(
                    """session_id IN (
                        SELECT DISTINCT s3.session_id
                        FROM session_summaries s3,
                             json_each(s3.risk_indicators) AS ri
                        WHERE s3.user_id = ?
                          AND json_extract(ri.value, '$.indicator') = ?
                    )"""
                )
                root_params.extend([user_id, risk_category])

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
            order_col = self._SORT_COLUMNS.get(sort or "", "sessions.created_at")
            # In this context, table alias is 's'
            order_col_aliased = order_col.replace("sessions.", "s.")
            order_dir_sql = "ASC" if sort_dir == "asc" else "DESC"
            paginated_root_sql = f"""
                SELECT s.* FROM sessions s
                INNER JOIN ({root_id_sql}) AS roots
                    ON s.session_id = roots.session_id
                ORDER BY {order_col_aliased} {order_dir_sql}
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
    for field in ("analytics_enabled", "guardrails_enabled"):
        d[field] = bool(d.get(field, True))
    for json_field in ("details", "policy"):
        if d.get(json_field):
            try:
                d[json_field] = json.loads(d[json_field])
            except (json.JSONDecodeError, TypeError):
                pass
    policy = d.get("policy") or {}
    if policy.get("evaluation_policy_version") == "hybrid_v1":
        d["policy_hash"] = policy.get("policy_hash")
        d["active_policy"] = policy.get("active_policy")
        d["advisory_clauses"] = (d["active_policy"] or {}).get("advisory_clauses", [])
    return d
