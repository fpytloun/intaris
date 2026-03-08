"""Alignment barrier for parent/child session intention enforcement.

When a child session is created (or its intention is updated), an async
alignment check runs against the parent session's intention using the
analysis LLM. The first POST /evaluate call waits for the check to
complete before proceeding — same barrier pattern as IntentionBarrier.

Design principles:
- No tool calls execute before alignment is verified
- Fail-open on LLM failure (session stays active, per-call eval catches
  misaligned WRITE calls)
- Barrier is session-scoped: keyed by (user_id, session_id)
- Cancel-and-restart on re-trigger (e.g., intention update while check
  is in flight)

Components:
- check_intention_alignment(): LLM-based alignment check function
- AlignmentBarrier: Coordination primitive between /intention and /evaluate
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any

from intaris.db import Database
from intaris.llm import LLMClient, parse_json_response
from intaris.prompts import (
    ALIGNMENT_CHECK_SCHEMA,
    ALIGNMENT_CHECK_SYSTEM_PROMPT,
    build_alignment_check_prompt,
)
from intaris.session import SessionStore

logger = logging.getLogger(__name__)

# Default barrier timeout: generous because this is a one-time check per
# child session, not on the hot path. Budget: 15s barrier + 4s LLM eval
# = 19s max for first evaluate of a new child session.
_DEFAULT_ALIGNMENT_TIMEOUT_MS = 15000


def check_intention_alignment(
    *,
    llm: LLMClient,
    parent_intention: str,
    child_intention: str,
) -> tuple[bool, str]:
    """Check whether a child session's intention is aligned with its parent.

    Uses the analysis LLM with structured output to evaluate compatibility.
    Fail-open: returns (True, "") on any LLM failure.

    Args:
        llm: Analysis LLM client (more capable model).
        parent_intention: Parent session's declared intention.
        child_intention: Child session's declared intention.

    Returns:
        Tuple of (aligned: bool, reasoning: str). On failure, returns
        (True, "") to avoid blocking session creation.
    """
    try:
        user_prompt = build_alignment_check_prompt(
            parent_intention=parent_intention,
            child_intention=child_intention,
        )

        raw = llm.generate(
            [
                {"role": "system", "content": ALIGNMENT_CHECK_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            json_schema=ALIGNMENT_CHECK_SCHEMA,
            temperature=0.1,
        )

        result = parse_json_response(raw)
        aligned = bool(result.get("aligned", True))
        reasoning = str(result.get("reasoning", ""))
        return aligned, reasoning

    except Exception:
        logger.exception("Alignment check failed — defaulting to aligned (fail-open)")
        return True, ""


class AlignmentBarrier:
    """Manages async alignment checks with barrier semantics.

    When a child session is created or its intention is updated:
    1. trigger() starts an async alignment check (non-blocking)
    2. The next evaluate() call waits for it to complete (up to timeout)
    3. If misaligned, the barrier auto-suspends the session
    4. If a new trigger arrives while a check is running, cancel-and-restart

    This mirrors the IntentionBarrier pattern but for cross-session
    intention validation.
    """

    def __init__(
        self,
        *,
        db: Database,
        llm: LLMClient,
        timeout_ms: int = _DEFAULT_ALIGNMENT_TIMEOUT_MS,
    ) -> None:
        self._db = db
        self._llm = llm
        self._timeout = timeout_ms / 1000.0
        self._event_bus: Any | None = None
        self._pending: dict[tuple[str, str], tuple[asyncio.Event, asyncio.Task]] = {}

        # Metrics
        self.wait_count: int = 0
        self.timeout_count: int = 0
        self.check_count: int = 0
        self.misaligned_count: int = 0
        self.check_errors: int = 0

    def set_event_bus(self, event_bus: Any) -> None:
        """Set the EventBus reference for publishing events.

        Called after initialization since EventBus is created separately.
        """
        self._event_bus = event_bus

    def metrics(self) -> dict[str, Any]:
        """Export barrier metrics for health check response."""
        return {
            "wait_count": self.wait_count,
            "timeout_count": self.timeout_count,
            "check_count": self.check_count,
            "misaligned_count": self.misaligned_count,
            "check_errors": self.check_errors,
            "pending": len(self._pending),
        }

    async def trigger(self, user_id: str, session_id: str) -> None:
        """Start an async alignment check. Non-blocking.

        If a check is already pending for this session, it is cancelled
        and a fresh one starts (cancel-and-restart).

        Args:
            user_id: Tenant identifier.
            session_id: Child session to check.
        """
        key = (user_id, session_id)

        # Cancel any existing pending check (cancel-and-restart)
        old = self._pending.get(key)
        if old is not None:
            old_event, old_task = old
            if not old_task.done():
                old_task.cancel()
                old_event.set()  # Unblock any waiters on the old event

        event = asyncio.Event()
        task = asyncio.create_task(self._run(key, event))
        self._pending[key] = (event, task)

    async def wait(self, user_id: str, session_id: str) -> bool:
        """Wait for a pending alignment check to complete.

        Called from the evaluate endpoint before running the evaluator.
        If no check is pending, returns immediately.

        Args:
            user_id: Tenant identifier.
            session_id: Session to check.

        Returns:
            True if a check was awaited (or timed out), False if
            nothing was pending.
        """
        key = (user_id, session_id)
        entry = self._pending.get(key)
        if entry is None:
            return False

        event, _task = entry
        if event.is_set():
            return False

        self.wait_count += 1
        logger.debug(
            "Alignment barrier wait: blocking for check user=%s session=%s",
            user_id,
            session_id,
        )

        try:
            await asyncio.wait_for(event.wait(), timeout=self._timeout)
            return True
        except asyncio.TimeoutError:
            self.timeout_count += 1
            logger.warning(
                "Alignment barrier timeout (%.1fs) for user=%s session=%s "
                "— proceeding (fail-open)",
                self._timeout,
                user_id,
                session_id,
            )
            return True

    async def _run(
        self,
        key: tuple[str, str],
        event: asyncio.Event,
    ) -> None:
        """Run alignment check in a thread executor, then signal.

        If misaligned, auto-suspends the session and publishes an event.
        The event is set in the finally block to always unblock waiters.

        Args:
            key: (user_id, session_id) tuple.
            event: Event to set when the check completes.
        """
        user_id, session_id = key
        try:
            loop = asyncio.get_running_loop()
            session_store = SessionStore(self._db)

            # Fetch the child and parent sessions
            session = session_store.get(session_id, user_id=user_id)
            parent_session_id = session.get("parent_session_id")
            if not parent_session_id:
                # Not a child session — nothing to check
                return

            child_intention = session.get("intention", "")
            try:
                parent_session = session_store.get(parent_session_id, user_id=user_id)
            except ValueError:
                logger.debug(
                    "Parent session %s not found for alignment check "
                    "of session %s — skipping",
                    parent_session_id,
                    session_id,
                )
                return

            parent_intention = parent_session.get("intention", "")
            if not parent_intention or not child_intention:
                return

            # Run the LLM check in a thread (synchronous call)
            aligned, reasoning = await loop.run_in_executor(
                None,
                functools.partial(
                    check_intention_alignment,
                    llm=self._llm,
                    parent_intention=parent_intention,
                    child_intention=child_intention,
                ),
            )

            self.check_count += 1

            if not aligned:
                self.misaligned_count += 1
                status_reason = (
                    f"Child intention conflicts with parent session: {reasoning}"
                )
                logger.warning(
                    "Alignment check FAILED for session %s (parent=%s): %s",
                    session_id,
                    parent_session_id,
                    reasoning,
                )

                # Auto-suspend the child session
                session_store.update_status(
                    session_id,
                    "suspended",
                    user_id=user_id,
                    status_reason=status_reason,
                )

                # Publish session_status_changed event
                if self._event_bus is not None:
                    self._event_bus.publish(
                        {
                            "type": "session_status_changed",
                            "session_id": session_id,
                            "user_id": user_id,
                            "status": "suspended",
                            "status_reason": status_reason,
                        }
                    )
            else:
                logger.debug(
                    "Alignment check PASSED for session %s (parent=%s): %s",
                    session_id,
                    parent_session_id,
                    reasoning,
                )

        except asyncio.CancelledError:
            logger.debug(
                "Alignment check cancelled (superseded) for user=%s session=%s",
                user_id,
                session_id,
            )
        except Exception:
            self.check_errors += 1
            logger.exception(
                "Alignment check failed for user=%s session=%s "
                "— session stays active (fail-open)",
                user_id,
                session_id,
            )
        finally:
            event.set()
            # Yield to let waiters process, then clean up.
            # Identity check prevents deleting a newer entry.
            await asyncio.sleep(0)
            if self._pending.get(key, (None, None))[0] is event:
                del self._pending[key]
