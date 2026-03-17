"""
grub/contracts/task.py
======================
GrubTask — the unit of work Grub hands to a Minion.

Why a separate contract type?
------------------------------
Tinker has its own Task dataclass (tasks/schema.py) for design-level work.
Grub needs a different shape: instead of "research this topic", Grub tasks
say "implement this module from this design artifact".

GrubTask does NOT inherit from Tinker's Task — they are independent.
Grub converts Tinker design-tasks into GrubTasks at the boundary (feedback.py).

Fields explained
----------------
id              : Unique identifier (UUID string).
title           : Short human-readable description.
description     : Full instructions for the Minion.
artifact_path   : Path to the Tinker design artifact this implements.
                  Example: "tinker_artifacts/api_gateway_design.md"
target_files    : Which source files this task produces or modifies.
                  Empty list = Minion decides.
language        : Programming language. Default: "python".
subsystem       : Which Tinker subsystem this belongs to (for grouping).
priority        : HIGH / NORMAL / LOW (affects queue ordering in Mode C).
tinker_task_id  : The original Tinker task ID (for feedback traceability).
context         : Extra key/value context (test framework, style guide, etc.).
created_at      : ISO timestamp when this task was created.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class TaskPriority(str, Enum):
    """
    How urgently this task should be processed.

    In sequential mode (A) this is informational only — tasks run one by one.
    In queue mode (C) workers pick HIGH priority tasks first.
    """
    HIGH   = "high"
    NORMAL = "normal"
    LOW    = "low"


@dataclass
class GrubTask:
    """
    A single unit of implementation work.

    Created by Grub when it reads a Tinker design artifact, then passed to
    the appropriate Minion for execution.

    Example
    -------
    ::

        task = GrubTask(
            title        = "Implement API gateway request router",
            description  = "Based on the attached design, implement the request "
                           "routing logic in api_gateway/router.py",
            artifact_path= "tinker_artifacts/api_gateway_design.md",
            target_files = ["api_gateway/router.py"],
            language     = "python",
            subsystem    = "api_gateway",
        )
    """

    # Required fields (must be provided)
    title:          str
    description:    str

    # Auto-generated if not provided
    id:             str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at:     str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # Optional context fields
    artifact_path:  str            = ""    # path to Tinker design doc
    target_files:   list[str]      = field(default_factory=list)
    language:       str            = "python"
    subsystem:      str            = "unknown"
    priority:       TaskPriority   = TaskPriority.NORMAL
    tinker_task_id: str            = ""    # traceability back to Tinker
    context:        dict[str, Any] = field(default_factory=dict)

    # Filled in after Minion runs
    assigned_minion: str = ""   # which minion handled this
    attempt_count:   int = 0    # how many times it has been retried

    def to_dict(self) -> dict:
        """Serialise to a plain dict (for SQLite storage in queue mode)."""
        return {
            "id":              self.id,
            "title":           self.title,
            "description":     self.description,
            "artifact_path":   self.artifact_path,
            "target_files":    self.target_files,
            "language":        self.language,
            "subsystem":       self.subsystem,
            "priority":        self.priority.value,
            "tinker_task_id":  self.tinker_task_id,
            "context":         self.context,
            "assigned_minion": self.assigned_minion,
            "attempt_count":   self.attempt_count,
            "created_at":      self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GrubTask":
        """Deserialise from a plain dict (when loading from SQLite)."""
        return cls(
            id              = d["id"],
            title           = d["title"],
            description     = d["description"],
            artifact_path   = d.get("artifact_path", ""),
            target_files    = d.get("target_files", []),
            language        = d.get("language", "python"),
            subsystem       = d.get("subsystem", "unknown"),
            priority        = TaskPriority(d.get("priority", "normal")),
            tinker_task_id  = d.get("tinker_task_id", ""),
            context         = d.get("context", {}),
            assigned_minion = d.get("assigned_minion", ""),
            attempt_count   = d.get("attempt_count", 0),
            created_at      = d.get("created_at", ""),
        )
