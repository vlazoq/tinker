"""
Tests for resilience/backpressure.py
======================================

Verifies that BackpressureController returns the correct recommendations
for different combinations of system load signals.
"""
from __future__ import annotations

import pytest

from resilience.backpressure import (
    BackpressureAction,
    BackpressureController,
    BackpressureRecommendation,
)


@pytest.fixture
def controller():
    """A BackpressureController with tight test-friendly thresholds."""
    return BackpressureController(
        queue_warn_depth=5,
        queue_pause_depth=10,
        failure_warn_streak=2,
        failure_pause_streak=4,
        compress_artifact_count=50,
    )


class TestBackpressureEvaluation:
    def test_no_pressure_returns_none(self, controller):
        rec = controller.evaluate(queue_depth=0, failure_streak=0)
        assert rec.action == BackpressureAction.NONE

    def test_warn_on_moderate_queue_depth(self, controller):
        rec = controller.evaluate(queue_depth=6, failure_streak=0)
        assert rec.action in (BackpressureAction.WARN, BackpressureAction.SLOW_DOWN)

    def test_pause_on_deep_queue(self, controller):
        rec = controller.evaluate(queue_depth=15, failure_streak=0)
        assert rec.action in (BackpressureAction.PAUSE_GENERATION, BackpressureAction.SLOW_DOWN)
        assert rec.wait_seconds > 0

    def test_warn_on_moderate_failure_streak(self, controller):
        rec = controller.evaluate(queue_depth=0, failure_streak=3)
        assert rec.action != BackpressureAction.NONE

    def test_pause_on_high_failure_streak(self, controller):
        rec = controller.evaluate(queue_depth=0, failure_streak=5)
        assert rec.action in (BackpressureAction.PAUSE_GENERATION, BackpressureAction.SLOW_DOWN)

    def test_compress_on_high_artifact_count(self, controller):
        rec = controller.evaluate(queue_depth=0, failure_streak=0, artifact_count=100)
        assert rec.action == BackpressureAction.COMPRESS_MEMORY

    def test_recommendation_has_reason(self, controller):
        rec = controller.evaluate(queue_depth=20, failure_streak=0)
        assert rec.reason
        assert isinstance(rec.reason, str)

    def test_recommendation_dataclass_fields(self):
        rec = BackpressureRecommendation(
            action=BackpressureAction.SLOW_DOWN,
            wait_seconds=5.0,
            reason="test",
            signals={"q": 3},
        )
        assert rec.action == BackpressureAction.SLOW_DOWN
        assert rec.wait_seconds == 5.0
