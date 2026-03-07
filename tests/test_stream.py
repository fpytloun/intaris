"""Tests for the EventBus pub/sub system."""

from __future__ import annotations

import asyncio

import pytest

from intaris.api.stream import EventBus


class TestEventBus:
    """Tests for EventBus subscribe/publish/unsubscribe."""

    def test_subscribe_and_publish(self):
        """Basic pub/sub: subscriber receives published event."""
        bus = EventBus()
        queue = bus.subscribe("alice", "sess1")

        bus.publish({"type": "evaluated", "user_id": "alice", "session_id": "sess1"})

        event = queue.get_nowait()
        assert event["type"] == "evaluated"
        assert event["user_id"] == "alice"
        assert event["session_id"] == "sess1"

    def test_publish_routes_by_user_id(self):
        """Events are only delivered to subscribers with matching user_id."""
        bus = EventBus()
        alice_q = bus.subscribe("alice", "sess1")
        bob_q = bus.subscribe("bob", "sess1")

        bus.publish({"type": "evaluated", "user_id": "alice", "session_id": "sess1"})

        assert not alice_q.empty()
        assert bob_q.empty()

    def test_publish_routes_to_global_subscriber(self):
        """A subscriber with session_id=None receives events for all sessions."""
        bus = EventBus()
        global_q = bus.subscribe("alice", None)
        specific_q = bus.subscribe("alice", "sess1")

        bus.publish({"type": "evaluated", "user_id": "alice", "session_id": "sess1"})

        # Both should receive the event
        assert not global_q.empty()
        assert not specific_q.empty()

    def test_global_subscriber_does_not_receive_other_sessions_exact(self):
        """A session-specific subscriber does not receive events for other sessions."""
        bus = EventBus()
        sess1_q = bus.subscribe("alice", "sess1")
        sess2_q = bus.subscribe("alice", "sess2")

        bus.publish({"type": "evaluated", "user_id": "alice", "session_id": "sess1"})

        assert not sess1_q.empty()
        assert sess2_q.empty()

    def test_publish_adds_seq_counter(self):
        """Each published event gets a monotonically increasing seq number."""
        bus = EventBus()
        queue = bus.subscribe("alice", None)

        bus.publish({"type": "a", "user_id": "alice", "session_id": "s1"})
        bus.publish({"type": "b", "user_id": "alice", "session_id": "s2"})
        bus.publish({"type": "c", "user_id": "alice", "session_id": "s3"})

        e1 = queue.get_nowait()
        e2 = queue.get_nowait()
        e3 = queue.get_nowait()

        assert e1["seq"] == 1
        assert e2["seq"] == 2
        assert e3["seq"] == 3

    def test_publish_drops_event_without_user_id(self):
        """Events without user_id are silently dropped."""
        bus = EventBus()
        queue = bus.subscribe("alice", None)

        bus.publish({"type": "evaluated", "session_id": "sess1"})

        assert queue.empty()

    def test_queue_overflow_drops_oldest(self):
        """When the queue is full, the oldest event is dropped to make room."""
        bus = EventBus()
        queue = bus.subscribe("alice", None)

        # Fill the queue to capacity (1000 events)
        for i in range(1000):
            bus.publish({"type": "fill", "user_id": "alice", "session_id": "s", "i": i})

        assert queue.full()

        # Publish one more — should drop the oldest (i=0)
        bus.publish(
            {"type": "overflow", "user_id": "alice", "session_id": "s", "i": 1000}
        )

        # The oldest event should now be i=1 (i=0 was dropped)
        first = queue.get_nowait()
        assert first["i"] == 1

    def test_connection_limit_per_user(self):
        """Subscribing beyond the per-user limit raises ValueError."""
        bus = EventBus()

        # Subscribe 10 times (the limit)
        queues = []
        for i in range(10):
            queues.append(bus.subscribe("alice", f"sess{i}"))

        # The 11th should fail
        with pytest.raises(ValueError, match="Connection limit exceeded"):
            bus.subscribe("alice", "sess10")

        # A different user should still be able to subscribe
        bob_q = bus.subscribe("bob", "sess1")
        assert bob_q is not None

    def test_unsubscribe_removes_queue(self):
        """After unsubscribe, the queue no longer receives events."""
        bus = EventBus()
        queue = bus.subscribe("alice", "sess1")

        bus.unsubscribe("alice", "sess1", queue)
        bus.publish({"type": "evaluated", "user_id": "alice", "session_id": "sess1"})

        assert queue.empty()

    def test_unsubscribe_nonexistent_is_safe(self):
        """Unsubscribing a queue that was never subscribed does not raise."""
        bus = EventBus()
        fake_queue: asyncio.Queue = asyncio.Queue()

        # Should not raise
        bus.unsubscribe("alice", "sess1", fake_queue)

    def test_unsubscribe_frees_connection_slot(self):
        """Unsubscribing frees a slot for new subscriptions."""
        bus = EventBus()

        queues = []
        for i in range(10):
            queues.append(bus.subscribe("alice", f"sess{i}"))

        # At limit — cannot subscribe
        with pytest.raises(ValueError, match="Connection limit exceeded"):
            bus.subscribe("alice", "extra")

        # Unsubscribe one
        bus.unsubscribe("alice", "sess0", queues[0])

        # Now we can subscribe again
        new_q = bus.subscribe("alice", "extra")
        assert new_q is not None

    def test_publish_does_not_mutate_original_event(self):
        """Publishing adds seq to a copy, not the original dict."""
        bus = EventBus()
        bus.subscribe("alice", None)

        original = {"type": "evaluated", "user_id": "alice", "session_id": "s1"}
        bus.publish(original)

        assert "seq" not in original

    def test_multiple_subscribers_same_key(self):
        """Multiple subscribers on the same (user_id, session_id) all receive events."""
        bus = EventBus()
        q1 = bus.subscribe("alice", "sess1")
        q2 = bus.subscribe("alice", "sess1")

        bus.publish({"type": "evaluated", "user_id": "alice", "session_id": "sess1"})

        assert not q1.empty()
        assert not q2.empty()
        assert q1.get_nowait()["type"] == "evaluated"
        assert q2.get_nowait()["type"] == "evaluated"
