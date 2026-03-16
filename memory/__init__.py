"""
Tinker Memory Package
=====================

What this file does
-------------------
This is the "front door" for the ``memory`` package.  It re-exports the
key classes so the rest of Tinker can write short, clean imports.

Why Tinker needs memory
-----------------------
Tinker's AI agents run in loops, generating architectural ideas, critiques,
research findings, and decisions over time.  Without memory, every loop
iteration would start from scratch — the agents couldn't learn from earlier
iterations or refer back to past decisions.

The memory package solves this by providing four different storage systems,
each optimised for a different kind of information and a different time horizon:

  Layer 1 — Working Memory (Redis)
      Think of this as the agents' short-term memory — a whiteboard that is
      wiped clean at the end of a session.  It holds ephemeral, fast-changing
      state: "what task am I working on right now?", "what was the last thing
      the Architect said?".  Redis is an in-memory key-value store, so reads
      and writes are extremely fast (microseconds).

  Layer 2 — Session Memory (DuckDB)
      This is the session notebook — a structured log of every "artifact"
      (output, analysis, decision, diagram) produced during the current run.
      DuckDB is a fast analytical database embedded in the process, so no
      separate server is needed.  When the session fills up, the Compressor
      summarises old entries and archives them.

  Layer 3 — Research Archive (ChromaDB)
      This is the long-term library — a vector database that stores research
      notes and compressed session summaries in a way that allows *semantic
      search*.  "Semantic" means you can search by meaning, not just keywords:
      searching for "load balancing strategies" will find notes about "traffic
      distribution" even if those exact words don't appear.  ChromaDB persists
      data to disk, so it survives across sessions.

  Layer 4 — Task Registry (SQLite)
      This is the permanent task ledger — a relational database that records
      every task Tinker has ever created, along with its status, result, and
      any errors.  SQLite writes to a single file on disk and is perfectly
      suited to this kind of durable, structured record-keeping.

How it fits into Tinker
-----------------------
The ``MemoryManager`` (from manager.py) is the single class that Tinker's
Orchestrator and agents use.  It hides the four storage backends behind one
clean interface.  Callers don't need to know that Redis, DuckDB, ChromaDB,
and SQLite exist — they just call ``store_artifact()``, ``search_research()``,
etc.

This ``__init__.py`` re-exports the most important classes so callers can
write:

    from tinker.memory import MemoryManager, Artifact, MemoryConfig

instead of the longer form:

    from tinker.memory.manager import MemoryManager
    from tinker.memory.schemas import Artifact, MemoryConfig
"""

# The unified interface — this is what almost all callers will use.
from .manager import MemoryManager

# Data schemas — needed when building objects to store or interpreting results.
from .schemas import Artifact, ResearchNote, Task, MemoryConfig

# Advanced components — exposed for callers that need direct access.
from .embeddings import EmbeddingPipeline   # converts text to semantic vectors
from .compression import MemoryCompressor   # compresses old session artifacts

__all__ = [
    "MemoryManager",      # the main interface: store, search, compress
    "Artifact",           # a single output produced during a session
    "ResearchNote",       # a semantically-indexed research finding
    "Task",               # a unit of work tracked in the task registry
    "MemoryConfig",       # all configuration knobs in one place
    "EmbeddingPipeline",  # wraps the sentence-transformer model
    "MemoryCompressor",   # summarises and archives old session artifacts
]
