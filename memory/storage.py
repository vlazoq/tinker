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
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Redis — Working Memory
# ---------------------------------------------------------------------------

class RedisAdapter:
    """
    Async Redis adapter for ephemeral per-task context.

    Keys are namespaced:  tinker:<session_id>:<key>
    """

    def __init__(self, url: str, default_ttl: int = 3600):
        self.url = url
        self.default_ttl = default_ttl
        self._client = None

    async def connect(self) -> None:
        import redis.asyncio as aioredis        # type: ignore
        self._client = await aioredis.from_url(
            self.url, encoding="utf-8", decode_responses=True
        )
        logger.info("Redis connected at %s", self.url)

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
        ttl: Optional[int] = None,
    ) -> None:
        payload = json.dumps(value)
        ttl = ttl if ttl is not None else self.default_ttl
        if ttl > 0:
            await self._client.setex(self._key(session_id, key), ttl, payload)
        else:
            await self._client.set(self._key(session_id, key), payload)

    async def get(self, session_id: str, key: str) -> Optional[Any]:
        raw = await self._client.get(self._key(session_id, key))
        return json.loads(raw) if raw is not None else None

    async def delete(self, session_id: str, key: str) -> None:
        await self._client.delete(self._key(session_id, key))

    async def keys(self, session_id: str) -> list[str]:
        """Return all keys for a session (strips the namespace prefix)."""
        prefix = f"tinker:{session_id}:"
        raw_keys = await self._client.keys(f"{prefix}*")
        return [k[len(prefix):] for k in raw_keys]

    async def flush_session(self, session_id: str) -> int:
        """Delete every key belonging to a session. Returns count deleted."""
        ks = await self._client.keys(f"tinker:{session_id}:*")
        if ks:
            return await self._client.delete(*ks)
        return 0

    async def ping(self) -> bool:
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
        import duckdb                            # type: ignore
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
        async with self._lock:                  # serialise writes
            return await loop.run_in_executor(None, fn)

    # -- Write --------------------------------------------------------------

    async def insert_artifact(self, artifact) -> None:
        d = artifact.to_dict()
        await self._run(lambda: self._conn.execute(
            """INSERT OR REPLACE INTO artifacts
               (id, session_id, task_id, artifact_type, content, metadata, archived, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                d["id"], d["session_id"], d.get("task_id"),
                d["artifact_type"], d["content"],
                json.dumps(d.get("metadata", {})),
                d.get("archived", False),
                d["created_at"],
            ],
        ))

    async def mark_archived(self, artifact_ids: list[str]) -> None:
        if not artifact_ids:
            return
        placeholders = ",".join("?" * len(artifact_ids))
        await self._run(lambda: self._conn.execute(
            f"UPDATE artifacts SET archived = TRUE WHERE id IN ({placeholders})",
            artifact_ids,
        ))

    # -- Read ---------------------------------------------------------------

    async def get_artifact(self, artifact_id: str) -> Optional[dict]:
        def _get():
            rows = self._conn.execute(
                "SELECT * FROM artifacts WHERE id = ?", [artifact_id]
            ).fetchall()
            cols = [d[0] for d in self._conn.description]
            return [dict(zip(cols, r)) for r in rows]

        rows = await self._run(_get)
        return rows[0] if rows else None

    async def get_recent(
        self,
        session_id: str,
        artifact_type: Optional[str] = None,
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
                params + [limit],
            ).fetchall()
            cols = [d[0] for d in self._conn.description]
            return [dict(zip(cols, r)) for r in rows]

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
            return [dict(zip(cols, r)) for r in rows]

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
            return [dict(zip(cols, r)) for r in rows]

        return await self._run(_query)


# ---------------------------------------------------------------------------
# ChromaDB — Research Archive
# ---------------------------------------------------------------------------

class ChromaAdapter:
    """
    Async-compatible ChromaDB adapter for the Research Archive.
    ChromaDB is sync-only; calls run in a thread-pool executor.
    """

    def __init__(self, path: str, collection_name: str):
        self.path = path
        self.collection_name = collection_name
        self._client = None
        self._collection = None

    def _open(self):
        import chromadb                          # type: ignore
        client = chromadb.PersistentClient(path=self.path)
        collection = client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        return client, collection

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        self._client, self._collection = await loop.run_in_executor(None, self._open)
        logger.info(
            "ChromaDB connected at %s (collection: %s)", self.path, self.collection_name
        )

    async def close(self) -> None:
        pass   # ChromaDB PersistentClient flushes on GC

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
        await self._run(lambda: self._collection.upsert(
            ids=[doc_id],
            documents=[document],
            embeddings=[embedding],
            metadatas=[metadata],
        ))

    # -- Read ---------------------------------------------------------------

    async def query(
        self,
        embedding: list[float],
        n_results: int = 5,
        where: Optional[dict] = None,
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
            out.append({
                "id": doc_id,
                "document": result["documents"][0][i],
                "metadata": result["metadatas"][0][i],
                "distance": result["distances"][0][i],
            })
        return out

    async def get_by_id(self, doc_id: str) -> Optional[dict]:
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
    """

    def __init__(self, path: str):
        self.path = path
        self._conn = None

    async def connect(self) -> None:
        import aiosqlite                         # type: ignore
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SQLITE_SCHEMA)
        await self._conn.commit()
        logger.info("SQLite connected at %s", self.path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    # -- Write --------------------------------------------------------------

    async def upsert_task(self, task) -> None:
        d = task.to_dict()
        await self._conn.execute(
            """INSERT OR REPLACE INTO tasks
               (id, title, description, priority, status, parent_task_id,
                session_id, result, error, metadata, created_at, updated_at, completed_at)
               VALUES (:id, :title, :description, :priority, :status, :parent_task_id,
                :session_id, :result, :error, :metadata, :created_at, :updated_at, :completed_at)""",
            d,
        )
        await self._conn.commit()

    async def update_task_status(
        self,
        task_id: str,
        status: str,
        result: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        completed_at = now if status in ("completed", "failed", "archived") else None
        await self._conn.execute(
            """UPDATE tasks SET status = ?, result = ?, error = ?,
               updated_at = ?, completed_at = ?
               WHERE id = ?""",
            [status, result, error, now, completed_at, task_id],
        )
        await self._conn.commit()

    # -- Read ---------------------------------------------------------------

    async def get_task(self, task_id: str) -> Optional[dict]:
        async with self._conn.execute(
            "SELECT * FROM tasks WHERE id = ?", [task_id]
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_tasks_by_status(
        self, status: str, limit: int = 50
    ) -> list[dict]:
        async with self._conn.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY priority DESC, created_at ASC LIMIT ?",
            [status, limit],
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_tasks_by_session(
        self, session_id: str, limit: int = 200
    ) -> list[dict]:
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
