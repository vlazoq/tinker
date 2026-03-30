"""
storage.py — Thin async adapters for each memory layer.

Each adapter owns one connection / client and exposes only the primitives
that MemoryManager needs.  The manager itself handles cross-layer logic.

Adapters
--------
RedisAdapter   → Working Memory  (ephemeral, key-value)
DuckDBAdapter  → Session Memory  (structured artifacts, current run)
ChromaAdapter  → Research Archive (vector search, cross-session)
SQLiteAdapter  → Task Registry   (durable task log, all time)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Redis — Working Memory
# ---------------------------------------------------------------------------


class RedisAdapter:
    """
    Async Redis adapter for ephemeral per-task context.

    Keys are namespaced:  tinker:<session_id>:<key>

    If Redis is unavailable (not installed, not running, wrong URL), all
    operations silently no-op and ``available`` returns False.  This lets
    the orchestrator run on Windows or in minimal environments without Redis,
    at the cost of per-task working memory (context is lost between loops).
    """

    def __init__(self, url: str, default_ttl: int = 3600):
        self.url = url
        self.default_ttl = default_ttl
        self._client = None
        # In-process fallback used when Redis is unavailable.
        # Maps  "<session_id>:<key>"  →  (value, expiry_monotonic_seconds)
        # expiry = None means the entry never expires.
        self._fallback: dict[str, tuple[Any, float | None]] = {}

    @property
    def available(self) -> bool:
        return self._client is not None

    def _fallback_sweep(self) -> None:
        """Remove expired entries from the fallback dict (O(n) sweep)."""
        now = time.monotonic()
        expired = [k for k, (_, exp) in self._fallback.items() if exp is not None and now > exp]
        for k in expired:
            del self._fallback[k]

    async def connect(self) -> None:
        try:
            import redis.asyncio as aioredis  # type: ignore

            client = await aioredis.from_url(self.url, encoding="utf-8", decode_responses=True)
            await client.ping()  # verify reachability before accepting
            self._client = client
            logger.info("Redis connected at %s", self.url)
        except ImportError:
            logger.info(
                "RedisAdapter: redis package not installed — "
                "working memory disabled (run: pip install redis)"
            )
        except Exception as exc:
            logger.warning(
                "RedisAdapter: Redis not reachable at %s (%s) — "
                "working memory disabled. "
                "On Windows: start Redis with 'docker compose up -d'",
                self.url,
                exc,
            )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    def _key(self, session_id: str, key: str) -> str:
        return f"tinker:{session_id}:{key}"

    async def set(
        self,
        session_id: str,
        key: str,
        value: Any,
        ttl: int | None = None,
    ) -> None:
        effective_ttl = ttl if ttl is not None else self.default_ttl
        if not self._client:
            # Fallback path: store in-process with TTL eviction
            self._fallback_sweep()
            fkey = self._key(session_id, key)
            expiry = (time.monotonic() + effective_ttl) if effective_ttl > 0 else None
            self._fallback[fkey] = (value, expiry)
            return
        payload = json.dumps(value)
        if effective_ttl > 0:
            await self._client.setex(self._key(session_id, key), effective_ttl, payload)
        else:
            await self._client.set(self._key(session_id, key), payload)

    async def get(self, session_id: str, key: str) -> Any | None:
        if not self._client:
            # Fallback path: check in-process dict, evicting expired entries
            self._fallback_sweep()
            fkey = self._key(session_id, key)
            entry = self._fallback.get(fkey)
            if entry is None:
                return None
            value, expiry = entry
            if expiry is not None and time.monotonic() > expiry:
                del self._fallback[fkey]
                return None
            return value
        raw = await self._client.get(self._key(session_id, key))
        return json.loads(raw) if raw is not None else None

    async def delete(self, session_id: str, key: str) -> None:
        if not self._client:
            self._fallback.pop(self._key(session_id, key), None)
            return
        await self._client.delete(self._key(session_id, key))

    async def keys(self, session_id: str) -> list[str]:
        """Return all keys for a session (strips the namespace prefix)."""
        if not self._client:
            self._fallback_sweep()
            prefix = f"tinker:{session_id}:"
            return [k[len(prefix) :] for k in self._fallback if k.startswith(prefix)]
        prefix = f"tinker:{session_id}:"
        raw_keys = await self._client.keys(f"{prefix}*")
        return [k[len(prefix) :] for k in raw_keys]

    async def flush_session(self, session_id: str) -> int:
        """Delete every key belonging to a session. Returns count deleted."""
        if not self._client:
            prefix = f"tinker:{session_id}:"
            to_del = [k for k in self._fallback if k.startswith(prefix)]
            for k in to_del:
                del self._fallback[k]
            return len(to_del)
        ks = await self._client.keys(f"tinker:{session_id}:*")
        if ks:
            return await self._client.delete(*ks)
        return 0

    async def ping(self) -> bool:
        if not self._client:
            return False
        try:
            return await self._client.ping()
        except Exception:
            return False


# ---------------------------------------------------------------------------
# DuckDB — Session Memory
# ---------------------------------------------------------------------------

_DUCKDB_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    id           TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    task_id      TEXT,
    artifact_type TEXT NOT NULL,
    content      TEXT NOT NULL,
    metadata     TEXT NOT NULL DEFAULT '{}',
    archived     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_session
    ON artifacts(session_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_type
    ON artifacts(session_id, artifact_type);
CREATE INDEX IF NOT EXISTS idx_artifacts_task
    ON artifacts(task_id);
"""


