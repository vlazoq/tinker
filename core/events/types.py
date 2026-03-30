"""
core/events/types.py
====================

Event type definitions for the Tinker event bus.

``EventType`` covers the system-level events that are meaningful for
reactive behaviour (as opposed to AuditEventType which is broader and
includes lower-level operational events).

The two enums are deliberately separate:
  * AuditEventType  — "what happened" for compliance / forensics
  * EventType       — "what should react" for business logic

This keeps the audit log stable while the reactive event system can
evolve independently.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    """System events that trigger reactive behaviour.

    Naming convention: ``<SUBJECT>_<PAST_PARTICIPLE>``
    so that event names read as facts that have already occurred.
    """

    # ── Loop lifecycle ────────────────────────────────────────────────────────
    MICRO_LOOP_COMPLETED = "micro_loop_completed"
    MICRO_LOOP_FAILED = "micro_loop_failed"
    MESO_LOOP_COMPLETED = "meso_loop_completed"
    MESO_LOOP_FAILED = "meso_loop_failed"
    MACRO_LOOP_COMPLETED = "macro_loop_completed"
    MACRO_LOOP_FAILED = "macro_loop_failed"

    # ── Task events ───────────────────────────────────────────────────────────
    TASK_SELECTED = "task_selected"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASKS_GENERATED = "tasks_generated"

    # ── Agent events ──────────────────────────────────────────────────────────
    ARCHITECT_COMPLETED = "architect_completed"
    CRITIC_SCORED = "critic_scored"
    SYNTHESIZER_COMPLETED = "synthesizer_completed"
    REFINEMENT_ITERATION = "refinement_iteration"

    # ── Research events ───────────────────────────────────────────────────────
    RESEARCH_COMPLETED = "research_completed"

    # ── Human Judge events ────────────────────────────────────────────────────
    HUMAN_REVIEW_REQUESTED = "human_review_requested"
    HUMAN_REVIEW_SUBMITTED = "human_review_submitted"
    HUMAN_REVIEW_TIMEOUT = "human_review_timeout"

    # ── Artifact events ───────────────────────────────────────────────────────
    ARTIFACT_STORED = "artifact_stored"

    # ── Stagnation ────────────────────────────────────────────────────────────
    STAGNATION_DETECTED = "stagnation_detected"
    STAGNATION_RESOLVED = "stagnation_resolved"
    STAGNATION_INTERVENTION = "stagnation_intervention"

    # ── Resilience ────────────────────────────────────────────────────────────
    CIRCUIT_OPENED = "circuit_opened"
    CIRCUIT_CLOSED = "circuit_closed"
    SLA_BREACHED = "sla_breached"
    BACKPRESSURE_APPLIED = "backpressure_applied"

    # ── System lifecycle ──────────────────────────────────────────────────────
    SYSTEM_STARTED = "system_started"
    SYSTEM_STOPPING = "system_stopping"

    # ── Extension point ───────────────────────────────────────────────────────
    CUSTOM = "custom"


@dataclass
class Event:
    """An immutable event emitted on the bus.

    Producers create an Event and call ``bus.publish(event)``.
    Handlers receive the same Event object; they must not mutate it.

    Parameters
    ----------
    type     : The kind of event (from EventType).
    payload  : Arbitrary dict with event-specific data.
    source   : Name of the component that emitted the event.
    trace_id : Optional distributed trace ID for correlation.
    id       : Auto-generated unique event ID.
    timestamp: Auto-set to the current UTC time.
    """

    type: EventType
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = "unknown"
    trace_id: str | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
