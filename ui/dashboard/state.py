"""
tinker/dashboard/state.py
─────────────────────────
Canonical shared-state model for the Tinker Observability Dashboard.
All data flowing from the Orchestrator is normalised into these dataclasses
before being handed to the UI.  Nothing in here depends on Textual or Rich.
"""

from __future__ import annotations

import contextlib
import threading
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

# ──────────────────────────────────────────
# Enumerations
# ──────────────────────────────────────────


class LoopLevel(StrEnum):
    MICRO = "micro"
    MESO = "meso"
    MACRO = "macro"


class TaskStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETE = "complete"
    FAILED = "failed"
    SKIPPED = "skipped"


class TaskType(StrEnum):
    DESIGN = "design"
    CRITIQUE = "critique"
    REFINE = "refine"
    RESEARCH = "research"
    COMMIT = "commit"
    STAGNATION_BREAK = "stagnation_break"


# ──────────────────────────────────────────
# Sub-state dataclasses
# ──────────────────────────────────────────


@dataclass
class TaskInfo:
    id: str
    type: TaskType
    subsystem: str
    description: str
    status: TaskStatus
    created_at: datetime = field(default_factory=datetime.utcnow)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result_summary: str | None = None
    full_content: str | None = None  # for detail view


@dataclass
class ArchitectOutput:
    summary: str
    full_content: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    task_id: str | None = None


@dataclass
class CriticOutput:
    score: float  # 0.0 – 10.0
    top_objection: str
    full_content: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    task_id: str | None = None


@dataclass
class ArchitectureState:
    version: str
    last_commit_time: datetime | None
    summary: str
    full_content: str


@dataclass
class StagnationEvent:
    timestamp: datetime
    description: str
    action_taken: str


@dataclass
class StagnationStatus:
    is_stagnant: bool
    stagnation_score: float  # 0.0 – 1.0
    monitor_status: str = "nominal"
    recent_events: list[StagnationEvent] = field(default_factory=list)


@dataclass
class MemoryStats:
    session_artifact_count: int
    research_archive_size: int  # item count
    working_memory_tokens: int


@dataclass
class ModelMetrics:
    avg_latency_ms: float
    p99_latency_ms: float
    error_rate: float  # 0.0 – 1.0
    total_calls: int
    recent_errors: list[str] = field(default_factory=list)


@dataclass
class QueueStats:
    total_depth: int
    by_status: dict[str, int] = field(default_factory=dict)
    by_type: dict[str, int] = field(default_factory=dict)


# ──────────────────────────────────────────
# Root state
# ──────────────────────────────────────────


@dataclass
class TinkerState:
    # ── connection ──────────────────────────
    connected: bool = False
    last_update: datetime | None = None

    # ── loop counters ────────────────────────
    loop_level: LoopLevel = LoopLevel.MICRO
    micro_count: int = 0
    meso_count: int = 0
    macro_count: int = 0

    # ── active work ──────────────────────────
    active_task: TaskInfo | None = None
    last_architect: ArchitectOutput | None = None
    last_critic: CriticOutput | None = None

    # ── queue ────────────────────────────────
    queue_stats: QueueStats = field(default_factory=lambda: QueueStats(total_depth=0))
    recent_tasks: list[TaskInfo] = field(default_factory=list)

    # ── architecture ─────────────────────────
    arch_state: ArchitectureState | None = None

    # ── health ───────────────────────────────
    stagnation: StagnationStatus = field(default_factory=lambda: StagnationStatus(False, 0.0))
    model_metrics: ModelMetrics = field(default_factory=lambda: ModelMetrics(0.0, 0.0, 0.0, 0))

    # ── memory ───────────────────────────────
    memory_stats: MemoryStats = field(default_factory=lambda: MemoryStats(0, 0, 0))


# ──────────────────────────────────────────
# Thread-safe state store
# ──────────────────────────────────────────


class StateStore:
    """
    Thread-safe wrapper around TinkerState.
    The subscriber thread writes via `apply_patch()`; Textual reads via
    `snapshot()` which returns a deep-copied frozen view.
    """

    def __init__(self) -> None:
        self._state = TinkerState()
        self._lock = threading.Lock()
        self._listeners: list[Any] = []  # asyncio.Queue objects

    # ── writer (subscriber thread) ───────────

    def apply_patch(self, patch: dict[str, Any]) -> None:
        """Merge a flat dict of top-level field updates into state."""
        with self._lock:
            for key, value in patch.items():
                if hasattr(self._state, key):
                    setattr(self._state, key, value)
            self._state.last_update = datetime.utcnow()

    def mark_disconnected(self) -> None:
        with self._lock:
            self._state.connected = False

    def mark_connected(self) -> None:
        with self._lock:
            self._state.connected = True

    # ── reader (Textual main thread) ─────────

    def snapshot(self) -> TinkerState:
        with self._lock:
            return deepcopy(self._state)

    # ── listener registration (for push model) ──

    def add_listener(self, q: Any) -> None:
        self._listeners.append(q)

    def _notify_listeners(self) -> None:
        snap = self.snapshot()
        for q in self._listeners:
            with contextlib.suppress(Exception):
                q.put_nowait(snap)


# Singleton used by the rest of the dashboard
_store = StateStore()


def get_store() -> StateStore:
    return _store
