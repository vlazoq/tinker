Here's a full breakdown of what was built:

---

## Memory Manager — 6 files, ~850 lines

### `schemas.py` — Data Models
Three dataclasses covering all four layers, each with serialisation round-trips:
- **`Artifact`** — DuckDB session output (content, type, task_id, metadata)
- **`ResearchNote`** — ChromaDB entry with a `to_chroma_metadata()` helper that flattens nested fields to ChromaDB's scalar-only requirement
- **`Task`** — SQLite task with full status lifecycle (pending → running → completed/failed)
- **`MemoryConfig`** — every tuneable knob in one place (URLs, paths, thresholds)

### `storage.py` — Four Thin Adapters
Each adapter owns one connection and exposes only the primitives MemoryManager needs:
- **`RedisAdapter`** — namespaced keys (`tinker:<session>:<key>`), TTL, flush-by-session
- **`DuckDBAdapter`** — sync DuckDB behind `run_in_executor` + an `asyncio.Lock` to serialise writes; indexed on session, type, and task
- **`ChromaAdapter`** — cosine-space collection, query by embedding, upsert, get-by-id
- **`SQLiteAdapter`** — genuine async via `aiosqlite`; indexed on status and session

### `embeddings.py` — EmbeddingPipeline
- Lazy-loads the model on first use with a double-checked lock (safe under concurrent coroutines)
- CPU-bound `.encode()` runs in a thread-pool executor so the event loop never blocks
- Normalised unit vectors, batched for efficiency

### `compression.py` — MemoryCompressor
Two triggers: **age-based** (artifacts older than N hours) and **threshold-based** (session exceeds N artifacts). Each chunk of old artifacts is:
1. Summarised via an injected `async summariser(prompt) -> str` callable
2. Stored as a `SUMMARY` artifact in DuckDB
3. Cross-archived as a `ResearchNote` in ChromaDB (so it survives beyond the session)
4. Original artifacts marked `archived=True` (not deleted — audit trail preserved)

A stub summariser is used when none is injected, clearly labelling outputs so you know to wire in the real model client.

### `manager.py` — MemoryManager
The unified API surface. Usable as an async context manager or manually. Key design decisions:
- `store_artifact()` has `auto_compress=True` by default — the Orchestrator doesn't need to think about memory housekeeping
- `search_research()` accepts optional `filter_topic` / `filter_session` which translate to ChromaDB `where` clauses
- `health_check()` and `stats()` give the Observability Dashboard what it needs

### `tests/test_memory_manager.py`
- Isolated unit tests for each adapter and the compressor
- Full integration suite wiring all four layers together with `fakeredis` for Redis
- A `run_smoke_test()` you can call directly with `python tests/test_memory_manager.py` before pytest is set up

**To run:** `pip install -r requirements.txt && pytest tests/ -v --asyncio-mode=auto`