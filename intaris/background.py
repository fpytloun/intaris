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
        # Denial override metrics (ex-post approval of L1 denials)
        self.denial_overrides_total: int = 0
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
            "denial_overrides_total": self.denial_overrides_total,
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
                    error[:200],
                )

    def reset_stale_running(self, max_age_minutes: int = 0) -> int:
        """Reset tasks stuck in 'running' state back to 'pending'.

        Called on startup (max_age_minutes=0 resets all) and periodically
        by the worker loop (max_age_minutes>0 resets only tasks stuck
        longer than the threshold).

        Args:
            max_age_minutes: Only reset tasks whose next_attempt_at is
                older than this many minutes. 0 = reset all running tasks
                (startup behavior).

        Returns:
            Number of tasks reset.
        """
        now = datetime.now(timezone.utc).isoformat()
        if max_age_minutes > 0:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
            ).isoformat()
            with self._db.cursor() as cur:
                cur.execute(
                    """
                    UPDATE analysis_tasks
                    SET status = 'pending', next_attempt_at = ?
                    WHERE status = 'running'
                      AND next_attempt_at < ?
                    """,
                    (now, cutoff),
                )
                count = cur.rowcount
        else:
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
            logger.info(
                "Reset %d stale running tasks to pending (max_age=%dm)",
                count,
                max_age_minutes,
            )
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

    def recently_completed(
        self,
        task_type: str,
        user_id: str,
        session_id: str | None = None,
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
        self._running = False
        self.metrics = Metrics()
        # Analyzer readiness flag — controls whether automatic summary
        # and analysis tasks are enqueued by the idle sweeper and
        # periodic scheduler. Enabled when analysis config is active.
        self.analyzer_ready = config.enabled
        self._event_bus = None
        self._event_store = None
        self._notification_dispatcher = None

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
        if not self._config.enabled:
            logger.info("Behavioral analysis disabled — background worker not started")
            return

        self._running = True
        self._catchup_event = asyncio.Event()

        # Launch parallel worker loops (atomic claim prevents double-processing)
        worker_count = self._config.worker_count
        for i in range(worker_count):
            self._tasks.append(
                asyncio.create_task(
                    self._resilient_loop(f"worker-{i}", self._worker_loop)
                )
            )

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

        # Add event store flush loop if event store is configured
        loop_count = worker_count + 2  # workers + sweeper + scheduler
        if self._event_store is not None:
            self._tasks.append(
                asyncio.create_task(
                    self._resilient_loop(
                        "event_store_flusher", self._event_store_flusher
                    )
                )
            )
            loop_count += 1

        logger.info(
            "Background worker started (%d loops: %d workers, idle_sweeper, "
            "scheduler%s)",
            loop_count,
            worker_count,
            ", event_store_flusher" if self._event_store is not None else "",
        )

    async def stop(self) -> None:
        """Cancel all background loops.

        Called during server lifespan shutdown. Uses a 5-second timeout
        to prevent hanging on unresponsive tasks.
        """
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._tasks, return_exceptions=True),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                logger.warning("Background tasks did not stop within 5s timeout")
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

        stale_check_counter = 0

        while self._running:
            self.metrics.last_worker_poll = datetime.now(timezone.utc).isoformat()

            # Periodically reset tasks stuck in 'running' state.
            # Every ~50 iterations (~500s / ~8 min) check for tasks
            # that have been running for more than 10 minutes.
            stale_check_counter += 1
            if stale_check_counter >= 50:
                stale_check_counter = 0
                self._task_queue.reset_stale_running(max_age_minutes=10)

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
                    if result.get("status") != "waiting_for_children":
                        self.metrics.summaries_generated_total += 1
                    # Mark session as "summary attempted" even for skips.
                    # This prevents parent sessions from endlessly
                    # re-enqueuing child tasks that will always be skipped.
                    if (
                        result.get("status") == "skipped"
                        and task.get("session_id")
                        and task.get("user_id")
                    ):
                        try:
                            from intaris.session import SessionStore

                            SessionStore(self._db).increment_summary_count(
                                task["session_id"],
                                user_id=task["user_id"],
                            )
                        except Exception:
                            logger.debug(
                                "Failed to increment summary_count for skipped task",
                                exc_info=True,
                            )
                    if result.get("compacted"):
                        self.metrics.compaction_total += 1
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
                    # Notify on concerning L2 summaries
                    if self._notification_dispatcher and self._should_notify_summary(
                        result
                    ):
                        await self._notify_summary_alert(task, result)
                elif task_type == "analysis":
                    result = await self._execute_analysis_task(task)
                    self.metrics.analyses_completed_total += 1
                    # Notify on elevated+ L3 analysis results (score >= 7)
                    if (
                        self._notification_dispatcher
                        and (result.get("risk_level") or 0) >= 7
                    ):
                        await self._notify_analysis_alert(task, result)
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

                self._task_queue.complete(task_id, result)

                # Publish task completion event for real-time UI updates
                if self._event_bus is not None:
                    self._event_bus.publish(
                        {
                            "type": "task_completed",
                            "task_type": task_type,
                            "task_id": task_id,
                            "user_id": task.get("user_id", ""),
                            "session_id": task.get("session_id"),
                        }
                    )
            except Exception as e:
                logger.exception("Task %s failed: %s", task_id, e)
                self._task_queue.fail(task_id, str(e))
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
                        }
                    )

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
        result = await asyncio.to_thread(
            generate_summary, self._db, llm, task, self._event_store
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
                    self._task_queue.enqueue(
                        "summary",
                        child_user_id,
                        session_id=child_session_id,
                        payload={"trigger": "close"},
                        priority=3,  # Higher than parent's priority
                    )
                    enqueued += 1

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
            )

            # Update the next_attempt_at for the newly enqueued task
            # (the enqueue method sets it to now, but we want a delay).
            # Use subquery for PostgreSQL compatibility (no ORDER BY in UPDATE).
            with self._db.cursor() as cur:
                cur.execute(
                    """
                    UPDATE analysis_tasks
                    SET next_attempt_at = ?
                    WHERE id = (
                        SELECT id FROM analysis_tasks
                        WHERE user_id = ? AND session_id = ?
                          AND task_type = 'summary'
                          AND status = 'pending'
                        ORDER BY created_at DESC
                        LIMIT 1
                    )
                    """,
                    (next_attempt, user_id, session_id),
                )

            self.metrics.summary_parent_recheck_count += 1

            # Return a result that marks this iteration as done
            return {
                "status": "waiting_for_children",
                "children_enqueued": enqueued,
                "parent_check_count": parent_check_count + 1,
            }

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
    ) -> None:
        """Send a summary_alert notification for a concerning L2 summary."""
        from intaris.notifications.providers import Notification

        try:
            user_id = task.get("user_id", "")
            session_id = task.get("session_id", "")
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
        except Exception:
            logger.warning("Failed to send summary alert notification", exc_info=True)

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
        """Create an L2 analysis LLM client from config.

        Returns None if the analysis LLM is not configured.
        """
        try:
            from intaris.config import load_config
            from intaris.llm import LLMClient

            cfg = load_config()
            return LLMClient(cfg.llm_analysis)
        except Exception:
            logger.warning("Failed to create analysis LLM client", exc_info=True)
            return None

    def _get_l3_analysis_llm(self) -> Any | None:
        """Create an L3 analysis LLM client from config.

        Uses the dedicated L3 model (more capable) for cross-session
        behavioral analysis. Falls back to the L2 analysis LLM if the
        L3-specific config is not set.

        Returns None if the LLM is not configured.
        """
        try:
            from intaris.config import load_config
            from intaris.llm import LLMClient

            cfg = load_config()
            return LLMClient(cfg.llm_l3_analysis)
        except Exception:
            logger.warning("Failed to create L3 analysis LLM client", exc_info=True)
            return None

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
        from intaris.llm import LLMClient

        user_id = task.get("user_id", "")
        session_id = task.get("session_id", "")
        if not user_id or not session_id:
            return {"error": "Missing user_id or session_id"}

        payload = task.get("payload") or {}
        trigger = payload.get("trigger", "unknown")

        cfg = load_config()
        analysis_llm = LLMClient(cfg.llm_analysis)

        # Pass intention_source directly so the update is atomic —
        # no double-update race between generate_intention and here.
        source = "bootstrap" if trigger == "bootstrap" else "user"

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
        )

        if intention is not None:
            return {"intention": intention}
        return {"skipped": "No update needed"}

    async def _event_store_flusher(self) -> None:
        """Periodically flush event store buffers to storage.

        Runs every EVENT_STORE_FLUSH_INTERVAL seconds (default 30s).
        This bounds the maximum data loss window on hard crash.
        """
        # Import here to get the flush interval from the event store config
        from intaris.config import load_config

        cfg = load_config()
        interval = cfg.event_store.flush_interval

        while self._running:
            await asyncio.sleep(interval)

            if self._event_store is None:
                continue

            try:
                buffered = self._event_store.buffered_event_count
                if buffered > 0:
                    self._event_store.flush_all()
                    logger.debug(
                        "Event store periodic flush: %d events flushed", buffered
                    )
            except Exception:
                logger.exception("Event store periodic flush failed")

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
                    """
                )
                pairs = [(row[0], row[1]) for row in cur.fetchall()]

            enqueued = 0
            for user_id, agent_id in pairs:
                if not self._task_queue.cancel_duplicate("analysis", user_id):
                    self._task_queue.enqueue(
                        "analysis",
                        user_id,
                        payload={
                            "triggered_by": "periodic",
                            "agent_id": agent_id,
                            "lookback_days": self._config.lookback_days,
                        },
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
                        """
                    )
                    row = cur.fetchone()
                    count = row[0] if row else 0
                    self.metrics.sessions_needing_summaries = count
            except Exception:
                pass  # Best-effort metric

    async def _startup_catchup(self) -> None:
        """Recover from server crashes on startup.

        1. Reset stale running tasks back to pending.
        2. Enqueue summaries for idle/completed sessions that never got one.
        3. Check profile staleness.
        """
        # Reset tasks stuck in running state
        reset_count = self._task_queue.reset_stale_running()
        if reset_count:
            logger.info("Startup catch-up: reset %d stale tasks", reset_count)

        # Enqueue summaries for sessions that should have them but don't.
        # Covers: crash between idle transition and task enqueue, tasks
        # that failed permanently, terminated/suspended sessions.
        # Only catches up recent sessions (within lookback_days) to avoid
        # re-enqueuing ancient sessions on every restart.
        if self.analyzer_ready:
            try:
                enqueued = 0
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
                          AND last_activity_at >= ?
                        """,
                        (cutoff,),
                    )
                    no_summary = cur.fetchall()

                for row in no_summary:
                    uid = row["user_id"] if isinstance(row, dict) else row[0]
                    sid = row["session_id"] if isinstance(row, dict) else row[1]
                    if not self._task_queue.cancel_duplicate("summary", uid, sid):
                        self._task_queue.enqueue(
                            "summary",
                            uid,
                            session_id=sid,
                            payload={"trigger": "inactivity"},
                            priority=1,
                        )
                        enqueued += 1

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
                    uid = row["user_id"] if isinstance(row, dict) else row[0]
                    sid = row["session_id"] if isinstance(row, dict) else row[1]
                    if not self._task_queue.cancel_duplicate("summary", uid, sid):
                        self._task_queue.enqueue(
                            "summary",
                            uid,
                            session_id=sid,
                            payload={"trigger": "close"},
                            priority=2,
                        )
                        enqueued += 1

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
