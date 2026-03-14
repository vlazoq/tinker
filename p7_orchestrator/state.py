"""
OrchestratorState — serialisable snapshot of everything the Orchestrator
knows about itself right now.  The Dashboard polls this.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class LoopLevel(str, Enum):
    MICRO = "micro"
    MESO = "meso"
    MACRO = "macro"
    IDLE = "idle"


class LoopStatus(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SHUTDOWN = "shutdown"


@dataclass
class MicroLoopRecord:
    """Outcome of a single micro-loop iteration."""
    iteration: int
    task_id: str
    subsystem: str
    started_at: float
    finished_at: Optional[float] = None
    status: LoopStatus = LoopStatus.RUNNING
    architect_tokens: int = 0
    critic_tokens: int = 0
    artifact_id: Optional[str] = None
    new_tasks_generated: int = 0
    researcher_calls: int = 0
    error: Optional[str] = None

    def duration(self) -> float:
        if self.finished_at is None:
            return time.monotonic() - self.started_at
        return self.finished_at - self.started_at


@dataclass
class MesoLoopRecord:
    """Outcome of a meso-synthesis run."""
    subsystem: str
    trigger_iteration: int
    started_at: float
    artifacts_synthesised: int = 0
    finished_at: Optional[float] = None
    status: LoopStatus = LoopStatus.RUNNING
    document_id: Optional[str] = None
    error: Optional[str] = None


@dataclass
class MacroLoopRecord:
    """Outcome of a macro snapshot."""
    snapshot_version: int
    trigger_iteration: int
    started_at: float
    finished_at: Optional[float] = None
    status: LoopStatus = LoopStatus.RUNNING
    commit_hash: Optional[str] = None
    error: Optional[str] = None


@dataclass
class OrchestratorState:
    """
    Live state — written atomically to disk after every micro loop so the
    Dashboard can read it without acquiring a lock.
    """
    # identity
    started_at: float = field(default_factory=time.monotonic)
    wall_start: float = field(default_factory=time.time)

    # loop counters
    total_micro_loops: int = 0
    total_meso_loops: int = 0
    total_macro_loops: int = 0
    consecutive_failures: int = 0

    # per-subsystem micro-loop counter (used for meso trigger)
    subsystem_micro_counts: dict[str, int] = field(default_factory=dict)

    # current activity
    current_level: LoopLevel = LoopLevel.IDLE
    current_task_id: Optional[str] = None
    current_subsystem: Optional[str] = None

    # history (capped to last 100 entries to stay compact)
    micro_history: list[MicroLoopRecord] = field(default_factory=list)
    meso_history: list[MesoLoopRecord] = field(default_factory=list)
    macro_history: list[MacroLoopRecord] = field(default_factory=list)

    # last macro trigger time (monotonic)
    last_macro_at: float = field(default_factory=time.monotonic)

    # shutdown flag
    shutdown_requested: bool = False
    status: LoopStatus = LoopStatus.RUNNING

    # ── helpers ─────────────────────────────────────────────────────────────

    def increment_subsystem(self, subsystem: str) -> int:
        self.subsystem_micro_counts[subsystem] = (
            self.subsystem_micro_counts.get(subsystem, 0) + 1
        )
        return self.subsystem_micro_counts[subsystem]

    def reset_subsystem_count(self, subsystem: str) -> None:
        self.subsystem_micro_counts[subsystem] = 0

    def add_micro_record(self, record: MicroLoopRecord) -> None:
        self.micro_history.append(record)
        if len(self.micro_history) > 100:
            self.micro_history = self.micro_history[-100:]

    def add_meso_record(self, record: MesoLoopRecord) -> None:
        self.meso_history.append(record)
        if len(self.meso_history) > 50:
            self.meso_history = self.meso_history[-50:]

    def add_macro_record(self, record: MacroLoopRecord) -> None:
        self.macro_history.append(record)
        if len(self.macro_history) > 20:
            self.macro_history = self.macro_history[-20:]

    def to_dict(self) -> dict:
        """JSON-serialisable snapshot (monotonic times converted to wall offsets)."""
        now_mono = time.monotonic()
        uptime = now_mono - self.started_at

        def _record(r):
            d = asdict(r)
            # convert monotonic started_at to wall-clock
            if "started_at" in d:
                d["started_at_wall"] = self.wall_start + (d["started_at"] - self.started_at)
            return d

        return {
            "uptime_seconds": uptime,
            "wall_start": self.wall_start,
            "status": self.status.value,
            "current_level": self.current_level.value,
            "current_task_id": self.current_task_id,
            "current_subsystem": self.current_subsystem,
            "totals": {
                "micro": self.total_micro_loops,
                "meso": self.total_meso_loops,
                "macro": self.total_macro_loops,
                "consecutive_failures": self.consecutive_failures,
            },
            "subsystem_micro_counts": self.subsystem_micro_counts,
            "shutdown_requested": self.shutdown_requested,
            "micro_history": [_record(r) for r in self.micro_history[-10:]],
            "meso_history": [_record(r) for r in self.meso_history[-5:]],
            "macro_history": [_record(r) for r in self.macro_history[-3:]],
        }

    def write_snapshot(self, path: str) -> None:
        """Atomically overwrite the Dashboard snapshot file."""
        import os, tempfile
        data = json.dumps(self.to_dict(), indent=2)
        dir_ = os.path.dirname(path) or "."
        fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(data)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
