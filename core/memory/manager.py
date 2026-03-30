"""
manager.py — MemoryManager: unified async interface over all four layers.

  ┌─────────────────────────────────────────────────────────┐
  │                     MemoryManager                       │
  │                                                         │
  │  store_artifact()    get_artifact()                     │
  │  store_research()    search_research()  get_research()  │
  │  store_task()        get_task()         get_tasks()     │
  │  set_context()       get_context()      clear_context() │
  │  compress()          compress_all()                     │
  └────────────┬────────────────────────────────────────────┘
               │
   ┌───────────┼────────────────────────────────────┐
   │           │                                    │
RedisAdapter  DuckDBAdapter  ChromaAdapter  SQLiteAdapter
(Working Mem) (Session Mem)  (Research Arch)(Task Registry)

SOLID — Single Responsibility Principle
----------------------------------------
MemoryManager is intentionally a unified façade — it wires four storage
backends together and exposes them through one coherent interface.  However,
consumers that only need *one* aspect of memory should depend on the
narrower ``Protocol`` types defined below, not on the full ``MemoryManager``.

This follows the Interface Segregation Principle (ISP, the "I" in SOLID):
consumers declare what they actually need (ArtifactStore, ResearchStore,
WorkingMemory, TaskStore) and MemoryManager satisfies all four, but a stub
or alternative implementation only needs to implement the subset it provides.

Usage::

    # In the orchestrator — only needs artifact storage and research:
    def __init__(self, artifact_store: ArtifactStore, research_store: ResearchStore):
        self._artifacts = artifact_store
        self._research = research_store

    # Pass the full MemoryManager — it satisfies both protocols:
    orchestrator = Orchestrator(
        artifact_store=memory_manager,
        research_store=memory_manager,
    )
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from .compression import MemoryCompressor
from .embeddings import EmbeddingPipeline
from .schemas import (
    Artifact,
    ArtifactType,
    MemoryConfig,
    ResearchNote,
    Task,
    TaskStatus,
)
from .storage import ChromaAdapter, DuckDBAdapter, RedisAdapter, SQLiteAdapter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Semantic deduplication settings
#
# Before storing a new artifact, MemoryManager checks cosine similarity
# against the N most recent artifacts.  If any existing artifact is very
# similar (above the threshold), the new content is merged into the existing
# one instead of creating a near-duplicate entry.  This keeps the session
# memory lean and avoids redundant compression work later.
# ---------------------------------------------------------------------------

# How many recent artifacts to check for duplicates
_DEDUP_RECENT_WINDOW = 5

# Cosine similarity above which a new artifact is considered a duplicate
# of an existing one.  0.92 is deliberately high — we only merge when the
# two pieces of content are nearly identical.
_DEDUP_SIMILARITY_THRESHOLD = 0.92


# ---------------------------------------------------------------------------
# Focused service protocols (Interface Segregation / SRP)
#
# Consumers should depend on the narrowest protocol they actually use.
# MemoryManager satisfies all four — pass it wherever any protocol is needed.
# ---------------------------------------------------------------------------


@runtime_checkable
class ArtifactStore(Protocol):
    """Minimal interface for storing and retrieving design artifacts."""

    async def store_artifact(
        self,
        content: str,
        artifact_type: ArtifactType = ArtifactType.RAW,
        task_id: str | None = None,
        metadata: dict | None = None,
        session_id: str | None = None,
        auto_compress: bool = True,
    ) -> Artifact: ...

    async def get_artifact(self, artifact_id: str) -> Artifact | None: ...

    async def get_recent_artifacts(
        self,
        artifact_type: ArtifactType | None = None,
        limit: int = 20,
        include_archived: bool = False,
        session_id: str | None = None,
    ) -> list[Artifact]: ...


@runtime_checkable
class ResearchStore(Protocol):
    """Minimal interface for storing and searching research notes."""

    async def store_research(
        self,
        content: str,
        topic: str,
        tags: list[str] | None = None,
        source: str = "tinker-internal",
        task_id: str | None = None,
        metadata: dict | None = None,
        session_id: str | None = None,
    ) -> ResearchNote: ...

    async def search_research(
        self,
        query: str,
        n_results: int = 5,
        filter_topic: str | None = None,
        filter_session: str | None = None,
    ) -> list[ResearchNote]: ...


@runtime_checkable
class WorkingMemory(Protocol):
    """Minimal interface for ephemeral per-task context storage."""

    async def set_context(
        self, key: str, value: Any, ttl: int | None = None, session_id: str | None = None
    ) -> None: ...

    async def get_context(self, key: str, session_id: str | None = None) -> Any | None: ...

    async def delete_context(self, key: str, session_id: str | None = None) -> None: ...


@runtime_checkable
class TaskStore(Protocol):
    """Minimal interface for task persistence."""

    async def store_task(self, task: Task) -> Task: ...

    async def get_task(self, task_id: str) -> Task | None: ...

    async def update_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        result: str | None = None,
        error: str | None = None,
    ) -> None: ...


def _parse_row_metadata(row: dict) -> dict:
    """Parse the ``metadata`` field of a DB row from JSON string to dict in-place."""
    if isinstance(row.get("metadata"), str):
        try:
            row["metadata"] = json.loads(row["metadata"])
        except Exception:
            row["metadata"] = {}
    elif row.get("metadata") is None:
        row["metadata"] = {}
    return row


from ._research_archive import ResearchArchiveMixin
from ._session_memory import SessionMemoryMixin
from ._task_registry import TaskRegistryMixin
from ._working_memory import WorkingMemoryMixin


class MemoryManager(
    WorkingMemoryMixin,
    SessionMemoryMixin,
    ResearchArchiveMixin,
    TaskRegistryMixin,
):
    """
    Unified, async-first interface over all Tinker memory layers.

    Lifecycle
    ---------
    Use as an async context manager for automatic connect/close::

        async with MemoryManager(config=cfg, session_id="run-42") as mm:
            await mm.store_artifact(...)
            results = await mm.search_research("load balancing")

    Or manage manually::

        mm = MemoryManager(config=cfg, session_id="run-42")
        await mm.connect()
        ...
        await mm.close()

    Parameters
    ----------
    config      : MemoryConfig — all tuneable knobs in one place
    session_id  : identifies the current Tinker run; defaults to a timestamp
    summariser  : optional async callable(prompt) -> str for compression;
                  if None the compressor uses a stub that labels summaries clearly
    """

    def __init__(
        self,
        config: MemoryConfig | None = None,
        session_id: str | None = None,
        summariser: Callable[[str], Awaitable[str]] | None = None,
    ):
        self.config = config or MemoryConfig()
        self.session_id = session_id or f"session-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"

        # Storage adapters
        self._redis = RedisAdapter(self.config.redis_url, self.config.redis_default_ttl)
        self._duckdb = DuckDBAdapter(self.config.duckdb_path)
        self._chroma = ChromaAdapter(self.config.chroma_path, self.config.chroma_collection)
        self._sqlite = SQLiteAdapter(self.config.sqlite_path)

        # Embedding pipeline
        self._embeddings = EmbeddingPipeline(
            model_name=self.config.embedding_model,
            device=self.config.embedding_device,
        )

        # Compression
        self._compressor = MemoryCompressor(
            duckdb=self._duckdb,
            chroma=self._chroma,
            embeddings=self._embeddings,
            summariser=summariser,
            config=self.config,
        )

        self._connected = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open all storage connections. Call before any read/write."""
        results = await asyncio.gather(
            self._redis.connect(),
            self._duckdb.connect(),
            self._chroma.connect(),
            self._sqlite.connect(),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                logger.warning("Connection issue during startup: %s", r)
        self._connected = True
        logger.info("MemoryManager ready (session=%s)", self.session_id)

    async def close(self) -> None:
        """Close all storage connections gracefully."""
        await asyncio.gather(
            self._redis.close(),
            self._duckdb.close(),
            self._chroma.close(),
            self._sqlite.close(),
            return_exceptions=True,
        )
        self._connected = False
        logger.info("MemoryManager closed (session=%s)", self.session_id)

    async def __aenter__(self) -> MemoryManager:
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Backend-specific methods are provided by mixins:
    #   WorkingMemoryMixin  — set_context, get_context, delete_context, etc.
    #   SessionMemoryMixin  — store_artifact, get_artifact, etc.
    #   ResearchArchiveMixin — store_research, search_research, etc.
    #   TaskRegistryMixin   — store_task, get_task, etc.
    # ------------------------------------------------------------------

    # (adapt the richer internal API to the flat dict interface the
    #  orchestrator and its loops expect)
    # ------------------------------------------------------------------

    async def get_artifacts(
        self,
        subsystem: str,
        limit: int = 10,
        session_id: str | None = None,
    ) -> list[dict]:
        """
        Return up to *limit* recent artifacts for a given subsystem as plain dicts.
        Used by the meso loop to collect work done on a subsystem.
        """
        sid = session_id or self.session_id
        rows = await self._duckdb.get_recent(sid, artifact_type=None, limit=limit * 5)
        result = []
        for row in rows:
            _parse_row_metadata(row)
            meta = row.get("metadata", {})
            if meta.get("subsystem", "") == subsystem or not subsystem:
                result.append(
                    {
                        "id": row["id"],
                        "content": row["content"],
                        "subsystem": meta.get("subsystem", subsystem),
                        "artifact_type": row.get("artifact_type", "raw"),
                        "task_id": row.get("task_id"),
                        "created_at": row.get("created_at", ""),
                        "metadata": meta,
                    }
                )
            if len(result) >= limit:
                break
        return result

    async def store_document(self, document: dict) -> str:
        """
        Store a meso/macro synthesis document (plain dict) and return its ID.
        Internally stored as a SUMMARY artifact with metadata.
        """
        content = document.get("synthesis") or document.get("content", str(document))
        metadata = {k: v for k, v in document.items() if k not in ("synthesis", "content")}
        artifact = await self.store_artifact(
            content=content,
            artifact_type=ArtifactType.SUMMARY,
            task_id=document.get("task_id"),
            metadata=metadata,
        )
        return artifact.id

    async def get_all_documents(self, session_id: str | None = None) -> list[dict]:
        """
        Return all stored SUMMARY documents (meso/macro synthesis results).
        Used by the macro loop to compile a full architectural snapshot.
        """
        sid = session_id or self.session_id
        rows = await self._duckdb.get_recent(
            sid,
            artifact_type=ArtifactType.SUMMARY.value,
            limit=500,
            include_archived=True,
        )
        return [
            {
                "id": row["id"],
                "content": row["content"],
                "artifact_type": row.get("artifact_type", "summary"),
                "task_id": row.get("task_id"),
                "created_at": row.get("created_at", ""),
                "metadata": _parse_row_metadata(row).get("metadata", {}),
            }
            for row in rows
        ]

    async def search(
        self,
        query: str,
        top_k: int = 10,
        filters: dict | None = None,
    ) -> list[dict]:
        """
        Semantic search adapter for the MemoryQueryTool protocol.
        Delegates to search_research() and normalises results to plain dicts.
        """
        filter_topic = (filters or {}).get("artifact_type")
        filter_session = (filters or {}).get("session_id")
        # Query ChromaDB directly to expose the raw distance scores.
        # Converting L2 distance → relevance: score = 1 / (1 + distance)
        # so 0-distance (exact match) → 1.0, and larger distances → 0.
        embedding = await self._embeddings.embed(query)
        where: dict = {}
        if filter_topic:
            where["topic"] = {"$eq": filter_topic}
        if filter_session:
            where["session_id"] = {"$eq": filter_session}
        raw_results = await self._chroma.query(
            embedding=embedding,
            n_results=top_k,
            where=where if where else None,
        )
        return [
            {
                "id": r["id"],
                "memory_id": r["id"],
                "score": round(1.0 / (1.0 + r["distance"]), 4),
                "title": r["metadata"].get("topic", ""),
                "artifact_type": "research_note",
                "task_id": r["metadata"].get("task_id") or "",
                "created_at": r["metadata"].get("created_at", ""),
                "tags": (
                    r["metadata"].get("tags", "").split(",") if r["metadata"].get("tags") else []
                ),
                "snippet": r["document"][:300],
                "text": r["document"],
            }
            for r in raw_results
        ]

    # ------------------------------------------------------------------
    # Compression
    # ------------------------------------------------------------------

    async def compress(self, session_id: str | None = None) -> int:
        """
        Run compression checks for a session.

        Archives artifacts that exceed the threshold or have aged out.
        Returns the number of artifacts archived.
        """
        sid = session_id or self.session_id
        return await self._compressor.maybe_compress(sid)

    async def compress_all(self, session_id: str | None = None) -> int:
        """
        Force-compress every un-archived artifact in a session.

        Useful at end-of-session to pack everything into the Research Archive.
        Returns the number of artifacts archived.
        """
        sid = session_id or self.session_id
        return await self._compressor.force_compress_all(sid)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    async def health_check(self) -> dict[str, bool]:
        """Return connectivity status for each storage layer."""
        redis_ok = False
        try:
            redis_ok = await self._redis.ping()
        except Exception as exc:
            logger.warning("health_check: Redis ping failed: %s", exc)

        duckdb_ok = False
        try:
            count = await self._duckdb.count_session_artifacts(
                self.session_id, include_archived=True
            )
            duckdb_ok = isinstance(count, int)
        except Exception as exc:
            logger.warning("health_check: DuckDB probe failed: %s", exc)

        chroma_ok = False
        try:
            n = await self._chroma.count()
            chroma_ok = isinstance(n, int)
        except Exception as exc:
            logger.warning("health_check: ChromaDB probe failed: %s", exc)

        sqlite_ok = False
        try:
            tasks = await self._sqlite.get_tasks_by_status("pending", limit=1)
            sqlite_ok = isinstance(tasks, list)
        except Exception as exc:
            logger.warning("health_check: SQLite probe failed: %s", exc)

        return {
            "redis_working_memory": redis_ok,
            "duckdb_session_memory": duckdb_ok,
            "chroma_research_archive": chroma_ok,
            "sqlite_task_registry": sqlite_ok,
            "connected": self._connected,
        }

    async def stats(self, session_id: str | None = None) -> dict[str, Any]:
        """Return high-level memory stats for a session."""
        sid = session_id or self.session_id
        artifact_count = await self._duckdb.count_session_artifacts(sid)
        archived_count = await self._duckdb.count_session_artifacts(sid, include_archived=True)
        research_count = await self._chroma.count()
        context_keys = await self._redis.keys(sid)

        return {
            "session_id": sid,
            "artifacts_active": artifact_count,
            "artifacts_total_inc_archived": archived_count,
            "research_notes_total": research_count,
            "working_memory_keys": len(context_keys),
        }
