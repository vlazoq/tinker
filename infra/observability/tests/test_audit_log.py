"""
Tests for observability/audit_log.py
======================================

Verifies event logging, querying, stats, and graceful close/flush.
"""

from __future__ import annotations

import pytest

from infra.observability.audit_log import AuditLog, AuditEventType


@pytest.fixture
async def audit_log(tmp_path):
    """Provide a fresh AuditLog backed by a temp SQLite file."""
    db_path = str(tmp_path / "test_audit.sqlite")
    log = AuditLog(db_path=db_path)
    await log.connect()
    yield log
    await log.close()


class TestAuditLog:
    @pytest.mark.asyncio
    async def test_log_and_flush(self, audit_log):
        await audit_log.log(
            event_type=AuditEventType.TASK_SELECTED,
            actor="test",
            details={"task_id": "t-001"},
            trace_id="trace-abc",
        )
        await audit_log._flush_buffer()  # force flush to SQLite
        stats = await audit_log.stats()
        assert stats.get("total", 0) >= 1

    @pytest.mark.asyncio
    async def test_query_by_event_type(self, audit_log):
        await audit_log.log(
            event_type=AuditEventType.TASK_SELECTED,
            actor="test",
            details={"task_id": "t-001"},
        )
        await audit_log.log(
            event_type=AuditEventType.ARTIFACT_STORED,
            actor="test",
            details={"artifact_id": "a-001"},
        )
        await audit_log._flush_buffer()
        events = await audit_log.query(event_type=AuditEventType.TASK_SELECTED)
        assert len(events) >= 1
        assert all(
            e["event_type"] == AuditEventType.TASK_SELECTED.value for e in events
        )

    @pytest.mark.asyncio
    async def test_query_by_trace_id(self, audit_log):
        await audit_log.log(
            event_type=AuditEventType.SYSTEM_START,
            actor="test",
            trace_id="my-trace-xyz",
        )
        await audit_log._flush_buffer()
        events = await audit_log.query(trace_id="my-trace-xyz")
        assert len(events) >= 1

    @pytest.mark.asyncio
    async def test_stats_counts(self, audit_log):
        for _ in range(3):
            await audit_log.log(event_type=AuditEventType.TASK_COMPLETED, actor="test")
        await audit_log._flush_buffer()
        stats = await audit_log.stats()
        assert stats.get("total", 0) >= 3

    @pytest.mark.asyncio
    async def test_close_flushes_buffer(self, audit_log):
        """Closing the log should flush any buffered events."""
        db_path = audit_log._db_path
        await audit_log.log(event_type=AuditEventType.SYSTEM_STOP, actor="test")
        await audit_log.close()
        # Re-open the same DB file to verify the event was persisted
        log2 = AuditLog(db_path=db_path)
        await log2.connect()
        stats = await log2.stats()
        assert stats.get("total", 0) >= 1
        await log2.close()
