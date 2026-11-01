"""Background task infrastructure for behavioral analysis.

Provides a SQLite-backed task queue for reliable background processing
of session summaries and cross-session analyses. Tasks survive server
restarts via persistent storage with retry and exponential backoff.

Components:
- TaskQueue: CRUD for the analysis_tasks table
- BackgroundWorker: Async worker loop, idle sweeper, periodic scheduler
- Metrics: Simple counters/gauges for observability
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from intaris.config import AnalysisConfig
from intaris.db import Database
from intaris.session import SessionStore

logger = logging.getLogger(__name__)

# Exponential backoff schedule for failed tasks (seconds).
_RETRY_BACKOFF = [30, 120, 300]

# Maximum age for completed/failed tasks before cleanup.
_COMPLETED_TASK_RETENTION_DAYS = 7
_FAILED_TASK_RETENTION_DAYS = 30


class Metrics:
    """Simple counters and gauges for behavioral analysis observability.

    Exposed via the /health endpoint. No external metrics library needed.
    """

    def __init__(self) -> None:
        self.summaries_generated_total: int = 0
        self.summaries_failed_total: int = 0
        self.analyses_completed_total: int = 0
        self.analyses_failed_total: int = 0
        self.task_queue_depth: int = 0
        self.profile_staleness_max_seconds: float = 0.0
        # Hierarchy observability metrics
        self.summary_child_triggers_total: int = 0
        self.summary_max_children_per_task: int = 0
        self.summary_parent_recheck_count: int = 0
        self.summary_child_compressed_count: int = 0
        self.summary_child_overflow_total: int = 0
        self.compaction_total: int = 0
        self.compaction_supersede_total: int = 0
        # Event-aware analysis metrics (m3 fix)
        self.summary_event_enriched_total: int = 0
        self.summary_audit_only_total: int = 0
        self.summary_partitions_total: int = 0
        self.summary_event_store_fallback_total: int = 0
        # Safety valve activations (content truncation)
        self.safety_valve_hits_total: int = 0
        # Judge auto-resolution metrics
        self.judge_reviews_total: int = 0
        self.judge_approvals_total: int = 0
        self.judge_denials_total: int = 0
        self.judge_deferrals_total: int = 0
        self.judge_errors_total: int = 0
        self.judge_overrides_total: int = 0
        self.judge_session_hints_used_total: int = 0
        # Denial override metrics (ex-post approval of L1 denials)
        self.denial_overrides_total: int = 0
        self.stale_task_resets_total: int = 0
        self.task_heartbeat_updates_total: int = 0
        self.runtime_cache_sweeps_total: int = 0
        # Gauges
        self.sessions_needing_summaries: int = 0
        # Liveness timestamps (ISO 8601)
        self.last_worker_poll: str = ""
        self.last_idle_sweep: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Export metrics as a dict for health check response."""
        return {
            "summaries_generated_total": self.summaries_generated_total,
            "summaries_failed_total": self.summaries_failed_total,
            "analyses_completed_total": self.analyses_completed_total,
            "analyses_failed_total": self.analyses_failed_total,
            "task_queue_depth": self.task_queue_depth,
            "profile_staleness_max_seconds": self.profile_staleness_max_seconds,
            "summary_child_triggers_total": self.summary_child_triggers_total,
            "summary_max_children_per_task": self.summary_max_children_per_task,
            "summary_parent_recheck_count": self.summary_parent_recheck_count,
            "summary_child_compressed_count": self.summary_child_compressed_count,
            "summary_child_overflow_total": self.summary_child_overflow_total,
            "compaction_total": self.compaction_total,
            "compaction_supersede_total": self.compaction_supersede_total,
            "summary_event_enriched_total": self.summary_event_enriched_total,
            "summary_audit_only_total": self.summary_audit_only_total,
            "summary_partitions_total": self.summary_partitions_total,
            "summary_event_store_fallback_total": self.summary_event_store_fallback_total,
            "safety_valve_hits_total": self.safety_valve_hits_total,
            "judge_reviews_total": self.judge_reviews_total,
            "judge_approvals_total": self.judge_approvals_total,
            "judge_denials_total": self.judge_denials_total,
            "judge_deferrals_total": self.judge_deferrals_total,
            "judge_errors_total": self.judge_errors_total,
            "judge_overrides_total": self.judge_overrides_total,
            "judge_session_hints_used_total": self.judge_session_hints_used_total,
            "denial_overrides_total": self.denial_overrides_total,
            "stale_task_resets_total": self.stale_task_resets_total,
            "task_heartbeat_updates_total": self.task_heartbeat_updates_total,
            "runtime_cache_sweeps_total": self.runtime_cache_sweeps_total,
            "sessions_needing_summaries": self.sessions_needing_summaries,
            "last_worker_poll": self.last_worker_poll,
            "last_idle_sweep": self.last_idle_sweep,
        }


