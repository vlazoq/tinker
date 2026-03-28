"""
runtime/orchestrator/_resilience.py
====================================
Resilience-related methods extracted from the Orchestrator class.

Contains backpressure evaluation, capacity planning updates, and DLQ
auto-replayer wiring.  These are mixed back into the Orchestrator via
``ResilienceMixin``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    pass

try:
    from infra.resilience.backpressure import BackpressureController, BackpressureAction

    _BACKPRESSURE_AVAILABLE = True
except ImportError:
    BackpressureController = None  # type: ignore[assignment,misc]
    BackpressureAction = None  # type: ignore[assignment,misc]
    _BACKPRESSURE_AVAILABLE = False

try:
    from tinker_platform.capacity.planner import CapacityPlanner

    _CAPACITY_AVAILABLE = True
except ImportError:
    CapacityPlanner = None  # type: ignore[assignment,misc]
    _CAPACITY_AVAILABLE = False

try:
    from infra.resilience.dead_letter_queue import DLQAutoReplayer

    _DLQ_REPLAYER_AVAILABLE = True
except ImportError:
    DLQAutoReplayer = None  # type: ignore[assignment,misc]
    _DLQ_REPLAYER_AVAILABLE = False

logger = logging.getLogger("tinker.orchestrator")


class ResilienceMixin:
    """
    Mixin providing backpressure, capacity planning, and DLQ replay methods.

    Mixed into the Orchestrator class.  All methods access orchestrator state
    via ``self`` (config, enterprise, state, task_engine, memory_manager, etc.).
    """

    async def _setup_dlq_replayer(self) -> None:
        """
        Configure and start the DLQ auto-replayer if the module is available.

        The replayer periodically drains the Dead Letter Queue by re-enqueuing
        failed task operations back into the task engine.  This is wired once
        during orchestrator startup (inside ``run()``).

        If the DLQ module is not installed or no DLQ instance is wired, this
        method is a silent no-op.
        """
        if not _DLQ_REPLAYER_AVAILABLE:
            logger.debug(
                "DLQ replayer not available (infra.resilience.dead_letter_queue "
                "not installed) — skipping DLQ wiring"
            )
            return

        dlq = self.enterprise.get("dlq")
        if dlq is None:
            logger.debug(
                "No DLQ instance in enterprise dict — DLQ replay disabled"
            )
            return

        task_engine = self.task_engine

        async def _replay_handler(item: dict) -> None:
            """Re-enqueue a single failed DLQ item back into the task engine."""
            operation = item.get("operation", "")
            payload = item.get("payload", {})

            if operation == "complete_task":
                task_id = payload.get("task_id")
                artifact_id = payload.get("artifact_id")
                if task_id:
                    logger.info(
                        "DLQ replay: re-completing task %s (artifact=%s)",
                        task_id,
                        artifact_id,
                    )
                    await task_engine.complete_task(
                        task_id=task_id,
                        artifact_id=artifact_id,
                    )
                else:
                    raise ValueError(
                        f"DLQ item missing task_id in payload: {payload}"
                    )

            elif operation == "store_artifact":
                logger.info(
                    "DLQ replay: re-storing artifact for task %s",
                    payload.get("task_id", "unknown"),
                )
                if hasattr(self.memory_manager, "store_artifact"):
                    await self.memory_manager.store_artifact(
                        **payload,
                    )
                else:
                    raise ValueError(
                        "Memory manager does not support store_artifact"
                    )

            else:
                raise ValueError(
                    f"DLQ replay: unknown operation '{operation}' — "
                    f"cannot replay automatically"
                )

        try:
            self._dlq_replayer = DLQAutoReplayer(
                dlq=dlq,
                handler=_replay_handler,
                interval=60.0,
                batch_size=10,
                max_retries=5,
            )
            await self._dlq_replayer.start()
            logger.info(
                "DLQ auto-replayer started — replaying failed operations "
                "every 60s"
            )
        except Exception as exc:
            logger.warning(
                "Failed to start DLQ auto-replayer (non-fatal): %s", exc
            )
            self._dlq_replayer = None

    async def _apply_backpressure(self) -> None:
        """
        Evaluate system load and apply any recommended backpressure actions.

        The BackpressureController examines queue depth, failure streak, and
        artifact count and may recommend slowing down, pausing task generation,
        or compressing memory.

        This is a no-op when no backpressure controller is wired.
        """
        if not _BACKPRESSURE_AVAILABLE:
            return

        bp_controller = self.enterprise.get("backpressure")
        if bp_controller is None:
            return

        try:
            queue_depth = getattr(self.task_engine, "queue_depth", 0) or 0
            failure_streak = self.state.consecutive_failures
            artifact_count = sum(
                1 for r in self.state.micro_history if r.artifact_id is not None
            )

            recommendation = bp_controller.evaluate(
                queue_depth=queue_depth,
                failure_streak=failure_streak,
                artifact_count=artifact_count,
            )

            action = recommendation.action

            if action == BackpressureAction.NONE:
                return

            if action == BackpressureAction.WARN:
                logger.warning(
                    "Backpressure WARN: %s (queue=%d, failures=%d)",
                    recommendation.reason,
                    queue_depth,
                    failure_streak,
                )
                return

            if action == BackpressureAction.SLOW_DOWN:
                logger.warning(
                    "Backpressure SLOW_DOWN: sleeping %.1fs — %s",
                    recommendation.wait_seconds,
                    recommendation.reason,
                )
                await self._interruptible_sleep(recommendation.wait_seconds)

            elif action == BackpressureAction.PAUSE_GENERATION:
                logger.warning(
                    "Backpressure PAUSE_GENERATION: pausing task generation "
                    "for %.1fs — %s",
                    recommendation.wait_seconds,
                    recommendation.reason,
                )
                _has_pause_flag = hasattr(self.task_engine, "pause_generation")
                if _has_pause_flag:
                    self.task_engine.pause_generation = True
                try:
                    await self._interruptible_sleep(recommendation.wait_seconds)
                finally:
                    if _has_pause_flag:
                        self.task_engine.pause_generation = False

            elif action == BackpressureAction.COMPRESS_MEMORY:
                logger.warning(
                    "Backpressure COMPRESS_MEMORY: requesting memory compression — %s",
                    recommendation.reason,
                )
                if hasattr(self.memory_manager, "compress"):
                    try:
                        await self.memory_manager.compress()
                    except Exception as exc:
                        logger.warning("Memory compression failed: %s", exc)

        except Exception as exc:
            logger.warning("Backpressure evaluation failed (non-fatal): %s", exc)

    async def _update_capacity_planner(self, record: Any) -> None:
        """
        Record resource usage for the just-completed micro loop.

        Feeds token counts and artifact count into the CapacityPlanner.
        This is a no-op when no capacity planner is wired in.
        """
        if not _CAPACITY_AVAILABLE:
            return

        planner = self.enterprise.get("capacity_planner")
        if planner is None:
            return

        try:
            planner.record_tokens(
                micro_tokens=(
                    (record.architect_tokens or 0) + (record.critic_tokens or 0)
                )
            )
            total_artifacts = self.state.total_micro_loops
            planner.record_artifact_count(total=total_artifacts)

            alerts = planner.check_thresholds()
            for alert in alerts:
                logger.warning("CapacityPlanner: %s", alert)
        except Exception as exc:
            logger.debug("Capacity planner update failed (non-fatal): %s", exc)
