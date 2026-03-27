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
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable, Optional, Protocol, runtime_checkable

from .schemas import (
    Artifact,
    ArtifactType,
    ResearchNote,
    Task,
    TaskStatus,
    MemoryConfig,
)
from .storage import RedisAdapter, DuckDBAdapter, ChromaAdapter, SQLiteAdapter
from .embeddings import EmbeddingPipeline
from .compression import MemoryCompressor, _cosine_similarity

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
        task_id: Optional[str] = None,
        metadata: Optional[dict] = None,
        session_id: Optional[str] = None,
        auto_compress: bool = True,
    ) -> Artifact: ...

    async def get_artifact(self, artifact_id: str) -> Optional[Artifact]: ...

    async def get_recent_artifacts(
        self,
        artifact_type: Optional[ArtifactType] = None,
        limit: int = 20,
        include_archived: bool = False,
        session_id: Optional[str] = None,
    ) -> list[Artifact]: ...


@runtime_checkable
class ResearchStore(Protocol):
    """Minimal interface for storing and searching research notes."""

    async def store_research(
        self,
        content: str,
        topic: str,
        tags: Optional[list[str]] = None,
        source: str = "tinker-internal",
        task_id: Optional[str] = None,
        metadata: Optional[dict] = None,
        session_id: Optional[str] = None,
    ) -> ResearchNote: ...

    async def search_research(
        self,
        query: str,
        n_results: int = 5,
        filter_topic: Optional[str] = None,
        filter_session: Optional[str] = None,
    ) -> list[ResearchNote]: ...


@runtime_checkable
class WorkingMemory(Protocol):
    """Minimal interface for ephemeral per-task context storage."""

    async def set_context(
        self, key: str, value: Any, ttl: Optional[int] = None, session_id: Optional[str] = None
    ) -> None: ...

    async def get_context(self, key: str, session_id: Optional[str] = None) -> Optional[Any]: ...

    async def delete_context(self, key: str, session_id: Optional[str] = None) -> None: ...


@runtime_checkable
class TaskStore(Protocol):
    """Minimal interface for task persistence."""

    async def store_task(self, task: Task) -> Task: ...

    async def get_task(self, task_id: str) -> Optional[Task]: ...

    async def update_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        result: Optional[str] = None,
        error: Optional[str] = None,
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


