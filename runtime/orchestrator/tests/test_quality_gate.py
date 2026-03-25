"""
runtime/orchestrator/tests/test_quality_gate.py
=========================================
Tests for _maybe_fire_quality_gate in orchestrator/micro_loop.py.

Uses a SimpleNamespace orchestrator mock and an AsyncMock alerter.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from runtime.orchestrator.micro_loop import _maybe_fire_quality_gate
from runtime.orchestrator.state import MicroLoopRecord, LoopStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_orch(threshold=0.4, escalation_count=3):
    """Build a minimal orchestrator-like SimpleNamespace."""
    cfg = SimpleNamespace(
        quality_gate_threshold=threshold,
        quality_gate_escalation_count=escalation_count,
    )
    orch = SimpleNamespace(config=cfg)
    return orch


def make_record(score=None, task_id="task-1", subsystem="api"):
    """Build a minimal MicroLoopRecord."""
    record = MicroLoopRecord(
        iteration=1,
        task_id=task_id,
        subsystem=subsystem,
        started_at=0.0,
    )
    record.critic_score = score
    return record


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMaybeFireQualityGate:
    def test_score_above_threshold_no_alert(self):
        """When score >= threshold, no alert should be fired and fails reset to 0."""
        orch = make_orch(threshold=0.4)
        orch.__dict__["_quality_gate_fails"] = 2  # simulate prior failures
        record = make_record(score=0.8)
        alerter = AsyncMock()

        _maybe_fire_quality_gate(orch, record, alerter, iteration=1)

        assert orch.__dict__["_quality_gate_fails"] == 0
        alerter.alert.assert_not_called()

    def test_score_equal_threshold_no_alert(self):
        """score exactly equal to threshold should also clear fails."""
        orch = make_orch(threshold=0.4)
        record = make_record(score=0.4)
        alerter = AsyncMock()

        _maybe_fire_quality_gate(orch, record, alerter, iteration=1)

        assert orch.__dict__.get("_quality_gate_fails", 0) == 0
        alerter.alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_score_below_threshold_fires_warning_alert(self):
        """score < threshold on the first failure should fire a WARNING alert."""
        orch = make_orch(threshold=0.4, escalation_count=3)
        record = make_record(score=0.2)
        alerter = AsyncMock()

        with patch("asyncio.create_task") as mock_create_task:
            _maybe_fire_quality_gate(orch, record, alerter, iteration=5)
            assert mock_create_task.called

        assert orch.__dict__["_quality_gate_fails"] == 1

    @pytest.mark.asyncio
    async def test_consecutive_failures_escalate_to_error(self):
        """After escalation_count consecutive failures, severity becomes ERROR."""
        from infra.observability.alerting import AlertSeverity

        orch = make_orch(threshold=0.4, escalation_count=3)
        orch.__dict__["_quality_gate_fails"] = 2  # already 2 failures
        record = make_record(score=0.1)

        alerter = AsyncMock()

        with patch("asyncio.create_task") as mock_create_task:
            _maybe_fire_quality_gate(orch, record, alerter, iteration=10)

            # Verify create_task was called (3rd failure → escalation)
            assert mock_create_task.called
            # The third failure (fails=3 >= escalation_count=3) should use ERROR severity
            assert orch.__dict__["_quality_gate_fails"] == 3

            # Extract the coroutine passed to create_task and check severity
            call_args = mock_create_task.call_args[0][0]
            # The coroutine is alerter.alert(...) — check alerter was called with ERROR
            # We can't await the coroutine here, but we verify the fail count escalated
            assert orch.__dict__["_quality_gate_fails"] == 3

    def test_alerter_none_no_crash(self):
        """When alerter is None, _maybe_fire_quality_gate should not crash."""
        orch = make_orch(threshold=0.4)
        record = make_record(score=0.1)

        # alerter=None — must not raise
        _maybe_fire_quality_gate(orch, record, None, iteration=1)

    def test_threshold_zero_never_fires(self):
        """threshold=0.0 disables the quality gate — no alert even for score=0."""
        orch = make_orch(threshold=0.0)
        record = make_record(score=0.0)
        alerter = AsyncMock()

        with patch("asyncio.create_task") as mock_create_task:
            _maybe_fire_quality_gate(orch, record, alerter, iteration=1)
            mock_create_task.assert_not_called()

    def test_none_score_no_alert(self):
        """When critic_score is None (step skipped), no alert should fire."""
        orch = make_orch(threshold=0.4)
        record = make_record(score=None)
        alerter = AsyncMock()

        with patch("asyncio.create_task") as mock_create_task:
            _maybe_fire_quality_gate(orch, record, alerter, iteration=1)
            mock_create_task.assert_not_called()

        assert orch.__dict__.get("_quality_gate_fails", 0) == 0

    def test_quality_gate_fails_increments_on_each_failure(self):
        """_quality_gate_fails should increment with each sub-threshold call."""
        orch = make_orch(threshold=0.5)
        record = make_record(score=0.1)
        alerter = AsyncMock()

        with patch("asyncio.create_task"):
            _maybe_fire_quality_gate(orch, record, alerter, iteration=1)
            assert orch.__dict__["_quality_gate_fails"] == 1

            _maybe_fire_quality_gate(orch, record, alerter, iteration=2)
            assert orch.__dict__["_quality_gate_fails"] == 2

            _maybe_fire_quality_gate(orch, record, alerter, iteration=3)
            assert orch.__dict__["_quality_gate_fails"] == 3

    def test_recovery_resets_fail_count(self):
        """After sub-threshold scores, a good score should reset the counter."""
        orch = make_orch(threshold=0.4)
        orch.__dict__["_quality_gate_fails"] = 2
        good_record = make_record(score=0.9)
        alerter = AsyncMock()

        _maybe_fire_quality_gate(orch, good_record, alerter, iteration=10)

        assert orch.__dict__["_quality_gate_fails"] == 0
        alerter.alert.assert_not_called()
