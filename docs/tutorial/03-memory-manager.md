# Chapter 03 — The Memory Manager

## The Problem

The AI needs memory.  Not just "remember the last message" — it needs
*different kinds* of memory with different lifetimes and different access
patterns:

- **While working on one task:** scratch notes, intermediate results
- **Across tasks in the same session:** the artifacts each loop produced
- **Across sessions, searchable by meaning:** the research the AI has done
- **Forever, queryable by exact values:** what tasks exist, what their status is

One database cannot serve all four needs well.

---

## The Architecture Decision

We use four specialised stores, each with its own adapter class:

| Adapter | Backend | Lifespan | Access Pattern | Used For |
|---------|---------|---------|----------------|----------|
| `RedisAdapter` | Redis | Per-task (~1h TTL) | Key-value `get/set` | Working notes for current task |
| `DuckDBAdapter` | DuckDB | Per-session (~hours) | SQL analytics | Micro loop artifacts |
| `ChromaAdapter` | ChromaDB | Permanent | Vector similarity search | Research archive |
| `SQLiteAdapter` | SQLite | Permanent | SQL relational | Tasks, audit log, DLQ |

All four adapters are unified behind a single `MemoryManager` class that
the orchestrator talks to.  The orchestrator never knows which store it is
reading from.

```
Orchestrator
    │
    └── MemoryManager.store_artifact(...)
              │
              ├── DuckDB:  INSERT artifact row
              └── Chroma:  index by vector embedding
```

---

## Step 1 — Directory Structure

```
tinker/
  memory/
    __init__.py
    storage.py    ← the four adapters (this chapter)
    manager.py    ← the unified MemoryManager (this chapter)
```

---

## Step 2 — The Redis Adapter (Working Memory)

Redis is a very fast key-value store.  We use it to hold per-task context
that the AI might need mid-loop — things like "what did the Architect say
before we called the Researcher?"

```python
# tinker/memory/storage.py  (Part 1 of 4)

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


class RedisAdapter:
    """
    Async Redis adapter for ephemeral per-task context.

    Keys are namespaced:  tinker:<session_id>:<key>
    This prevents one session's data from colliding with another's.

    If Redis is unavailable, all methods silently no-op.
    This lets Tinker run on Windows or minimal environments without Redis.
    """

    def __init__(self, url: str, default_ttl: int = 3600) -> None:
        self.url = url
        self.default_ttl = default_ttl   # seconds until data expires
        self._client = None              # created lazily in connect()

    @property
    def available(self) -> bool:
        """True if Redis is connected and usable."""
        return self._client is not None

    async def connect(self) -> None:
        """Open a connection to Redis.  Non-fatal if Redis is not available."""
        try:
            import redis.asyncio as aioredis   # lazy import
            client = await aioredis.from_url(
                self.url, encoding="utf-8", decode_responses=True
            )
            await client.ping()    # verify the connection works
            self._client = client
            logger.info("Redis connected at %s", self.url)
        except ImportError:
            logger.info("redis package not installed — working memory disabled")
        except Exception as exc:
            logger.warning(
                "Redis not reachable at %s (%s) — working memory disabled. "
                "On Windows: run 'docker compose up -d'",
                self.url, exc,
            )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    def _key(self, session_id: str, key: str) -> str:
        """Build a namespaced key to avoid collisions between sessions."""
        return f"tinker:{session_id}:{key}"

    async def set(self, session_id: str, key: str, value: Any,
                  ttl: Optional[int] = None) -> None:
        if not self._client:
            return
        payload = json.dumps(value)      # serialize to JSON string
        ttl = ttl if ttl is not None else self.default_ttl
        if ttl > 0:
            await self._client.setex(self._key(session_id, key), ttl, payload)
        else:
            await self._client.set(self._key(session_id, key), payload)

    async def get(self, session_id: str, key: str) -> Optional[Any]:
        if not self._client:
            return None
        raw = await self._client.get(self._key(session_id, key))
        return json.loads(raw) if raw is not None else None

    async def delete(self, session_id: str, key: str) -> None:
        if not self._client:
            return
        await self._client.delete(self._key(session_id, key))

    async def flush_session(self, session_id: str) -> int:
        """Delete all keys belonging to a session. Returns how many were deleted."""
        if not self._client:
            return 0
        keys = await self._client.keys(f"tinker:{session_id}:*")
        if keys:
            return await self._client.delete(*keys)
        return 0
```

