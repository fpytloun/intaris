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
    - **OpenCode fallback**: ``part`` events for assistant text when the
      assistant ``message`` event only carries metadata

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
    # an assistant context even with interleaved tool_call/evaluation events.
    events = event_store.read_tail(
        user_id,
        session_id,
        limit=50,
        event_types={"user_message", "assistant_message", "message", "part"},
    )

    user_content: str | None = None
    user_seq: int | None = None
    user_message_id: str | None = None

    # Walk events in reverse chronological order (newest first) to find
    # the latest user message first.
    for event in reversed(events):
        event_type = event.get("type")
        data = event.get("data") or {}
        content = _extract_user_text(event_type, data)
        if content:
            user_content = content
            user_seq = int(event.get("seq", 0) or 0)
            user_message_id = (
                str(data.get("messageID") or data.get("message_id") or "") or None
            )
            break

    if user_content is None:
        logger.info(
            "No user message event found for %s/%s (checked %d events)",
            user_id,
            session_id,
            len(events),
        )
        return None

    assistant_context = _resolve_assistant_context(
        events,
        before_seq=user_seq or 0,
        latest_user_message_id=user_message_id,
    )

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


def _resolve_assistant_context(
    events: list[dict[str, Any]],
    *,
    before_seq: int,
    latest_user_message_id: str | None,
) -> str | None:
    """Resolve assistant context preceding the latest user message.

    Prefers canonical assistant message events. Falls back to OpenCode text
    parts when the assistant message event only contains metadata.
    """
    latest_assistant: tuple[int, str] | None = None
    assistant_message_ids: set[str] = set()
    user_message_ids: set[str] = set()
    latest_part_by_id: dict[str, dict[str, Any]] = {}

    for event in events:
        seq = int(event.get("seq", 0) or 0)
        if seq >= before_seq:
            continue
        event_type = event.get("type")
        data = event.get("data") or {}

        if event_type == "message":
            message_id = str(data.get("messageID") or data.get("message_id") or "")
            role = data.get("role")
            if message_id:
                if role == "assistant":
                    assistant_message_ids.add(message_id)
                elif role == "user":
                    user_message_ids.add(message_id)

        assistant_text = _extract_assistant_text(event_type, data)
        if assistant_text:
            latest_assistant = (seq, assistant_text)

        if event_type != "part":
            continue

        part = data.get("part") or {}
        if part.get("type") != "text":
            continue
        part_id = str(part.get("id") or "")
        key = part_id or f"_noid_{seq}"
        existing = latest_part_by_id.get(key)
        if existing is None or seq > int(existing.get("seq", 0) or 0):
            latest_part_by_id[key] = event

    if latest_assistant is not None:
        return latest_assistant[1]

    latest_part: tuple[int, str] | None = None
    for event in latest_part_by_id.values():
        seq = int(event.get("seq", 0) or 0)
        if seq >= before_seq:
            continue
        part = (event.get("data") or {}).get("part") or {}
        text = str(part.get("text") or "").strip()
        if not text:
            continue
        if part.get("synthetic"):
            continue

        message_id = str(part.get("messageID") or part.get("messageId") or "")
        if latest_user_message_id and message_id == latest_user_message_id:
            continue
        if message_id and message_id in user_message_ids:
            continue
        if (
            assistant_message_ids
            and message_id
            and message_id not in assistant_message_ids
        ):
            continue

        if latest_part is None or seq > latest_part[0]:
            latest_part = (seq, text)

    return None if latest_part is None else latest_part[1]


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
