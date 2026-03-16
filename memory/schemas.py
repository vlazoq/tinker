"""
memory/schemas.py — The data blueprints for everything Tinker stores.

What this file does
-------------------
This file defines the exact shape ("schema") of every piece of data that
flows through Tinker's memory system.  Think of it like a set of official
forms — there is one form for "an Artifact", one for "a ResearchNote", one
for "a Task", and one for overall "MemoryConfig" settings.

Why it exists
-------------
Having one place where all data shapes are defined means:
- Every storage backend (Redis, DuckDB, ChromaDB, SQLite) agrees on what
  the data looks like.
- If you need to add a field (say, "author_agent"), you change it here and
  nowhere else.
- Serialisation (converting a Python object to JSON/SQL and back) is
  handled by each class itself, keeping storage code thin and simple.

How it fits into Tinker
-----------------------
This file is imported by virtually every other file in the ``memory`` package.
The storage adapters (storage.py) use ``to_dict()`` to prepare data for the
database.  The manager (manager.py) uses ``from_dict()`` / ``from_chroma()``
to reconstruct Python objects after reading from the database.

Key concepts for beginners
--------------------------
- **@dataclass**: A Python decorator that automatically generates ``__init__``,
  ``__repr__``, and other boilerplate from the class fields.  It's a concise
  way to define a class that's mainly used to hold data.
- **Enum**: A class where the valid values are fixed and named.  Using enums
  instead of plain strings means a typo (``"summery"`` instead of ``"summary"``)
  is caught immediately rather than silently stored wrong.
- **uuid4**: A randomly-generated unique identifier (128 bits).  Used to give
  every Artifact, ResearchNote, and Task a unique ID without needing a
  central "ID counter".
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
    """
    The category of content an Artifact contains.

    Using a fixed set of types makes it easy to filter artifacts — e.g.
    "give me all DECISION artifacts from this session" — without relying
    on free-text tags.

    Values
    ------
    ARCHITECTURE : A proposed system architecture or design (the main output
                   of the Architect agent).
    ANALYSIS     : A detailed analysis of a problem, trade-off, or component.
    DECISION     : A recorded architectural decision, e.g. "chose PostgreSQL
                   over MySQL because...".
    DIAGRAM      : A textual representation of a diagram (e.g. Mermaid syntax).
    CODE         : A code snippet or pseudocode produced during analysis.
    EVALUATION   : A scored or ranked comparison of options.
    SUMMARY      : A compressed summary of multiple older artifacts, produced
                   automatically by the MemoryCompressor.
    RAW          : Unclassified content; the default when no type is specified.
    """
    ARCHITECTURE   = "architecture"
    ANALYSIS       = "analysis"
    DECISION       = "decision"
    DIAGRAM        = "diagram"
    CODE           = "code"
    EVALUATION     = "evaluation"
    SUMMARY        = "summary"          # produced by the compression step
    RAW            = "raw"


class TaskStatus(str, Enum):
    """
    The lifecycle state of a Task.

    Tasks move forward through these states but (except for failure recovery)
    never backward:

        PENDING → RUNNING → COMPLETED
                          → FAILED
                          → ARCHIVED

    Values
    ------
    PENDING   : Task has been created but not yet started.
    RUNNING   : An agent is currently working on this task.
    COMPLETED : The task finished successfully.
    FAILED    : The task encountered an unrecoverable error.
    ARCHIVED  : The task has been moved to long-term storage (no longer active).
    """
    PENDING    = "pending"
    RUNNING    = "running"
    COMPLETED  = "completed"
    FAILED     = "failed"
    ARCHIVED   = "archived"


class TaskPriority(int, Enum):
    """
    Numeric priority level for tasks.

    Higher numbers mean higher priority.  When multiple tasks are pending,
    the task registry returns them ordered by priority (highest first), so
    the most important work is always done first.

    Values
    ------
    LOW      = 1  : Background or optional tasks.
    NORMAL   = 5  : Default priority.
    HIGH     = 8  : Important but not blocking.
    CRITICAL = 10 : Must be done immediately; blocks everything else.
    """
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
    A single piece of output produced during a Tinker session.

    Think of an Artifact like a sticky note that an agent writes and pins to
    the session's whiteboard.  Each sticky note has:
    - The actual content (what was produced).
    - A type label (what kind of thing it is — design, decision, code, etc.).
    - A reference to which session and task produced it.
    - A timestamp so we can sort and age-out old notes.
    - An ``archived`` flag: when the MemoryCompressor summarises old artifacts,
      it marks them archived (hidden from normal queries) but keeps them in
      the database for auditing purposes.

    Artifacts are stored in DuckDB (Session Memory) because DuckDB excels at
    fast analytical queries like "give me all decision artifacts from this
    session, newest first".

    Fields
    ------
    content       : The text content of the artifact (e.g. an architecture
                    proposal, a code snippet, a decision record).
    artifact_type : What kind of content this is (see ArtifactType enum).
    session_id    : Which Tinker run produced this artifact.
    task_id       : Which task produced this artifact (optional).
    metadata      : A free-form dictionary for extra context (e.g. which
                    agent produced it, what subsystem it relates to).
    id            : A unique UUID, auto-generated on creation.
    created_at    : UTC timestamp, auto-set on creation.
    archived      : True if this artifact has been compressed into a summary.
    """
    content: str
    artifact_type: ArtifactType = ArtifactType.RAW
    session_id: str = field(default_factory=lambda: "")
    task_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    # These three fields are set automatically — callers don't need to supply them
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    archived: bool = False  # True once the MemoryCompressor has processed this artifact

    def to_dict(self) -> dict[str, Any]:
        """
        Serialise this Artifact to a plain Python dictionary.

        Used by DuckDBAdapter to prepare the data for SQL INSERT.  Two fields
        need special treatment:
        - ``artifact_type``: stored as its string value (e.g. ``"decision"``)
          because databases don't know about Python enums.
        - ``created_at``: stored as an ISO 8601 string (e.g.
          ``"2024-01-15T10:30:00+00:00"``) because databases store datetimes
          as strings.

        Returns
        -------
        dict : A flat dictionary with all fields serialised to basic types.
        """
        d = asdict(self)                              # convert dataclass to dict
        d["artifact_type"] = self.artifact_type.value  # enum → string
        d["created_at"] = self.created_at.isoformat()  # datetime → ISO string
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Artifact":
        """
        Reconstruct an Artifact from a plain dictionary (e.g. a database row).

        This is the reverse of ``to_dict()``.  It converts the stored string
        values back into proper Python types before constructing the object.

        Parameters
        ----------
        d : A dict, typically from a DuckDB row, with all fields present.

        Returns
        -------
        Artifact : A fully populated Artifact instance.
        """
        d = dict(d)  # make a copy so we don't mutate the caller's dict
        d["artifact_type"] = ArtifactType(d["artifact_type"])       # string → enum
        d["created_at"] = datetime.fromisoformat(d["created_at"])   # ISO string → datetime
        return cls(**d)


