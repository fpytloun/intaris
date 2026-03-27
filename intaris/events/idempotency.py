"""DB-backed idempotency ledger for session event appends."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any


class EventAppendIdempotencyStore:
    """Persist and resolve idempotency state for event append requests."""

    def __init__(self, db: Any) -> None:
        self._db = db

    def get(
        self, user_id: str, session_id: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        """Fetch an existing idempotency ledger row."""
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT user_id, session_id, idempotency_key, status, count, "
                "first_seq, last_seq, created_at, updated_at "
                "FROM event_append_idempotency "
                "WHERE user_id = ? AND session_id = ? AND idempotency_key = ?",
                (user_id, session_id, idempotency_key),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def claim(
        self,
        user_id: str,
        session_id: str,
        idempotency_key: str,
    ) -> tuple[bool, dict[str, Any] | None]:
        """Claim an idempotency key.

        Returns `(True, None)` when the caller successfully claimed a new key,
        or `(False, existing_row)` when the key is already present.
        """
        now = self._now()
        try:
            with self._db.cursor() as cur:
                cur.execute(
                    "INSERT INTO event_append_idempotency ("
                    "user_id, session_id, idempotency_key, status, created_at, updated_at"
                    ") VALUES (?, ?, ?, 'pending', ?, ?)",
                    (user_id, session_id, idempotency_key, now, now),
                )
        except Exception:
            existing = self.get(user_id, session_id, idempotency_key)
            if existing is None:
                raise
            return False, existing
        return True, None

    def mark_completed(
        self,
        user_id: str,
        session_id: str,
        idempotency_key: str,
        *,
        count: int,
        first_seq: int,
        last_seq: int,
    ) -> None:
        """Persist the successful append response for a claimed key."""
        with self._db.cursor() as cur:
            cur.execute(
                "UPDATE event_append_idempotency "
                "SET status = 'completed', count = ?, first_seq = ?, last_seq = ?, updated_at = ? "
                "WHERE user_id = ? AND session_id = ? AND idempotency_key = ?",
                (
                    count,
                    first_seq,
                    last_seq,
                    self._now(),
                    user_id,
                    session_id,
                    idempotency_key,
                ),
            )

    def delete(self, user_id: str, session_id: str, idempotency_key: str) -> None:
        """Delete an idempotency ledger row."""
        with self._db.cursor() as cur:
            cur.execute(
                "DELETE FROM event_append_idempotency "
                "WHERE user_id = ? AND session_id = ? AND idempotency_key = ?",
                (user_id, session_id, idempotency_key),
            )

    def delete_expired(self, *, max_age: timedelta) -> int:
        """Delete completed or abandoned rows older than the allowed age."""
        cutoff = (datetime.now(UTC) - max_age).isoformat()
        with self._db.cursor() as cur:
            cur.execute(
                "DELETE FROM event_append_idempotency WHERE created_at < ?",
                (cutoff,),
            )
            return cur.rowcount

    def is_stale_pending(
        self,
        record: dict[str, Any],
        *,
        stale_after: timedelta,
    ) -> bool:
        """Return True when a pending row is old enough to be reclaimed."""
        if record.get("status") != "pending":
            return False
        created_at = record.get("created_at")
        if not isinstance(created_at, str) or not created_at:
            return False
        try:
            created = datetime.fromisoformat(created_at)
        except ValueError:
            return False
        return created < datetime.now(UTC) - stale_after

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()
