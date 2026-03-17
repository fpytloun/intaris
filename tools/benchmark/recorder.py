"""Session recording event emitter for the Intaris benchmark.

Buffers events and sends them to Intaris's event store in batches,
matching the real OpenCode plugin's recording pattern.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from tools.benchmark.client import IntarisClient

logger = logging.getLogger(__name__)


class SessionRecorder:
    """Buffers events and sends them to Intaris event store in batches.

    Matches the real OpenCode plugin format for event types:
    - message: {role: "user"|"assistant", text: str, ...}
    - tool_call: {tool: str, args: dict, callID: str, sessionID: str}
    - tool_result: {tool: str, output: str, isError: bool, callID: str}
    """

    def __init__(
        self,
        client: IntarisClient,
        session_id: str,
        *,
        source: str = "benchmark",
        flush_size: int = 50,
    ) -> None:
        self._client = client
        self._session_id = session_id
        self._source = source
        self._flush_size = flush_size
        self._buffer: list[dict] = []

    def record(self, event_type: str, data: dict) -> None:
        """Buffer an event. Auto-flushes at flush_size threshold.

        Valid event types: message, tool_call, tool_result, checkpoint,
        reasoning, transcript
        """
        self._buffer.append(
            {
                "type": event_type,
                "data": {**data, "ts": datetime.now(timezone.utc).isoformat()},
            }
        )
        if len(self._buffer) >= self._flush_size:
            self.flush()

    def record_user_message(self, text: str) -> None:
        """Record a user message event."""
        self.record("message", {"role": "user", "text": text})

    def record_assistant_message(self, text: str) -> None:
        """Record an assistant message event."""
        self.record("message", {"role": "assistant", "text": text})

    def record_tool_call(self, tool: str, args: dict, call_id: str) -> None:
        """Record a tool call event (before evaluation)."""
        self.record(
            "tool_call",
            {
                "tool": tool,
                "args": args,
                "callID": call_id,
                "sessionID": self._session_id,
            },
        )

    def record_tool_result(
        self,
        tool: str,
        output: str,
        call_id: str,
        *,
        is_error: bool = False,
    ) -> None:
        """Record a tool result event (after evaluation + response)."""
        self.record(
            "tool_result",
            {
                "tool": tool,
                "output": output,
                "isError": is_error,
                "callID": call_id,
            },
        )

    def flush(self) -> None:
        """Send buffered events to Intaris. Errors are logged, not raised."""
        if not self._buffer:
            return

        events = self._buffer.copy()
        self._buffer.clear()

        try:
            self._client.append_events(self._session_id, events)
            logger.debug(
                "Flushed %d events for session %s", len(events), self._session_id
            )
        except Exception:
            logger.warning(
                "Failed to flush %d events for session %s (events lost)",
                len(events),
                self._session_id,
                exc_info=True,
            )

    @property
    def buffered_count(self) -> int:
        """Number of buffered (unflushed) events."""
        return len(self._buffer)
