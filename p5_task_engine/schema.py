"""
tinker/task_engine/schema.py
─────────────────────────────
Core Task dataclass and all enumerations.
Every field is type-annotated; dataclasses.asdict() serialises cleanly to JSON.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# ─────────────────────────────────────────────
# Enumerations
# ─────────────────────────────────────────────

class TaskType(str, Enum):
    """The *nature* of work a task represents."""
    DESIGN       = "design"        # Produce or refine an architecture artefact
    RESEARCH     = "research"      # Investigate a technology / pattern / constraint
    CRITIQUE     = "critique"      # Evaluate an existing artefact for weaknesses
    SYNTHESIS    = "synthesis"     # Merge / reconcile multiple artefacts
    EXPLORATION  = "exploration"   # Wild-card: follow an unexpected signal
    VALIDATION   = "validation"    # Verify a claim or assumption with evidence


class TaskStatus(str, Enum):
    """Lifecycle state machine."""
    PENDING   = "pending"    # Created, waiting to be scheduled
    ACTIVE    = "active"     # Currently being worked by an Architect agent
    CRITIQUE  = "critique"   # Completed, waiting for critique pass
    COMPLETE  = "complete"   # Fully resolved and accepted
    ARCHIVED  = "archived"   # Moved to cold storage (never deleted from DB)
    BLOCKED   = "blocked"    # Has unresolved dependency tasks
    FAILED    = "failed"     # Architect could not resolve it
    SKIPPED   = "skipped"    # Deliberately bypassed (staleness / low signal)


class Subsystem(str, Enum):
    """Which Tinker subsystem does this task primarily concern?"""
    MODEL_CLIENT         = "model_client"
    MEMORY_MANAGER       = "memory_manager"
    TOOL_LAYER           = "tool_layer"
    AGENT_PROMPTS        = "agent_prompts"
    TASK_ENGINE          = "task_engine"
    CONTEXT_ASSEMBLER    = "context_assembler"
    ORCHESTRATOR         = "orchestrator"
    ARCH_STATE_MANAGER   = "arch_state_manager"
    ANTI_STAGNATION      = "anti_stagnation"
    OBSERVABILITY        = "observability"
    CROSS_CUTTING        = "cross_cutting"   # Spans multiple subsystems


# ─────────────────────────────────────────────
# Task dataclass
# ─────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _new_id() -> str:
    return str(uuid.uuid4())


@dataclass
class Task:
    """
    The canonical unit of work in Tinker.

    Scoring factors stored directly on the task so the PriorityScorer
    can be stateless / functional.
    """

    # ── Identity ──────────────────────────────
    id:            str       = field(default_factory=_new_id)
    parent_id:     str | None = None          # Task that spawned this one
    title:         str       = ""
    description:   str       = ""

    # ── Classification ────────────────────────
    type:          TaskType   = TaskType.DESIGN
    subsystem:     Subsystem  = Subsystem.CROSS_CUTTING

    # ── Status ────────────────────────────────
    status:        TaskStatus = TaskStatus.PENDING

    # ── Dependencies ──────────────────────────
    # List of task IDs that must reach COMPLETE before this can be ACTIVE
    dependencies:  list[str]  = field(default_factory=list)

    # ── Outputs ───────────────────────────────
    # Artefact keys produced (stored in Memory Manager / Architecture State)
    outputs:       list[str]  = field(default_factory=list)

    # ── Scoring inputs (raw signals, 0-1 floats unless noted) ─────────────
    confidence_gap:   float = 0.5   # How uncertain is the current understanding
    staleness_hours:  float = 0.0   # Wall-clock hours spent in PENDING
    dependency_depth: int   = 0     # How many ancestors does this task have
    last_subsystem_work_hours: float = 0.0  # Recency of related work on same subsystem

    # ── Computed ──────────────────────────────
    priority_score:  float = 0.0    # Set by PriorityScorer; higher = sooner

    # ── Metadata ──────────────────────────────
    tags:          list[str]       = field(default_factory=list)
    metadata:      dict[str, Any]  = field(default_factory=dict)
    is_exploration: bool           = False   # Reserved exploration slot marker

    # ── Timestamps ────────────────────────────
    created_at:    str = field(default_factory=_now)
    updated_at:    str = field(default_factory=_now)
    started_at:    str | None = None
    completed_at:  str | None = None

    # ── Critique ──────────────────────────────
    critique_notes: str | None = None
    attempt_count:  int = 0

    def touch(self) -> None:
        """Update the updated_at timestamp."""
        self.updated_at = _now()

    def mark_started(self) -> None:
        self.status = TaskStatus.ACTIVE
        self.started_at = _now()
        self.attempt_count += 1
        self.touch()

    def mark_complete(self, outputs: list[str] | None = None) -> None:
        self.status = TaskStatus.COMPLETE
        self.completed_at = _now()
        if outputs:
            self.outputs.extend(outputs)
        self.touch()

    def mark_failed(self, reason: str = "") -> None:
        self.status = TaskStatus.FAILED
        self.metadata["failure_reason"] = reason
        self.touch()

    def to_dict(self) -> dict[str, Any]:
        import dataclasses
        d = dataclasses.asdict(self)
        # Enums → string
        d["type"]      = self.type.value
        d["status"]    = self.status.value
        d["subsystem"] = self.subsystem.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Task":
        d = dict(d)
        d["type"]      = TaskType(d["type"])
        d["status"]    = TaskStatus(d["status"])
        d["subsystem"] = Subsystem(d["subsystem"])
        # JSON stores lists as JSON strings in SQLite rows
        import json
        for list_field in ("dependencies", "outputs", "tags"):
            if isinstance(d.get(list_field), str):
                d[list_field] = json.loads(d[list_field])
        if isinstance(d.get("metadata"), str):
            d["metadata"] = json.loads(d["metadata"])
        return cls(**d)
