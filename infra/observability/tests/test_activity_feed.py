"""
infra/observability/tests/test_activity_feed.py
================================================
Unit tests for the ActivityFeed.
"""

from __future__ import annotations

import asyncio

import pytest

from infra.observability.activity_feed import (
    ActivityCategory,
    ActivityEntry,
    ActivityFeed,
)


class TestActivityEntry:
    def test_to_dict_basic(self):
        entry = ActivityEntry(message="hello", category=ActivityCategory.MICRO)
        d = entry.to_dict()
        assert d["message"] == "hello"
        assert d["category"] == "micro"
        assert "timestamp" in d
        assert "detail" not in d  # empty detail omitted
        assert "elapsed" not in d  # None elapsed omitted

    def test_to_dict_with_detail_and_elapsed(self):
        entry = ActivityEntry(
            message="done",
            category=ActivityCategory.QUALITY,
            detail="extra info",
            elapsed=1.234,
        )
        d = entry.to_dict()
        assert d["detail"] == "extra info"
        assert d["elapsed"] == 1.23  # rounded to 2 dp


class TestActivityFeed:
    @pytest.mark.asyncio
    async def test_post_and_history(self):
        feed = ActivityFeed(max_history=10)
        await feed.post("first message", category="micro")
        await feed.post("second message", category="task")

        history = feed.get_history()
        assert len(history) == 2
        assert history[0]["message"] == "first message"
        assert history[1]["message"] == "second message"

    @pytest.mark.asyncio
    async def test_latest(self):
        feed = ActivityFeed()
        assert feed.latest is None
        await feed.post("only one")
        assert feed.latest is not None
        assert feed.latest.message == "only one"

    @pytest.mark.asyncio
    async def test_listener_receives_entries(self):
        feed = ActivityFeed()
        received = []
        feed.add_listener(lambda entry: received.append(entry.message))

        await feed.post("test entry")

        assert received == ["test entry"]

    @pytest.mark.asyncio
    async def test_async_listener(self):
        feed = ActivityFeed()
        received = []

        async def async_listener(entry):
            received.append(entry.message)

        feed.add_listener(async_listener)
        await feed.post("async test")

        assert received == ["async test"]

    @pytest.mark.asyncio
    async def test_remove_listener(self):
        feed = ActivityFeed()
        received = []
        listener = lambda entry: received.append(entry.message)

        feed.add_listener(listener)
        await feed.post("before")
        assert feed.remove_listener(listener) is True
        await feed.post("after")

        assert received == ["before"]

    @pytest.mark.asyncio
    async def test_remove_nonexistent_listener(self):
        feed = ActivityFeed()
        assert feed.remove_listener(lambda e: None) is False

    @pytest.mark.asyncio
    async def test_history_limit(self):
        feed = ActivityFeed(max_history=3)
        for i in range(5):
            await feed.post(f"msg-{i}")

        history = feed.get_history()
        assert len(history) == 3
        assert history[0]["message"] == "msg-2"  # oldest retained
        assert history[-1]["message"] == "msg-4"

    @pytest.mark.asyncio
    async def test_get_history_limit_param(self):
        feed = ActivityFeed()
        for i in range(10):
            await feed.post(f"msg-{i}")

        history = feed.get_history(limit=3)
        assert len(history) == 3
        assert history[0]["message"] == "msg-7"

    @pytest.mark.asyncio
    async def test_category_string_coercion(self):
        feed = ActivityFeed()
        entry = await feed.post("test", category="quality")
        assert entry.category == ActivityCategory.QUALITY

    @pytest.mark.asyncio
    async def test_invalid_category_defaults_to_system(self):
        feed = ActivityFeed()
        entry = await feed.post("test", category="nonexistent_category")
        assert entry.category == ActivityCategory.SYSTEM

    @pytest.mark.asyncio
    async def test_listener_error_does_not_break_feed(self):
        feed = ActivityFeed()

        def bad_listener(entry):
            raise RuntimeError("boom")

        feed.add_listener(bad_listener)
        # Should not raise
        await feed.post("still works")

        assert feed.latest.message == "still works"


class TestActivityFeedEventBusWiring:
    @pytest.mark.asyncio
    async def test_wire_event_bus(self):
        from core.events import Event, EventBus, EventType

        feed = ActivityFeed()
        bus = EventBus()
        feed.wire_event_bus(bus)

        received = []
        feed.add_listener(lambda entry: received.append(entry.message))

        await bus.publish(
            Event(
                type=EventType.MICRO_LOOP_COMPLETED,
                payload={
                    "iteration": 42,
                    "subsystem": "memory_manager",
                    "score": 0.85,
                    "duration": 3.2,
                },
            )
        )

        assert len(received) == 1
        assert "42" in received[0]
        assert "memory_manager" in received[0]
