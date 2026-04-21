"""Tests for event resolution helpers (intaris.events.resolve).

Tests cover:
- resolve_last_user_message with Cognis event types (user_message, assistant_message)
- resolve_last_user_message with integration event types (message with role)
- resolve_last_user_message with mixed event types
- resolve_last_user_message with no user message events
- resolve_last_user_message with no assistant message (context is None)
"""

from __future__ import annotations

import pytest

from intaris.config import EventStoreConfig
from intaris.events.resolve import (
    _extract_assistant_text,
    _extract_user_text,
    resolve_last_user_message,
)
from intaris.events.store import EventStore

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    """EventStore with filesystem backend."""
    config = EventStoreConfig(
        enabled=True,
        backend="filesystem",
        filesystem_path=str(tmp_path / "events"),
        flush_size=100,
        flush_interval=30,
    )
    return EventStore(config)


USER_ID = "test-user"
SESSION_ID = "test-session"


# ── _extract_user_text ────────────────────────────────────────────────


class TestExtractUserText:
    """Tests for _extract_user_text helper."""

    def test_cognis_user_message(self):
        assert _extract_user_text("user_message", {"content": "hello"}) == "hello"

    def test_integration_message_role_user(self):
        assert (
            _extract_user_text("message", {"role": "user", "text": "hello"}) == "hello"
        )

    def test_ignores_assistant_message(self):
        assert _extract_user_text("assistant_message", {"content": "hello"}) is None

    def test_ignores_message_role_assistant(self):
        assert (
            _extract_user_text("message", {"role": "assistant", "text": "hello"})
            is None
        )

    def test_ignores_unknown_type(self):
        assert _extract_user_text("tool_call", {"content": "hello"}) is None

    def test_empty_content(self):
        assert _extract_user_text("user_message", {"content": ""}) is None

    def test_whitespace_only(self):
        assert _extract_user_text("user_message", {"content": "   "}) is None


# ── _extract_assistant_text ───────────────────────────────────────────


class TestExtractAssistantText:
    """Tests for _extract_assistant_text helper."""

    def test_cognis_assistant_message(self):
        assert (
            _extract_assistant_text("assistant_message", {"content": "sure"}) == "sure"
        )

    def test_integration_message_role_assistant(self):
        assert (
            _extract_assistant_text("message", {"role": "assistant", "text": "sure"})
            == "sure"
        )

    def test_ignores_user_message(self):
        assert _extract_assistant_text("user_message", {"content": "hello"}) is None

    def test_ignores_message_role_user(self):
        assert (
            _extract_assistant_text("message", {"role": "user", "text": "hello"})
            is None
        )


# ── resolve_last_user_message ─────────────────────────────────────────


class TestResolveLastUserMessage:
    """Tests for resolve_last_user_message."""

    def test_cognis_events(self, store):
        """Resolves user message and assistant context from Cognis event types."""
        store.append(
            USER_ID,
            SESSION_ID,
            [
                {"type": "assistant_message", "data": {"content": "How can I help?"}},
                {"type": "user_message", "data": {"content": "Fix the bug"}},
            ],
            source="cognis",
        )

        result = resolve_last_user_message(store, USER_ID, SESSION_ID)
        assert result is not None
        user_content, assistant_context = result
        assert user_content == "Fix the bug"
        assert assistant_context == "How can I help?"

    def test_integration_events(self, store):
        """Resolves from integration-style message events with role field."""
        store.append(
            USER_ID,
            SESSION_ID,
            [
                {"type": "message", "data": {"role": "assistant", "text": "Done!"}},
                {"type": "message", "data": {"role": "user", "text": "ok, do it"}},
            ],
            source="openclaw",
        )

        result = resolve_last_user_message(store, USER_ID, SESSION_ID)
        assert result is not None
        user_content, assistant_context = result
        assert user_content == "ok, do it"
        assert assistant_context == "Done!"

    def test_mixed_event_types(self, store):
        """Resolves correctly when Cognis and integration events are mixed."""
        store.append(
            USER_ID,
            SESSION_ID,
            [
                {
                    "type": "message",
                    "data": {"role": "assistant", "text": "old response"},
                },
                {"type": "assistant_message", "data": {"content": "newer response"}},
                {"type": "user_message", "data": {"content": "thanks"}},
            ],
            source="mixed",
        )

        result = resolve_last_user_message(store, USER_ID, SESSION_ID)
        assert result is not None
        user_content, assistant_context = result
        assert user_content == "thanks"
        assert assistant_context == "newer response"

    def test_no_user_message(self, store):
        """Returns None when no user message events exist."""
        store.append(
            USER_ID,
            SESSION_ID,
            [{"type": "assistant_message", "data": {"content": "Hello"}}],
            source="cognis",
        )

        result = resolve_last_user_message(store, USER_ID, SESSION_ID)
        assert result is None

    def test_no_events(self, store):
        """Returns None for empty session."""
        result = resolve_last_user_message(store, USER_ID, SESSION_ID)
        assert result is None

    def test_user_message_without_assistant(self, store):
        """Returns user content with None context when no assistant message."""
        store.append(
            USER_ID,
            SESSION_ID,
            [{"type": "user_message", "data": {"content": "first message"}}],
            source="cognis",
        )

        result = resolve_last_user_message(store, USER_ID, SESSION_ID)
        assert result is not None
        user_content, assistant_context = result
        assert user_content == "first message"
        assert assistant_context is None

    def test_multiple_user_messages_returns_last(self, store):
        """Returns the most recent user message."""
        store.append(
            USER_ID,
            SESSION_ID,
            [
                {"type": "user_message", "data": {"content": "first"}},
                {"type": "assistant_message", "data": {"content": "response"}},
                {"type": "user_message", "data": {"content": "second"}},
            ],
            source="cognis",
        )

        result = resolve_last_user_message(store, USER_ID, SESSION_ID)
        assert result is not None
        user_content, assistant_context = result
        assert user_content == "second"
        assert assistant_context == "response"

    def test_interleaved_tool_events(self, store):
        """Skips non-matching event types (tool_call, evaluation)."""
        store.append(
            USER_ID,
            SESSION_ID,
            [
                {"type": "assistant_message", "data": {"content": "I'll check"}},
                {"type": "tool_call", "data": {"tool": "read", "args": {}}},
                {"type": "user_message", "data": {"content": "check the file"}},
            ],
            source="cognis",
        )

        result = resolve_last_user_message(store, USER_ID, SESSION_ID)
        assert result is not None
        user_content, assistant_context = result
        assert user_content == "check the file"
        assert assistant_context == "I'll check"

    def test_opencode_part_fallback_for_assistant_context(self, store):
        """Falls back to assistant text parts when assistant messages are metadata-only."""
        store.append(
            USER_ID,
            SESSION_ID,
            [
                {
                    "type": "message",
                    "data": {"role": "assistant", "messageID": "msg-a"},
                },
                {
                    "type": "part",
                    "data": {
                        "part": {
                            "id": "part-a",
                            "type": "text",
                            "messageID": "msg-a",
                            "text": "I can push ainews to origin/main for you.",
                        }
                    },
                },
                {
                    "type": "message",
                    "data": {
                        "role": "user",
                        "messageID": "msg-u",
                        "text": "ok do it",
                    },
                },
            ],
            source="opencode",
        )

        result = resolve_last_user_message(store, USER_ID, SESSION_ID)
        assert result is not None
        user_content, assistant_context = result
        assert user_content == "ok do it"
        assert assistant_context == "I can push ainews to origin/main for you."
