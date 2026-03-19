"""
tinker/anti_stagnation/monitor.py
───────────────────────────────────
StagnationMonitor — the single entry-point for the Orchestrator.

Workflow (per micro loop):
  1. Orchestrator calls monitor.check(ctx: MicroLoopContext)
  2. Monitor runs all five detectors (or stops at first hit, depending on cfg)
  3. For each detection, builds an InterventionDirective and logs a StagnationEvent
  4. Returns a (possibly empty) list of InterventionDirective objects

The Orchestrator acts on the *first* directive (highest severity), or on all
of them if it implements a priority queue — that choice belongs to the
Orchestrator.

Thread safety: each detector is stateful; StagnationMonitor must be called
from a single thread or the caller must serialise access.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from .config import StagnationMonitorConfig
from .detectors import (
    CritiqueCollapseDetector,
    DetectionResult,
    ResearchSaturationDetector,
    SemanticLoopDetector,
    SubsystemFixationDetector,
    TaskStarvationDetector,
)
from .embeddings import EmbeddingBackend, make_embedding_backend
from .event_log import StagnationEventLog
from .models import (
    INTERVENTION_MAP,
    InterventionDirective,
    InterventionType,
    MicroLoopContext,
    StagnationEvent,
    StagnationType,
)

logger = logging.getLogger(__name__)


class StagnationMonitor:
    """
    Watchdog for the Tinker autonomous architecture engine.

    Parameters
    ----------
    config:
        Full configuration; all thresholds live here.
    embedding_backend:
        Override the embedding backend (useful in tests).
        If None, the backend is auto-detected via make_embedding_backend().
    """

    def __init__(
        self,
        config: Optional[StagnationMonitorConfig] = None,
        embedding_backend: Optional[EmbeddingBackend] = None,
    ):
        self.config = config or StagnationMonitorConfig()
        self.event_log = StagnationEventLog(max_size=self.config.event_log_max_size)

        # Resolve the embedding backend
        self._embed_backend = embedding_backend or make_embedding_backend(
            model=self.config.semantic_loop.embedding_model
        )

        # Instantiate detectors
        self._detectors = {
            StagnationType.SEMANTIC_LOOP: SemanticLoopDetector(
                cfg=self.config.semantic_loop,
                backend=self._embed_backend,
            ),
            StagnationType.SUBSYSTEM_FIXATION: SubsystemFixationDetector(
                cfg=self.config.subsystem_fixation,
            ),
            StagnationType.CRITIQUE_COLLAPSE: CritiqueCollapseDetector(
                cfg=self.config.critique_collapse,
            ),
            StagnationType.RESEARCH_SATURATION: ResearchSaturationDetector(
                cfg=self.config.research_saturation,
            ),
            StagnationType.TASK_STARVATION: TaskStarvationDetector(
                cfg=self.config.task_starvation,
            ),
        }

        logger.info(
            "StagnationMonitor initialised (run_all=%s, log_max=%d)",
            self.config.run_all_detectors,
            self.config.event_log_max_size,
        )

    # ─────────────────────────────────────────────────────────
    # Primary interface
    # ─────────────────────────────────────────────────────────

    def check(self, ctx: MicroLoopContext) -> List[InterventionDirective]:
        """
        Run all (or one) detectors against the current micro-loop context.

        Returns:
            A list of InterventionDirective objects, sorted by severity
            descending.  The list is empty when no stagnation is detected.
        """
        directives: List[InterventionDirective] = []

        for stagnation_type, detector in self._detectors.items():
            try:
                result: Optional[DetectionResult] = detector.check(ctx)
            except Exception as exc:  # never let a detector crash the loop
                logger.warning(
                    "Detector %s raised an exception: %s",
                    stagnation_type.value,
                    exc,
                    exc_info=True,
                )
                continue

            if result is None:
                continue

            directive = self._build_directive(result)
            event = self._build_event(ctx.loop_index, result, directive)
            self.event_log.append(event)
            directives.append(directive)

            logger.warning(
                "[StagnationMonitor] %s detected at loop %d — "
                "intervention: %s (severity=%.3f)",
                stagnation_type.value,
                ctx.loop_index,
                directive.intervention_type.value,
                directive.severity,
            )

            if not self.config.run_all_detectors:
                break  # stop at first hit

        # Sort highest severity first so the Orchestrator can act greedily
        directives.sort(key=lambda d: d.severity, reverse=True)
        return directives

    # ─────────────────────────────────────────────────────────
    # Convenience queries (for the Observability Dashboard)
    # ─────────────────────────────────────────────────────────

    def summary(self) -> dict:
        """Return a snapshot suitable for dashboard polling."""
        return {
            "total_events": self.event_log.total(),
            "counts_by_type": self.event_log.counts_by_type(),
            "recent_events": self.event_log.to_dicts(n=10),
        }

    def reset_all(self) -> None:
        """Reset all detector windows and clear the event log. Use with care."""
        for detector in self._detectors.values():
            detector.reset()
        self.event_log.clear()
        logger.info("StagnationMonitor: all detectors and event log reset.")

    def reset_detector(self, stagnation_type: StagnationType) -> None:
        detector = self._detectors.get(stagnation_type)
        if detector:
            detector.reset()

    # ─────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def _build_directive(result: DetectionResult) -> InterventionDirective:
        intervention_type = INTERVENTION_MAP.get(
            result.stagnation_type, InterventionType.NO_ACTION
        )
        # Carry detector evidence into directive metadata so the Orchestrator
        # has context without needing to parse the event log.
        metadata: dict = dict(result.evidence)

        # Enrich certain directives with actionable hints
        if (
            result.stagnation_type == StagnationType.SUBSYSTEM_FIXATION
            and "avoid_subsystem_hint" in result.evidence
        ):
            metadata["avoid_subsystem"] = result.evidence["avoid_subsystem_hint"]

        if result.stagnation_type == StagnationType.RESEARCH_SATURATION:
            metadata["exclude_urls"] = result.evidence.get("repeated_urls", [])

        return InterventionDirective(
            intervention_type=intervention_type,
            stagnation_type=result.stagnation_type,
            severity=result.severity,
            metadata=metadata,
        )

    @staticmethod
    def _build_event(
        loop_index: int,
        result: DetectionResult,
        directive: InterventionDirective,
    ) -> StagnationEvent:
        return StagnationEvent(
            stagnation_type=result.stagnation_type,
            detected_at=datetime.now(timezone.utc),
            loop_index=loop_index,
            directive=directive,
            detector_evidence=result.evidence,
        )