class TaskQueue:
    """SQLite-backed task queue for analysis tasks.

    Provides reliable task persistence with atomic claim, retry with
    exponential backoff, and crash recovery.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def enqueue(
        self,
        task_type: str,
        user_id: str,
        session_id: str | None = None,
        agent_id: str | None = None,
        payload: dict[str, Any] | None = None,
        priority: int = 0,
        next_attempt_at: str | None = None,
    ) -> str | None:
        """Insert a new task into the queue.

        Args:
            task_type: "summary" or "analysis".
            user_id: Tenant identifier.
            session_id: Session ID (for summary tasks).
            agent_id: Agent scope for the task. Derived from the session
                when omitted for session-scoped tasks.
            payload: Task-specific parameters.
            priority: Higher priority tasks are claimed first.

        Returns:
            The task ID.
        """
        task_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        task_agent_id = self._resolve_task_agent_id(
            task_type=task_type,
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
            payload=payload,
        )

        with self._db.cursor(notify_analysis=True) as cur:
            if session_id and task_type in {"summary", "intention_update"}:
                field = "analytics" if task_type == "summary" else "guardrails"
                # Acquire the session lock before task insertion, matching PATCH.
                cur.execute(
                    f"UPDATE sessions SET {field}_revision = {field}_revision "
                    "WHERE user_id = ? AND session_id = ?",
                    (user_id, session_id),
                )
                cur.execute(
                    f"SELECT {field}_enabled, {field}_revision FROM sessions "
                    "WHERE user_id = ? AND session_id = ?",
                    (user_id, session_id),
                )
                row = cur.fetchone()
                if row is not None:
                    if not row[f"{field}_enabled"]:
                        return None
                    payload = {
                        **(payload or {}),
                        f"{field}_revision": row[f"{field}_revision"],
                    }
            cur.execute(
                """
                INSERT INTO analysis_tasks
                    (id, task_type, user_id, session_id, agent_id, status,
                     priority, payload, retry_count, max_retries,
                     next_attempt_at, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, 0, 3, ?, ?)
                """,
                (
                    task_id,
                    task_type,
                    user_id,
                    session_id,
                    task_agent_id,
                    priority,
                    json.dumps(payload) if payload else None,
                    next_attempt_at or now,
                    now,
                ),
            )

        logger.debug(
            "Enqueued %s task %s for user=%s session=%s agent=%s",
            task_type,
            task_id,
            user_id,
            session_id,
            task_agent_id,
        )
        return task_id

    def enqueue_bootstrap_if_no_user_message(
        self,
        user_id: str,
        session_id: str,
        *,
        priority: int = 1,
    ) -> str | None:
        """Atomically enqueue bootstrap only for a message-free session."""
        task_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        payload = json.dumps({"trigger": "bootstrap"})
        with self._db.cursor(notify_analysis=True) as cur:
            cur.execute(
                "UPDATE sessions SET guardrails_revision = guardrails_revision WHERE user_id = ? AND session_id = ?",
                (user_id, session_id),
            )
            cur.execute(
                "SELECT guardrails_revision FROM sessions WHERE user_id = ? AND session_id = ?",
                (user_id, session_id),
            )
            row = cur.fetchone()
            if row is not None:
                payload = json.dumps(
                    {
                        "trigger": "bootstrap",
                        "guardrails_revision": row["guardrails_revision"],
                    }
                )
            cur.execute(
                """
                INSERT INTO analysis_tasks
                    (id, task_type, user_id, session_id, agent_id, status,
                     priority, payload, retry_count, max_retries,
                     next_attempt_at, created_at)
                SELECT ?, 'intention_update', s.user_id, s.session_id,
                       COALESCE(s.agent_id, ''), 'pending',
                       ?, ?, 0, 3, ?, ?
                FROM sessions AS s
                WHERE s.user_id = ? AND s.session_id = ?
                  AND s.user_message_observed = FALSE
                  AND s.guardrails_enabled = TRUE
                  AND s.intention_source = 'initial'
                  AND NOT EXISTS (
                      SELECT 1 FROM analysis_tasks AS t
                      WHERE t.task_type = 'intention_update'
                        AND t.user_id = s.user_id
                        AND t.session_id = s.session_id
                        AND t.status IN ('pending', 'running')
                  )
                """,
                (
                    task_id,
                    priority,
                    payload,
                    now,
                    now,
                    user_id,
                    session_id,
                ),
            )
            inserted = cur.rowcount == 1
        if not inserted:
            return None
        logger.debug(
            "Enqueued bootstrap intention task %s for user=%s session=%s",
            task_id,
            user_id,
            session_id,
        )
        return task_id

    def _resolve_task_agent_id(
        self,
        *,
        task_type: str,
        user_id: str,
        session_id: str | None,
        agent_id: str | None,
        payload: dict[str, Any] | None,
    ) -> str:
        """Resolve the agent scope stored on a task row."""
        if agent_id is not None:
            return agent_id

        if payload and payload.get("agent_id") is not None:
            return str(payload.get("agent_id") or "")

        if not session_id:
            return ""

        with self._db.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(agent_id, '') FROM sessions "
                "WHERE user_id = ? AND session_id = ?",
                (user_id, session_id),
            )
            row = cur.fetchone()

        if row is None:
            logger.debug(
                "Task agent scope fallback: session not found for %s task "
                "(user=%s session=%s)",
                task_type,
                user_id,
                session_id,
            )
            return ""
        return row[0] if not isinstance(row, dict) else row.get("agent_id", "")

    def claim_next(self) -> dict[str, Any] | None:
        """Atomically claim the next pending task.

        Uses UPDATE...RETURNING to atomically transition a pending task
        to running status. Returns None if no tasks are ready.

        Returns:
            Task dict or None.
        """
        now = datetime.now(timezone.utc).isoformat()
        lock_clause = (
            " FOR UPDATE SKIP LOCKED" if self._db.backend == "postgresql" else ""
        )

        with self._db.cursor() as cur:
            cur.execute(
                f"""
                UPDATE analysis_tasks
                SET status = 'running',
                    started_at = ?,
                    heartbeat_at = ?,
                    claim_token = ?,
                    completed_at = NULL
                WHERE id = (
                    SELECT id FROM analysis_tasks AS t
                    WHERE status = 'pending'
                      AND next_attempt_at <= ?
                      AND NOT (
                            task_type = 'analysis'
                            AND EXISTS (
                                SELECT 1 FROM analysis_tasks AS s_block
                                WHERE s_block.status IN ('pending', 'running')
                                  AND s_block.task_type = 'summary'
                                  AND (
                                        s_block.status = 'running'
                                        OR s_block.next_attempt_at <= ?
                                  )
                                  AND s_block.user_id = t.user_id
                                  AND COALESCE(s_block.agent_id, '') = COALESCE(t.agent_id, '')
                            )
                      )
                    ORDER BY priority DESC, next_attempt_at ASC
                    LIMIT 1
                    {lock_clause}
                )
                  AND status = 'pending'
                RETURNING *
                """,
                (now, now, str(uuid.uuid4()), now, now),
            )
            row = cur.fetchone()

        if row is None:
            return None
        return _task_row_to_dict(row)

    def complete(
        self, task_id: str, result: dict[str, Any] | None = None, *, claim_token: str
    ) -> bool:
        """Mark a task as completed.

        Args:
            task_id: Task to complete.
            result: Optional result data.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._db.cursor(notify_analysis=True) as cur:
            from intaris.session_modes import ParticipationRevoked

            scope = getattr(result, "scope", None)
            if scope is not None:
                try:
                    scope.validate(cur, lock=True)
                except ParticipationRevoked:
                    cur.execute(
                        "UPDATE analysis_tasks SET status = 'cancelled', completed_at = ?, "
                        "claim_token = NULL, result = ? WHERE id = ? AND status = 'running' AND claim_token = ?",
                        (
                            now,
                            json.dumps(
                                {"status": "skipped", "reason": "participation_revoked"}
                            ),
                            task_id,
                            claim_token,
                        ),
                    )
                    return False
            cur.execute(
                """
                UPDATE analysis_tasks
                SET status = 'completed',
                    result = ?,
                    completed_at = ?,
                    heartbeat_at = ?
                WHERE id = ? AND status = 'running' AND claim_token = ?
                """,
                (
                    json.dumps(result) if result else None,
                    now,
                    now,
                    task_id,
                    claim_token,
                ),
            )
            return cur.rowcount == 1

    def touch_heartbeat(self, task_id: str, *, claim_token: str) -> bool:
        """Update the heartbeat timestamp for a running task."""
        now = datetime.now(timezone.utc).isoformat()
        with self._db.cursor() as cur:
            cur.execute(
                """
                UPDATE analysis_tasks
                SET heartbeat_at = ?
                WHERE id = ? AND status = 'running' AND claim_token = ?
                """,
                (now, task_id, claim_token),
            )
            return cur.rowcount > 0

    def fail(self, task_id: str, error: str, *, claim_token: str) -> bool:
        """Record a task failure with retry logic.

        Increments retry_count. If max_retries exceeded, marks as failed.
        Otherwise, sets next_attempt_at with exponential backoff.

        Args:
            task_id: Task that failed.
            error: Error message.
        """
        now = datetime.now(timezone.utc)

        with self._db.cursor(notify_analysis=True) as cur:
            # Get current retry state
            cur.execute(
                "SELECT retry_count, max_retries FROM analysis_tasks "
                "WHERE id = ? AND status = 'running' AND claim_token = ?",
                (task_id, claim_token),
            )
            row = cur.fetchone()
            if row is None:
                return False

            retry_count = row[0] + 1
            max_retries = row[1]

            if retry_count >= max_retries:
                # Exhausted retries — mark as permanently failed
                cur.execute(
                    """
                    UPDATE analysis_tasks
                    SET status = 'failed',
                        retry_count = ?,
                        result = ?,
                        completed_at = ?,
                        heartbeat_at = ?
                    WHERE id = ? AND status = 'running' AND claim_token = ?
                    """,
                    (
                        retry_count,
                        json.dumps({"error": error}),
                        now.isoformat(),
                        now.isoformat(),
                        task_id,
                        claim_token,
                    ),
                )
                logger.warning(
                    "Task %s failed permanently after %d retries: %s",
                    task_id,
                    retry_count,
                    error[:200],
                )
            else:
                # Schedule retry with exponential backoff
                backoff_idx = min(retry_count - 1, len(_RETRY_BACKOFF) - 1)
                backoff_seconds = _RETRY_BACKOFF[backoff_idx]
                next_attempt = now + timedelta(seconds=backoff_seconds)

                cur.execute(
                    """
                    UPDATE analysis_tasks
                    SET status = 'pending',
                        retry_count = ?,
                        result = ?,
                        next_attempt_at = ?,
                        started_at = NULL,
                        heartbeat_at = NULL,
                        claim_token = NULL,
                        completed_at = NULL
                    WHERE id = ? AND status = 'running' AND claim_token = ?
                    """,
                    (
                        retry_count,
                        json.dumps({"error": error}),
                        next_attempt.isoformat(),
                        task_id,
                        claim_token,
                    ),
                )
                logger.info(
                    "Task %s failed (attempt %d/%d), retrying in %ds: %s",
                    task_id,
                    retry_count,
                    max_retries,
                    backoff_seconds,
                    error[:200],
                )
            return cur.rowcount == 1

    def reset_stale_running(self, max_age_minutes: int = 10) -> int:
        """Reset tasks stuck in 'running' state back to 'pending'.

        Startup and periodic recovery use the same positive stale age.
        The conditional UPDATE arbitrates heartbeat/reclaim races and
        revokes ownership before another worker can claim.

        Args:
            max_age_minutes: Only reset tasks whose heartbeat (or initial
                started_at fallback) is older than this many minutes.
                Must be positive, including on startup.

        Returns:
            Number of tasks reset.
        """
        if max_age_minutes <= 0:
            raise ValueError("Recovery requires a positive stale age")
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(minutes=max_age_minutes)).isoformat()
        with self._db.cursor(notify_analysis=True) as cur:
            cur.execute(
                """
                UPDATE analysis_tasks
                SET status = 'pending',
                    next_attempt_at = ?,
                    started_at = NULL,
                    heartbeat_at = NULL,
                    claim_token = NULL,
                    completed_at = NULL
                WHERE status = 'running'
                  AND COALESCE(heartbeat_at, started_at, next_attempt_at) <= ?
                """,
                (now.isoformat(), cutoff),
            )
            count = cur.rowcount
        if count > 0:
            logger.warning(
                "Reset %d stale running tasks to pending (max_age=%dm)",
                count,
                max_age_minutes,
            )
        return count

    def next_wakeup(self) -> tuple[float | None, float | None]:
        """Read earliest eligible pending deadline and running lease expiry.

        Summary blocking matches claim_next. Future summaries do not block
        analysis until due; running summaries block until completion/recovery.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._db.cursor() as cur:
            cur.execute(
                """
                SELECT
                    MIN(CASE WHEN t.status = 'pending' AND NOT (
                        t.task_type = 'analysis' AND EXISTS (
                            SELECT 1 FROM analysis_tasks s
                            WHERE s.task_type = 'summary'
                              AND (s.status = 'running' OR (
                                  s.status = 'pending' AND s.next_attempt_at <= ?))
                              AND s.user_id = t.user_id
                              AND COALESCE(s.agent_id, '') = COALESCE(t.agent_id, '')
                        )
                    ) THEN t.next_attempt_at END) AS pending_due,
                    MIN(CASE WHEN t.status = 'running'
                        THEN COALESCE(t.heartbeat_at, t.started_at, t.next_attempt_at)
                    END) AS running_heartbeat
                FROM analysis_tasks t WHERE t.status IN ('pending', 'running')
                """,
                (now,),
            )
            row = cur.fetchone()
        due = datetime.fromisoformat(str(row[0])).timestamp() if row[0] else None
        expiry = (
            datetime.fromisoformat(str(row[1])).timestamp() + 600 if row[1] else None
        )
        return due, expiry

    def cancel_duplicate(
        self,
        task_type: str,
        user_id: str,
        session_id: str | None = None,
        agent_id: str | None = None,
    ) -> bool:
        """Check if a pending/running task already exists for this scope.

        Returns True if a duplicate exists (caller should skip enqueue).
        """
        with self._db.cursor() as cur:
            if session_id:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM analysis_tasks
                    WHERE task_type = ? AND user_id = ? AND session_id = ?
                      AND status IN ('pending', 'running')
                    """,
                    (task_type, user_id, session_id),
                )
            elif agent_id is not None:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM analysis_tasks
                    WHERE task_type = ? AND user_id = ?
                      AND session_id IS NULL
                      AND COALESCE(agent_id, '') = ?
                      AND status IN ('pending', 'running')
                    """,
                    (task_type, user_id, agent_id),
                )
            else:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM analysis_tasks
                    WHERE task_type = ? AND user_id = ?
                      AND session_id IS NULL
                      AND status IN ('pending', 'running')
                    """,
                    (task_type, user_id),
                )
            return cur.fetchone()[0] > 0

    def recently_completed(
        self,
        task_type: str,
        user_id: str,
        session_id: str | None = None,
        agent_id: str | None = None,
        cooldown_seconds: int = 60,
    ) -> bool:
        """Check if a task of this type completed recently (cooldown).

        Returns True if a matching task completed within the cooldown
        window, meaning a new enqueue should be skipped.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=cooldown_seconds)
        ).isoformat()

        with self._db.cursor() as cur:
            if session_id:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM analysis_tasks
                    WHERE task_type = ? AND user_id = ? AND session_id = ?
                      AND status = 'completed' AND completed_at >= ?
                    """,
                    (task_type, user_id, session_id, cutoff),
                )
            elif agent_id is not None:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM analysis_tasks
                    WHERE task_type = ? AND user_id = ?
                      AND session_id IS NULL
                      AND COALESCE(agent_id, '') = ?
                      AND status = 'completed' AND completed_at >= ?
                    """,
                    (task_type, user_id, agent_id, cutoff),
                )
            else:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM analysis_tasks
                    WHERE task_type = ? AND user_id = ?
                      AND session_id IS NULL
                      AND status = 'completed' AND completed_at >= ?
                    """,
                    (task_type, user_id, cutoff),
                )
            return cur.fetchone()[0] > 0

    def get_queue_stats(self) -> dict[str, int]:
        """Get task counts by status for health check.

        Returns:
            Dict mapping status to count.
        """
        with self._db.cursor() as cur:
            cur.execute(
                """
                SELECT status, COUNT(*) FROM analysis_tasks
                GROUP BY status
                """
            )
            rows = cur.fetchall()
        return {row[0]: row[1] for row in rows}

    def cleanup_old_tasks(self) -> int:
        """Delete old completed and failed tasks.

        Completed tasks older than 7 days and failed tasks older than
        30 days are removed to prevent unbounded growth.

        Returns:
            Number of tasks deleted.
        """
        now = datetime.now(timezone.utc)
        completed_cutoff = (
            now - timedelta(days=_COMPLETED_TASK_RETENTION_DAYS)
        ).isoformat()
        failed_cutoff = (now - timedelta(days=_FAILED_TASK_RETENTION_DAYS)).isoformat()

        with self._db.cursor() as cur:
            cur.execute(
                """
                DELETE FROM analysis_tasks
                WHERE (status = 'completed' AND completed_at < ?)
                   OR (status = 'failed' AND completed_at < ?)
                """,
                (completed_cutoff, failed_cutoff),
            )
            count = cur.rowcount
        if count > 0:
            logger.info("Cleaned up %d old analysis tasks", count)
        return count


