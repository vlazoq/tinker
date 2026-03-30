"""
runtime/orchestrator/_stagnation.py
====================================
Anti-stagnation detection and intervention methods extracted from the
Orchestrator class.

Contains stagnation checking (runs the StagnationMonitor) and directive
application (acts on detected stagnation patterns).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from core.events import EventType

logger = logging.getLogger("tinker.orchestrator")


class StagnationMixin:
    """
    Mixin providing anti-stagnation detection and intervention methods.

    Mixed into the Orchestrator class.  Methods access orchestrator state
    via ``self`` (stagnation_monitor, state, config, task_engine, metrics).
    """

    async def _check_stagnation(self, record: Any) -> list:
        """
        Run the StagnationMonitor against the just-completed micro loop record.

        Builds a ``MicroLoopContext`` from the record and runs all five
        detectors.  Returns a list of ``InterventionDirective`` objects sorted
        by severity (highest first).  Returns an empty list if nothing is
        detected or if the monitor raises.
        """
        from runtime.stagnation.models import MicroLoopContext

        ctx = MicroLoopContext(
            loop_index=record.iteration,
            output_text=None,
            subsystem_tag=record.subsystem,
            critic_score=record.critic_score,
            queue_depth=getattr(self.task_engine, "queue_depth", None),
            tasks_generated=record.new_tasks_generated,
            tasks_consumed=1,
        )
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self.stagnation_monitor.check, ctx)
        except Exception as exc:
            logger.warning("StagnationMonitor.check() raised unexpectedly: %s", exc)
            return []

    async def _apply_stagnation_directive(self, directive: Any) -> None:
        """
        Act on the highest-severity stagnation directive.

        Each ``InterventionType`` maps to a concrete action:

        FORCE_BRANCH
            Bump the stagnation-flagged subsystem's meso counter to trigger
            an early meso synthesis.

        ALTERNATIVE_FORCING / INJECT_CONTRADICTION
            Queue a one-shot prompt hint for the next Architect call.

        SPAWN_EXPLORATION / ESCALATE_LOOP
            Enqueue a fresh exploration task to break stagnation.

        NO_ACTION
            Informational only; nothing to do.
        """
        from runtime.stagnation.models import InterventionType

        if not directive.is_actionable():
            return

        self.state.stagnation_events_total += 1
        if self.metrics is not None:
            self.metrics.on_stagnation(directive)

        await self.emit_event(EventType.STAGNATION_DETECTED, {
            "intervention_type": directive.intervention_type.value,
            "stagnation_type": directive.stagnation_type.value,
            "severity": directive.severity,
            "subsystem": self.state.current_subsystem,
        })

        logger.warning(
            "[Stagnation] %s directive triggered (type=%s, severity=%.2f)",
            directive.intervention_type.value,
            directive.stagnation_type.value,
            directive.severity,
        )

        itype = directive.intervention_type

        if itype == InterventionType.FORCE_BRANCH:
            avoid = (
                directive.metadata.get("avoid_subsystem")
                or self.state.current_subsystem
            )
            if avoid:
                target = self.config.meso_trigger_count
                self.state.subsystem_micro_counts[avoid] = target
                logger.info(
                    "[Stagnation] Forced early meso on subsystem=%s to pivot away",
                    avoid,
                )

        elif itype in (
            InterventionType.ALTERNATIVE_FORCING,
            InterventionType.INJECT_CONTRADICTION,
        ):
            if itype == InterventionType.ALTERNATIVE_FORCING:
                hint = (
                    "[STAGNATION INTERVENTION — ALTERNATIVE FORCING] "
                    "You have been cycling through similar solutions. "
                    "Deliberately propose an approach you have NOT tried before. "
                    "Reject any solution that resembles previous proposals and "
                    "instead explore a fundamentally different design direction."
                )
            else:
                hint = (
                    "[STAGNATION INTERVENTION — INJECT CONTRADICTION] "
                    "Actively challenge your current assumptions. "
                    "Identify the core hypothesis behind your recent proposals "
                    "and argue the opposite: what if that hypothesis is wrong? "
                    "Build your next proposal around the counter-hypothesis."
                )
            self.state.pending_stagnation_hint = hint
            logger.info(
                "[Stagnation] %s — prompt hint queued for next micro loop",
                itype.value,
            )

        elif itype in (
            InterventionType.SPAWN_EXPLORATION,
            InterventionType.ESCALATE_LOOP,
        ):
            if hasattr(self.task_engine, "enqueue_exploration_task"):
                await self.task_engine.enqueue_exploration_task(
                    title="Stagnation-break: explore a new design direction",
                    description=(
                        f"The system detected {directive.stagnation_type.value} "
                        f"(severity={directive.severity:.2f}).  Investigate a part of "
                        "the architecture that has not been explored recently and "
                        "propose a new line of inquiry."
                    ),
                )
                logger.info(
                    "[Stagnation] Exploration task enqueued to break %s",
                    directive.stagnation_type.value,
                )
