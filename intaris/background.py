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
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

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

    def to_dict(self) -> dict[str, Any]:
        """Export metrics as a dict for health check response."""
        return {
            "summaries_generated_total": self.summaries_generated_total,
            "summaries_failed_total": self.summaries_failed_total,
            "analyses_completed_total": self.analyses_completed_total,
            "analyses_failed_total": self.analyses_failed_total,
            "task_queue_depth": self.task_queue_depth,
            "profile_staleness_max_seconds": self.profile_staleness_max_seconds,
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
        payload: dict[str, Any] | None = None,
        priority: int = 0,
    ) -> str:
        """Insert a new task into the queue.

        Args:
            task_type: "summary" or "analysis".
            user_id: Tenant identifier.
            session_id: Session ID (for summary tasks).
            payload: Task-specific parameters.
            priority: Higher priority tasks are claimed first.

        Returns:
            The task ID.
        """
        task_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        with self._db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO analysis_tasks
                    (id, task_type, user_id, session_id, status, priority,
                     payload, retry_count, max_retries, next_attempt_at,
                     created_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?, 0, 3, ?, ?)
                """,
                (
                    task_id,
                    task_type,
                    user_id,
                    session_id,
                    priority,
                    json.dumps(payload) if payload else None,
                    now,
                    now,
                ),
            )

        logger.debug(
            "Enqueued %s task %s for user=%s session=%s",
            task_type,
            task_id,
            user_id,
            session_id,
        )
        return task_id

    def claim_next(self) -> dict[str, Any] | None:
        """Atomically claim the next pending task.

        Uses UPDATE...RETURNING to atomically transition a pending task
        to running status. Returns None if no tasks are ready.

        Returns:
            Task dict or None.
        """
        now = datetime.now(timezone.utc).isoformat()

        with self._db.cursor() as cur:
            cur.execute(
                """
                UPDATE analysis_tasks
                SET status = 'running'
                WHERE id = (
                    SELECT id FROM analysis_tasks
                    WHERE status = 'pending'
                      AND next_attempt_at <= ?
                    ORDER BY priority DESC, next_attempt_at ASC
                    LIMIT 1
                )
                RETURNING *
                """,
                (now,),
            )
            row = cur.fetchone()

        if row is None:
            return None
        return _task_row_to_dict(row)

    def complete(self, task_id: str, result: dict[str, Any] | None = None) -> None:
        """Mark a task as completed.

        Args:
            task_id: Task to complete.
            result: Optional result data.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._db.cursor() as cur:
            cur.execute(
                """
                UPDATE analysis_tasks
                SET status = 'completed',
                    result = ?,
                    completed_at = ?
                WHERE id = ?
                """,
                (json.dumps(result) if result else None, now, task_id),
            )

    def fail(self, task_id: str, error: str) -> None:
        """Record a task failure with retry logic.

        Increments retry_count. If max_retries exceeded, marks as failed.
        Otherwise, sets next_attempt_at with exponential backoff.

        Args:
            task_id: Task that failed.
            error: Error message.
        """
        now = datetime.now(timezone.utc)

        with self._db.cursor() as cur:
            # Get current retry state
            cur.execute(
                "SELECT retry_count, max_retries FROM analysis_tasks WHERE id = ?",
                (task_id,),
            )
            row = cur.fetchone()
            if row is None:
                return

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
                        completed_at = ?
                    WHERE id = ?
                    """,
                    (
                        retry_count,
                        json.dumps({"error": error}),
                        now.isoformat(),
                        task_id,
                    ),
                )
                logger.warning(
                    "Task %s failed permanently after %d retries: %s",
                    task_id,
                    retry_count,
                    error,
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
                        next_attempt_at = ?
                    WHERE id = ?
                    """,
                    (
                        retry_count,
                        json.dumps({"error": error}),
                        next_attempt.isoformat(),
                        task_id,
                    ),
                )
                logger.info(
                    "Task %s failed (attempt %d/%d), retrying in %ds: %s",
                    task_id,
                    retry_count,
                    max_retries,
                    backoff_seconds,
                    error,
                )

    def reset_stale_running(self) -> int:
        """Reset tasks stuck in 'running' state back to 'pending'.

        Called on startup to recover from server crashes that left
        tasks in an incomplete state.

        Returns:
            Number of tasks reset.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._db.cursor() as cur:
            cur.execute(
                """
                UPDATE analysis_tasks
                SET status = 'pending', next_attempt_at = ?
                WHERE status = 'running'
                """,
                (now,),
            )
            count = cur.rowcount
        if count > 0:
            logger.info("Reset %d stale running tasks to pending", count)
        return count

    def cancel_duplicate(
        self,
        task_type: str,
        user_id: str,
        session_id: str | None = None,
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
        self._running = False
        self.metrics = Metrics()
        # Analyzer readiness flag — False in Phase 1 (stubs),
        # set to True in Phase 3 when real analyzer is available.
        self.analyzer_ready = False

    async def start(self) -> None:
        """Launch all background loops.

        Called during server lifespan startup.
        """
        if not self._config.enabled:
            logger.info("Behavioral analysis disabled — background worker not started")
            return

        self._running = True

        # Startup catch-up: recover from crashes
        await self._startup_catchup()

        # Launch background loops with restart-on-failure wrappers
        self._tasks = [
            asyncio.create_task(self._resilient_loop("worker", self._worker_loop)),
            asyncio.create_task(
                self._resilient_loop("idle_sweeper", self._idle_sweeper)
            ),
            asyncio.create_task(
                self._resilient_loop("scheduler", self._periodic_scheduler)
            ),
        ]
        logger.info("Background worker started (3 loops)")

    async def stop(self) -> None:
        """Cancel all background loops.

        Called during server lifespan shutdown.
        """
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("Background worker stopped")

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
                # Normal exit (shouldn't happen for infinite loops)
                return

    async def _worker_loop(self) -> None:
        """Poll task queue and execute tasks."""
        while self._running:
            task = self._task_queue.claim_next()
            if task is None:
                # Update queue depth metric
                stats = self._task_queue.get_queue_stats()
                self.metrics.task_queue_depth = stats.get("pending", 0)
                await asyncio.sleep(10)
                continue

            task_id = task["id"]
            task_type = task["task_type"]
            logger.info(
                "Executing %s task %s (attempt %d)",
                task_type,
                task_id,
                task.get("retry_count", 0) + 1,
            )

            try:
                if task_type == "summary":
                    result = await self._execute_summary_task(task)
                    self.metrics.summaries_generated_total += 1
                elif task_type == "analysis":
                    result = await self._execute_analysis_task(task)
                    self.metrics.analyses_completed_total += 1
                else:
                    result = {"error": f"Unknown task type: {task_type}"}

                self._task_queue.complete(task_id, result)
            except Exception as e:
                logger.exception("Task %s failed: %s", task_id, e)
                self._task_queue.fail(task_id, str(e))
                if task_type == "summary":
                    self.metrics.summaries_failed_total += 1
                elif task_type == "analysis":
                    self.metrics.analyses_failed_total += 1

    async def _execute_summary_task(self, task: dict[str, Any]) -> dict[str, Any]:
        """Execute a summary generation task.

        In Phase 1, this is a stub that logs and returns an empty result.
        Phase 3 will implement actual LLM-based summary generation.
        """
        from intaris.analyzer import generate_summary

        return await generate_summary(self._db, None, task)

    async def _execute_analysis_task(self, task: dict[str, Any]) -> dict[str, Any]:
        """Execute a cross-session analysis task.

        In Phase 1, this is a stub that logs and returns an empty result.
        Phase 3 will implement actual LLM-based analysis.
        """
        from intaris.analyzer import run_analysis

        return await run_analysis(self._db, None, task)

    async def _idle_sweeper(self) -> None:
        """Periodically transition inactive sessions to idle.

        Runs every 5 minutes. Uses atomic conditional UPDATE to prevent
        TOCTOU races with concurrent evaluate calls.
        """
        while self._running:
            await asyncio.sleep(300)  # 5 minutes

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

            # Find users with new summaries since last analysis
            with self._db.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT ss.user_id
                    FROM session_summaries ss
                    LEFT JOIN behavioral_profiles bp
                        ON ss.user_id = bp.user_id
                    WHERE ss.created_at > COALESCE(bp.updated_at, '1970-01-01')
                    """
                )
                users = [row[0] for row in cur.fetchall()]

            for user_id in users:
                if not self._task_queue.cancel_duplicate("analysis", user_id):
                    self._task_queue.enqueue(
                        "analysis",
                        user_id,
                        payload={"triggered_by": "periodic"},
                    )

            if users:
                logger.info(
                    "Periodic scheduler: enqueued analysis for %d users",
                    len(users),
                )

            # Update profile staleness metric
            self._update_staleness_metric()

    async def _startup_catchup(self) -> None:
        """Recover from server crashes on startup.

        1. Reset stale running tasks back to pending.
        2. Check for sessions needing summaries (if analyzer ready).
        3. Check profile staleness.
        """
        # Reset tasks stuck in running state
        reset_count = self._task_queue.reset_stale_running()
        if reset_count:
            logger.info("Startup catch-up: reset %d stale tasks", reset_count)

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