### Key concepts here

- **Lazy import:** `import redis.asyncio as aioredis` is inside `connect()`,
  not at the top of the file.  This means importing the file works even if
  the `redis` package isn't installed.
- **Graceful degradation:** every method checks `if not self._client: return`.
  If Redis failed to connect, all operations silently do nothing.
- **TTL (Time To Live):** Redis automatically deletes keys after `default_ttl`
  seconds.  This prevents working memory from growing forever.

---

## Step 3 — The DuckDB Adapter (Session Memory)

DuckDB is a fast analytics database embedded directly in your process
(like SQLite, but designed for analytical queries over large datasets).
We use it to store and query the artifacts each micro loop produces.

```python
# tinker/memory/storage.py  (Part 2 of 4 — add below RedisAdapter)

import duckdb   # pip install duckdb

# The SQL schema for our artifacts table
_DUCKDB_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    id            TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    task_id       TEXT,
    artifact_type TEXT NOT NULL,      -- e.g. "design", "critique", "synthesis"
    content       TEXT NOT NULL,      -- the actual AI output
    metadata      TEXT NOT NULL DEFAULT '{}',
    archived      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TEXT NOT NULL
);
"""


class DuckDBAdapter:
    """
    Async DuckDB adapter for session artifacts.

    DuckDB is synchronous but very fast, so we run it in a thread pool
    with asyncio.to_thread() to avoid blocking the event loop.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._con: duckdb.DuckDBPyConnection | None = None

    async def connect(self) -> None:
        # Run the synchronous connect in a thread
        self._con = await asyncio.to_thread(
            duckdb.connect, self.db_path
        )
        await asyncio.to_thread(self._con.execute, _DUCKDB_SCHEMA)
        logger.info("DuckDB connected at %s", self.db_path)

    async def close(self) -> None:
        if self._con:
            await asyncio.to_thread(self._con.close)
            self._con = None

    async def store_artifact(
        self,
        artifact_id: str,
        session_id: str,
        task_id: str,
        artifact_type: str,
        content: str,
        metadata: dict | None = None,
    ) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        meta_json = json.dumps(metadata or {})

        def _insert():
            self._con.execute(
                """INSERT INTO artifacts
                   (id, session_id, task_id, artifact_type, content, metadata, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (id) DO NOTHING""",
                [artifact_id, session_id, task_id, artifact_type, content, meta_json, ts]
            )
        await asyncio.to_thread(_insert)

    async def get_recent_artifacts(
        self,
        session_id: str,
        artifact_type: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Return the most recent artifacts for a session."""
        where = "WHERE session_id = ?"
        params = [session_id]
        if artifact_type:
            where += " AND artifact_type = ?"
            params.append(artifact_type)

        def _query():
            rows = self._con.execute(
                f"SELECT * FROM artifacts {where} ORDER BY created_at DESC LIMIT ?",
                params + [limit]
            ).fetchall()
            cols = [d[0] for d in self._con.description]
            return [dict(zip(cols, row)) for row in rows]

        return await asyncio.to_thread(_query)
```

---

## Step 4 — The ChromaDB Adapter (Research Archive)

ChromaDB is a vector database — it stores text as numerical embeddings
and lets you search by semantic similarity.  "Find me all previous
research about caching strategies" even if those documents never use the
exact word "caching".