class BackgroundWorker:
    """Async background worker for behavioral analysis tasks.

    Manages three concurrent loops:
    - Worker: polls task queue, executes summary/analysis tasks
    - Idle sweeper: transitions inactive sessions to idle
    - Periodic scheduler: enqueues analysis for users with new data

    All loops have restart-on-failure wrappers with exponential backoff.
    """

    def __init__(
        self,
        db: Database,
        config: AnalysisConfig,
        task_queue: TaskQueue | None = None,
    ) -> None:
        self._db = db
        self._config = config
        self._task_queue = task_queue or TaskQueue(db)
        self._session_store = SessionStore(db)
        self._tasks: list[asyncio.Task] = []
        self._queue_stats_task: asyncio.Task | None = None
        self._maintenance_tasks: set[asyncio.Task] = set()
        self._maintenance_stop = threading.Event()
        self._maintenance_lock = threading.Lock()
        self._maintenance_active = 0
        self._database_closing = False
        self._queue_wakeup = db.analysis_wakeup()
        self._queue_recovered_seen = 0
        self._running = False
        self.metrics = Metrics()
        # Analyzer readiness flag — controls whether automatic summary
        # and analysis tasks are enqueued by the idle sweeper and
        # periodic scheduler. Enabled when analysis config is active.
        self.analyzer_ready = config.enabled
        self._event_bus = None
        self._event_store = None
        self._evaluator = None
        self._alignment_barrier = None
        self._notification_dispatcher = None
        # Cached analysis LLM clients — created lazily on first use,
        # reused across all tasks to avoid per-task OpenAI/httpx client
        # churn that causes RSS ratcheting from pymalloc fragmentation.
        # Thread-safety note: LLMClient mutable fields (_supports_structured,
        # _param_fixes) are write-once convergent — they stabilize to the
        # same idempotent value after the first few calls regardless of
        # which thread writes first. httpx.Client is explicitly thread-safe.
        self._analysis_llm: Any | None = None  # L2 (summary + intention)
        self._l3_analysis_llm: Any | None = None  # L3 (cross-session)

    def set_event_bus(self, event_bus) -> None:
        """Set the EventBus reference for publishing events.

        Called after initialization since EventBus is created separately.
        """
        self._event_bus = event_bus

    def set_event_store(self, event_store) -> None:
        """Set the EventStore reference for periodic flushing.

        Called after initialization since EventStore is created separately.
        When set, the background worker runs a periodic flush loop.
        """
        self._event_store = event_store

    def set_evaluator(self, evaluator) -> None:
        """Set the Evaluator reference for runtime cache sweeping."""
        self._evaluator = evaluator

    def set_alignment_barrier(self, alignment_barrier) -> None:
        """Set the AlignmentBarrier reference for runtime cache sweeping."""
        self._alignment_barrier = alignment_barrier

    def set_notification_dispatcher(self, dispatcher) -> None:
        """Set the NotificationDispatcher for behavioral analysis alerts.

        Called after initialization since the dispatcher is created
        separately. When set, the background worker sends notifications
        for concerning L2 summaries and high/critical L3 analyses.
        """
        self._notification_dispatcher = dispatcher

    async def start(self) -> None:
        """Launch all background loops.

        Called during server lifespan startup. Does NOT block on startup
        catchup — catchup runs in the first worker loop iteration so the
        server can accept requests immediately.
        """
        if self._running:
            return
        if self._database_closing:
            raise RuntimeError("Background worker database is closing")
        if any(not task.done() for task in self._maintenance_tasks):
            raise RuntimeError("Previous background maintenance is still stopping")
        self._maintenance_tasks.clear()
        self._maintenance_stop.clear()
        self._running = True
        self._catchup_event = asyncio.Event()
        loop_count = 0

        worker_count = 0
        if self._config.enabled:
            await self._queue_wakeup.start()
            # Launch parallel worker loops (atomic claim prevents double-processing)
            worker_count = self._config.worker_count
            for i in range(worker_count):
                self._tasks.append(
                    asyncio.create_task(
                        self._resilient_loop(f"worker-{i}", self._worker_loop)
                    )
                )
            loop_count += worker_count
            self._tasks.append(
                asyncio.create_task(
                    self._resilient_loop("queue_metrics", self._queue_metrics_loop)
                )
            )
            loop_count += 1

            # Launch idle sweeper and periodic scheduler
            self._tasks.append(
                asyncio.create_task(
                    self._resilient_loop("idle_sweeper", self._idle_sweeper)
                )
            )
            self._tasks.append(
                asyncio.create_task(
                    self._resilient_loop("scheduler", self._periodic_scheduler)
                )
            )
            loop_count += 2
        else:
            # Keep lightweight runtime maintenance active even when
            # behavioral analysis is disabled so session-scoped caches
            # do not grow without bound.
            self._tasks.append(
                asyncio.create_task(
                    self._resilient_loop(
                        "runtime_maintenance", self._runtime_maintenance_loop
                    )
                )
            )
            loop_count += 1

        # Add event store flush loop if event store is configured
        if self._event_store is not None:
            self._tasks.append(
                asyncio.create_task(
                    self._resilient_loop(
                        "event_store_flusher", self._event_store_flusher
                    )
                )
            )
            loop_count += 1

        if loop_count == 0:
            self._running = False
            logger.info("Background worker not started (no active loops configured)")
            return

        logger.info(
            "Background worker started (%d loops: %d workers%s%s)",
            loop_count,
            worker_count,
            ", idle_sweeper, scheduler"
            if self._config.enabled
            else ", runtime_maintenance",
            ", event_store_flusher" if self._event_store is not None else "",
        )

    async def _queue_metrics_loop(self) -> None:
        """Sample queue depth once per process, not once per idle worker."""
        while self._running:
            # Retain outstanding work across cancellation/restart. Python threads
            # cannot be killed; a new sampler must join, not duplicate, this query.
            if self._queue_stats_task is None:
                self._queue_stats_task = asyncio.create_task(
                    asyncio.to_thread(self._task_queue.get_queue_stats)
                )
            pending = self._queue_stats_task
            try:
                stats = await asyncio.shield(pending)
            finally:
                if pending.done():
                    self._queue_stats_task = None
            if not self._running:
                return
            self.metrics.task_queue_depth = stats.get("pending", 0)
            self.metrics.last_worker_poll = self._queue_wakeup.last_check_at
            self.metrics.stale_task_resets_total += (
                self._queue_wakeup.recovered_total - self._queue_recovered_seen
            )
            self._queue_recovered_seen = self._queue_wakeup.recovered_total
            await asyncio.sleep(10)

    async def stop(self) -> None:
        """Cancel all background loops.

        Finish bounded waits within the lifespan's five-second outer timeout.
        """
        self._running = False
        self._maintenance_stop.set()
        deadline = time.monotonic() + 4.0
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._tasks, return_exceptions=True),
                    timeout=max(0, deadline - time.monotonic()),
                )
            except asyncio.TimeoutError:
                logger.warning("Background tasks exceeded the shutdown wait budget")
        self._tasks.clear()
        if self._maintenance_tasks:
            done, pending = await asyncio.wait(
                self._maintenance_tasks, timeout=max(0, deadline - time.monotonic())
            )
            for task in done:
                try:
                    task.result()
                except Exception:
                    logger.exception("Background maintenance failed during shutdown")
            self._maintenance_tasks.difference_update(done)
            if pending:
                logger.warning("Background maintenance is still stopping")
        await self._queue_wakeup.stop()
        if self._queue_stats_task is not None:
            # Bounded shutdown wait; keep ownership if the DB is still busy.
            await asyncio.wait(
                {self._queue_stats_task}, timeout=max(0, deadline - time.monotonic())
            )
            if self._queue_stats_task.done():
                try:
                    self._queue_stats_task.result()
                except Exception:
                    logger.exception("Queue metrics query failed during shutdown")
                self._queue_stats_task = None

        # Close cached analysis LLM clients after all tasks have stopped.
        # Note: asyncio.to_thread() work may still be running if the 5s
        # timeout fired. httpx.Client.close() is safe in this case — it
        # lets in-flight requests complete and only prevents new ones.
        closed = 0
        for client in (self._analysis_llm, self._l3_analysis_llm):
            if client is not None:
                try:
                    client.close()
                    closed += 1
                except Exception:
                    logger.debug("Failed to close cached LLM client", exc_info=True)
        self._analysis_llm = None
        self._l3_analysis_llm = None
        if closed:
            logger.info("Closed %d cached analysis LLM client(s)", closed)

        logger.info("Background worker stopped")

    def close_database(self) -> None:
        """Defer connection closure if a maintenance query is still in flight."""
        with self._maintenance_lock:
            self._database_closing = True
            if self._maintenance_active and self._db.backend == "postgresql":
                logger.warning("Database close deferred until maintenance stops")
            else:
                self._db.close()

    async def _resilient_loop(
        self,
        name: str,
        loop_fn: Any,
    ) -> None:
        """Wrapper that restarts a loop on failure with exponential backoff.

        Args:
            name: Loop name for logging.
            loop_fn: Async function to run in a loop.
        """
        backoff = 5
        max_backoff = 60

        while self._running:
            try:
                await loop_fn()
            except asyncio.CancelledError:
                logger.debug("Background loop '%s' cancelled", name)
                return
            except Exception:
                logger.exception(
                    "Background loop '%s' failed, restarting in %ds",
                    name,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
            else:
                # Normal exit — loop functions should run forever while
                # self._running is True. Log and restart (indicates a bug).
                logger.warning(
                    "Background loop '%s' exited normally (unexpected), restarting",
                    name,
                )
                backoff = 5  # Reset backoff on normal exit

    async def _worker_loop(self) -> None:
        """Poll task queue and execute tasks.

        Multiple instances of this loop run concurrently (configurable
        via ANALYSIS_WORKER_COUNT). Each claims tasks atomically via
        UPDATE...RETURNING — no double-processing risk.
        """
        # Run startup catchup once. First worker runs it; others wait.
        # asyncio is single-threaded so the is_set() + set() is atomic.
        if not self._catchup_event.is_set():
            # First worker to reach this runs catchup and signals others
            self._catchup_event.set()  # Claim immediately (prevents others)
            await self._startup_catchup()
        # Workers that arrive after set() skip straight through

        while self._running:
            task = await self._queue_wakeup.claim(self._task_queue)
            self.metrics.last_worker_poll = datetime.now(timezone.utc).isoformat()

            task_id = task["id"]
            task_type = task["task_type"]
            logger.info(
                "Executing %s task %s (attempt %d, user=%s, session=%s)",
                task_type,
                task_id,
                task.get("retry_count", 0) + 1,
                task.get("user_id"),
                task.get("session_id"),
            )

            start_monotonic = time.monotonic()
            heartbeat_task = asyncio.create_task(
                self._task_heartbeat_loop(task_id, task["claim_token"])
            )

            try:
                if task_type == "summary":
                    result = await self._execute_summary_task(task)
                    skip_reason = result.get("reason", "")
                    if (
                        result.get("status") == "skipped"
                        and skip_reason
                        in {
                            "Insufficient data in window",
                            "Single window exists, no compaction needed",
                        }
                        and task.get("session_id")
                        and task.get("user_id")
                    ):
                        try:
                            SessionStore(self._db).mark_summary_attempt(
                                task["session_id"],
                                user_id=task["user_id"],
                            )
                        except Exception:
                            logger.debug(
                                "Failed to record skipped summary attempt",
                                exc_info=True,
                            )
                    children_count = len(result.get("child_sessions", []))
                    if children_count > self.metrics.summary_max_children_per_task:
                        self.metrics.summary_max_children_per_task = children_count
                    compressed = result.get("children_compressed", 0)
                    if compressed:
                        self.metrics.summary_child_overflow_total += 1
                        self.metrics.summary_child_compressed_count += compressed
                    # Event-aware metrics
                    if result.get("event_enriched"):
                        self.metrics.summary_event_enriched_total += 1
                        windows_gen = result.get("windows_generated", 0)
                        self.metrics.summary_partitions_total += windows_gen
                    elif result.get("status") != "skipped":
                        self.metrics.summary_audit_only_total += 1
                    if result.get("event_store_fallback"):
                        self.metrics.summary_event_store_fallback_total += 1
                elif task_type == "analysis":
                    result = await self._execute_analysis_task(task)
                elif task_type == "intention_update":
                    result = await self._execute_intention_update_task(task)
                else:
                    result = {"error": f"Unknown task type: {task_type}"}

                # Distinguish transient failures from legitimate skips.
                # Errors and transient failures should be retried via fail().
                # Legitimate skips (e.g., no data in window) are completed.
                # Transient failures (No LLM) are retried via fail().
                # Permanent failures (session not found) are completed.
                error_msg = result.get("error", "")
                skip_reason = result.get("reason", "")
                if error_msg:
                    # "Session not found" is permanent — don't retry
                    if "not found" in error_msg.lower():
                        logger.warning(
                            "Task %s: permanent error: %s",
                            task_id,
                            error_msg[:200],
                        )
                    else:
                        raise RuntimeError(f"Task returned error: {error_msg}")
                elif result.get("status") == "skipped" and "No LLM" in skip_reason:
                    raise RuntimeError(f"Task skipped (transient): {skip_reason}")

                if not self._task_queue.complete(
                    task_id, result, claim_token=task["claim_token"]
                ):
                    logger.warning("Discarding completion of revoked claim %s", task_id)
                    continue
                from intaris.session_modes import ParticipationRevoked

                scope = getattr(result, "scope", None)
                try:
                    if scope is not None:
                        scope.check(check_claim=False)
                except ParticipationRevoked:
                    continue
                if task_type == "summary":
                    if result.get("summary_id"):
                        self.metrics.summaries_generated_total += 1
                    if result.get("compacted"):
                        self.metrics.compaction_total += 1
                    if self._notification_dispatcher and scope is not None:
                        await self._notify_pending_summaries(task, scope=scope)
                elif task_type == "analysis" and result.get("analysis_id"):
                    self.metrics.analyses_completed_total += 1
                    if (
                        self._notification_dispatcher
                        and (result.get("risk_level") or 0) >= 7
                    ):
                        await self._notify_analysis_alert(task, result)
                runtime_s = time.monotonic() - start_monotonic
                logger.info(
                    "Completed %s task %s in %.2fs (status=%s, session=%s, windows=%s, event_enriched=%s)",
                    task_type,
                    task_id,
                    runtime_s,
                    result.get("status", "completed"),
                    task.get("session_id"),
                    result.get("windows_generated"),
                    result.get("event_enriched"),
                )

                # Publish task completion event for real-time UI updates
                if self._event_bus is not None:
                    try:
                        if scope is not None:
                            scope.check(check_claim=False)
                    except ParticipationRevoked:
                        continue
                    self._event_bus.publish(
                        {
                            "type": "task_completed",
                            "task_type": task_type,
                            "task_id": task_id,
                            "user_id": task.get("user_id", ""),
                            "session_id": task.get("session_id"),
                            "agent_id": task.get("agent_id", ""),
                            "status": result.get("status", "completed"),
                            "reason": result.get("reason"),
                        }
                    )
            except Exception as e:
                runtime_s = time.monotonic() - start_monotonic
                error_message = str(e)
                try:
                    from intaris.llm import LLMTemporaryError

                    if isinstance(e, LLMTemporaryError):
                        error_message = e.user_message
                except Exception:
                    pass
                logger.exception(
                    "Failed %s task %s after %.2fs (user=%s, session=%s): %s",
                    task_type,
                    task_id,
                    runtime_s,
                    task.get("user_id"),
                    task.get("session_id"),
                    error_message,
                )
                if not self._task_queue.fail(
                    task_id, error_message, claim_token=task["claim_token"]
                ):
                    logger.warning("Discarding failure of revoked claim %s", task_id)
                    continue
                if task_type == "summary":
                    self.metrics.summaries_failed_total += 1
                elif task_type == "analysis":
                    self.metrics.analyses_failed_total += 1

                # Publish task failure event for real-time UI updates
                if self._event_bus is not None:
                    self._event_bus.publish(
                        {
                            "type": "task_failed",
                            "task_type": task_type,
                            "task_id": task_id,
                            "user_id": task.get("user_id", ""),
                            "session_id": task.get("session_id"),
                            "agent_id": task.get("agent_id", ""),
                        }
                    )
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

    async def _task_heartbeat_loop(self, task_id: str, claim_token: str) -> None:
        """Keep a running task alive while it executes.

        Long analysis tasks can legitimately run for many minutes. This
        periodic heartbeat prevents the stale-task reaper from resetting
        a live task back to pending mid-execution.
        """
        try:
            while self._running:
                await asyncio.sleep(60)
                if self._task_queue.touch_heartbeat(task_id, claim_token=claim_token):
                    self.metrics.task_heartbeat_updates_total += 1
                else:
                    return
        except asyncio.CancelledError:
            raise

    async def _execute_summary_task(self, task: dict[str, Any]) -> dict[str, Any]:
        """Execute a summary generation task.

        Creates an analysis LLM client and delegates to the analyzer
        for windowed session summary generation. Passes the event store
        for event-enriched analysis when available. Handles parent/child
        orchestration: when the analyzer signals that children need
        summaries, enqueues child tasks and re-enqueues the parent
        with a delay.
        """
        from intaris.analyzer import (
            _PARENT_RECHECK_DELAY_S,
            drain_safety_valve_hits,
            generate_summary,
        )

        llm = self._get_analysis_llm()
        # Run in thread executor to avoid blocking the event loop.
        # generate_summary() makes synchronous LLM calls (up to 30s each,
        # potentially multiple per partition) that would block all HTTP
        # request processing, WebSocket pings, and barrier events.
        try:
            result = await asyncio.to_thread(
                generate_summary, self._db, llm, task, self._event_store
            )
        except Exception:
            # Persisted windows own their alert delivery, independently of a
            # later partition's failure. Current source eligibility still gates
            # delivery; failed/revoked tasks never gain success side effects.
            if self._notification_dispatcher:
                try:
                    await self._notify_pending_summaries(task)
                except Exception:
                    logger.exception("Failed to deliver persisted summary alerts")
            raise
        if result.get("status") == "skipped":
            return result
        scope = getattr(result, "scope", None)
        if scope is not None:
            from intaris.session_modes import ParticipationRevoked

            try:
                scope.check()
            except ParticipationRevoked:
                return {"status": "skipped", "reason": "participation_revoked"}
        session = self._session_store.get(task["session_id"], user_id=task["user_id"])
        if not session["analytics_enabled"] or (
            (task.get("payload") or {}).get("analytics_revision") is not None
            and (task.get("payload") or {})["analytics_revision"]
            != session["analytics_revision"]
        ):
            return {"status": "skipped", "reason": "analytics_disabled"}
        # Reuse the existing durable queue: child summaries invalidate their
        # parent's dependency snapshot even without new local parent activity.
        if result.get("summary_id"):
            with self._db.cursor() as cur:
                cur.execute(
                    "SELECT parent_session_id FROM sessions WHERE user_id = ? AND session_id = ?",
                    (task["user_id"], task["session_id"]),
                )
                row = cur.fetchone()
            if row and row["parent_session_id"]:
                parent_id = row["parent_session_id"]
                if not self._task_queue.cancel_duplicate(
                    "summary", task["user_id"], parent_id
                ):
                    self._task_queue.enqueue(
                        "summary",
                        task["user_id"],
                        session_id=parent_id,
                        payload={"trigger": "manual"},
                        priority=2,
                    )

        # Propagate safety valve hits to metrics
        self.metrics.safety_valve_hits_total += drain_safety_valve_hits()

        # Handle parent waiting for children
        if result.get("needs_children"):
            child_sessions = result.get("child_sessions", [])
            parent_check_count = result.get("parent_check_count", 0)

            # Enqueue child summary tasks (higher priority than parent)
            enqueued = 0
            for child_user_id, child_session_id in child_sessions:
                if not self._task_queue.cancel_duplicate(
                    "summary", child_user_id, child_session_id
                ):
                    child_task = self._task_queue.enqueue(
                        "summary",
                        child_user_id,
                        session_id=child_session_id,
                        payload={"trigger": "close"},
                        priority=3,  # Higher than parent's priority
                    )
                    enqueued += int(child_task is not None)

            self.metrics.summary_child_triggers_total += enqueued

            logger.info(
                "Parent session %s: enqueuing %d child summary tasks, re-enqueue #%d",
                task.get("session_id"),
                enqueued,
                parent_check_count + 1,
            )

            # Re-enqueue the parent task with delay and incremented count
            user_id = task.get("user_id", "")
            session_id = task.get("session_id", "")
            payload = task.get("payload") or {}
            payload["depends_on_children"] = True
            payload["parent_check_count"] = parent_check_count + 1

            # Calculate next attempt time with delay
            from datetime import timedelta

            next_attempt = (
                datetime.now(timezone.utc) + timedelta(seconds=_PARENT_RECHECK_DELAY_S)
            ).isoformat()

            # Enqueue as a new task (the current one will be marked complete)
            self._task_queue.enqueue(
                "summary",
                user_id,
                session_id=session_id,
                payload=payload,
                priority=2,
                next_attempt_at=next_attempt,
            )

            self.metrics.summary_parent_recheck_count += 1

            # Return a result that marks this iteration as done
            result.update(
                {
                    "status": "waiting_for_children",
                    "children_enqueued": enqueued,
                    "parent_check_count": parent_check_count + 1,
                }
            )
            return result

        return result

    async def _execute_analysis_task(self, task: dict[str, Any]) -> dict[str, Any]:
        """Execute a cross-session analysis task.

        Creates an L3 analysis LLM client and delegates to the analyzer
        for agent-scoped cross-session behavioral analysis. Uses the
        dedicated L3 model (more capable) for cross-session pattern
        detection.
        """
        from intaris.analyzer import drain_safety_valve_hits, run_analysis

        llm = self._get_l3_analysis_llm()
        # Run in thread executor to avoid blocking the event loop.
        # run_analysis() makes synchronous LLM calls (up to 30s).
        result = await asyncio.to_thread(run_analysis, self._db, llm, task)

        # Propagate safety valve hits to metrics
        self.metrics.safety_valve_hits_total += drain_safety_valve_hits()

        return result

    def _enqueue_changed_parents(self, *, stop: threading.Event | None = None) -> int:
        """Recover a child update racing an already-running parent summary."""
        from intaris.analytics_evidence import metadata
        from intaris.analyzer import (
            _budget_child_sessions,
            _collect_child_data,
            _get_child_sessions,
        )
        from intaris.session_modes import analytics_scope

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=self._config.lookback_days)
        ).isoformat()
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT p.user_id, p.session_id FROM sessions p "
                "JOIN sessions c ON c.user_id = p.user_id AND c.parent_session_id = p.session_id "
                "JOIN session_summaries ss ON ss.user_id = c.user_id AND ss.session_id = c.session_id "
                "WHERE ss.created_at >= ? AND p.analytics_enabled = TRUE AND c.analytics_enabled = TRUE "
                "ORDER BY p.user_id, p.session_id",
                (cutoff,),
            )
            parents = list(cur.fetchall())
        enqueued = 0
        scope = None
        logger.info("Parent summary recovery: scanning %d parents", len(parents))
        started = last_progress = time.monotonic()
        for index, parent in enumerate(parents, 1):
            if stop is not None and stop.is_set():
                return enqueued
            user_id = parent["user_id"]
            if scope is None or scope.user_id != user_id:
                scope = analytics_scope(self._db, user_id)
            children = _collect_child_data(
                self._db,
                user_id,
                _get_child_sessions(self._db, user_id, parent["session_id"]),
                scope=scope,
            )
            children, _, _ = _budget_child_sessions(children)
            dependencies = {c["session_id"]: c.get("summary_ids", []) for c in children}
            with self._db.cursor() as cur:
                cur.execute(
                    "SELECT evidence_metadata FROM session_summaries WHERE user_id = ? "
                    "AND session_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                    (parent["user_id"], parent["session_id"]),
                )
                previous = cur.fetchone()
            now = time.monotonic()
            if index == len(parents) or now - last_progress >= 10:
                logger.info(
                    "Parent summary recovery: scanned %d/%d parents in %.1fs",
                    index,
                    len(parents),
                    now - started,
                )
                last_progress = now
            if stop is not None and stop.is_set():
                return enqueued
            if dependencies == metadata(
                previous["evidence_metadata"] if previous else None
            ).get("children", {}):
                continue
            if not self._task_queue.cancel_duplicate(
                "summary", parent["user_id"], parent["session_id"]
            ):
                parent_task = self._task_queue.enqueue(
                    "summary",
                    parent["user_id"],
                    session_id=parent["session_id"],
                    payload={"trigger": "manual"},
                    priority=2,
                )
                enqueued += int(parent_task is not None)
        return enqueued

    async def _notify_pending_summaries(
        self, task: dict[str, Any], *, scope: Any = None
    ) -> None:
        import json

        from intaris.analytics_evidence import metadata
        from intaris.session_modes import ParticipationRevoked, analytics_scope

        scope = scope or analytics_scope(self._db, task["user_id"])
        try:
            scope.include(task["session_id"])
        except ParticipationRevoked:
            return

        with self._db.cursor() as cur:
            cur.execute(
                "SELECT id, intent_alignment, risk_indicators, evidence_metadata "
                "FROM session_summaries WHERE user_id = ? AND session_id = ? "
                "ORDER BY created_at, id",
                (task["user_id"], task["session_id"]),
            )
            rows = [dict(r) for r in cur.fetchall()]
        for row in rows:
            try:
                scope.check(check_claim=False)
            except ParticipationRevoked:
                return
            if not scope.usable_summary({**row, "session_id": task["session_id"]}):
                continue
            evidence = metadata(row["evidence_metadata"])
            if not evidence or evidence.get("notification_sent"):
                continue
            row["risk_indicators"] = json.loads(row["risk_indicators"] or "[]")
            if self._should_notify_summary(row):
                if not await self._notify_summary_alert(task, row):
                    continue
            evidence["notification_sent"] = True
            with self._db.cursor() as cur:
                cur.execute(
                    "UPDATE session_summaries SET evidence_metadata = ? "
                    "WHERE user_id = ? AND id = ?",
                    (json.dumps(evidence), task["user_id"], row["id"]),
                )

    @staticmethod
    def _should_notify_summary(result: dict[str, Any]) -> bool:
        """Check if an L2 summary result warrants a notification.

        Triggers when:
        - intent_alignment is "misaligned" (always)
        - intent_alignment is "partially_aligned" AND any risk indicator
          has severity >= 7 (elevated+)
        """
        if result.get("status") == "skipped":
            return False
        alignment = result.get("intent_alignment", "")
        if alignment == "misaligned":
            return True
        if alignment == "partially_aligned":
            indicators = result.get("risk_indicators", [])
            if isinstance(indicators, list):
                from intaris.analyzer import coerce_risk_score

                return any(
                    coerce_risk_score(ind.get("severity", 1)) >= 7 for ind in indicators
                )
        return False

    async def _notify_summary_alert(
        self, task: dict[str, Any], result: dict[str, Any]
    ) -> bool:
        """Send a summary_alert notification for a concerning L2 summary."""
        from intaris.notifications.providers import Notification

        try:
            user_id = task.get("user_id", "")
            session_id = task.get("session_id", "")
            session = SessionStore(self._db).get(session_id, user_id=user_id)
            if not session["analytics_enabled"]:
                return False
            # Parse risk_indicators if stored as JSON string
            indicators = result.get("risk_indicators", [])
            if isinstance(indicators, str):
                import json

                try:
                    indicators = json.loads(indicators)
                except (json.JSONDecodeError, TypeError):
                    indicators = []

            notification = Notification(
                event_type="summary_alert",
                call_id="",  # No specific call for summaries
                session_id=session_id,
                user_id=user_id,
                agent_id=task.get("payload", {}).get("agent_id"),
                tool=None,
                args_redacted=None,
                risk=None,
                reasoning=None,
                ui_url=None,  # Enriched by dispatcher
                approve_url=None,
                deny_url=None,
                timestamp=datetime.now(timezone.utc).isoformat(),
                intent_alignment=result.get("intent_alignment"),
                risk_indicators=indicators if isinstance(indicators, list) else None,
                risk_level=None,
            )
            await self._notification_dispatcher.notify(
                user_id=user_id, notification=notification
            )
            logger.info(
                "Summary alert notification sent for session %s (alignment=%s)",
                session_id,
                result.get("intent_alignment"),
            )
            return True
        except Exception:
            logger.warning("Failed to send summary alert notification", exc_info=True)
            return False

    async def _notify_analysis_alert(
        self, task: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """Send an analysis_alert notification for elevated+ (>= 7) L3 analysis."""
        from intaris.notifications.providers import Notification

        try:
            user_id = task.get("user_id", "")
            agent_id = task.get("payload", {}).get("agent_id", "")
            notification = Notification(
                event_type="analysis_alert",
                call_id="",  # No specific call for analysis
                session_id="",  # Cross-session, no single session
                user_id=user_id,
                agent_id=agent_id or None,
                tool=None,
                args_redacted=None,
                risk=None,
                reasoning=None,
                ui_url=None,  # Enriched by dispatcher
                approve_url=None,
                deny_url=None,
                timestamp=datetime.now(timezone.utc).isoformat(),
                risk_level=result.get("risk_level"),
                findings_count=result.get("findings_count"),
                context_summary=result.get("context_summary"),
                analysis_id=result.get("analysis_id"),
                sessions_analyzed=result.get("sessions_analyzed"),
            )
            await self._notification_dispatcher.notify(
                user_id=user_id, notification=notification
            )
            logger.info(
                "Analysis alert notification sent for user %s (risk=%s)",
                user_id,
                result.get("risk_level"),
            )
        except Exception:
            logger.warning("Failed to send analysis alert notification", exc_info=True)

    def _get_analysis_llm(self) -> Any | None:
        """Get or create the cached L2 analysis LLM client.

        Lazily creates the client on first use and caches it for all
        subsequent summary and intention-update tasks. Failed creation
        is NOT cached — the next call will retry.

        Returns None if the analysis LLM is not configured.
        """
        if self._analysis_llm is not None:
            return self._analysis_llm
        try:
            client = self._create_llm_client("analysis")
            self._analysis_llm = client
            return client
        except Exception:
            logger.warning("Failed to create analysis LLM client", exc_info=True)
            return None

    def _get_l3_analysis_llm(self) -> Any | None:
        """Get or create the cached L3 analysis LLM client.

        Lazily creates the client on first use and caches it for all
        subsequent cross-session analysis tasks. Failed creation is
        NOT cached — the next call will retry.

        Returns None if the LLM is not configured.
        """
        if self._l3_analysis_llm is not None:
            return self._l3_analysis_llm
        try:
            client = self._create_llm_client("l3_analysis")
            self._l3_analysis_llm = client
            return client
        except Exception:
            logger.warning("Failed to create L3 analysis LLM client", exc_info=True)
            return None

    def _create_llm_client(self, role: str) -> Any:
        """Create an LLM client for the given analysis role.

        Args:
            role: "analysis" for L2 or "l3_analysis" for L3.

        Returns:
            A new LLMClient instance.

        Raises:
            Exception: If client creation fails (propagated to caller).
        """
        from intaris.config import load_config
        from intaris.llm import LLMClient

        cfg = load_config()
        if role == "l3_analysis":
            llm_cfg = cfg.llm_l3_analysis
        else:
            llm_cfg = cfg.llm_analysis
        client = LLMClient(llm_cfg)
        logger.info(
            "Created cached %s LLM client (model=%s)",
            role,
            llm_cfg.model,
        )
        return client

    async def _execute_intention_update_task(
        self, task: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute an intention update task via the shared generate_intention().

        Used for one-time bootstrap updates for sessions without user
        messages (Claude Code, MCP proxy). User-message-triggered updates
        go through the IntentionBarrier instead.
        """
        from intaris.config import load_config
        from intaris.intention import generate_intention

        user_id = task.get("user_id", "")
        session_id = task.get("session_id", "")
        if not user_id or not session_id:
            return {"error": "Missing user_id or session_id"}

        payload = task.get("payload") or {}
        trigger = payload.get("trigger", "unknown")

        analysis_llm = self._get_analysis_llm()
        if analysis_llm is None:
            return {"status": "skipped", "reason": "No LLM client available"}

        # Pass intention_source directly so the update is atomic —
        # no double-update race between generate_intention and here.
        source = "bootstrap" if trigger == "bootstrap" else "user"
        if source == "bootstrap":
            session = self._session_store.get(session_id, user_id=user_id)
            if session.get("user_message_observed"):
                return {
                    "status": "skipped",
                    "reason": "User message already observed",
                }
        session = self._session_store.get(session_id, user_id=user_id)
        if not session["guardrails_enabled"] or (
            payload.get("guardrails_revision") is not None
            and payload["guardrails_revision"] != session["guardrails_revision"]
        ):
            return {"status": "skipped", "reason": "guardrails_disabled"}

        # Run in thread executor to avoid blocking the event loop.
        # generate_intention() makes synchronous LLM calls (up to 30s).
        intention = await asyncio.to_thread(
            generate_intention,
            llm=analysis_llm,
            db=self._db,
            session_store=self._session_store,
            user_id=user_id,
            session_id=session_id,
            event_bus=self._event_bus,
            intention_source=source,
            generation=load_config().source_context_generation,
            expected_guardrails_revision=payload.get("guardrails_revision"),
        )

        if intention is not None:
            return {"intention": intention}
        return {"skipped": "No update needed"}

    async def _event_store_flusher(self) -> None:
        """Periodically flush event store buffers to storage.

        Polls age/health deadlines once per second. With spooling enabled,
        EVENT_STORE_FLUSH_INTERVAL bounds archival delay, not crash durability.
        """
        # Import here to get the flush interval from the event store config
        from intaris.config import load_config

        cfg = load_config()
        interval = cfg.event_store.flush_interval
        poll_interval = min(float(interval), 1.0)

        while self._running:
            await asyncio.sleep(poll_interval)

            if self._event_store is None:
                continue

            try:
                flushed = await asyncio.to_thread(self._event_store.flush_stale)
                if flushed > 0:
                    logger.debug(
                        "Event store periodic flush: %d events flushed", flushed
                    )
            except Exception:
                logger.exception("Event store periodic flush failed")

    async def _runtime_maintenance_loop(self) -> None:
        """Run lightweight cache maintenance when analysis is disabled."""
        while self._running:
            await asyncio.sleep(300)
            self.metrics.last_idle_sweep = datetime.now(timezone.utc).isoformat()
            self._sweep_runtime_caches()

    async def _idle_sweeper(self) -> None:
        """Periodically transition inactive sessions to idle.

        Also auto-completes idle child sessions (sub-agent defense-in-depth)
        and publishes status change events to the EventBus.

        Runs every 5 minutes. Uses atomic conditional UPDATE to prevent
        TOCTOU races with concurrent evaluate calls.
        """
        # Shorter idle timeout for child sessions (5 minutes)
        child_idle_timeout_min = 5

        while self._running:
            await asyncio.sleep(300)  # 5 minutes

            self.metrics.last_idle_sweep = datetime.now(timezone.utc).isoformat()

            cutoff = (
                datetime.now(timezone.utc)
                - timedelta(minutes=self._config.session_idle_timeout_min)
            ).isoformat()

            transitioned = self._session_store.sweep_idle_sessions(cutoff)

            if transitioned:
                logger.info(
                    "Idle sweep: transitioned %d sessions to idle",
                    len(transitioned),
                )

                # Publish status change events
                if self._event_bus is not None:
                    for user_id, session_id in transitioned:
                        self._event_bus.publish(
                            {
                                "type": "session_status_changed",
                                "session_id": session_id,
                                "user_id": user_id,
                                "status": "idle",
                            }
                        )

                # Enqueue summary tasks for transitioned sessions
                # (only if analyzer is ready — Phase 1 stubs skip this)
                if self.analyzer_ready:
                    for user_id, session_id in transitioned:
                        if not self._task_queue.cancel_duplicate(
                            "summary", user_id, session_id
                        ):
                            self._task_queue.enqueue(
                                "summary",
                                user_id,
                                session_id=session_id,
                                payload={"trigger": "inactivity"},
                                priority=1,
                            )
                        else:
                            logger.debug(
                                "Idle sweep: skipped summary for %s/%s "
                                "(duplicate task exists)",
                                user_id,
                                session_id,
                            )

            # Auto-complete idle child sessions (sub-agent defense-in-depth).
            # Child sessions use a shorter idle timeout since sub-agents
            # typically finish quickly and may not signal completion.
            child_cutoff = (
                datetime.now(timezone.utc) - timedelta(minutes=child_idle_timeout_min)
            ).isoformat()

            completed_children = self._session_store.sweep_child_sessions(child_cutoff)

            if completed_children:
                logger.info(
                    "Idle sweep: auto-completed %d idle child sessions",
                    len(completed_children),
                )
                if self._event_bus is not None:
                    for user_id, session_id in completed_children:
                        self._event_bus.publish(
                            {
                                "type": "session_status_changed",
                                "session_id": session_id,
                                "user_id": user_id,
                                "status": "completed",
                            }
                        )

                # Enqueue summary tasks for auto-completed child sessions
                if self.analyzer_ready:
                    for user_id, session_id in completed_children:
                        if not self._task_queue.cancel_duplicate(
                            "summary", user_id, session_id
                        ):
                            self._task_queue.enqueue(
                                "summary",
                                user_id,
                                session_id=session_id,
                                payload={"trigger": "close"},
                                priority=2,
                            )
                        else:
                            logger.debug(
                                "Idle sweep: skipped child summary for %s/%s "
                                "(duplicate task exists)",
                                user_id,
                                session_id,
                            )

            self._sweep_runtime_caches()

    async def _periodic_scheduler(self) -> None:
        """Periodically enqueue analysis for users with new data.

        Runs every analysis_interval_min minutes. Only enqueues for
        users with new summaries since their last analysis (incremental).
        Also cleans up old completed/failed tasks.
        """
        interval = self._config.analysis_interval_min * 60

        while self._running:
            await asyncio.sleep(interval)

            # Cleanup old tasks
            self._task_queue.cleanup_old_tasks()

            # Only enqueue analysis if analyzer is ready
            if not self.analyzer_ready:
                continue

            # Find (user_id, agent_id) pairs with new summaries since
            await self._run_maintenance(
                self._enqueue_changed_parents, stop=self._maintenance_stop
            )
            # their last analysis. Agent-scoped: each agent gets its own
            # analysis and behavioral profile.
            with self._db.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT ss.user_id, COALESCE(s.agent_id, '') as agent_id
                    FROM session_summaries ss
                    JOIN sessions s
                        ON ss.user_id = s.user_id
                        AND ss.session_id = s.session_id
                    LEFT JOIN behavioral_profiles bp
                        ON ss.user_id = bp.user_id
                        AND COALESCE(s.agent_id, '') = bp.agent_id
                     WHERE ss.created_at > COALESCE(bp.updated_at, '1970-01-01')
                       AND s.analytics_enabled = TRUE
                    """
                )
                pairs = [(row[0], row[1]) for row in cur.fetchall()]

            enqueued = 0
            for user_id, agent_id in pairs:
                if not self._task_queue.cancel_duplicate(
                    "analysis", user_id, agent_id=agent_id
                ):
                    self._task_queue.enqueue(
                        "analysis",
                        user_id,
                        agent_id=agent_id,
                        payload={
                            "triggered_by": "periodic",
                            "agent_id": agent_id,
                            "lookback_days": self._config.lookback_days,
                        },
                        priority=2,
                    )
                    enqueued += 1

            if enqueued:
                logger.info(
                    "Periodic scheduler: enqueued analysis for %d user/agent pairs",
                    enqueued,
                )

            # Update profile staleness metric
            self._update_staleness_metric()

            # Update sessions needing summaries gauge.
            # No total_calls filter — sessions with only
            # reasoning/checkpoints also need summaries.
            try:
                with self._db.cursor() as cur:
                    cur.execute(
                        """
                        SELECT COUNT(*) FROM sessions
                        WHERE status IN ('idle', 'completed',
                                         'terminated', 'suspended')
                           AND summary_count = 0
                           AND analytics_enabled = TRUE
                          AND last_activity_at > created_at
                          AND (
                                last_summary_attempt_at IS NULL
                                OR last_activity_at IS NULL
                                OR last_summary_attempt_at < last_activity_at
                          )
                        """
                    )
                    row = cur.fetchone()
                    count = row[0] if row else 0
                    self.metrics.sessions_needing_summaries = count
            except Exception:
                pass  # Best-effort metric

    async def _startup_catchup(self) -> None:
        """Run synchronous recovery off the API event loop."""
        await self._run_maintenance(self._run_startup_catchup)

    async def _run_maintenance(
        self, function: Callable[..., Any], **kwargs: Any
    ) -> Any:
        """Retain ownership of synchronous scans across async cancellation."""
        task = asyncio.create_task(
            asyncio.to_thread(self._execute_maintenance, function, kwargs)
        )
        self._maintenance_tasks.add(task)
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self._maintenance_tasks.discard(task)

    def _execute_maintenance(
        self, function: Callable[..., Any], kwargs: dict[str, Any]
    ) -> Any:
        with self._maintenance_lock:
            if self._database_closing:
                return None
            self._maintenance_active += 1
        try:
            return function(**kwargs)
        finally:
            with self._maintenance_lock:
                self._maintenance_active -= 1
                if self._db.backend == "sqlite" or (
                    self._database_closing and not self._maintenance_active
                ):
                    self._db.close()

    def _run_startup_catchup(self) -> None:
        """Recover from server crashes on startup.

        1. Reset stale running tasks back to pending.
        2. Enqueue summaries for idle/completed sessions that never got one.
        3. Check profile staleness.
        """
        logger.info("Startup catch-up: starting recovery")
        if self._maintenance_stop.is_set():
            return
        # Reset tasks stuck in running state
        reset_count = self._task_queue.reset_stale_running()
        self.metrics.stale_task_resets_total += reset_count
        if reset_count:
            logger.info("Startup catch-up: reset %d stale tasks", reset_count)

        # Enqueue summaries for sessions that should have them but don't.
        # Covers: crash between idle transition and task enqueue, tasks
        # that failed permanently, terminated/suspended sessions.
        # Only catches up recent sessions (within lookback_days) to avoid
        # re-enqueuing ancient sessions on every restart.
        if self.analyzer_ready:
            try:
                enqueued = self._enqueue_changed_parents(stop=self._maintenance_stop)
                if self._maintenance_stop.is_set():
                    return
                cutoff = (
                    datetime.now(timezone.utc)
                    - timedelta(days=self._config.lookback_days)
                ).isoformat()

                # 1. Sessions with no summaries at all
                with self._db.cursor() as cur:
                    cur.execute(
                        """
                        SELECT user_id, session_id FROM sessions
                        WHERE status IN ('idle', 'completed',
                                         'terminated', 'suspended')
                           AND summary_count = 0
                           AND analytics_enabled = TRUE
                          AND last_activity_at > created_at
                          AND (
                                last_summary_attempt_at IS NULL
                                OR last_activity_at IS NULL
                                OR last_summary_attempt_at < last_activity_at
                          )
                          AND last_activity_at >= ?
                        """,
                        (cutoff,),
                    )
                    no_summary = cur.fetchall()

                for row in no_summary:
                    if self._maintenance_stop.is_set():
                        return
                    uid = row["user_id"] if isinstance(row, dict) else row[0]
                    sid = row["session_id"] if isinstance(row, dict) else row[1]
                    if not self._task_queue.cancel_duplicate("summary", uid, sid):
                        summary_task = self._task_queue.enqueue(
                            "summary",
                            uid,
                            session_id=sid,
                            payload={"trigger": "inactivity"},
                            priority=1,
                        )
                        enqueued += int(summary_task is not None)

                # 2. Completed/terminated sessions with multiple window
                #    summaries but no compacted summary (compaction gap —
                #    e.g., volume summary was running when session closed,
                #    close summary was skipped by cancel_duplicate).
                #    Requires summary_count > 1 because compaction needs
                #    at least 2 window summaries.
                with self._db.cursor() as cur:
                    cur.execute(
                        """
                        SELECT s.user_id, s.session_id FROM sessions s
                        WHERE s.status IN ('completed', 'terminated')
                           AND s.summary_count > 1
                           AND s.analytics_enabled = TRUE
                          AND s.last_activity_at >= ?
                          AND NOT EXISTS (
                              SELECT 1 FROM session_summaries ss
                              WHERE ss.user_id = s.user_id
                                AND ss.session_id = s.session_id
                                AND ss.summary_type = 'compacted'
                          )
                        """,
                        (cutoff,),
                    )
                    no_compaction = cur.fetchall()

                for row in no_compaction:
                    if self._maintenance_stop.is_set():
                        return
                    uid = row["user_id"] if isinstance(row, dict) else row[0]
                    sid = row["session_id"] if isinstance(row, dict) else row[1]
                    if not self._task_queue.cancel_duplicate("summary", uid, sid):
                        summary_task = self._task_queue.enqueue(
                            "summary",
                            uid,
                            session_id=sid,
                            payload={"trigger": "close"},
                            priority=2,
                        )
                        enqueued += int(summary_task is not None)

                if enqueued:
                    logger.info(
                        "Startup catch-up: enqueued %d summary tasks "
                        "(%d no-summary, %d no-compaction)",
                        enqueued,
                        len(no_summary),
                        len(no_compaction),
                    )
            except Exception:
                logger.warning(
                    "Startup catch-up: failed to scan for orphaned sessions",
                    exc_info=True,
                )

        # Update initial metrics
        stats = self._task_queue.get_queue_stats()
        self.metrics.task_queue_depth = stats.get("pending", 0)

        logger.info(
            "Startup catch-up complete (pending=%d, failed=%d)",
            stats.get("pending", 0),
            stats.get("failed", 0),
        )

    def _update_staleness_metric(self) -> None:
        """Update the max profile staleness metric."""
        try:
            with self._db.cursor() as cur:
                cur.execute(
                    """
                    SELECT MIN(updated_at) FROM behavioral_profiles
                    """
                )
                row = cur.fetchone()
                if row and row[0]:
                    oldest = datetime.fromisoformat(row[0])
                    age = (datetime.now(timezone.utc) - oldest).total_seconds()
                    self.metrics.profile_staleness_max_seconds = max(0.0, age)
                else:
                    self.metrics.profile_staleness_max_seconds = 0.0
        except Exception:
            logger.debug("Failed to update staleness metric", exc_info=True)

    def _sweep_runtime_caches(self) -> None:
        """Sweep session-scoped in-memory caches for non-active sessions."""
        if (
            self._event_store is None
            and self._evaluator is None
            and self._alignment_barrier is None
        ):
            return

        try:
            with self._db.cursor() as cur:
                cur.execute(
                    """
                    SELECT user_id, session_id FROM sessions
                    WHERE status IN ('active', 'idle')
                    """
                )
                active_sessions = {(row[0], row[1]) for row in cur.fetchall()}

            removed = 0
            if self._event_store is not None:
                removed += self._event_store.sweep_seq_counters(active_sessions)
            if self._evaluator is not None:
                removed += self._evaluator.sweep_approved_paths(active_sessions)
            if self._alignment_barrier is not None:
                removed += self._alignment_barrier.sweep(active_sessions)

            self.metrics.runtime_cache_sweeps_total += 1
            if removed:
                logger.info(
                    "Swept %d stale runtime cache entries (active_sessions=%d)",
                    removed,
                    len(active_sessions),
                )
        except Exception:
            logger.debug("Failed to sweep runtime caches", exc_info=True)


def _task_row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a sqlite3.Row to a plain dict, parsing JSON fields."""
    d = dict(row)
    for json_field in ("payload", "result"):
        if d.get(json_field):
            try:
                d[json_field] = json.loads(d[json_field])
            except (json.JSONDecodeError, TypeError):
                pass
    return d
