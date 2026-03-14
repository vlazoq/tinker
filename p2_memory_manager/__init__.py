"""
Tinker Memory Manager
=====================
Unified async interface over all four memory layers:
  - Working Memory  → Redis      (ephemeral per-task context)
  - Session Memory  → DuckDB     (all artifacts from the current run)
  - Research Archive→ ChromaDB   (semantically searchable notes, cross-session)
  - Task Registry   → SQLite     (persistent log of every task ever created)
"""

from .manager import MemoryManager
from .schemas import Artifact, ResearchNote, Task, MemoryConfig
from .embeddings import EmbeddingPipeline
from .compression import MemoryCompressor

__all__ = [
    "MemoryManager",
    "Artifact",
    "ResearchNote",
    "Task",
    "MemoryConfig",
    "EmbeddingPipeline",
    "MemoryCompressor",
]