class MemoryManager:
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
        config: Optional[MemoryConfig] = None,
        session_id: Optional[str] = None,
        summariser: Optional[Callable[[str], Awaitable[str]]] = None,
    ):
        self.config = config or MemoryConfig()
        self.session_id = (
            session_id
            or f"session-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
        )

        # Storage adapters
        self._redis = RedisAdapter(self.config.redis_url, self.config.redis_default_ttl)
        self._duckdb = DuckDBAdapter(self.config.duckdb_path)
        self._chroma = ChromaAdapter(
            self.config.chroma_path, self.config.chroma_collection
        )
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

    async def __aenter__(self) -> "MemoryManager":
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Working Memory — Redis
    # ------------------------------------------------------------------

    async def set_context(
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = None,
        session_id: Optional[str] = None,
    ) -> None:
        """
        Store an ephemeral key-value pair in Working Memory (Redis).

        Typical uses: current task state, in-progress reasoning, short-lived flags.
        """
        sid = session_id or self.session_id
        await self._redis.set(sid, key, value, ttl)

    async def get_context(
        self,
        key: str,
        session_id: Optional[str] = None,
    ) -> Optional[Any]:
        """Retrieve a value from Working Memory. Returns None if missing / expired."""
        sid = session_id or self.session_id
        return await self._redis.get(sid, key)

    async def delete_context(self, key: str, session_id: Optional[str] = None) -> None:
        sid = session_id or self.session_id
        await self._redis.delete(sid, key)

    async def clear_working_memory(self, session_id: Optional[str] = None) -> int:
        """Flush all Working Memory keys for a session. Returns count deleted."""
        sid = session_id or self.session_id
        return await self._redis.flush_session(sid)

    async def list_context_keys(self, session_id: Optional[str] = None) -> list[str]:
        sid = session_id or self.session_id
        return await self._redis.keys(sid)

    # ------------------------------------------------------------------
    # Session Memory — DuckDB
    # ------------------------------------------------------------------

    async def store_artifact(
        self,
        content: str,
        artifact_type: ArtifactType = ArtifactType.RAW,
        task_id: Optional[str] = None,
        metadata: Optional[dict] = None,
        session_id: Optional[str] = None,
        auto_compress: bool = True,
    ) -> Artifact:
        """
        Persist an output artifact to Session Memory (DuckDB).

        Before writing, runs semantic deduplication: if the new content is
        near-identical (cosine similarity > 0.92) to a recent artifact, the
        new content is merged into the existing artifact instead of creating
        a duplicate.

        After writing, optionally triggers compression if the session
        has grown beyond the configured threshold.

        Returns the stored Artifact (with its assigned id).  When dedup merges
        into an existing artifact, the *existing* artifact is returned.
        """
        if not self._connected:
            logger.warning("store_artifact called before connect() — call connect() first")
        sid = session_id or self.session_id

        # ── Semantic deduplication ────────────────────────────────────
        # Check the last N artifacts for near-duplicates before inserting.
        # This prevents the session from accumulating redundant entries
        # (e.g. repeated micro-loop outputs for the same sub-problem).
        existing = await self._check_semantic_duplicate(content, sid)
        if existing is not None:
            return existing

        artifact = Artifact(
            content=content,
            artifact_type=artifact_type,
            session_id=sid,
            task_id=task_id,
            metadata=metadata or {},
        )
        await self._duckdb.insert_artifact(artifact)
        logger.debug("Stored artifact %s (type=%s)", artifact.id, artifact_type.value)

        if auto_compress:
            await self._compressor.maybe_compress(sid)

        return artifact

    async def _check_semantic_duplicate(
        self,
        new_content: str,
        session_id: str,
    ) -> Optional[Artifact]:
        """
        Check if *new_content* is semantically near-identical to any of the
        last ``_DEDUP_RECENT_WINDOW`` artifacts in the session.

        How it works
        ------------
        1. Fetch the N most recent (non-archived) artifacts from DuckDB.
        2. Embed the new content and each recent artifact's content.
        3. Compute cosine similarity between the new embedding and each
           existing embedding.
        4. If any similarity exceeds ``_DEDUP_SIMILARITY_THRESHOLD``, merge
           the new content into the existing artifact (append a separator
           and the new text) and return the updated Artifact.
        5. Otherwise return ``None`` to signal "no duplicate found".

        Returns
        -------
        Artifact or None
            The merged artifact if dedup triggered, otherwise None.
        """
        try:
            recent_rows = await self._duckdb.get_recent(
                session_id, limit=_DEDUP_RECENT_WINDOW, include_archived=False
            )
            if not recent_rows:
                return None

            # Embed the new content
            new_emb = await self._embeddings.embed(new_content)

            for row in recent_rows:
                existing_content = str(row.get("content", ""))
                if not existing_content:
                    continue

                existing_emb = await self._embeddings.embed(existing_content)
                similarity = _cosine_similarity(new_emb, existing_emb)

                if similarity > _DEDUP_SIMILARITY_THRESHOLD:
                    # Merge: append the new content to the existing artifact
                    merged_content = (
                        f"{existing_content}\n\n"
                        f"--- [merged duplicate, similarity={similarity:.3f}] ---\n\n"
                        f"{new_content}"
                    )
                    row_id = row["id"]
                    logger.info(
                        "[dedup] Merging new artifact into existing %s "
                        "(cosine_similarity=%.3f > %.3f). "
                        "Duplicate content appended instead of creating new entry.",
                        row_id,
                        similarity,
                        _DEDUP_SIMILARITY_THRESHOLD,
                    )

                    # Update the existing artifact in DuckDB with merged content.
                    # We reconstruct an Artifact with the same ID and upsert it.
                    _parse_row_metadata(row)
                    merged_artifact = Artifact(
                        content=merged_content,
                        artifact_type=ArtifactType(
                            row.get("artifact_type", "raw")
                        ),
                        session_id=session_id,
                        task_id=row.get("task_id"),
                        metadata=row.get("metadata", {}),
                    )
                    # Preserve the original artifact's ID so the upsert
                    # replaces it rather than creating a new row.
                    merged_artifact.id = row_id
                    await self._duckdb.insert_artifact(merged_artifact)
                    return merged_artifact

        except Exception as exc:
            # Dedup is best-effort — if embeddings fail or anything goes wrong,
            # fall through and store the artifact normally.
            logger.debug(
                "[dedup] Semantic dedup check failed (non-fatal): %s", exc
            )

        return None

    async def get_artifact(self, artifact_id: str) -> Optional[Artifact]:
        """Retrieve an artifact by its UUID. Returns None if not found."""
        row = await self._duckdb.get_artifact(artifact_id)
        if not row:
            return None
        return Artifact.from_dict(_parse_row_metadata(row))

    async def get_recent_artifacts(
        self,
        artifact_type: Optional[ArtifactType] = None,
        limit: int = 20,
        include_archived: bool = False,
        session_id: Optional[str] = None,
    ) -> list[Artifact]:
        """
        Return the most recent artifacts for the current (or specified) session.

        Optionally filter by ArtifactType.
        """
        sid = session_id or self.session_id
        type_val = artifact_type.value if artifact_type else None
        rows = await self._duckdb.get_recent(
            sid, artifact_type=type_val, limit=limit, include_archived=include_archived
        )
        return [Artifact.from_dict(_parse_row_metadata(row)) for row in rows]

    async def get_artifacts_by_task_ids(
        self,
        task_ids: list,
        limit_each: int = 2,
    ) -> list[dict]:
        """
        Return raw artifact dicts for a batch of task_ids in one DB round-trip.

        Used by the meso loop to supplement the subsystem-tag search with
        a targeted lookup of artifacts from known micro-loop task IDs.  This
        catches any artifacts whose subsystem metadata was missing or mismatched
        but whose task_id correctly identifies them as part of the batch.

        Returns plain dicts (not Artifact objects) so the meso loop can merge
        them directly with the dicts returned by ``get_artifacts()``.

        Parameters
        ----------
        task_ids   : List of task UUID strings from recent MicroLoopRecords.
        limit_each : Max artifacts per task_id (default 2).
        """
        if not task_ids:
            return []
        rows = await self._duckdb.get_by_task_ids(task_ids, limit_each=limit_each)
        return [_parse_row_metadata(row) for row in rows]

    async def get_artifacts_by_task(
        self,
        task_id: str,
        limit: int = 5,
    ) -> list[Artifact]:
        """Return artifacts stored under a specific task_id, newest first."""
        rows = await self._duckdb.get_by_task_id(task_id, limit=limit)
        return [Artifact.from_dict(_parse_row_metadata(row)) for row in rows]

    async def count_artifacts(
        self, session_id: Optional[str] = None, include_archived: bool = False
    ) -> int:
        sid = session_id or self.session_id
        return await self._duckdb.count_session_artifacts(sid, include_archived)

    # ------------------------------------------------------------------
    # Research Archive — ChromaDB
    # ------------------------------------------------------------------

    async def store_research(
        self,
        content: str,
        topic: str,
        tags: Optional[list[str]] = None,
        source: str = "tinker-internal",
        task_id: Optional[str] = None,
        metadata: Optional[dict] = None,
        session_id: Optional[str] = None,
    ) -> ResearchNote:
        """
        Embed and store a research note in the Research Archive (ChromaDB).

        Notes are semantically searchable across all sessions.
        Returns the stored ResearchNote (with its assigned id).
        """
        sid = session_id or self.session_id
        note = ResearchNote(
            content=content,
            topic=topic,
            source=source,
            tags=tags or [],
            session_id=sid,
            task_id=task_id,
            metadata=metadata or {},
        )
        embedding = await self._embeddings.embed(content)
        await self._chroma.upsert(
            doc_id=note.id,
            document=note.content,
            embedding=embedding,
            metadata=note.to_chroma_metadata(),
        )
        logger.debug("Stored research note %s (topic=%s)", note.id, topic)
        return note

    async def search_research(
        self,
        query: str,
        n_results: int = 5,
        filter_topic: Optional[str] = None,
        filter_session: Optional[str] = None,
    ) -> list[ResearchNote]:
        """
        Semantic search over the Research Archive.

        Parameters
        ----------
        query          : natural-language search string
        n_results      : max number of results
        filter_topic   : restrict to a specific topic (exact match)
        filter_session : restrict to notes from a specific session
        """
        embedding = await self._embeddings.embed(query)

        where: dict = {}
        if filter_topic:
            where["topic"] = {"$eq": filter_topic}
        if filter_session:
            where["session_id"] = {"$eq": filter_session}

        results = await self._chroma.query(
            embedding=embedding,
            n_results=n_results,
            where=where if where else None,
        )
        return [
            ResearchNote.from_chroma(r["id"], r["document"], r["metadata"])
            for r in results
        ]

    async def get_research(self, note_id: str) -> Optional[ResearchNote]:
        """Retrieve a specific research note by its ID."""
        result = await self._chroma.get_by_id(note_id)
        if not result:
            return None
        return ResearchNote.from_chroma(
            result["id"], result["document"], result["metadata"]
        )

    async def count_research_notes(self) -> int:
        return await self._chroma.count()

    # ------------------------------------------------------------------
    # Task Registry — SQLite
    # ------------------------------------------------------------------

    async def store_task(self, task: Task) -> Task:
        """
        Persist a task to the Task Registry (SQLite).

        If the task has no session_id set, the manager's session_id is used.
        Returns the task (for chaining).
        """
        if not task.session_id:
            task.session_id = self.session_id
        await self._sqlite.upsert_task(task)
        logger.debug("Stored task %s ('%s')", task.id, task.title)
        return task

    async def get_task(self, task_id: str) -> Optional[Task]:
        """Retrieve a task by its UUID. Returns None if not found."""
        row = await self._sqlite.get_task(task_id)
        return Task.from_dict(row) if row else None

    async def update_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        result: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Update a task's status, result, and/or error in the Task Registry."""
        await self._sqlite.update_task_status(task_id, status.value, result, error)

    async def get_pending_tasks(self, limit: int = 50) -> list[Task]:
        rows = await self._sqlite.get_tasks_by_status("pending", limit)
        return [Task.from_dict(r) for r in rows]

    async def get_running_tasks(self, limit: int = 50) -> list[Task]:
        rows = await self._sqlite.get_tasks_by_status("running", limit)
        return [Task.from_dict(r) for r in rows]

    async def get_session_tasks(
        self, session_id: Optional[str] = None, limit: int = 200
    ) -> list[Task]:
        sid = session_id or self.session_id
        rows = await self._sqlite.get_tasks_by_session(sid, limit)
        return [Task.from_dict(r) for r in rows]

    async def get_child_tasks(self, parent_task_id: str) -> list[Task]:
        rows = await self._sqlite.get_child_tasks(parent_task_id)
        return [Task.from_dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Orchestrator-facing convenience methods
    # (adapt the richer internal API to the flat dict interface the
    #  orchestrator and its loops expect)
    # ------------------------------------------------------------------

    async def get_artifacts(
        self,
        subsystem: str,
        limit: int = 10,
        session_id: Optional[str] = None,
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
        metadata = {
            k: v for k, v in document.items() if k not in ("synthesis", "content")
        }
        artifact = await self.store_artifact(
            content=content,
            artifact_type=ArtifactType.SUMMARY,
            task_id=document.get("task_id"),
            metadata=metadata,
        )
        return artifact.id

    async def get_all_documents(self, session_id: Optional[str] = None) -> list[dict]:
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
        filters: Optional[dict] = None,
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
                    r["metadata"].get("tags", "").split(",")
                    if r["metadata"].get("tags")
                    else []
                ),
                "snippet": r["document"][:300],
                "text": r["document"],
            }
            for r in raw_results
        ]

    # ------------------------------------------------------------------
    # Compression
    # ------------------------------------------------------------------

    async def compress(self, session_id: Optional[str] = None) -> int:
        """
        Run compression checks for a session.

        Archives artifacts that exceed the threshold or have aged out.
        Returns the number of artifacts archived.
        """
        sid = session_id or self.session_id
        return await self._compressor.maybe_compress(sid)

    async def compress_all(self, session_id: Optional[str] = None) -> int:
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

    async def stats(self, session_id: Optional[str] = None) -> dict[str, Any]:
        """Return high-level memory stats for a session."""
        sid = session_id or self.session_id
        artifact_count = await self._duckdb.count_session_artifacts(sid)
        archived_count = await self._duckdb.count_session_artifacts(
            sid, include_archived=True
        )
        research_count = await self._chroma.count()
        context_keys = await self._redis.keys(sid)

        return {
            "session_id": sid,
            "artifacts_active": artifact_count,
            "artifacts_total_inc_archived": archived_count,
            "research_notes_total": research_count,
            "working_memory_keys": len(context_keys),
        }