class DuckDBAdapter:
    """
    Async-compatible DuckDB adapter for Session Memory.

    DuckDB has no async driver, so all operations run in a thread-pool
    executor to keep the event loop free.
    """

    def __init__(self, path: str):
        self.path = path
        self._conn = None
        self._lock = asyncio.Lock()

    def _open(self):
        import duckdb  # type: ignore

        conn = duckdb.connect(self.path)
        conn.execute(_DUCKDB_SCHEMA)
        return conn

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        self._conn = await loop.run_in_executor(None, self._open)
        logger.info("DuckDB connected at %s", self.path)

    async def close(self) -> None:
        if self._conn:
            self._conn.close()

    async def _run(self, fn):
        """Execute a blocking DuckDB call in a thread-pool executor."""
        loop = asyncio.get_running_loop()
        async with self._lock:  # serialise writes
            return await loop.run_in_executor(None, fn)

    # -- Write --------------------------------------------------------------

    async def insert_artifact(self, artifact) -> None:
        d = artifact.to_dict()
        await self._run(
            lambda: self._conn.execute(
                """INSERT OR REPLACE INTO artifacts
               (id, session_id, task_id, artifact_type, content, metadata, archived, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    d["id"],
                    d["session_id"],
                    d.get("task_id"),
                    d["artifact_type"],
                    d["content"],
                    json.dumps(d.get("metadata", {})),
                    d.get("archived", False),
                    d["created_at"],
                ],
            )
        )

    async def mark_archived(self, artifact_ids: list[str]) -> None:
        if not artifact_ids:
            return
        placeholders = ",".join("?" * len(artifact_ids))
        await self._run(
            lambda: self._conn.execute(
                f"UPDATE artifacts SET archived = TRUE WHERE id IN ({placeholders})",
                artifact_ids,
            )
        )

    # -- Read ---------------------------------------------------------------

    async def get_artifact(self, artifact_id: str) -> dict | None:
        def _get():
            rows = self._conn.execute(
                "SELECT * FROM artifacts WHERE id = ?", [artifact_id]
            ).fetchall()
            cols = [d[0] for d in self._conn.description]
            return [dict(zip(cols, r, strict=False)) for r in rows]

        rows = await self._run(_get)
        return rows[0] if rows else None

    async def get_recent(
        self,
        session_id: str,
        artifact_type: str | None = None,
        limit: int = 20,
        include_archived: bool = False,
    ) -> list[dict]:
        def _query():
            clauses = ["session_id = ?"]
            params: list[Any] = [session_id]
            if artifact_type:
                clauses.append("artifact_type = ?")
                params.append(artifact_type)
            if not include_archived:
                clauses.append("archived = FALSE")
            where = " AND ".join(clauses)
            rows = self._conn.execute(
                f"SELECT * FROM artifacts WHERE {where} ORDER BY created_at DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
            cols = [d[0] for d in self._conn.description]
            return [dict(zip(cols, r, strict=False)) for r in rows]

        return await self._run(_query)

    async def count_session_artifacts(
        self, session_id: str, include_archived: bool = False
    ) -> int:
        def _count():
            q = "SELECT COUNT(*) FROM artifacts WHERE session_id = ?"
            if not include_archived:
                q += " AND archived = FALSE"
            return self._conn.execute(q, [session_id]).fetchone()[0]

        return await self._run(_count)

    async def get_by_task_id(
        self,
        task_id: str,
        limit: int = 5,
    ) -> list[dict]:
        """Return artifacts associated with a specific task, newest first."""

        def _query():
            rows = self._conn.execute(
                "SELECT * FROM artifacts WHERE task_id = ? ORDER BY created_at DESC LIMIT ?",
                [task_id, limit],
            ).fetchall()
            cols = [d[0] for d in self._conn.description]
            return [dict(zip(cols, r, strict=False)) for r in rows]

        return await self._run(_query)

    async def get_by_task_ids(
        self,
        task_ids: list,
        limit_each: int = 2,
    ) -> list[dict]:
        """
        Return up to ``limit_each`` artifacts per task_id for a list of IDs.

        Uses a single SQL query with an IN clause and a window function rather
        than issuing one query per task_id, which is dramatically faster when
        fetching the artifact batch for a meso synthesis.

        Parameters
        ----------
        task_ids   : Task UUID strings to look up.
        limit_each : Max artifacts returned per task_id.  Default 2 keeps
                     the total payload manageable for the Synthesizer context.
        """
        if not task_ids:
            return []

        def _query():
            placeholders = ",".join("?" * len(task_ids))
            # ROW_NUMBER() partitioned by task_id lets us get the N most
            # recent artifacts per task without a Python-side groupby.
            rows = self._conn.execute(
                f"""
                SELECT *
                FROM (
                    SELECT *,
                           ROW_NUMBER() OVER (
                               PARTITION BY task_id
                               ORDER BY created_at DESC
                           ) AS _rn
                    FROM artifacts
                    WHERE task_id IN ({placeholders})
                )
                WHERE _rn <= ?
                ORDER BY created_at DESC
                """,
                [*task_ids, limit_each],
            ).fetchall()
            # Strip the synthetic _rn column; return same shape as other queries.
            all_cols = [d[0] for d in self._conn.description]
            return [
                {col: row[i] for i, col in enumerate(all_cols) if col != "_rn"} for row in rows
            ]

        return await self._run(_query)

    async def get_old_artifacts(
        self, session_id: str, older_than: datetime, limit: int = 100
    ) -> list[dict]:
        def _query():
            rows = self._conn.execute(
                """SELECT * FROM artifacts
                   WHERE session_id = ? AND archived = FALSE AND created_at < ?
                   ORDER BY created_at ASC LIMIT ?""",
                [session_id, older_than.isoformat(), limit],
            ).fetchall()
            cols = [d[0] for d in self._conn.description]
            return [dict(zip(cols, r, strict=False)) for r in rows]

        return await self._run(_query)


# ---------------------------------------------------------------------------
# ChromaDB — Research Archive
# ---------------------------------------------------------------------------


class _InMemoryChromaFallback:
    """
    Minimal in-memory replacement for a ChromaDB collection.

    This fallback activates when ChromaDB cannot be initialised (missing
    package, corrupt data directory, permission errors, etc.).  It stores
    documents in a plain Python list and performs brute-force cosine
    similarity search.

    Limitations compared to real ChromaDB
    --------------------------------------
    - No persistence — data is lost when the process exits.
    - O(n) search instead of HNSW approximate nearest-neighbour.
    - No ``where`` filter support (all documents are always searched).

    These trade-offs are acceptable for a fallback: the system stays
    functional and can still run research loops, and the operator is
    warned via logging to fix the ChromaDB installation.
    """

    def __init__(self) -> None:
        # Keyed by doc ID for O(1) upsert.
        # Each value: {"id": str, "document": str, "embedding": list[float], "metadata": dict}
        self._store: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """Pure-Python cosine similarity (no numpy dependency)."""
        if not a or not b or len(a) != len(b):
            return 0.0
        import math

        dot = sum(x * y for x, y in zip(a, b, strict=False))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return dot / (norm_a * norm_b)

    def upsert(
        self,
        ids: list[str],
        documents: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict],
    ) -> None:
        """Insert or update documents (mirrors ChromaDB Collection.upsert)."""
        for doc_id, doc, emb, meta in zip(ids, documents, embeddings, metadatas, strict=False):
            self._store[doc_id] = {
                "id": doc_id,
                "document": doc,
                "embedding": emb,
                "metadata": meta,
            }

    def query(
        self,
        query_embeddings: list[list[float]],
        n_results: int = 5,
        include: list[str] | None = None,
        where: dict | None = None,
    ) -> dict:
        """
        Brute-force cosine similarity search (mirrors ChromaDB Collection.query).

        Returns the same nested-list structure that ChromaDB uses:
        ``{"ids": [[...]], "documents": [[...]], "metadatas": [[...]], "distances": [[...]]}``.

        ChromaDB distances are L2 by default, but we configured the collection
        with cosine space.  In cosine space ChromaDB returns ``1 - similarity``
        as the distance, so we do the same here.
        """
        query_emb = query_embeddings[0]
        scored = []
        for entry in self._store.values():
            sim = self._cosine_similarity(query_emb, entry["embedding"])
            # ChromaDB cosine distance = 1 - similarity
            scored.append((1.0 - sim, entry))
        scored.sort(key=lambda x: x[0])
        top = scored[:n_results]

        return {
            "ids": [[e["id"] for _, e in top]],
            "documents": [[e["document"] for _, e in top]],
            "metadatas": [[e["metadata"] for _, e in top]],
            "distances": [[d for d, _ in top]],
        }

    def get(
        self,
        ids: list[str],
        include: list[str] | None = None,
    ) -> dict:
        """Retrieve documents by ID (mirrors ChromaDB Collection.get)."""
        found_ids = []
        found_docs = []
        found_metas = []
        for doc_id in ids:
            entry = self._store.get(doc_id)
            if entry:
                found_ids.append(entry["id"])
                found_docs.append(entry["document"])
                found_metas.append(entry["metadata"])
        return {
            "ids": found_ids,
            "documents": found_docs,
            "metadatas": found_metas,
        }

    def count(self) -> int:
        return len(self._store)


class ChromaAdapter:
    """
    Async-compatible ChromaDB adapter for the Research Archive.
    ChromaDB is sync-only; calls run in a thread-pool executor.

    Fallback behaviour
    ------------------
    If ChromaDB cannot be initialised (package not installed, data directory
    issues, etc.), the adapter automatically falls back to an in-memory
    implementation using ``_InMemoryChromaFallback``.  This lets the system
    continue running — albeit without persistence for the Research Archive —
    rather than crashing on startup.  A WARNING is logged so the operator
    knows to fix the underlying ChromaDB issue.
    """

    def __init__(self, path: str, collection_name: str):
        self.path = path
        self.collection_name = collection_name
        self._client = None
        self._collection = None
        # Tracks whether we are using the in-memory fallback instead of
        # a real ChromaDB collection.  Useful for diagnostics / health checks.
        self._using_fallback = False

    def _open(self):
        import chromadb  # type: ignore

        client = chromadb.PersistentClient(path=self.path)
        collection = client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        return client, collection

    async def connect(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            self._client, self._collection = await loop.run_in_executor(None, self._open)
            logger.info(
                "ChromaDB connected at %s (collection: %s)",
                self.path,
                self.collection_name,
            )
        except Exception as exc:
            # ── Fallback to in-memory implementation ──────────────────
            # This keeps the system functional even when ChromaDB is broken
            # or missing.  The operator should fix the root cause (install
            # chromadb, check permissions, etc.) but meanwhile Tinker can
            # still run research loops with ephemeral vector storage.
            logger.warning(
                "ChromaDB connection failed (%s). Falling back to in-memory "
                "vector store. Research Archive will NOT persist across restarts. "
                "Fix: install chromadb (`pip install chromadb`) or check "
                "data directory permissions at %s.",
                exc,
                self.path,
            )
            self._collection = _InMemoryChromaFallback()
            self._client = None
            self._using_fallback = True

    async def close(self) -> None:
        pass  # ChromaDB PersistentClient flushes on GC

    async def _run(self, fn):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn)

    # -- Write --------------------------------------------------------------

    async def upsert(
        self,
        doc_id: str,
        document: str,
        embedding: list[float],
        metadata: dict,
    ) -> None:
        await self._run(
            lambda: self._collection.upsert(
                ids=[doc_id],
                documents=[document],
                embeddings=[embedding],
                metadatas=[metadata],
            )
        )

    # -- Read ---------------------------------------------------------------

    async def query(
        self,
        embedding: list[float],
        n_results: int = 5,
        where: dict | None = None,
    ) -> list[dict]:
        def _q():
            kwargs: dict[str, Any] = dict(
                query_embeddings=[embedding],
                n_results=min(n_results, self._collection.count() or 1),
                include=["documents", "metadatas", "distances"],
            )
            if where:
                kwargs["where"] = where
            return self._collection.query(**kwargs)

        result = await self._run(_q)
        out = []
        for i, doc_id in enumerate(result["ids"][0]):
            out.append(
                {
                    "id": doc_id,
                    "document": result["documents"][0][i],
                    "metadata": result["metadatas"][0][i],
                    "distance": result["distances"][0][i],
                }
            )
        return out

    async def get_by_id(self, doc_id: str) -> dict | None:
        def _get():
            return self._collection.get(ids=[doc_id], include=["documents", "metadatas"])

        result = await self._run(_get)
        if not result["ids"]:
            return None
        return {
            "id": result["ids"][0],
            "document": result["documents"][0],
            "metadata": result["metadatas"][0],
        }

    async def count(self) -> int:
        return await self._run(lambda: self._collection.count())


# ---------------------------------------------------------------------------
# SQLite — Task Registry
# ---------------------------------------------------------------------------

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL,
    priority        INTEGER NOT NULL DEFAULT 5,
    status          TEXT NOT NULL DEFAULT 'pending',
    parent_task_id  TEXT,
    session_id      TEXT NOT NULL DEFAULT '',
    result          TEXT,
    error           TEXT,
    metadata        TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    completed_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_status   ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_session  ON tasks(session_id);
CREATE INDEX IF NOT EXISTS idx_tasks_parent   ON tasks(parent_task_id);
"""


class SQLiteAdapter:
    """
    Async-compatible SQLite adapter for the Task Registry.
    Uses aiosqlite for genuine async I/O.

    Write retry
    -----------
    SQLite uses file-level locking.  When multiple async tasks write
    concurrently (e.g. the orchestrator storing tasks while compression
    runs), a ``sqlite3.OperationalError: database is locked`` can occur.

    All write operations use ``_retry_write()`` which catches lock errors
    and retries up to 3 times with exponential backoff (100ms, 200ms, 400ms).
    This is enough to resolve contention in virtually all homelab scenarios.
    """

    # Retry settings for "database is locked" errors
    _LOCK_RETRY_DELAYS = [0.1, 0.2, 0.4]  # seconds: 100ms, 200ms, 400ms

    def __init__(self, path: str):
        self.path = path
        self._conn = None

    async def connect(self) -> None:
        import aiosqlite  # type: ignore

        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SQLITE_SCHEMA)
        await self._conn.commit()
        logger.info("SQLite connected at %s", self.path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    async def _retry_write(self, write_fn) -> None:
        """
        Execute a write operation with retry logic for SQLite lock errors.

        SQLite allows only one writer at a time.  When another connection
        (or the same connection from a different async task) holds the lock,
        SQLite raises ``sqlite3.OperationalError: database is locked``.

        This helper catches that specific error and retries with increasing
        delays (100ms → 200ms → 400ms).  If all retries are exhausted, the
        original exception is re-raised so the caller can handle it.

        Parameters
        ----------
        write_fn : async callable
            An async function that performs the write + commit.
        """
        import sqlite3

        for attempt, delay in enumerate(self._LOCK_RETRY_DELAYS):
            try:
                await write_fn()
                return  # Success — exit immediately
            except sqlite3.OperationalError as exc:
                if "database is locked" not in str(exc):
                    raise  # Not a lock error — re-raise immediately
                logger.warning(
                    "[SQLiteAdapter] Database locked on write attempt %d/%d. Retrying in %.0fms…",
                    attempt + 1,
                    len(self._LOCK_RETRY_DELAYS),
                    delay * 1000,
                )
                await asyncio.sleep(delay)

        # Final attempt (no more retries after this)
        await write_fn()

    # -- Write --------------------------------------------------------------

    async def upsert_task(self, task) -> None:
        d = task.to_dict()

        async def _do_write():
            await self._conn.execute(
                """INSERT OR REPLACE INTO tasks
                   (id, title, description, priority, status, parent_task_id,
                    session_id, result, error, metadata, created_at, updated_at, completed_at)
                   VALUES (:id, :title, :description, :priority, :status, :parent_task_id,
                    :session_id, :result, :error, :metadata, :created_at, :updated_at, :completed_at)""",
                d,
            )
            await self._conn.commit()

        await self._retry_write(_do_write)

    async def update_task_status(
        self,
        task_id: str,
        status: str,
        result: str | None = None,
        error: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        completed_at = now if status in ("completed", "failed", "archived") else None

        async def _do_write():
            await self._conn.execute(
                """UPDATE tasks SET status = ?, result = ?, error = ?,
                   updated_at = ?, completed_at = ?
                   WHERE id = ?""",
                [status, result, error, now, completed_at, task_id],
            )
            await self._conn.commit()

        await self._retry_write(_do_write)

    # -- Read ---------------------------------------------------------------

    async def get_task(self, task_id: str) -> dict | None:
        async with self._conn.execute("SELECT * FROM tasks WHERE id = ?", [task_id]) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_tasks_by_status(self, status: str, limit: int = 50) -> list[dict]:
        async with self._conn.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY priority DESC, created_at ASC LIMIT ?",
            [status, limit],
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_tasks_by_session(self, session_id: str, limit: int = 200) -> list[dict]:
        async with self._conn.execute(
            "SELECT * FROM tasks WHERE session_id = ? ORDER BY created_at ASC LIMIT ?",
            [session_id, limit],
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_child_tasks(self, parent_task_id: str) -> list[dict]:
        async with self._conn.execute(
            "SELECT * FROM tasks WHERE parent_task_id = ? ORDER BY created_at ASC",
            [parent_task_id],
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]