@dataclass
class ResearchNote:
    """
    A research finding or architectural observation stored for long-term recall.

    ResearchNotes live in ChromaDB (the Research Archive).  Unlike Artifacts
    (which are tied to one session), ResearchNotes persist across sessions and
    can be retrieved by semantic similarity — meaning you can find relevant
    notes by searching for concepts, not just exact keywords.

    Analogy: if an Artifact is a sticky note on a session whiteboard,
    a ResearchNote is an entry in a permanent indexed encyclopedia.  When
    the Compressor summarises a session's artifacts, it creates ResearchNotes
    so the knowledge outlasts the session.

    ChromaDB works by storing each note alongside a "vector embedding" — a
    list of hundreds of numbers that represent the meaning of the text in a
    high-dimensional space.  Similar-meaning texts get similar vectors, which
    is how semantic search works.

    Fields
    ------
    content    : The text of the research finding.
    topic      : A short category label (e.g. "load-balancing", "security",
                 "session-summary").  Used for filtering searches.
    source     : Where this note came from (e.g. "tinker-internal",
                 "web-search", "tinker-compression").
    tags       : A list of keyword tags for additional filtering.
    session_id : The session that produced this note (for reference).
    task_id    : The task that produced this note (optional, for reference).
    metadata   : Extra free-form data.
    id         : UUID, auto-generated.
    created_at : UTC timestamp, auto-set.
    """
    content: str
    topic: str
    source: str = "tinker-internal"
    tags: list[str] = field(default_factory=list)
    session_id: str = ""
    task_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    # Auto-assigned fields
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        """
        Serialise this ResearchNote to a plain dictionary.

        Tags are joined into a comma-separated string because ChromaDB's
        metadata values must be scalar (strings, numbers, booleans) — lists
        are not supported.  The ``from_chroma`` method reverses this by
        splitting on commas.

        Returns
        -------
        dict : A flat dictionary suitable for storage.
        """
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        d["tags"] = ",".join(self.tags)   # list → comma-separated string for ChromaDB
        return d

    def to_chroma_metadata(self) -> dict[str, str | int | float | bool]:
        """
        Build the metadata dict that ChromaDB stores alongside the embedding.

        ChromaDB has a restriction: metadata values must be simple scalars
        (strings, ints, floats, or booleans).  No lists, no nested dicts.
        This method creates a "flat" version of the note's metadata that
        satisfies that constraint.

        The ``content`` field is stored separately by ChromaDB as the
        "document" text (used for display), not in metadata.

        Returns
        -------
        dict : A flat dict of scalar values, safe to pass to ChromaDB.
        """
        return {
            "topic": self.topic,
            "source": self.source,
            "tags": ",".join(self.tags),     # list → comma-separated string
            "session_id": self.session_id,
            "task_id": self.task_id or "",   # None → "" because ChromaDB wants strings
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_chroma(cls, doc_id: str, document: str, metadata: dict) -> "ResearchNote":
        """
        Reconstruct a ResearchNote from ChromaDB's query results.

        ChromaDB returns results as three separate pieces: the ID, the document
        text, and the metadata dict.  This method reassembles them into a
        proper ResearchNote object.

        Parameters
        ----------
        doc_id   : The UUID stored as ChromaDB's document ID.
        document : The text content stored as ChromaDB's document body.
        metadata : The flat metadata dict stored alongside the embedding.

        Returns
        -------
        ResearchNote : A fully populated ResearchNote instance.
        """
        # Split the comma-separated tags back into a list, filtering empty strings
        tags = [t for t in metadata.get("tags", "").split(",") if t]
        return cls(
            id=doc_id,
            content=document,
            topic=metadata.get("topic", ""),
            source=metadata.get("source", "tinker-internal"),
            tags=tags,
            session_id=metadata.get("session_id", ""),
            # Convert "" back to None for task_id (we stored None as "" above)
            task_id=metadata.get("task_id") or None,
            created_at=datetime.fromisoformat(
                # Fall back to current time if created_at is missing (shouldn't happen)
                metadata.get("created_at", datetime.now(timezone.utc).isoformat())
            ),
        )


@dataclass
class Task:
    """
    A unit of work tracked permanently in Tinker's Task Registry.

    Tasks are stored in SQLite (the Task Registry) because they need to
    survive across sessions and be queryable by status, priority, and
    session membership.

    Tasks can form a tree: a large task (e.g. "Design the authentication
    subsystem") can be broken into smaller sub-tasks ("Design the login flow",
    "Design the token refresh flow") by setting ``parent_task_id``.  This
    lets the Orchestrator track complex multi-step work.

    Analogy: think of a Task like a ticket in a project management system
    (Jira, GitHub Issues, etc.).  It has a title, a description, a status
    that changes as work progresses, and a record of what happened when it
    finished (result or error).

    Fields
    ------
    title          : A short human-readable name for the task.
    description    : A longer explanation of what needs to be done.
    priority       : How urgently this task should be worked on (see
                     TaskPriority enum).  Higher numbers = higher priority.
    status         : Current lifecycle state (see TaskStatus enum).
    parent_task_id : UUID of the parent task, if this is a sub-task.  None
                     for top-level tasks.
    session_id     : Which Tinker run created this task.
    result         : The output text when the task completed successfully.
    error          : The error message if the task failed.
    metadata       : A free-form dict for extra information.
    id             : UUID, auto-generated.
    created_at     : UTC timestamp when the task was created.
    updated_at     : UTC timestamp of the last status change.
    completed_at   : UTC timestamp when the task finished (completed/failed).
                     None while the task is still pending or running.
    """
    title: str
    description: str
    priority: TaskPriority = TaskPriority.NORMAL
    status: TaskStatus = TaskStatus.PENDING
    parent_task_id: Optional[str] = None
    session_id: str = ""
    result: Optional[str] = None     # populated when status becomes COMPLETED
    error: Optional[str] = None      # populated when status becomes FAILED
    metadata: dict[str, Any] = field(default_factory=dict)

    # Auto-assigned fields
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None  # set by SQLiteAdapter when task finishes

    def to_dict(self) -> dict[str, Any]:
        """
        Serialise this Task to a plain dictionary for SQLite storage.

        Several fields need special treatment:
        - ``priority`` and ``status``: enums → their plain values (int and string).
        - ``metadata``: dict → JSON string, because SQLite stores it as TEXT.
        - Datetime fields → ISO 8601 strings.
        - ``completed_at``: serialised to a string or ``None``.

        Returns
        -------
        dict : A flat dictionary with all fields serialised to basic types.
        """
        import json   # local import avoids a top-level circular dependency risk
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "priority": self.priority.value,      # int enum → plain int
            "status": self.status.value,          # str enum → plain string
            "parent_task_id": self.parent_task_id,
            "session_id": self.session_id,
            "result": self.result,
            "error": self.error,
            "metadata": json.dumps(self.metadata),  # dict → JSON string
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            # None if not yet completed; ISO string otherwise
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Task":
        """
        Reconstruct a Task from a plain dictionary (e.g. a SQLite row).

        Reverses the serialisation done by ``to_dict()``.

        Parameters
        ----------
        d : A dict with all Task fields.  Typically a SQLite row converted
            to a dict by the adapter.

        Returns
        -------
        Task : A fully populated Task instance with proper Python types.
        """
        import json
        d = dict(d)  # copy so we don't mutate the caller's dict
        d["priority"] = TaskPriority(d["priority"])        # int → enum
        d["status"] = TaskStatus(d["status"])              # string → enum
        d["created_at"] = datetime.fromisoformat(d["created_at"])
        d["updated_at"] = datetime.fromisoformat(d["updated_at"])
        if d.get("completed_at"):
            # Only parse if non-None/non-empty
            d["completed_at"] = datetime.fromisoformat(d["completed_at"])
        if isinstance(d.get("metadata"), str):
            # SQLite stores metadata as a JSON string; parse it back to a dict
            d["metadata"] = json.loads(d["metadata"])
        return cls(**d)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class MemoryConfig:
    """
    All the configurable settings for the MemoryManager, in one place.

    Think of this as a single settings file for the entire memory system.
    Rather than scattering magic constants through the code, every knob
    is here with a sensible default that works out-of-the-box.

    To customise behaviour (e.g. use a GPU for embeddings, or a different
    database file path) create a ``MemoryConfig`` and change the fields you
    care about, then pass it to ``MemoryManager``.

    Example::

        from tinker.memory import MemoryConfig, MemoryManager

        cfg = MemoryConfig(
            redis_url="redis://my-redis:6379",
            embedding_device="cuda",        # use GPU for faster embeddings
            compression_artifact_threshold=100,  # compress more aggressively
        )
        async with MemoryManager(config=cfg) as mm:
            ...

    Fields — Redis (Working Memory)
    --------------------------------
    redis_url         : Connection URL for the Redis server.
    redis_default_ttl : How long (in seconds) to keep each key before it
                        auto-expires.  0 means "never expire".  Default: 3600
                        (1 hour) — enough for a typical session.

    Fields — DuckDB (Session Memory)
    ---------------------------------
    duckdb_path : Path to the DuckDB database file.  Created automatically
                  if it doesn't exist.

    Fields — ChromaDB (Research Archive)
    -------------------------------------
    chroma_path       : Directory where ChromaDB stores its persistent files.
    chroma_collection : Name of the ChromaDB collection to use.  Collections
                        are like database tables — separate collections are
                        fully independent.

    Fields — SQLite (Task Registry)
    --------------------------------
    sqlite_path : Path to the SQLite database file.  Created automatically.

    Fields — Embedding Model
    -------------------------
    embedding_model  : The sentence-transformers model name.  ``all-MiniLM-L6-v2``
                       is small (80 MB) and fast.  ``nomic-embed-text`` is
                       larger but may produce better embeddings for code/docs.
    embedding_device : ``"cpu"`` works everywhere.  ``"cuda"`` uses an NVIDIA
                       GPU for much faster embedding generation if available.

    Fields — Compression Thresholds
    ---------------------------------
    compression_artifact_threshold : Trigger compression when a session has
                                     more than this many un-archived artifacts.
                                     Default: 500.
    compression_max_age_hours      : Also compress artifacts older than this
                                     many hours, regardless of count.  Default:
                                     24 (1 day).
    compression_summary_chunk      : How many artifacts to summarise in one
                                     batch.  Larger batches need more model
                                     context.  Default: 20.
    """

    # --- Redis ---
    redis_url: str = "redis://localhost:6379"
    redis_default_ttl: int = 3600          # seconds; 0 = never expire

    # --- DuckDB ---
    duckdb_path: str = "tinker_session.duckdb"

    # --- ChromaDB ---
    chroma_path: str = "./chroma_db"
    chroma_collection: str = "research_archive"

    # --- SQLite ---
    sqlite_path: str = "tinker_tasks.sqlite"

    # --- Embedding model ---
    embedding_model: str = "all-MiniLM-L6-v2"   # fast small model; try "nomic-embed-text" for better quality
    embedding_device: str = "cpu"                 # change to "cuda" if an NVIDIA GPU is available

    # --- Compression thresholds ---
    compression_artifact_threshold: int = 500     # run compression when session exceeds this many artifacts
    compression_max_age_hours: int = 24           # also compress artifacts older than this (in hours)
    compression_summary_chunk: int = 20           # summarise this many artifacts per LLM call
