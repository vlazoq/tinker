"""
core/events/ — Async publish-subscribe event bus.

The event bus decouples event producers (orchestrator, circuit breakers,
stagnation monitor, …) from event consumers (alerting, interventions,
external integrations, …).

Without an event bus:
  - The orchestrator must check stagnation after every loop (polling)
  - Adding a new reaction to circuit-open requires editing the circuit breaker
  - Components are tightly coupled via direct method calls

With an event bus:
  - Components emit events and are unaware of who handles them
  - New handlers can be attached at startup without touching producers
  - Handlers can be async, making it easy to fan out to multiple consumers

Public API
----------
::

    from core.events import EventBus, Event, EventType

    bus = EventBus()

    @bus.subscribe(EventType.STAGNATION_DETECTED)
    async def on_stagnation(event: Event) -> None:
        await alerter.alert(...)

    await bus.publish(Event(type=EventType.STAGNATION_DETECTED, payload={...}))
"""

from .bus import EventBus
from .types import Event, EventType

__all__ = ["EventBus", "Event", "EventType"]
