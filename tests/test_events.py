"""
Companest EventBus Tests

Tests for the async pub/sub event system.
"""

import asyncio

import pytest

from companest.events import EventBus, Event, EventType


class TestEventBus:
    """Test EventBus pub/sub mechanics."""

    def test_on_and_emit(self):
        """Subscribe to an event and verify callback fires."""
        bus = EventBus()
        received = []

        async def handler(event: Event):
            received.append(event)

        bus.on(EventType.TASK_STARTED, handler)

        asyncio.get_event_loop().run_until_complete(
            bus.emit(EventType.TASK_STARTED, {"team_id": "stock"})
        )
        assert len(received) == 1
        assert received[0].type == EventType.TASK_STARTED
        assert received[0].data["team_id"] == "stock"

    def test_on_any(self):
        """Wildcard subscriber receives all events."""
        bus = EventBus()
        received = []

        async def handler(event: Event):
            received.append(event.type)

        bus.on_any(handler)

        async def emit_two():
            await bus.emit(EventType.TASK_STARTED, {})
            await bus.emit(EventType.TASK_COMPLETED, {})

        asyncio.get_event_loop().run_until_complete(emit_two())
        assert EventType.TASK_STARTED in received
        assert EventType.TASK_COMPLETED in received

    def test_off(self):
        """Unsubscribed callback no longer fires."""
        bus = EventBus()
        received = []

        async def handler(event: Event):
            received.append(event)

        bus.on(EventType.TASK_STARTED, handler)
        bus.off(EventType.TASK_STARTED, handler)

        asyncio.get_event_loop().run_until_complete(
            bus.emit(EventType.TASK_STARTED, {})
        )
        assert len(received) == 0

    def test_clear(self):
        """Clear removes all listeners."""
        bus = EventBus()
        received = []

        async def handler(event: Event):
            received.append(event)

        bus.on(EventType.TASK_STARTED, handler)
        bus.on_any(handler)
        bus.clear()

        asyncio.get_event_loop().run_until_complete(
            bus.emit(EventType.TASK_STARTED, {})
        )
        assert len(received) == 0

    def test_emit_with_no_listeners(self):
        """Emit without listeners does not crash."""
        bus = EventBus()
        asyncio.get_event_loop().run_until_complete(
            bus.emit(EventType.TASK_STARTED, {"foo": "bar"})
        )

    def test_callback_error_logged_not_raised(self):
        """Errors in callbacks are caught  emit does not raise."""
        bus = EventBus()

        async def bad_handler(event: Event):
            raise ValueError("boom")

        bus.on(EventType.TASK_STARTED, bad_handler)

        # Should not raise
        asyncio.get_event_loop().run_until_complete(
            bus.emit(EventType.TASK_STARTED, {})
        )

    def test_multiple_listeners_same_event(self):
        """Multiple listeners on same event type all fire."""
        bus = EventBus()
        results = []

        async def h1(event: Event):
            results.append("h1")

        async def h2(event: Event):
            results.append("h2")

        bus.on(EventType.TASK_COMPLETED, h1)
        bus.on(EventType.TASK_COMPLETED, h2)

        asyncio.get_event_loop().run_until_complete(
            bus.emit(EventType.TASK_COMPLETED, {})
        )
        assert "h1" in results
        assert "h2" in results

    def test_event_has_timestamp(self):
        """Events include an ISO timestamp."""
        bus = EventBus()
        received = []

        async def handler(event: Event):
            received.append(event)

        bus.on(EventType.TOOL_REGISTERED, handler)
        asyncio.get_event_loop().run_until_complete(
            bus.emit(EventType.TOOL_REGISTERED, {"provider": "kalshi"})
        )
        assert received[0].timestamp is not None
        assert "T" in received[0].timestamp  # ISO format

    def test_no_duplicate_subscriptions(self):
        """Same callback registered twice only fires once."""
        bus = EventBus()
        count = []

        async def handler(event: Event):
            count.append(1)

        bus.on(EventType.TASK_STARTED, handler)
        bus.on(EventType.TASK_STARTED, handler)

        asyncio.get_event_loop().run_until_complete(
            bus.emit(EventType.TASK_STARTED, {})
        )
        assert len(count) == 1

    def test_event_type_values(self):
        """All expected event types exist."""
        expected = [
            "task_started", "task_completed", "task_failed", "task_routed",
            "team_created", "team_registered", "team_evicted",
            "cost_approved", "cost_rejected", "tool_registered",
        ]
        for val in expected:
            assert EventType(val) is not None
