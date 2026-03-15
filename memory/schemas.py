"""
schemas.py — Canonical data models for all Tinker memory layers.

Each dataclass is the single source of truth for its storage layer.
Serialisation helpers (to_dict / from_dict) keep storage adapters thin.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class ArtifactType(str, Enum):
    ARCHITECTURE   = "architecture"
    ANALYSIS       = "analysis"
    DECISION       = "decision"
    DIAGRAM        = "diagram"
    CODE           = "code"
    EVALUATION     = "evaluation"
    SUMMARY        = "summary"          # produced by compression
    RAW            = "raw"


class TaskStatus(str, Enum):
    PENDING    = "pending"
    RUNNING    = "running"
    COMPLETED  = "completed"
    FAILED     = "failed"
    ARCHIVED   = "archived"


class TaskPriority(int, Enum):
    LOW      = 1
    NORMAL   = 5
    HIGH     = 8
    CRITICAL = 10


# ---------------------------------------------------------------------------
# Core data models
# ---------------------------------------------------------------------------

@dataclass
class Artifact:
    """
    A single output produced during a Tinker session.
    Stored in DuckDB (Session Memory).
    """
    content: str
    artifact_type: ArtifactType = ArtifactType.RAW
    session_id: str = field(default_factory=lambda: "")
    task_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    # Auto-assigned
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    archived: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["artifact_type"] = self.artifact_type.value
        d["created_at"] = self.created_at.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Artifact":
        d = dict(d)
        d["artifact_type"] = ArtifactType(d["artifact_type"])
        d["created_at"] = datetime.fromisoformat(d["created_at"])
        return cls(**d)


@dataclass
class ResearchNote:
    """
    A semantically-indexed research finding or architectural observation.
    Stored in ChromaDB (Research Archive) with an embedding vector.
    """
    content: str
    topic: str
    source: str = "tinker-internal"
    tags: list[str] = field(default_factory=list)
    session_id: str = ""
    task_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    # Auto-assigned
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        d["tags"] = ",".join(self.tags)          # ChromaDB metadata must be scalar
        return d

    def to_chroma_metadata(self) -> dict[str, str | int | float | bool]:
        """ChromaDB only accepts flat scalar metadata."""
        return {
            "topic": self.topic,
            "source": self.source,
            "tags": ",".join(self.tags),
            "session_id": self.session_id,
            "task_id": self.task_id or "",
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_chroma(cls, doc_id: str, document: str, metadata: dict) -> "ResearchNote":
        tags = [t for t in metadata.get("tags", "").split(",") if t]
        return cls(
            id=doc_id,
            content=document,
            topic=metadata.get("topic", ""),
            source=metadata.get("source", "tinker-internal"),
            tags=tags,
            session_id=metadata.get("session_id", ""),
            task_id=metadata.get("task_id") or None,
            created_at=datetime.fromisoformat(
                metadata.get("created_at", datetime.now(timezone.utc).isoformat())
            ),
        )


@dataclass
class Task:
    """
    A unit of work tracked across the lifetime of Tinker.
    Stored in SQLite (Task Registry).
    """
    title: str
    description: str
    priority: TaskPriority = TaskPriority.NORMAL
    status: TaskStatus = TaskStatus.PENDING
    parent_task_id: Optional[str] = None
    session_id: str = ""
    result: Optional[str] = None
    error: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    # Auto-assigned
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        import json
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "priority": self.priority.value,
            "status": self.status.value,
            "parent_task_id": self.parent_task_id,
            "session_id": self.session_id,
            "result": self.result,
            "error": self.error,
            "metadata": json.dumps(self.metadata),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Task":
        import json
        d = dict(d)
        d["priority"] = TaskPriority(d["priority"])
        d["status"] = TaskStatus(d["status"])
        d["created_at"] = datetime.fromisoformat(d["created_at"])
        d["updated_at"] = datetime.fromisoformat(d["updated_at"])
        if d.get("completed_at"):
            d["completed_at"] = datetime.fromisoformat(d["completed_at"])
        if isinstance(d.get("metadata"), str):
            d["metadata"] = json.loads(d["metadata"])
        return cls(**d)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class MemoryConfig:
    """All tuneable knobs for the MemoryManager in one place."""

    # Redis
    redis_url: str = "redis://localhost:6379"
    redis_default_ttl: int = 3600          # seconds; 0 = no expiry

    # DuckDB
    duckdb_path: str = "tinker_session.duckdb"

    # ChromaDB
    chroma_path: str = "./chroma_db"
    chroma_collection: str = "research_archive"

    # SQLite
    sqlite_path: str = "tinker_tasks.sqlite"

    # Embedding model
    embedding_model: str = "all-MiniLM-L6-v2"   # or "nomic-embed-text"
    embedding_device: str = "cpu"                 # "cuda" if GPU available

    # Compression thresholds
    compression_artifact_threshold: int = 500     # compress when session > N artifacts
    compression_max_age_hours: int = 24           # archive artifacts older than N hours
    compression_summary_chunk: int = 20           # summarise N artifacts at a time
