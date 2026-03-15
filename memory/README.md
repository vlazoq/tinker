# Tinker — Memory Manager

Unified async interface over all four of Tinker's memory layers.

```
┌─────────────────────────────────────────────────────────────────┐
│                        MemoryManager                            │
│                                                                 │
│  set_context()       get_context()      clear_working_memory()  │
│  store_artifact()    get_artifact()     get_recent_artifacts()  │
│  store_research()    search_research()  get_research()          │
│  store_task()        get_task()         get_pending_tasks()     │
│  compress()          compress_all()     stats() / health_check()│
└────────┬────────────────┬──────────────────┬────────────────────┘
         │                │                  │                │
    RedisAdapter    DuckDBAdapter      ChromaAdapter    SQLiteAdapter
    (Working Mem)  (Session Mem)    (Research Archive) (Task Registry)
    ephemeral/TTL  structured SQL    vector search      durable log
    per-task ctx   current-run arts  cross-session      all-time tasks
```

## Memory Layers

| Layer | Backend | Purpose | Lifetime |
|-------|---------|---------|----------|
| Working Memory | Redis | Per-task ephemeral context | TTL (default 1h) |
| Session Memory | DuckDB | All artifacts in the current run | Session |
| Research Archive | ChromaDB + sentence-transformers | Semantically searchable notes | Permanent |
| Task Registry | SQLite | Every task ever created | Permanent |

## Quick Start

```python
import asyncio
from memory_manager import MemoryManager, MemoryConfig, Task
from memory_manager.schemas import ArtifactType, TaskPriority

async def main():
    config = MemoryConfig(
        redis_url="redis://localhost:6379",
        duckdb_path="tinker.duckdb",
        chroma_path="./chroma_db",
        sqlite_path="tinker_tasks.sqlite",
        embedding_model="all-MiniLM-L6-v2",
    )

    async with MemoryManager(config=config, session_id="run-001") as mm:
        # Working Memory — ephemeral task context
        await mm.set_context("current_focus", "caching layer design")
        focus = await mm.get_context("current_focus")

        # Session Memory — store an output artifact
        artifact = await mm.store_artifact(
            content="Decision: use Redis write-behind at the API gateway.",
            artifact_type=ArtifactType.DECISION,
            metadata={"confidence": 0.87},
        )

        # Research Archive — embed and store a finding
        note = await mm.store_research(
            content="Write-behind caching defers DB writes asynchronously.",
            topic="caching-patterns",
            tags=["redis", "write-behind"],
        )

        # Semantic search across all sessions
        results = await mm.search_research("cache invalidation strategies", n_results=5)
        for r in results:
            print(r.topic, r.content[:80])

        # Task Registry — durable task tracking
        task = Task(
            title="Evaluate CDN options",
            description="Compare Cloudflare, Fastly, and AWS CloudFront.",
            priority=TaskPriority.HIGH,
        )
        await mm.store_task(task)
        await mm.update_task_status(task.id, TaskStatus.RUNNING)

        # Stats
        print(await mm.stats())

asyncio.run(main())
```

## Compression

The compressor automatically archives old/excess artifacts when:
- The active artifact count for a session exceeds `compression_artifact_threshold` (default 500)
- An artifact is older than `compression_max_age_hours` (default 24h)

Summaries are stored as both DuckDB artifacts (type=SUMMARY) and ChromaDB
ResearchNotes so knowledge survives session boundaries.

Wire in a real summariser to get meaningful summaries:

```python
async def my_summariser(prompt: str) -> str:
    # call your Ollama model client here
    return await model_client.generate(prompt)

mm = MemoryManager(config=config, summariser=my_summariser)
```

## Running Tests

```bash
pip install -r requirements.txt
pytest tests/ -v --asyncio-mode=auto
```

## Module Structure

```
memory_manager/
├── __init__.py       # Public exports
├── schemas.py        # Artifact, ResearchNote, Task, MemoryConfig dataclasses
├── storage.py        # RedisAdapter, DuckDBAdapter, ChromaAdapter, SQLiteAdapter
├── embeddings.py     # EmbeddingPipeline (lazy-loaded, async, batched)
├── compression.py    # MemoryCompressor (threshold + age-based archival)
└── manager.py        # MemoryManager — unified public API

tests/
└── test_memory_manager.py  # Full test suite + smoke test runner
```
