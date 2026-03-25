"""
runtime/tasks/schema.py — Core data shapes for the Task Engine
=======================================================

What this file does
--------------------
This is the data-definition layer.  It answers the question:
"What does a Task *look* like?"

It contains:
  - Three enumerations (controlled vocabularies) that classify tasks.
  - The ``Task`` dataclass itself — the single source of truth for every
    field a task can have.
  - A handful of short methods on ``Task`` that handle state transitions
    and serialisation.

Why a separate schema file?
----------------------------
Keeping data definitions separate from business logic makes the codebase
easier to navigate.  When you want to know "what fields does a task have?",
you come here.  When you want to know "how does scoring work?", you go to
scorer.py.  Nothing in this file imports from any other task-engine module,
so there are no circular-import problems.

How Python dataclasses work (quick primer for beginners)
---------------------------------------------------------
A ``@dataclass`` is a shorthand way of writing a class that mostly holds
data.  Python auto-generates ``__init__``, ``__repr__``, and ``__eq__``
for you based on the field annotations.  ``field(default_factory=list)``
means "give every new instance its *own* fresh list" (instead of sharing
one list across all instances, which is a classic Python footgun).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# =============================================================================
# Enumerations
# =============================================================================
# Python Enum gives us named constants with a string representation.
# Using ``str, Enum`` as base classes means the enum value IS a string, which
# makes JSON serialisation trivial — no extra conversion needed.


class TaskType(str, Enum):
    """
    The *nature* of the work a task represents.

    Think of this as the "verb" of the task — what kind of thinking
    does working on this task require?

    Values
    ------
    DESIGN      : Produce or refine an architecture artefact.
                  Example: "Design the API gateway layer."
    RESEARCH    : Investigate a technology, pattern, or constraint.
                  Example: "Compare HNSW vs IVF for vector search."
    CRITIQUE    : Evaluate an existing artefact for weaknesses.
                  Example: "Review the event-sourcing proposal for failure modes."
    SYNTHESIS   : Merge / reconcile multiple artefacts into one coherent view.
                  Example: "Integrate the auth design with the gateway proposal."
    EXPLORATION : Wild-card — follow an unexpected or open-ended signal.
                  These tasks are deliberately vague; they exist to prevent
                  the AI from getting stuck in a rut.
    VALIDATION  : Verify a claim or assumption with evidence.
                  Example: "Confirm that Kafka supports exactly-once delivery."
    """

    DESIGN = "design"
    RESEARCH = "research"
    CRITIQUE = "critique"
    SYNTHESIS = "synthesis"
    EXPLORATION = "exploration"
    VALIDATION = "validation"
    # ── Grub integration task types ───────────────────────────────────────────
    IMPLEMENTATION = "implementation"  # Grub: implement this design artifact
    REVIEW = "review"  # Grub: review what was implemented


class TaskStatus(str, Enum):
    """
    The lifecycle state of a task — where it currently sits in the pipeline.

    A task moves through these states roughly like this:

        PENDING → ACTIVE → CRITIQUE → COMPLETE
                         ↘ FAILED
        PENDING ← CRITIQUE  (if rejected)
        PENDING ← BLOCKED   (once dependencies are done)

    Values
    ------
    PENDING  : Created and waiting to be scheduled.  Most tasks start here.
    ACTIVE   : Currently being worked on by an Architect agent.  Only one
               agent works a task at a time.
    CRITIQUE : Work is done, but the result is waiting for a review pass
               (either by another AI agent or a human).
    COMPLETE : Fully resolved and accepted.  Terminal state for success.
    ARCHIVED : Moved to cold storage.  The row stays in the DB forever so
               history is never lost, but archived tasks are never scheduled.
    BLOCKED  : Waiting for one or more prerequisite tasks to finish.
               Automatically transitions back to PENDING once deps are done.
    FAILED   : The Architect could not produce a result after trying.
               Terminal state for failure.
    SKIPPED  : Deliberately bypassed — for example, because it became stale
               or the underlying question was resolved another way.
    """

    PENDING = "pending"
    ACTIVE = "active"
    CRITIQUE = "critique"
    COMPLETE = "complete"
    ARCHIVED = "archived"
    BLOCKED = "blocked"
    FAILED = "failed"
    SKIPPED = "skipped"


class Subsystem(str, Enum):
    """
    Which part of Tinker does this task primarily concern?

    Tinker is itself a complex system made of many sub-components.  When
    Tinker thinks about its own architecture (which it does constantly),
    tasks are tagged with the subsystem they belong to.

    This tag is used by the scorer to avoid spending too much consecutive
    time on a single subsystem (the "recency" scoring component).

    Values
    ------
    MODEL_CLIENT         : The wrapper around the LLM API.
    MEMORY_MANAGER       : Long-term and short-term memory storage.
    TOOL_LAYER           : Tools the AI can call (web search, code exec, …).
    AGENT_PROMPTS        : The system prompts that define each agent's role.
    TASK_ENGINE          : This very subsystem — task queue, scoring, etc.
    CONTEXT_ASSEMBLER    : The part that builds token-budgeted prompts.
    ORCHESTRATOR         : The loop that drives agent calls.
    ARCH_STATE_MANAGER   : Tracks the current state of the architecture design.
    ANTI_STAGNATION      : Mechanisms to prevent the AI from getting stuck.
    OBSERVABILITY        : Logging, metrics, tracing.
    CROSS_CUTTING        : Tasks that span multiple subsystems and don't fit
                           neatly into any one category.
    """

    MODEL_CLIENT = "model_client"
    MEMORY_MANAGER = "memory_manager"
    TOOL_LAYER = "tool_layer"
    AGENT_PROMPTS = "agent_prompts"
    TASK_ENGINE = "task_engine"
    CONTEXT_ASSEMBLER = "context_assembler"
    ORCHESTRATOR = "orchestrator"
    ARCH_STATE_MANAGER = "arch_state_manager"
    ANTI_STAGNATION = "anti_stagnation"
    OBSERVABILITY = "observability"
    CROSS_CUTTING = "cross_cutting"  # Spans multiple subsystems


# =============================================================================
# Utility functions
# =============================================================================


def _now() -> str:
    """Return the current UTC time as an ISO-8601 string.

    We store timestamps as strings rather than Python datetime objects because
    strings round-trip cleanly through SQLite and JSON without extra conversion
    logic.  ISO-8601 strings also sort lexicographically, which means database
    ORDER BY on timestamp columns works correctly without parsing.
    """
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    """Generate a random UUID string to use as a task's unique identifier.

    UUIDs are 128-bit random numbers represented as hex strings like:
      '3f4a2c8e-1234-4abc-8def-000000000001'
    The probability of two UUIDs colliding is astronomically small, so we
    don't need a central counter or database sequence to ensure uniqueness.
    """
    return str(uuid.uuid4())


# =============================================================================
# Task dataclass
# =============================================================================


@dataclass
class Task:
    """
    The canonical unit of work in Tinker.

    Every piece of thinking Tinker needs to do is represented as a Task.
    Tasks are created (by the TaskGenerator), stored (in the TaskRegistry),
    scored (by the PriorityScorer), queued (by the TaskQueue), and finally
    worked on by an Architect agent.

    Design note: scoring inputs are stored directly on the task object.
    This keeps the PriorityScorer stateless — it can compute a score from
    just the task data alone, without needing to query the database.  That
    makes scoring easy to test in isolation.

    Field groups
    ------------
    Identity       : id, parent_id, title, description
    Classification : type, subsystem
    Status         : status
    Dependencies   : dependencies (list of task IDs that must finish first)
    Outputs        : outputs (artefact keys produced by completing this task)
    Scoring inputs : confidence_gap, staleness_hours, dependency_depth,
                     last_subsystem_work_hours
    Computed       : priority_score (written by PriorityScorer)
    Metadata       : tags, metadata dict, is_exploration flag
    Timestamps     : created_at, updated_at, started_at, completed_at
    Critique       : critique_notes, attempt_count
    """

    # ── Identity ──────────────────────────────────────────────────────────────
    # Every task gets a unique random ID at creation time.
    id: str = field(default_factory=_new_id)

    # If this task was created by the generator after an Architect finished
    # a parent task, parent_id links back to that parent.  Root tasks have
    # parent_id = None.
    parent_id: str | None = None

    title: str = ""  # Short human-readable label
    description: str = ""  # Full description of what needs to be done

    # ── Classification ────────────────────────────────────────────────────────
    # What kind of work is it, and which part of Tinker does it concern?
    type: TaskType = TaskType.DESIGN
    subsystem: Subsystem = Subsystem.CROSS_CUTTING

    # ── Status ────────────────────────────────────────────────────────────────
    status: TaskStatus = TaskStatus.PENDING

    # ── Dependencies ──────────────────────────────────────────────────────────
    # A list of other task IDs that must reach COMPLETE before this task can
    # be scheduled.  An empty list means "no prerequisites; schedule freely."
    # The DependencyResolver watches this list and flips the task from
    # BLOCKED → PENDING once all listed tasks are done.
    dependencies: list[str] = field(default_factory=list)

    # ── Outputs ───────────────────────────────────────────────────────────────
    # When an Architect completes this task, it may produce "artefacts" —
    # design documents, research summaries, etc.  Those are stored in the
    # Memory Manager / Architecture State, and their keys are listed here so
    # downstream tasks know where to find them.
    outputs: list[str] = field(default_factory=list)

    # ── Scoring inputs (raw signals, each normalised to [0, 1]) ───────────────
    # These fields feed into PriorityScorer.score().  They are stored on the
    # task so the scorer can be stateless (it doesn't need to query the DB).

    # How uncertain is the current understanding of this topic?
    # 0.0 = very confident, 1.0 = totally unknown.
    # Higher gap → higher priority (we should explore uncertain areas).
    confidence_gap: float = 0.5

    # How many hours has this task been sitting in PENDING without being picked?
    # Grows over time; used to prevent tasks from being starved indefinitely.
    staleness_hours: float = 0.0

    # How deep in the dependency chain is this task?
    # depth 0 = no dependencies, depth 3 = depends on something that depends
    # on something that depends on something.
    # Shallower tasks tend to be worked first (fewer ancestors = more ready).
    dependency_depth: int = 0

    # How recently (in hours) did we do any work on this task's subsystem?
    # 0.0 = just now, large number = not worked on in a long time.
    # The recency scorer inverts this: old subsystem → higher score.
    last_subsystem_work_hours: float = 0.0

    # ── Computed ──────────────────────────────────────────────────────────────
    # priority_score is calculated by PriorityScorer and written back here.
    # It is a float in [0, 1]; higher = pick sooner.
    # We store it in the DB so the queue can use a fast ORDER BY on it.
    priority_score: float = 0.0

    # ── Metadata ──────────────────────────────────────────────────────────────
    # Free-form labels (e.g. ["memory", "performance"]) for filtering/display.
    tags: list[str] = field(default_factory=list)

    # Arbitrary key-value pairs for extension without schema changes.
    # The resolver also uses this dict to record which deps are currently
    # blocking the task (key "blocking_deps").
    metadata: dict[str, Any] = field(default_factory=dict)

    # True for tasks generated by the anti-stagnation / exploration logic.
    # The queue reserves a small random slot specifically for these tasks
    # so they aren't crowded out by higher-scoring regular tasks.
    is_exploration: bool = False

    # ── Timestamps ────────────────────────────────────────────────────────────
    # All stored as ISO-8601 strings (see _now() above).
    created_at: str = field(default_factory=_now)  # When task was created
    updated_at: str = field(default_factory=_now)  # Last modification time
    started_at: str | None = None  # Set when ACTIVE begins
    completed_at: str | None = None  # Set when COMPLETE

    # ── Critique ──────────────────────────────────────────────────────────────
    # Notes added by a reviewer when a task is in the CRITIQUE state.
    critique_notes: str | None = None

    # How many times has an agent tried to work on this task?
    # Incremented each time mark_started() is called.  If it keeps failing,
    # external logic can use this to give up after N attempts.
    attempt_count: int = 0

    # ── Completion metrics ─────────────────────────────────────────────────────
    # Recorded by mark_complete() to track resource usage.
    tokens_used: int = 0        # LLM tokens consumed while completing this task
    duration_seconds: float = 0.0  # Wall-clock time taken to complete this task

    # =========================================================================
    # State-transition helpers
    # =========================================================================
    # These methods encapsulate the "side effects" of each state change so
    # callers don't have to remember to update timestamps manually.

    def touch(self) -> None:
        """Refresh the updated_at timestamp to right now.

        Called internally by all state-transition methods.  Also useful
        when changing a field that doesn't have its own transition method
        (e.g. updating metadata).
        """
        self.updated_at = _now()

    def mark_started(self) -> None:
        """Transition this task from PENDING → ACTIVE.

        Records when the task started and increments the attempt counter.
        The attempt counter is important for retry/give-up logic: if Tinker
        tries many times and keeps failing, it may eventually skip the task.
        """
        self.status = TaskStatus.ACTIVE
        self.started_at = _now()
        self.attempt_count += 1  # Track how many attempts have been made
        self.touch()

    def mark_complete(self, outputs: list[str] | None = None) -> None:
        """Transition this task to COMPLETE and optionally record outputs.

        Parameters
        ----------
        outputs : list of artefact keys produced by the completing agent.
                  These are appended (not replaced) so multiple agents can
                  contribute outputs to the same task across retries.
        """
        self.status = TaskStatus.COMPLETE
        self.completed_at = _now()
        if outputs:
            # Use extend (not assignment) so we keep any outputs from earlier
            # partial attempts.
            self.outputs.extend(outputs)
        self.touch()

    def mark_failed(self, reason: str = "") -> None:
        """Transition this task to FAILED and record the failure reason.

        The reason string is stored in metadata so it shows up in the DB
        without needing a dedicated column.
        """
        self.status = TaskStatus.FAILED
        self.metadata["failure_reason"] = reason  # Keep the reason for post-mortem
        self.touch()

    # =========================================================================
    # Serialisation helpers
    # =========================================================================
    # SQLite and JSON both need plain dicts/strings, not Python objects.
    # These two methods handle the conversion in both directions.

    def to_dict(self) -> dict[str, Any]:
        """Convert the task to a plain dictionary.

        Uses Python's built-in ``dataclasses.asdict()`` to recursively
        convert every field, then converts the Enum objects to their string
        values (because JSON/SQLite don't know what an Enum is).
        """
        import dataclasses

        d = dataclasses.asdict(self)
        # Enums → their .value string (e.g. TaskType.DESIGN → "design")
        d["type"] = self.type.value
        d["status"] = self.status.value
        d["subsystem"] = self.subsystem.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Task":
        """Reconstruct a Task from a plain dictionary.

        This is the reverse of ``to_dict()``.  It handles two sources of
        data:
          1. In-memory dicts where lists/dicts are already Python objects.
          2. SQLite rows where lists/dicts have been JSON-encoded as strings
             (the registry stores them that way to fit in TEXT columns).

        Parameters
        ----------
        d : dict from either ``to_dict()`` or a SQLite row.
        """
        d = dict(d)  # Make a copy so we don't mutate the caller's dict

        # Convert string values back to their Enum types
        d["type"] = TaskType(d["type"])
        d["status"] = TaskStatus(d["status"])
        d["subsystem"] = Subsystem(d["subsystem"])

        # SQLite stores list/dict fields as JSON strings.
        # If a field is a string here, parse it back to a Python object.
        import json

        for list_field in ("dependencies", "outputs", "tags"):
            if isinstance(d.get(list_field), str):
                d[list_field] = json.loads(d[list_field])
        if isinstance(d.get("metadata"), str):
            d["metadata"] = json.loads(d["metadata"])

        return cls(**d)
