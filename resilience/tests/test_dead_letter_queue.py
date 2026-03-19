"""
Tests for resilience/dead_letter_queue.py
==========================================

Verifies DLQ enqueue, status transitions, stats, and purge operations
using a temporary in-memory SQLite database (`:memory:`).
"""

from __future__ import annotations

import pytest

from resilience.dead_letter_queue import DeadLetterQueue


@pytest.fixture
async def dlq(tmp_path):
    """Provide a fresh DLQ backed by a temp file for each test."""
    db_path = str(tmp_path / "test_dlq.sqlite")
    q = DeadLetterQueue(db_path=db_path)
    await q.connect()
    yield q
    await q.close()


class TestDeadLetterQueueEnqueue:
    @pytest.mark.asyncio
    async def test_enqueue_stores_item(self, dlq):
        item_id = await dlq.enqueue(
            operation="micro_loop",
            payload={"task_id": "t-001"},
            error="Connection reset by peer",
        )
        assert item_id is not None

    @pytest.mark.asyncio
    async def test_pending_items_returned(self, dlq):
        await dlq.enqueue("op1", {"x": 1}, "err1")
        await dlq.enqueue("op2", {"x": 2}, "err2")
        items = await dlq.pending_items()
        assert len(items) == 2

    @pytest.mark.asyncio
    async def test_mark_resolved(self, dlq):
        item_id = await dlq.enqueue("op", {}, "error")
        await dlq.mark_resolved(item_id, notes="fixed manually")
        pending = await dlq.pending_items()
        assert len(pending) == 0

    @pytest.mark.asyncio
    async def test_mark_discarded(self, dlq):
        item_id = await dlq.enqueue("op", {}, "error")
        await dlq.mark_discarded(item_id, reason="no longer relevant")
        pending = await dlq.pending_items()
        assert len(pending) == 0

    @pytest.mark.asyncio
    async def test_stats_counts(self, dlq):
        await dlq.enqueue("op", {}, "err")
        stats = await dlq.stats()
        assert stats.get("total", 0) >= 1
        assert "pending" in stats

    @pytest.mark.asyncio
    async def test_purge_resolved_removes_old_items(self, dlq):
        item_id = await dlq.enqueue("op", {}, "err")
        await dlq.mark_resolved(item_id)
        # Purge items older than 0 seconds (i.e., everything resolved)
        removed = await dlq.purge_resolved(older_than_days=0)
        assert removed >= 1
