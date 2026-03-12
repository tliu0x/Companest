"""
Companest Event Bus

Simple async pub/sub event system for lifecycle events.

Usage:
    bus = EventBus()
    bus.on(EventType.TASK_STARTED, my_callback)
    await bus.emit(EventType.TASK_STARTED, {"team_id": "stock", "task": "..."})
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List

logger = logging.getLogger(__name__)

# Callback type: async fn(Event) -> None
EventCallback = Callable[["Event"], Coroutine[Any, Any, None]]


class EventType(str, Enum):
    """Lifecycle events emitted by the Companest framework."""
    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_ROUTED = "task_routed"
    TEAM_CREATED = "team_created"
    TEAM_REGISTERED = "team_registered"
    TEAM_EVICTED = "team_evicted"
    COST_APPROVED = "cost_approved"
    COST_REJECTED = "cost_rejected"
    CIRCUIT_BREAKER_TRIPPED = "circuit_breaker_tripped"
    TOOL_REGISTERED = "tool_registered"
    EVOLUTION_PROPOSAL_CREATED = "evolution_proposal_created"
    CANARY_STARTED = "canary_started"
    CANARY_PROMOTED = "canary_promoted"


@dataclass
class Event:
    """A single event emitted by the EventBus."""
    type: EventType
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class EventBus:
    """
    Async pub/sub event bus.

    Subscribers receive Event objects. Errors in callbacks are logged, not raised.
    """

    def __init__(self):
        self._listeners: Dict[EventType, List[EventCallback]] = {}
        self._any_listeners: List[EventCallback] = []

    def on(self, event_type: EventType, callback: EventCallback) -> None:
        """Subscribe to a specific event type."""
        if event_type not in self._listeners:
            self._listeners[event_type] = []
        if callback not in self._listeners[event_type]:
            self._listeners[event_type].append(callback)

    def on_any(self, callback: EventCallback) -> None:
        """Subscribe to all events (wildcard)."""
        if callback not in self._any_listeners:
            self._any_listeners.append(callback)

    def off(self, event_type: EventType, callback: EventCallback) -> None:
        """Unsubscribe from a specific event type."""
        if event_type in self._listeners:
            try:
                self._listeners[event_type].remove(callback)
            except ValueError:
                pass

    async def emit(self, event_type: EventType, data: Dict[str, Any] = None) -> None:
        """Fire an event. All callbacks run concurrently; errors are logged not raised."""
        event = Event(type=event_type, data=data or {})

        callbacks = list(self._listeners.get(event_type, []))
        callbacks.extend(self._any_listeners)

        if not callbacks:
            return

        results = await asyncio.gather(
            *[self._safe_call(cb, event) for cb in callbacks],
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                logger.error(f"[EventBus] Callback error for {event_type}: {r}")

    @staticmethod
    async def _safe_call(callback: EventCallback, event: Event) -> None:
        """Call a callback, catching and re-raising exceptions for gather."""
        await callback(event)

    def clear(self) -> None:
        """Remove all listeners."""
        self._listeners.clear()
        self._any_listeners.clear()
