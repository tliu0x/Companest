"""Companest EventBus Tests"""

import pytest

from companest.events import EventBus, Event, EventType

pytestmark = pytest.mark.asyncio


async def test_on_and_emit():
    bus = EventBus()
    received = []

    async def handler(event: Event):
        received.append(event)

    bus.on(EventType.TASK_STARTED, handler)
    await bus.emit(EventType.TASK_STARTED, {"team_id": "stock"})

    assert len(received) == 1
    assert received[0].type == EventType.TASK_STARTED
    assert received[0].data["team_id"] == "stock"


async def test_on_any():
    bus = EventBus()
    received = []

    async def handler(event: Event):
        received.append(event.type)

    bus.on_any(handler)
    await bus.emit(EventType.TASK_STARTED, {})
    await bus.emit(EventType.TASK_COMPLETED, {})

    assert EventType.TASK_STARTED in received
    assert EventType.TASK_COMPLETED in received


async def test_callback_error_does_not_stop_other_listeners():
    bus = EventBus()
    results = []

    async def bad_handler(event: Event):
        raise ValueError("boom")

    async def h1(event: Event):
        results.append("h1")

    async def h2(event: Event):
        results.append("h2")

    bus.on(EventType.TASK_COMPLETED, bad_handler)
    bus.on(EventType.TASK_COMPLETED, h1)
    bus.on(EventType.TASK_COMPLETED, h2)

    await bus.emit(EventType.TASK_COMPLETED, {})
    assert "h1" in results
    assert "h2" in results


async def test_no_duplicate_subscriptions():
    bus = EventBus()
    count = []

    async def handler(event: Event):
        count.append(1)

    bus.on(EventType.TASK_STARTED, handler)
    bus.on(EventType.TASK_STARTED, handler)

    await bus.emit(EventType.TASK_STARTED, {})
    assert len(count) == 1
