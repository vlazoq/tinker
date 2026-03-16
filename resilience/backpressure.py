"""
resilience/backpressure.py
===========================

Backpressure feedback loop for Tinker task generation.

What is backpressure?
---------------------
Backpressure is a mechanism to slow down producers when consumers are falling
behind.  In Tinker, the "producer" is the task generator (creates new tasks
after every micro loop) and the "consumer" is the micro loop executor.

Without backpressure, Tinker can generate tasks faster than it processes them,
leading to:
  - Queue depths of thousands of tasks (memory exhaustion)
  - Old, stale tasks accumulating while new "hot" topics are always preferred
  - Wasted compute on a queue that will never drain
  - Database bloat from too many stored but unprocessed tasks

How it works
------------
The BackpressureController monitors three signals:
  1. Queue depth (too many pending tasks → slow down generation)
  2. Consecutive failures (system struggling → pause generation temporarily)
  3. Memory pressure (too many stored artifacts → compress before adding more)

When any signal exceeds its threshold, the controller returns a "wait" duration
that the orchestrator should sleep before generating more tasks.

Usage
------
::

    bp = BackpressureController()

    # After each micro loop, check if we should pause:
    wait = bp.check(queue_depth=queue.depth, failure_streak=state.consecutive_failures)
    if wait > 0:
        await asyncio.sleep(wait)
        return 0  # Skip task generation this iteration

    # Or just get a recommendation:
    recommendation = bp.evaluate(queue_depth=200, failure_streak=0, artifact_count=5000)
    print(recommendation.action)    # BackpressureAction.PAUSE_GENERATION
    print(recommendation.wait_seconds)
    print(recommendation.reason)
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


class BackpressureAction(enum.Enum):
    """Recommended action from the backpressure controller."""
    NONE            = "none"             # System healthy — proceed normally
    WARN            = "warn"             # Approaching threshold — log warning
    SLOW_DOWN       = "slow_down"        # Generate fewer tasks (reduce rate)
    PAUSE_GENERATION = "pause_generation"  # Stop generating tasks this cycle
    COMPRESS_MEMORY  = "compress_memory"  # Trigger memory compression


@dataclass
class BackpressureRecommendation:
    """
    Recommendation from the backpressure controller for one evaluation cycle.

    Attributes
    ----------
    action       : What the orchestrator should do.
    wait_seconds : How long to wait before the next task generation (0 = no wait).
    reason       : Human-readable explanation of why this action was recommended.
    signals      : Dict of the raw signal values that triggered the recommendation.
    """
    action: BackpressureAction = BackpressureAction.NONE
    wait_seconds: float = 0.0
    reason: str = ""
    signals: dict = field(default_factory=dict)

    @property
    def should_pause(self) -> bool:
        """True if task generation should be skipped this cycle."""
        return self.action in (
            BackpressureAction.PAUSE_GENERATION,
            BackpressureAction.COMPRESS_MEMORY,
        )


class BackpressureController:
    """
    Evaluates system health signals and recommends backpressure actions.

    Parameters
    ----------
    queue_warn_depth      : Queue depth that triggers WARN (default: 50).
    queue_pause_depth     : Queue depth that triggers PAUSE_GENERATION (default: 200).
    failure_slow_threshold: Consecutive failures that trigger SLOW_DOWN (default: 2).
    failure_pause_threshold: Consecutive failures for PAUSE (default: 5).
    artifact_warn_count   : Artifact count triggering WARN (default: 500).
    artifact_compress_count: Artifact count triggering COMPRESS_MEMORY (default: 2000).
    pause_seconds         : How long to pause when PAUSE_GENERATION fires (default: 5s).
    slow_seconds          : How long to slow down when SLOW_DOWN fires (default: 2s).
    """

    def __init__(
        self,
        queue_warn_depth: int = 50,
        queue_pause_depth: int = 200,
        failure_slow_threshold: int = 2,
        failure_pause_threshold: int = 5,
        artifact_warn_count: int = 500,
        artifact_compress_count: int = 2000,
        pause_seconds: float = 5.0,
        slow_seconds: float = 2.0,
    ) -> None:
        self._queue_warn = queue_warn_depth
        self._queue_pause = queue_pause_depth
        self._failure_slow = failure_slow_threshold
        self._failure_pause = failure_pause_threshold
        self._artifact_warn = artifact_warn_count
        self._artifact_compress = artifact_compress_count
        self._pause_seconds = pause_seconds
        self._slow_seconds = slow_seconds

        # Track stats
        self._total_evaluations: int = 0
        self._pauses_triggered: int = 0
        self._slow_downs_triggered: int = 0
        self._last_pause_at: Optional[float] = None

    def evaluate(
        self,
        queue_depth: int = 0,
        failure_streak: int = 0,
        artifact_count: int = 0,
    ) -> BackpressureRecommendation:
        """
        Evaluate current system signals and return a backpressure recommendation.

        Parameters
        ----------
        queue_depth    : Current number of pending tasks in the queue.
        failure_streak : Consecutive micro loop failures.
        artifact_count : Total artifacts stored this session.

        Returns
        -------
        BackpressureRecommendation with the recommended action and wait duration.
        """
        self._total_evaluations += 1
        signals = {
            "queue_depth": queue_depth,
            "failure_streak": failure_streak,
            "artifact_count": artifact_count,
        }

        # Priority: failures > queue depth > memory pressure
        # (failures are most urgent — system is actively struggling)

        if failure_streak >= self._failure_pause:
            self._pauses_triggered += 1
            self._last_pause_at = time.monotonic()
            return BackpressureRecommendation(
                action=BackpressureAction.PAUSE_GENERATION,
                wait_seconds=self._pause_seconds,
                reason=(
                    f"Consecutive failure streak={failure_streak} ≥ {self._failure_pause} — "
                    "pausing task generation to let system recover"
                ),
                signals=signals,
            )

        if queue_depth >= self._queue_pause:
            self._pauses_triggered += 1
            self._last_pause_at = time.monotonic()
            return BackpressureRecommendation(
                action=BackpressureAction.PAUSE_GENERATION,
                wait_seconds=self._pause_seconds,
                reason=(
                    f"Queue depth={queue_depth} ≥ {self._queue_pause} — "
                    "pausing task generation until queue drains"
                ),
                signals=signals,
            )

        if artifact_count >= self._artifact_compress:
            return BackpressureRecommendation(
                action=BackpressureAction.COMPRESS_MEMORY,
                wait_seconds=self._pause_seconds,
                reason=(
                    f"Artifact count={artifact_count} ≥ {self._artifact_compress} — "
                    "triggering memory compression before generating more tasks"
                ),
                signals=signals,
            )

        if failure_streak >= self._failure_slow:
            self._slow_downs_triggered += 1
            return BackpressureRecommendation(
                action=BackpressureAction.SLOW_DOWN,
                wait_seconds=self._slow_seconds,
                reason=(
                    f"Consecutive failure streak={failure_streak} — "
                    "slowing down task generation"
                ),
                signals=signals,
            )

        if queue_depth >= self._queue_warn:
            self._slow_downs_triggered += 1
            return BackpressureRecommendation(
                action=BackpressureAction.SLOW_DOWN,
                wait_seconds=self._slow_seconds,
                reason=(
                    f"Queue depth={queue_depth} ≥ {self._queue_warn} — "
                    "slowing down task generation"
                ),
                signals=signals,
            )

        if artifact_count >= self._artifact_warn:
            logger.debug(
                "Backpressure WARN: artifact_count=%d ≥ %d",
                artifact_count, self._artifact_warn,
            )
            return BackpressureRecommendation(
                action=BackpressureAction.WARN,
                wait_seconds=0.0,
                reason=f"Artifact count={artifact_count} approaching compression threshold",
                signals=signals,
            )

        return BackpressureRecommendation(
            action=BackpressureAction.NONE,
            wait_seconds=0.0,
            reason="System healthy",
            signals=signals,
        )

    def stats(self) -> dict:
        """Return backpressure statistics for monitoring."""
        return {
            "total_evaluations": self._total_evaluations,
            "pauses_triggered": self._pauses_triggered,
            "slow_downs_triggered": self._slow_downs_triggered,
            "last_pause_ago": (
                round(time.monotonic() - self._last_pause_at, 1)
                if self._last_pause_at else None
            ),
        }
