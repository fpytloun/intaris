"""Intention barrier for immediate user-driven intention updates.

When a user message arrives via POST /reasoning, the intention update
runs as an immediate asyncio.Task (not via the 10s-poll background queue).
The next POST /evaluate waits for the pending update before proceeding.

Design principles:
- Intention is user-driven only (user messages, explicit declarations)
- Agent tool calls never redefine intention
- Barrier is best-effort: if timeout expires, evaluate proceeds with
  the current intention

Components:
- generate_intention(): Shared function for LLM-based intention generation
- IntentionBarrier: Coordination primitive between /reasoning and /evaluate
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any

from intaris.audit import AuditStore
from intaris.db import Database
from intaris.llm import LLMClient
from intaris.session import SessionStore

logger = logging.getLogger(__name__)

# Default barrier timeout in milliseconds.
# Budget: 1s barrier + 4s LLM eval = 5s max (within circuit breaker).
_DEFAULT_TIMEOUT_MS = 1000


def generate_intention(
    *,
    llm: LLMClient,
    db: Database,
    session_store: SessionStore,
    user_id: str,
    session_id: str,
    event_bus: Any | None = None,
    intention_source: str = "user",
) -> str | None:
    """Generate an updated intention from user messages and tool calls.

    Uses the analysis LLM to summarize what the session is about based
    on user messages (primary signal) and recent tool calls (secondary).

    This is a synchronous function — it blocks on the LLM call. When
    called from the IntentionBarrier, it runs in a thread executor to
    avoid blocking the event loop.

    Args:
        llm: Analysis LLM client (injected, not created per-call).
        db: Database instance.
        session_store: Session store for reading/updating sessions.
        user_id: Tenant identifier.
        session_id: Session to update.
        event_bus: Optional EventBus for publishing session_updated events.
        intention_source: How the intention was set. Defaults to "user"
            (from user message via barrier). Use "bootstrap" for the
            one-time refinement from tool patterns.

    Returns:
        The new intention string, or None if no update was needed.
    """
    try:
        session = session_store.get(session_id, user_id=user_id)
    except ValueError:
        logger.debug("Session %s not found for intention update", session_id)
        return None

    # Fetch tool calls and user messages separately to ensure
    # balanced context regardless of record type distribution
    audit_store = AuditStore(db)
    recent_tools = audit_store.get_recent(
        session_id, user_id=user_id, limit=10, record_type="tool_call"
    )
    recent_messages = audit_store.get_recent(
        session_id, user_id=user_id, limit=5, record_type="reasoning"
    )

    if not recent_tools and not recent_messages:
        return None

    # Build tool call summary
    tool_summary: list[str] = []
    for record in recent_tools:
        tool = record.get("tool", "unknown")
        decision = record.get("decision", "unknown")
        args = record.get("args_redacted", {})
        brief = ""
        if isinstance(args, dict):
            if "command" in args:
                brief = f": {str(args['command'])[:100]}"
            elif "filePath" in args:
                brief = f": {args['filePath']}"
            elif "path" in args:
                brief = f": {args['path']}"
        tool_summary.append(f"  {tool}{brief} → {decision}")

    # Extract user messages (chronological order — oldest first)
    user_messages: list[str] = []
    for record in reversed(recent_messages):
        content = record.get("content", "")
        if content:
            if content.startswith("User message: "):
                content = content[len("User message: ") :]
            user_messages.append(content[:200])

    # Build prompt with user messages as primary signal
    details = session.get("details") or {}
    wd = details.get("working_directory", "unknown")

    prompt_parts = [
        f"Current intention: {session.get('intention', 'unknown')}",
        f"Working directory: {wd}",
    ]

    # For sub-sessions, include parent intention to keep the
    # generated intention within the parent's scope
    parent_session_id = session.get("parent_session_id")
    if parent_session_id:
        try:
            parent_session = session_store.get(parent_session_id, user_id=user_id)
            parent_intention = parent_session.get("intention", "")
            if parent_intention:
                prompt_parts.append(
                    f"Parent session intention (this is a sub-session — "
                    f"stay within scope): {parent_intention}"
                )
        except ValueError:
            logger.debug(
                "Parent session %s not found for intention update",
                parent_session_id,
            )

    if user_messages:
        msgs_text = "\n".join(f"  - {m}" for m in user_messages)
        prompt_parts.append(
            f"User messages (primary signal — the user's own words):\n{msgs_text}"
        )
    if tool_summary:
        tools_text = "\n".join(tool_summary)
        prompt_parts.append(f"Recent tool calls:\n{tools_text}")
    prompt_parts.append("Generate a concise session description:")

    messages = [
        {
            "role": "system",
            "content": (
                "You are a concise summarizer. Given context from a coding "
                "session (user messages and tool calls), generate a single "
                "sentence (max 100 words) describing what the user is working "
                "on. User messages are the most important signal — they "
                "capture the user's actual intent. Focus on the goal, not "
                "the tools."
            ),
        },
        {
            "role": "user",
            "content": "\n".join(prompt_parts),
        },
    ]

    try:
        raw = llm.generate(messages, max_tokens=150)
        intention = raw.strip().strip('"').strip("'")
        if not intention or len(intention) < 5:
            return None

        # Truncate to 500 chars
        intention = intention[:500]

        session_store.update_session(
            session_id,
            user_id=user_id,
            intention=intention,
            intention_source=intention_source,
        )

        # Publish event
        if event_bus is not None:
            event_bus.publish(
                {
                    "type": "session_updated",
                    "session_id": session_id,
                    "user_id": user_id,
                    "intention": intention,
                }
            )

        return intention
    except Exception as e:
        logger.warning("Failed to generate intention: %s", e)
        return None


class IntentionBarrier:
    """Manages immediate intention updates with barrier semantics.

    When a user message triggers an intention update:
    1. The update runs as an asyncio.Task (non-blocking for /reasoning)
    2. The next evaluate() call waits for it to complete (up to timeout)
    3. If a new message arrives while an update is running, the old
       update is cancelled and a fresh one starts (cancel-and-restart)

    This ensures the evaluator always sees the latest user intent without
    adding significant latency to the /reasoning endpoint.

    Note on cancel-and-restart: cancelling an asyncio.Task that is awaiting
    run_in_executor does NOT interrupt the thread — the LLM call completes
    in the background. The cancellation only prevents the result from being
    used. This is acceptable: the wasted LLM call is a minor cost, and the
    new trigger will start a fresh update with the latest context.
    """

    def __init__(
        self,
        *,
        db: Database,
        llm: LLMClient,
        timeout_ms: int = _DEFAULT_TIMEOUT_MS,
    ) -> None:
        self._db = db
        self._llm = llm
        self._timeout = timeout_ms / 1000.0
        self._event_bus: Any | None = None
        self._alignment_barrier: Any | None = None
        self._pending: dict[tuple[str, str], tuple[asyncio.Event, asyncio.Task]] = {}

        # Metrics
        self.wait_count: int = 0
        self.timeout_count: int = 0
        self.update_count: int = 0
        self.update_errors: int = 0

    def set_event_bus(self, event_bus: Any) -> None:
        """Set the EventBus reference for publishing events.

        Called after initialization since EventBus is created separately.
        """
        self._event_bus = event_bus

    def set_alignment_barrier(self, alignment_barrier: Any) -> None:
        """Set the AlignmentBarrier reference for chaining re-checks.

        When an intention update completes for a child session, we
        trigger an alignment re-check to verify the new intention is
        still compatible with the parent.
        """
        self._alignment_barrier = alignment_barrier

    def metrics(self) -> dict[str, Any]:
        """Export barrier metrics for health check response."""
        return {
            "wait_count": self.wait_count,
            "timeout_count": self.timeout_count,
            "update_count": self.update_count,
            "update_errors": self.update_errors,
            "pending": len(self._pending),
        }

    async def trigger(self, user_id: str, session_id: str) -> None:
        """Start an immediate intention update. Non-blocking.

        If an update is already pending for this session, it is cancelled
        and a fresh one starts (cancel-and-restart). This handles rapid
        user message turns — only the latest message's update runs to
        completion.

        Args:
            user_id: Tenant identifier.
            session_id: Session to update.
        """
        key = (user_id, session_id)

        # Cancel any existing pending update (cancel-and-restart)
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
        """Wait for a pending intention update to complete.

        Called from the evaluate endpoint before running the evaluator.
        If no update is pending, returns immediately.

        Args:
            user_id: Tenant identifier.
            session_id: Session to check.

        Returns:
            True if an update was awaited (or timed out), False if
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
            "Barrier wait: blocking for intention update user=%s session=%s",
            user_id,
            session_id,
        )

        try:
            await asyncio.wait_for(event.wait(), timeout=self._timeout)
            return True
        except asyncio.TimeoutError:
            self.timeout_count += 1
            logger.warning(
                "Intention barrier timeout (%.1fs) for user=%s session=%s",
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
        """Run generate_intention() in a thread executor, then signal.

        The LLM call is synchronous, so we use run_in_executor to avoid
        blocking the event loop. The event is set in the finally block
        to ensure waiters are always unblocked.

        Args:
            key: (user_id, session_id) tuple.
            event: Event to set when the update completes.
        """
        user_id, session_id = key
        try:
            loop = asyncio.get_running_loop()
            session_store = SessionStore(self._db)

            result = await loop.run_in_executor(
                None,
                functools.partial(
                    generate_intention,
                    llm=self._llm,
                    db=self._db,
                    session_store=session_store,
                    user_id=user_id,
                    session_id=session_id,
                    event_bus=self._event_bus,
                ),
            )

            if result is not None:
                self.update_count += 1
                logger.debug(
                    "Intention updated for user=%s session=%s: %s",
                    user_id,
                    session_id,
                    result[:80],
                )

                # Chain alignment re-check for child sessions.
                # When a child session's intention is updated (from user
                # message), re-verify it's still aligned with the parent.
                # Clear any prior alignment override so the barrier
                # re-checks with the new intention.
                if self._alignment_barrier is not None:
                    session = session_store.get(session_id, user_id=user_id)
                    if session.get("parent_session_id"):
                        self._alignment_barrier.clear_override(user_id, session_id)
                        await self._alignment_barrier.trigger(user_id, session_id)
        except asyncio.CancelledError:
            logger.debug(
                "Intention update cancelled (superseded) for user=%s session=%s",
                user_id,
                session_id,
            )
        except Exception:
            self.update_errors += 1
            logger.exception(
                "Intention update failed for user=%s session=%s",
                user_id,
                session_id,
            )
        finally:
            event.set()
            # Yield to let waiters process, then clean up.
            # Identity check (`is event`) prevents deleting a newer entry
            # if trigger() was called between event.set() and this cleanup.
            await asyncio.sleep(0)
            if self._pending.get(key, (None, None))[0] is event:
                del self._pending[key]
