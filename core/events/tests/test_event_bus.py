"""
Tests for core/events/bus.py and core/events/types.py.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from core.events import Event, EventBus, EventType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_event(event_type: EventType = EventType.MICRO_LOOP_COMPLETED, **kwargs) -> Event:
    return Event(type=event_type, source="test", **kwargs)


# ---------------------------------------------------------------------------
# Event dataclass
# ---------------------------------------------------------------------------


class TestEventDataclass:
    def test_auto_id(self):
        e1 = make_event()
        e2 = make_event()
        assert e1.id != e2.id

    def test_auto_timestamp(self):
        e = make_event()
        assert isinstance(e.timestamp, datetime)
        assert e.timestamp.tzinfo == UTC

    def test_payload_defaults_empty(self):
        e = make_event()
        assert e.payload == {}

    def test_custom_payload(self):
        e = make_event(payload={"score": 0.9})
        assert e.payload["score"] == 0.9

    def test_event_type_is_string_enum(self):
        assert EventType.MICRO_LOOP_COMPLETED == "micro_loop_completed"


# ---------------------------------------------------------------------------
# EventBus — subscribe and publish
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestEventBusSubscribePublish:
    async def test_handler_called(self):
        bus = EventBus()
        received: list[Event] = []

        @bus.subscribe(EventType.MICRO_LOOP_COMPLETED)
        async def handler(e: Event) -> None:
            received.append(e)

        event = make_event(EventType.MICRO_LOOP_COMPLETED)
        count = await bus.publish(event)

        assert count == 1
        assert len(received) == 1
        assert received[0] is event

    async def test_wildcard_handler_receives_all(self):
        bus = EventBus()
        received: list[Event] = []

        @bus.subscribe()
        async def all_handler(e: Event) -> None:
            received.append(e)

        await bus.publish(make_event(EventType.TASK_COMPLETED))
        await bus.publish(make_event(EventType.CIRCUIT_OPENED))

        assert len(received) == 2

    async def test_specific_and_wildcard_both_called(self):
        bus = EventBus()
        specific: list[Event] = []
        wild: list[Event] = []

        @bus.subscribe(EventType.STAGNATION_DETECTED)
        async def on_stagnation(e: Event) -> None:
            specific.append(e)

        @bus.subscribe()
        async def on_all(e: Event) -> None:
            wild.append(e)

        await bus.publish(make_event(EventType.STAGNATION_DETECTED))

        assert len(specific) == 1
        assert len(wild) == 1

    async def test_no_handlers_returns_zero(self):
        bus = EventBus()
        count = await bus.publish(make_event(EventType.CUSTOM))
        assert count == 0

    async def test_multiple_handlers_same_type(self):
        bus = EventBus()
        calls = []

        @bus.subscribe(EventType.ARTIFACT_STORED)
        async def h1(e: Event) -> None:
            calls.append("h1")

        @bus.subscribe(EventType.ARTIFACT_STORED)
        async def h2(e: Event) -> None:
            calls.append("h2")

        await bus.publish(make_event(EventType.ARTIFACT_STORED))
        assert set(calls) == {"h1", "h2"}

    async def test_wrong_type_not_called(self):
        bus = EventBus()
        received = []

        @bus.subscribe(EventType.MACRO_LOOP_COMPLETED)
        async def handler(e: Event) -> None:
            received.append(e)

        await bus.publish(make_event(EventType.MICRO_LOOP_COMPLETED))
        assert len(received) == 0


# ---------------------------------------------------------------------------
# EventBus — error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestEventBusErrorHandling:
    async def test_log_policy_isolates_error(self):
        """A failing handler does not prevent other handlers from running."""
        bus = EventBus(error_policy="log")
        ran = []

        @bus.subscribe(EventType.TASK_FAILED)
        async def bad(e: Event) -> None:
            raise ValueError("boom")

        @bus.subscribe(EventType.TASK_FAILED)
        async def good(e: Event) -> None:
            ran.append("good")

        # Should not raise
        await bus.publish(make_event(EventType.TASK_FAILED))
        assert "good" in ran

    async def test_raise_policy_propagates_error(self):
        bus = EventBus(error_policy="raise")

        @bus.subscribe(EventType.TASK_FAILED)
        async def bad(e: Event) -> None:
            raise RuntimeError("intentional")

        with pytest.raises(RuntimeError, match="intentional"):
            await bus.publish(make_event(EventType.TASK_FAILED))

    async def test_invalid_error_policy_raises(self):
        with pytest.raises(ValueError):
            EventBus(error_policy="silent")


# ---------------------------------------------------------------------------
# EventBus — unsubscribe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestEventBusUnsubscribe:
    async def test_unsubscribe_stops_handler(self):
        bus = EventBus()
        received = []

        async def handler(e: Event) -> None:
            received.append(e)

        bus.subscribe_handler(EventType.SYSTEM_STARTED, handler)
        bus.unsubscribe(EventType.SYSTEM_STARTED, handler)

        await bus.publish(make_event(EventType.SYSTEM_STARTED))
        assert len(received) == 0

    def test_unsubscribe_nonexistent_returns_false(self):
        bus = EventBus()

        async def handler(e: Event) -> None:
            pass

        result = bus.unsubscribe(EventType.CUSTOM, handler)
        assert result is False


# ---------------------------------------------------------------------------
# EventBus — introspection
# ---------------------------------------------------------------------------


class TestEventBusIntrospection:
    def test_handler_count_empty(self):
        bus = EventBus()
        assert bus.handler_count(EventType.MICRO_LOOP_COMPLETED) == 0

    def test_handler_count_after_subscribe(self):
        bus = EventBus()

        @bus.subscribe(EventType.MESO_LOOP_COMPLETED)
        async def h(e: Event) -> None:
            pass

        assert bus.handler_count(EventType.MESO_LOOP_COMPLETED) == 1

    def test_handler_count_wildcard(self):
        bus = EventBus()

        @bus.subscribe()
        async def h(e: Event) -> None:
            pass

        assert bus.handler_count(None) == 1

    def test_clear_specific_type(self):
        bus = EventBus()

        @bus.subscribe(EventType.SLA_BREACHED)
        async def h(e: Event) -> None:
            pass

        bus.clear(EventType.SLA_BREACHED)
        assert bus.handler_count(EventType.SLA_BREACHED) == 0

    def test_clear_all(self):
        bus = EventBus()

        @bus.subscribe(EventType.SLA_BREACHED)
        async def h1(e: Event) -> None:
            pass

        @bus.subscribe()
        async def h2(e: Event) -> None:
            pass

        bus.clear()
        assert bus.handler_count(EventType.SLA_BREACHED) == 0
        assert bus.handler_count(None) == 0


# ---------------------------------------------------------------------------
# EventBus — subscribe_handler (non-decorator)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestEventBusSubscribeHandler:
    async def test_subscribe_handler_programmatic(self):
        bus = EventBus()
        received = []

        async def handler(e: Event) -> None:
            received.append(e)

        bus.subscribe_handler(EventType.TASK_SELECTED, handler)
        await bus.publish(make_event(EventType.TASK_SELECTED))

        assert len(received) == 1


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestEventBusConcurrency:
    async def test_concurrent_publishes_dont_lose_events(self):
        """100 concurrent publishes should all be handled."""
        bus = EventBus()
        counter = {"n": 0}
        lock = asyncio.Lock()

        @bus.subscribe(EventType.MICRO_LOOP_COMPLETED)
        async def inc(e: Event) -> None:
            async with lock:
                counter["n"] += 1

        await asyncio.gather(
            *[bus.publish(make_event(EventType.MICRO_LOOP_COMPLETED)) for _ in range(100)]
        )

        assert counter["n"] == 100
