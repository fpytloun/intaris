"""Event resolution helpers for extracting structured data from session events.

Used by the /reasoning endpoint to resolve user messages and assistant
context from the event store when ``from_events=True``.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def resolve_last_user_message(
    event_store: Any,
    user_id: str,
    session_id: str,
) -> tuple[str, str | None] | None:
    """Extract the last user message and assistant context from the event store.

    Handles both event type conventions used across integrations:

    - **Cognis**: ``user_message`` / ``assistant_message`` event types with
      ``data.content``
    - **OpenClaw / OpenCode / Claude Code**: ``message`` event type with
      ``data.role`` (``"user"`` / ``"assistant"``) and ``data.text``

    Flushes the session buffer before reading to ensure events appended
    by a different worker (or buffered locally) are visible.

    Args:
        event_store: The Intaris EventStore instance.
        user_id: Tenant identifier.
        session_id: Session to read events from.

    Returns:
        A ``(user_content, assistant_context)`` tuple where
        ``user_content`` is the raw user message text and
        ``assistant_context`` is the last assistant response (or ``None``).
        Returns ``None`` if no user message event was found.
    """
    # Flush buffered events to storage so cross-worker reads succeed
    try:
        event_store.flush_session(user_id, session_id)
    except Exception:
        logger.debug(
            "Failed to flush event store for %s/%s before resolution",
            user_id,
            session_id,
            exc_info=True,
        )

    # Read recent events — fetch enough to find both a user message and
    # an assistant message even with interleaved tool_call/evaluation events.
    events = event_store.read_tail(
        user_id,
        session_id,
        limit=20,
        event_types={"user_message", "assistant_message", "message"},
    )

    user_content: str | None = None
    assistant_context: str | None = None

    # Walk events in reverse chronological order (newest first)
    for event in reversed(events):
        event_type = event.get("type")
        data = event.get("data") or {}

        if user_content is None:
            content = _extract_user_text(event_type, data)
            if content:
                user_content = content
                continue

        if assistant_context is None:
            content = _extract_assistant_text(event_type, data)
            if content:
                assistant_context = content

        # Stop once we have both
        if user_content is not None and assistant_context is not None:
            break

    if user_content is None:
        logger.info(
            "No user message event found for %s/%s (checked %d events)",
            user_id,
            session_id,
            len(events),
        )
        return None

    logger.info(
        "Resolved user message from events for %s/%s "
        "(user_len=%d, context_len=%d, events_checked=%d)",
        user_id,
        session_id,
        len(user_content),
        len(assistant_context) if assistant_context else 0,
        len(events),
    )
    return user_content, assistant_context


def _extract_user_text(event_type: str, data: dict[str, Any]) -> str | None:
    """Extract user message text from an event, or None if not a user event."""
    if event_type == "user_message":
        # Cognis convention: type=user_message, data.content
        return (data.get("content") or "").strip() or None
    if event_type == "message" and data.get("role") == "user":
        # Integration convention: type=message, data.role=user, data.text
        return (data.get("text") or "").strip() or None
    return None


def _extract_assistant_text(event_type: str, data: dict[str, Any]) -> str | None:
    """Extract assistant message text from an event, or None if not an assistant event."""
    if event_type == "assistant_message":
        # Cognis convention: type=assistant_message, data.content
        return (data.get("content") or "").strip() or None
    if event_type == "message" and data.get("role") == "assistant":
        # Integration convention: type=message, data.role=assistant, data.text
        return (data.get("text") or "").strip() or None
    return None