```python
# tinker/memory/storage.py  (Part 3 of 4)

class ChromaAdapter:
    """
    Async ChromaDB adapter for the permanent research archive.

    ChromaDB converts text into vectors (lists of numbers that capture
    meaning) and stores them.  Similarity search finds documents whose
    meaning is close to a query, even if they use different words.
    """

    def __init__(self, persist_dir: str, collection_name: str = "tinker") -> None:
        self.persist_dir = persist_dir
        self.collection_name = collection_name
        self._collection = None

    async def connect(self) -> None:
        try:
            import chromadb   # lazy import
            client = await asyncio.to_thread(
                chromadb.PersistentClient, path=self.persist_dir
            )
            self._collection = await asyncio.to_thread(
                client.get_or_create_collection, self.collection_name
            )
            logger.info("ChromaDB connected at %s", self.persist_dir)
        except ImportError:
            logger.info("chromadb not installed — research archive disabled")
        except Exception as exc:
            logger.warning("ChromaDB unavailable: %s", exc)

    async def add_document(
        self,
        doc_id: str,
        text: str,
        metadata: dict | None = None,
    ) -> None:
        if not self._collection:
            return
        def _add():
            self._collection.upsert(
                ids=[doc_id],
                documents=[text],
                metadatas=[metadata or {}],
            )
        await asyncio.to_thread(_add)

    async def search(
        self,
        query: str,
        n_results: int = 5,
        where: dict | None = None,
    ) -> list[dict]:
        """Return the n_results most semantically similar documents."""
        if not self._collection:
            return []
        def _query():
            results = self._collection.query(
                query_texts=[query],
                n_results=n_results,
                where=where,
            )
            docs  = results.get("documents", [[]])[0]
            metas = results.get("metadatas", [[]])[0]
            ids   = results.get("ids", [[]])[0]
            return [
                {"id": i, "text": d, "metadata": m}
                for i, d, m in zip(ids, docs, metas)
            ]
        return await asyncio.to_thread(_query)
```

---

## Step 5 — The SQLite Adapter (Durable Relational Store)

SQLite is the most reliable option for anything that must survive a crash:
tasks, audit events, dead letter queue entries.

```python
# tinker/memory/storage.py  (Part 4 of 4)

class SQLiteAdapter:
    """
    Async SQLite adapter for durable relational data.

    We use asyncio.to_thread() because Python's sqlite3 is synchronous.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=5, check_same_thread=False)
        con.row_factory = sqlite3.Row   # rows behave like dicts
        con.execute("PRAGMA journal_mode=WAL")  # faster concurrent writes
        return con

    async def execute(self, sql: str, params: tuple = ()) -> bool:
        """Run an INSERT/UPDATE/DELETE.  Returns True on success."""
        def _run():
            con = self._connect()
            con.execute(sql, params)
            con.commit()
            con.close()
            return True
        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:
            logger.error("SQLite execute failed: %s", exc)
            return False

    async def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Run a SELECT. Returns list of row dicts."""
        def _run():
            con = self._connect()
            rows = [dict(r) for r in con.execute(sql, params).fetchall()]
            con.close()
            return rows
        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:
            logger.error("SQLite query failed: %s", exc)
            return []
```

---

## Step 6 — The Memory Manager

Now we wire all four adapters together behind a single interface:

