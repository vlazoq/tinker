"""
tinker/anti_stagnation/models.py
─────────────────────────────────
Shared data models: stagnation event types, intervention directives,
and the MicroLoopContext payload the Orchestrator passes each cycle.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Set


# ─────────────────────────────────────────────────────────────
# Stagnation failure modes
# ─────────────────────────────────────────────────────────────

class StagnationType(str, Enum):
    SEMANTIC_LOOP         = "semantic_loop"
    SUBSYSTEM_FIXATION    = "subsystem_fixation"
    CRITIQUE_COLLAPSE     = "critique_collapse"
    RESEARCH_SATURATION   = "research_saturation"
    TASK_STARVATION       = "task_starvation"


# ─────────────────────────────────────────────────────────────
# Intervention directives
# ─────────────────────────────────────────────────────────────

class InterventionType(str, Enum):
    FORCE_BRANCH          = "force_branch"
    INJECT_CONTRADICTION  = "inject_contradiction"
    ALTERNATIVE_FORCING   = "alternative_forcing"
    SPAWN_EXPLORATION     = "spawn_exploration_task"
    ESCALATE_LOOP         = "escalate_loop"
    NO_ACTION             = "no_action"


# Canonical mapping: each failure mode → its primary intervention
INTERVENTION_MAP: Dict[StagnationType, InterventionType] = {
    StagnationType.SEMANTIC_LOOP:       InterventionType.ALTERNATIVE_FORCING,
    StagnationType.SUBSYSTEM_FIXATION:  InterventionType.FORCE_BRANCH,
    StagnationType.CRITIQUE_COLLAPSE:   InterventionType.INJECT_CONTRADICTION,
    StagnationType.RESEARCH_SATURATION: InterventionType.SPAWN_EXPLORATION,
    StagnationType.TASK_STARVATION:     InterventionType.ESCALATE_LOOP,
}


@dataclass
class InterventionDirective:
    """
    Returned to the Orchestrator when stagnation is detected.
    The Orchestrator reads `intervention_type` and acts accordingly.
    `metadata` carries detector-specific hints (e.g. which subsystem to avoid).
    """
    intervention_type: InterventionType
    stagnation_type: StagnationType
    severity: float                          # 0.0 – 1.0 normalised score
    metadata: Dict[str, Any] = field(default_factory=dict)
    directive_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def is_actionable(self) -> bool:
        return self.intervention_type != InterventionType.NO_ACTION

    def to_dict(self) -> dict:
        return {
            "directive_id": self.directive_id,
            "intervention_type": self.intervention_type.value,
            "stagnation_type": self.stagnation_type.value,
            "severity": round(self.severity, 4),
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
        }


# ─────────────────────────────────────────────────────────────
# Stagnation event (written to the event log)
# ─────────────────────────────────────────────────────────────

@dataclass
class StagnationEvent:
    """Immutable record stored in the StagnationEventLog."""

    stagnation_type: StagnationType
    detected_at: datetime
    loop_index: int
    directive: InterventionDirective
    detector_evidence: Dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "stagnation_type": self.stagnation_type.value,
            "detected_at": self.detected_at.isoformat(),
            "loop_index": self.loop_index,
            "directive": self.directive.to_dict(),
            "detector_evidence": self.detector_evidence,
        }


# ─────────────────────────────────────────────────────────────
# Payload the Orchestrator passes on each micro-loop tick
# ─────────────────────────────────────────────────────────────

@dataclass
class MicroLoopContext:
    """
    Everything the StagnationMonitor needs to know about the just-completed
    micro loop. Fields are Optional so callers can omit irrelevant ones.
    """

    loop_index: int

    # Architect / general output text for semantic similarity checks
    output_text: Optional[str] = None

    # Tag identifying which subsystem this loop focused on
    subsystem_tag: Optional[str] = None

    # Critic confidence score for this loop (0.0 – 1.0)
    critic_score: Optional[float] = None

    # Set of source URLs the Researcher found this loop
    research_urls: Optional[Set[str]] = None

    # Current task queue depth
    queue_depth: Optional[int] = None

    # How many NEW tasks were generated this loop
    tasks_generated: Optional[int] = None

    # How many tasks were consumed (completed) this loop
    tasks_consumed: Optional[int] = None

    # Free-form extras for future extension
    extras: Dict[str, Any] = field(default_factory=dict)
