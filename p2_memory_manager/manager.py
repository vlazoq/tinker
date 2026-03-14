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
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable, Optional

from .schemas import Artifact, ArtifactType, ResearchNote, Task, TaskStatus, MemoryConfig
from .storage import RedisAdapter, DuckDBAdapter, ChromaAdapter, SQLiteAdapter
from .embeddings import EmbeddingPipeline
from .compression import MemoryCompressor

logger = logging.getLogger(__name__)


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
        self.session_id = session_id or f"session-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"

        # Storage adapters
        self._redis  = RedisAdapter(self.config.redis_url, self.config.redis_default_ttl)
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

        After writing, optionally triggers compression if the session
        has grown beyond the configured threshold.

        Returns the stored Artifact (with its assigned id).
        """
        sid = session_id or self.session_id
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

    async def get_artifact(self, artifact_id: str) -> Optional[Artifact]:
        """Retrieve an artifact by its UUID. Returns None if not found."""
        row = await self._duckdb.get_artifact(artifact_id)
        if not row:
            return None
        row["metadata"] = row.get("metadata") or {}
        if isinstance(row["metadata"], str):
            import json
            row["metadata"] = json.loads(row["metadata"])
        return Artifact.from_dict(row)

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
        result = []
        for row in rows:
            if isinstance(row.get("metadata"), str):
                import json
                row["metadata"] = json.loads(row["metadata"])
            result.append(Artifact.from_dict(row))
        return result

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
        return ResearchNote.from_chroma(result["id"], result["document"], result["metadata"])

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
        await self._sqlite.update_task_status(
            task_id, status.value, result, error
        )

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
        except Exception:
            pass

        duckdb_ok = False
        try:
            count = await self._duckdb.count_session_artifacts(self.session_id, include_archived=True)
            duckdb_ok = isinstance(count, int)
        except Exception:
            pass

        chroma_ok = False
        try:
            n = await self._chroma.count()
            chroma_ok = isinstance(n, int)
        except Exception:
            pass

        sqlite_ok = False
        try:
            tasks = await self._sqlite.get_tasks_by_status("pending", limit=1)
            sqlite_ok = isinstance(tasks, list)
        except Exception:
            pass

        return {
            "redis_working_memory": redis_ok,
            "duckdb_session_memory": duckdb_ok,
            "chroma_research_archive": chroma_ok,
            "sqlite_task_registry": sqlite_ok,
        }

    async def stats(self, session_id: Optional[str] = None) -> dict[str, Any]:
        """Return high-level memory stats for a session."""
        sid = session_id or self.session_id
        artifact_count = await self._duckdb.count_session_artifacts(sid)
        archived_count = await self._duckdb.count_session_artifacts(sid, include_archived=True)
        research_count = await self._chroma.count()
        context_keys   = await self._redis.keys(sid)

        return {
            "session_id": sid,
            "artifacts_active": artifact_count,
            "artifacts_total_inc_archived": archived_count,
            "research_notes_total": research_count,
            "working_memory_keys": len(context_keys),
        }