```python
# tinker/memory/manager.py

"""
MemoryManager — unified interface over all four memory stores.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .storage import RedisAdapter, DuckDBAdapter, ChromaAdapter, SQLiteAdapter

logger = logging.getLogger(__name__)


class MemoryManager:
    """
    The single object the orchestrator uses for all memory operations.

    Hides which store each operation goes to.  Callers ask for:
        "store this artifact"   → DuckDB + Chroma
        "get context for task"  → Redis + DuckDB
        "search research"       → Chroma
    """

    def __init__(
        self,
        redis: RedisAdapter,
        duckdb: DuckDBAdapter,
        chroma: ChromaAdapter,
        sqlite: SQLiteAdapter,
    ) -> None:
        self._redis  = redis
        self._duckdb = duckdb
        self._chroma = chroma
        self._sqlite = sqlite

    async def connect(self) -> None:
        """Connect all stores.  Failures are logged but not fatal."""
        for name, store in [
            ("redis",  self._redis),
            ("duckdb", self._duckdb),
            ("chroma", self._chroma),
        ]:
            try:
                await store.connect()
            except Exception as exc:
                logger.warning("%s connect failed: %s — continuing without it", name, exc)

    async def close(self) -> None:
        for store in [self._redis, self._duckdb, self._chroma]:
            try:
                await store.close()
            except Exception:
                pass

    # ── Working memory (Redis) ─────────────────────────────────────────────────

    async def set_working(self, session_id: str, key: str, value: Any) -> None:
        await self._redis.set(session_id, key, value)

    async def get_working(self, session_id: str, key: str) -> Optional[Any]:
        return await self._redis.get(session_id, key)

    async def flush_working(self, session_id: str) -> None:
        await self._redis.flush_session(session_id)

    # ── Artifacts (DuckDB + Chroma) ────────────────────────────────────────────

    async def store_artifact(
        self,
        session_id: str,
        task_id: str,
        artifact_type: str,
        content: str,
        metadata: dict | None = None,
    ) -> str:
        """Store an artifact in both DuckDB (fast retrieval) and Chroma (search)."""
        artifact_id = str(uuid.uuid4())

        await self._duckdb.store_artifact(
            artifact_id=artifact_id,
            session_id=session_id,
            task_id=task_id,
            artifact_type=artifact_type,
            content=content,
            metadata=metadata,
        )
        # Index in Chroma so we can search by semantic similarity later
        await self._chroma.add_document(
            doc_id=artifact_id,
            text=content,
            metadata={"session_id": session_id, "task_id": task_id,
                      "type": artifact_type},
        )
        return artifact_id

    async def get_recent_artifacts(
        self,
        session_id: str,
        artifact_type: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        return await self._duckdb.get_recent_artifacts(
            session_id, artifact_type, limit
        )

    async def search_research(self, query: str, n: int = 5) -> list[dict]:
        """Semantic search over the research archive."""
        return await self._chroma.search(query, n_results=n)
```

---

## Step 7 — Try It

```python
# test_memory.py
import asyncio
from memory.storage import RedisAdapter, DuckDBAdapter, ChromaAdapter, SQLiteAdapter
from memory.manager import MemoryManager

async def main():
    mm = MemoryManager(
        redis  = RedisAdapter(url="redis://localhost:6379"),
        duckdb = DuckDBAdapter(db_path="test_session.duckdb"),
        chroma = ChromaAdapter(persist_dir="./test_chroma"),
        sqlite = SQLiteAdapter(db_path="test_tasks.sqlite"),
    )

    await mm.connect()
    print("Connected.")

    # Store a fake artifact
    artifact_id = await mm.store_artifact(
        session_id="test-session-1",
        task_id="task-001",
        artifact_type="design",
        content="The API gateway should use JWT tokens for authentication.",
        metadata={"subsystem": "api_gateway"},
    )
    print(f"Stored artifact: {artifact_id}")

    # Search for it
    results = await mm.search_research("authentication tokens")
    for r in results:
        print(f"Found: {r['text'][:80]}")

    await mm.close()

asyncio.run(main())
```

Expected output:
```
Connected.
Stored artifact: 3f7e1c2a-...
Found: The API gateway should use JWT tokens for authentication.
```

---

## What We Have So Far

```
tinker/
  llm/         ✅  model client + router
  memory/      ✅  four adapters + unified manager
```

The key insight of this chapter: **the `MemoryManager` is the only thing
the orchestrator talks to**.  If you want to swap DuckDB for PostgreSQL
later, you change one adapter class and nothing else in the system needs
to know.

---

→ Next: [Chapter 04 — The Tool Layer](./04-tool-layer.md)
