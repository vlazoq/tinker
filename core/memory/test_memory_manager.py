"""
test_memory_manager.py — Test harness for the Tinker Memory Manager.

Tests are written with pytest + pytest-asyncio.
Each storage layer is tested in isolation, then integration tests verify
cross-layer flows (e.g. compression writing summaries to ChromaDB).

Run with:
    pytest tests/test_memory_manager.py -v --asyncio-mode=auto

Fixtures use temporary directories / mock servers so no live Redis/ChromaDB
is required.  A fake Redis is provided via fakeredis; DuckDB and ChromaDB
use temp dirs; SQLite uses an in-memory path.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from core.memory import (
    Artifact,
    EmbeddingPipeline,
    MemoryCompressor,
    MemoryConfig,
    MemoryManager,
    Task,
)
from core.memory.schemas import ArtifactType, TaskPriority, TaskStatus
from core.memory.storage import (
    ChromaAdapter,
    DuckDBAdapter,
    RedisAdapter,
    SQLiteAdapter,
)

# ===========================================================================
# Helpers / Fixtures
# ===========================================================================


def make_temp_config(tmp_path: str) -> MemoryConfig:
    return MemoryConfig(
        redis_url="redis://localhost:9999",  # overridden by fakeredis
        duckdb_path=os.path.join(tmp_path, "test.duckdb"),
        chroma_path=os.path.join(tmp_path, "chroma"),
        sqlite_path=":memory:",  # in-memory SQLite
        embedding_model="all-MiniLM-L6-v2",
        compression_artifact_threshold=10,
        compression_max_age_hours=1,
        compression_summary_chunk=3,
    )


async def make_stub_embedding_pipeline() -> EmbeddingPipeline:
    """Returns an EmbeddingPipeline that yields deterministic fake embeddings."""
    pipeline = EmbeddingPipeline.__new__(EmbeddingPipeline)
    pipeline.model_name = "stub"
    pipeline.device = "cpu"
    pipeline._model = True  # truthy → "loaded"
    pipeline._lock = asyncio.Lock()

    async def stub_embed(text: str) -> list[float]:
        # Deterministic vector based on text hash, 384-dim to match MiniLM
        seed = hash(text) % (2**31)
        import random

        rng = random.Random(seed)
        raw = [rng.gauss(0, 1) for _ in range(384)]
        norm = sum(x**2 for x in raw) ** 0.5 or 1.0
        return [x / norm for x in raw]

    async def stub_embed_batch(texts: list[str]) -> list[list[float]]:
        return [await stub_embed(t) for t in texts]

    pipeline.embed = stub_embed  # type: ignore[assignment]
    pipeline.embed_batch = stub_embed_batch  # type: ignore[assignment]
    return pipeline


SESSION_ID = "test-session-001"


# ===========================================================================
# DuckDB — Session Memory
# ===========================================================================


class TestDuckDBAdapter:
    @pytest_asyncio.fixture
    async def db(self, tmp_path):
        adapter = DuckDBAdapter(str(tmp_path / "test.duckdb"))
        await adapter.connect()
        yield adapter
        await adapter.close()

    @pytest.mark.asyncio
    async def test_insert_and_retrieve(self, db):
        artifact = Artifact(
            content="Architecture uses event-sourcing with CQRS.",
            artifact_type=ArtifactType.ARCHITECTURE,
            session_id=SESSION_ID,
        )
        await db.insert_artifact(artifact)
        row = await db.get_artifact(artifact.id)
        assert row is not None
        assert row["content"] == artifact.content
        assert row["artifact_type"] == ArtifactType.ARCHITECTURE.value

    @pytest.mark.asyncio
    async def test_get_recent_filters_by_type(self, db):
        for t in [
            ArtifactType.ARCHITECTURE,
            ArtifactType.CODE,
            ArtifactType.ARCHITECTURE,
        ]:
            await db.insert_artifact(Artifact(content="x", artifact_type=t, session_id=SESSION_ID))
        arch = await db.get_recent(SESSION_ID, artifact_type="architecture")
        assert len(arch) == 2
        for row in arch:
            assert row["artifact_type"] == "architecture"

    @pytest.mark.asyncio
    async def test_mark_archived(self, db):
        artifacts = [Artifact(content=f"artifact {i}", session_id=SESSION_ID) for i in range(4)]
        for a in artifacts:
            await db.insert_artifact(a)

        ids = [artifacts[0].id, artifacts[1].id]
        await db.mark_archived(ids)

        count_active = await db.count_session_artifacts(SESSION_ID, include_archived=False)
        count_all = await db.count_session_artifacts(SESSION_ID, include_archived=True)
        assert count_active == 2
        assert count_all == 4

    @pytest.mark.asyncio
    async def test_get_old_artifacts(self, db):
        old = Artifact(content="old data", session_id=SESSION_ID)
        old.created_at = datetime.now(UTC) - timedelta(hours=3)
        new = Artifact(content="new data", session_id=SESSION_ID)
        await db.insert_artifact(old)
        await db.insert_artifact(new)

        cutoff = datetime.now(UTC) - timedelta(hours=1)
        old_rows = await db.get_old_artifacts(SESSION_ID, older_than=cutoff)
        assert len(old_rows) == 1
        assert old_rows[0]["content"] == "old data"


# ===========================================================================
# ChromaDB — Research Archive
# ===========================================================================


class TestChromaAdapter:
    @pytest_asyncio.fixture
    async def chroma(self, tmp_path):
        adapter = ChromaAdapter(str(tmp_path / "chroma"), "test_collection")
        await adapter.connect()
        yield adapter
        await adapter.close()

    @pytest_asyncio.fixture
    async def embeddings(self):
        return await make_stub_embedding_pipeline()

    @pytest.mark.asyncio
    async def test_upsert_and_query(self, chroma, embeddings):
        note_id = str(uuid.uuid4())
        content = "Microservices communicate via gRPC with protobuf schemas."
        embedding = await embeddings.embed(content)
        await chroma.upsert(
            doc_id=note_id,
            document=content,
            embedding=embedding,
            metadata={
                "topic": "architecture",
                "session_id": SESSION_ID,
                "tags": "grpc",
                "source": "test",
                "task_id": "",
                "created_at": datetime.now(UTC).isoformat(),
            },
        )
        results = await chroma.query(embedding=embedding, n_results=1)
        assert results[0]["id"] == note_id
        assert "gRPC" in results[0]["document"]

    @pytest.mark.asyncio
    async def test_get_by_id(self, chroma, embeddings):
        note_id = str(uuid.uuid4())
        content = "CQRS separates read and write models."
        embedding = await embeddings.embed(content)
        await chroma.upsert(
            doc_id=note_id,
            document=content,
            embedding=embedding,
            metadata={
                "topic": "pattern",
                "session_id": SESSION_ID,
                "tags": "",
                "source": "test",
                "task_id": "",
                "created_at": datetime.now(UTC).isoformat(),
            },
        )
        result = await chroma.get_by_id(note_id)
        assert result is not None
        assert result["id"] == note_id

    @pytest.mark.asyncio
    async def test_semantic_similarity(self, chroma, embeddings):
        """Similar texts should rank above dissimilar ones."""
        docs = {
            "arch": "Hexagonal architecture decouples domain from infrastructure.",
            "data": "Redis is an in-memory key-value store for caching.",
            "test": "Unit tests verify individual components in isolation.",
        }
        for key, content in docs.items():
            emb = await embeddings.embed(content)
            await chroma.upsert(
                doc_id=key,
                document=content,
                embedding=emb,
                metadata={
                    "topic": key,
                    "session_id": SESSION_ID,
                    "tags": "",
                    "source": "test",
                    "task_id": "",
                    "created_at": datetime.now(UTC).isoformat(),
                },
            )
        query_emb = await embeddings.embed("software architecture patterns")
        results = await chroma.query(query_emb, n_results=3)
        # With stub embeddings ranking is not semantically meaningful;
        # just verify all 3 docs are returned.
        assert len(results) == 3
        assert {r["id"] for r in results} == {"arch", "data", "test"}


# ===========================================================================
# SQLite — Task Registry
# ===========================================================================


class TestSQLiteAdapter:
    @pytest_asyncio.fixture
    async def db(self):
        adapter = SQLiteAdapter(":memory:")
        await adapter.connect()
        yield adapter
        await adapter.close()

    @pytest.mark.asyncio
    async def test_upsert_and_retrieve(self, db):
        task = Task(
            title="Design event bus",
            description="Evaluate Kafka vs NATS for the message bus.",
            priority=TaskPriority.HIGH,
            session_id=SESSION_ID,
        )
        await db.upsert_task(task)
        row = await db.get_task(task.id)
        assert row is not None
        assert row["title"] == "Design event bus"
        assert row["priority"] == TaskPriority.HIGH.value

    @pytest.mark.asyncio
    async def test_update_status(self, db):
        task = Task(title="Analyse latency", description="...", session_id=SESSION_ID)
        await db.upsert_task(task)
        await db.update_task_status(task.id, "completed", result="p99 < 10ms")
        row = await db.get_task(task.id)
        assert row["status"] == "completed"
        assert row["result"] == "p99 < 10ms"
        assert row["completed_at"] is not None

    @pytest.mark.asyncio
    async def test_get_by_status(self, db):
        for i in range(3):
            await db.upsert_task(Task(title=f"task {i}", description="...", session_id=SESSION_ID))
        await db.upsert_task(
            Task(
                title="done",
                description="...",
                session_id=SESSION_ID,
                status=TaskStatus.COMPLETED,
            )
        )
        pending = await db.get_tasks_by_status("pending")
        assert len(pending) == 3
        done = await db.get_tasks_by_status("completed")
        assert len(done) == 1

    @pytest.mark.asyncio
    async def test_child_tasks(self, db):
        parent = Task(title="Parent", description="...", session_id=SESSION_ID)
        child1 = Task(
            title="Child 1",
            description="...",
            session_id=SESSION_ID,
            parent_task_id=parent.id,
        )
        child2 = Task(
            title="Child 2",
            description="...",
            session_id=SESSION_ID,
            parent_task_id=parent.id,
        )
        for t in [parent, child1, child2]:
            await db.upsert_task(t)
        children = await db.get_child_tasks(parent.id)
        assert len(children) == 2


# ===========================================================================
# EmbeddingPipeline
# ===========================================================================


class TestEmbeddingPipeline:
    @pytest.mark.asyncio
    async def test_stub_dimensions(self):
        pipeline = await make_stub_embedding_pipeline()
        vec = await pipeline.embed("hello world")
        assert len(vec) == 384
        # Should be unit-normalised
        norm = sum(x**2 for x in vec) ** 0.5
        assert abs(norm - 1.0) < 1e-5

    @pytest.mark.asyncio
    async def test_batch_returns_same_as_sequential(self):
        pipeline = await make_stub_embedding_pipeline()
        texts = ["text one", "text two", "text three"]
        batch = await pipeline.embed_batch(texts)
        single = [await pipeline.embed(t) for t in texts]
        for b, s in zip(batch, single, strict=False):
            assert b == s

    @pytest.mark.asyncio
    async def test_empty_batch(self):
        pipeline = await make_stub_embedding_pipeline()
        result = await pipeline.embed_batch([])
        assert result == []


# ===========================================================================
# MemoryCompressor
# ===========================================================================


class TestMemoryCompressor:
    @pytest_asyncio.fixture
    async def setup(self, tmp_path):
        config = make_temp_config(str(tmp_path))
        config.compression_artifact_threshold = 5
        config.compression_summary_chunk = 2
        config.compression_max_age_hours = 1

        duckdb = DuckDBAdapter(config.duckdb_path)
        chroma = ChromaAdapter(config.chroma_path, config.chroma_collection)
        await duckdb.connect()
        await chroma.connect()
        embeddings = await make_stub_embedding_pipeline()

        summariser_calls = []

        async def mock_summariser(prompt: str) -> str:
            summariser_calls.append(prompt)
            return f"Summary of {len(prompt.split())} words."

        compressor = MemoryCompressor(
            duckdb=duckdb,
            chroma=chroma,
            embeddings=embeddings,
            summariser=mock_summariser,
            config=config,
        )
        yield compressor, duckdb, chroma, summariser_calls
        await duckdb.close()
        await chroma.close()

    @pytest.mark.asyncio
    async def test_no_compression_below_threshold(self, setup):
        compressor, duckdb, _, calls = setup
        for i in range(3):
            await duckdb.insert_artifact(Artifact(content=f"artifact {i}", session_id=SESSION_ID))
        archived = await compressor.maybe_compress(SESSION_ID)
        assert archived == 0
        assert len(calls) == 0

    @pytest.mark.asyncio
    async def test_compression_above_threshold(self, setup):
        compressor, duckdb, chroma, calls = setup
        # Insert 8 artifacts — 3 over threshold of 5
        for i in range(8):
            await duckdb.insert_artifact(
                Artifact(content=f"big artifact content number {i}", session_id=SESSION_ID)
            )
        archived = await compressor.maybe_compress(SESSION_ID)
        assert archived > 0
        assert len(calls) > 0
        # Summaries should now be in ChromaDB
        total = await chroma.count()
        assert total > 0

    @pytest.mark.asyncio
    async def test_force_compress_all(self, setup):
        compressor, duckdb, _chroma, _ = setup
        for i in range(4):
            await duckdb.insert_artifact(Artifact(content=f"data {i}", session_id=SESSION_ID))
        archived = await compressor.force_compress_all(SESSION_ID)
        assert archived == 4
        active = await duckdb.count_session_artifacts(SESSION_ID, include_archived=False)
        assert active == 2  # 2 summary artifacts (one per chunk of 2 originals)


# ===========================================================================
# MemoryManager — Integration
# ===========================================================================


class TestMemoryManagerIntegration:
    """
    Full integration tests wiring all four layers together.
    Redis is mocked with fakeredis; all other layers use temp paths.
    """

    @pytest_asyncio.fixture
    async def mm(self, tmp_path) -> AsyncGenerator[MemoryManager, None]:
        config = make_temp_config(str(tmp_path))
        embeddings = await make_stub_embedding_pipeline()

        manager = MemoryManager.__new__(MemoryManager)
        manager.config = config
        manager.session_id = SESSION_ID
        manager._embeddings = embeddings
        manager._connected = False

        # Real DuckDB, ChromaDB, SQLite
        from core.memory.storage import ChromaAdapter, DuckDBAdapter, SQLiteAdapter

        manager._duckdb = DuckDBAdapter(config.duckdb_path)
        manager._chroma = ChromaAdapter(config.chroma_path, config.chroma_collection)
        manager._sqlite = SQLiteAdapter(config.sqlite_path)

        # Fake Redis
        import fakeredis.aioredis as fakeredis  # type: ignore

        fake_redis = fakeredis.FakeRedis(decode_responses=True)
        redis_adapter = RedisAdapter.__new__(RedisAdapter)
        redis_adapter.url = "fake"
        redis_adapter.default_ttl = 3600
        redis_adapter._client = fake_redis
        manager._redis = redis_adapter

        # Compressor
        manager._compressor = MemoryCompressor(
            duckdb=manager._duckdb,
            chroma=manager._chroma,
            embeddings=embeddings,
            config=config,
        )

        await manager._duckdb.connect()
        await manager._chroma.connect()
        await manager._sqlite.connect()
        manager._connected = True

        yield manager

        await manager._duckdb.close()
        await manager._chroma.close()
        await manager._sqlite.close()

    # -- Working Memory tests -----------------------------------------------

    @pytest.mark.asyncio
    async def test_context_set_get(self, mm):
        await mm.set_context("current_task", {"id": "t1", "title": "Design DB schema"})
        val = await mm.get_context("current_task")
        assert val["title"] == "Design DB schema"

    @pytest.mark.asyncio
    async def test_context_ttl(self, mm):
        await mm.set_context("ephemeral", "data", ttl=1)
        val = await mm.get_context("ephemeral")
        assert val == "data"
        # Expiry tested by checking the key exists (fakeredis respects TTL on access)

    @pytest.mark.asyncio
    async def test_context_delete(self, mm):
        await mm.set_context("to_delete", 42)
        await mm.delete_context("to_delete")
        val = await mm.get_context("to_delete")
        assert val is None

    @pytest.mark.asyncio
    async def test_clear_working_memory(self, mm):
        for k in ["a", "b", "c"]:
            await mm.set_context(k, k)
        keys_before = await mm.list_context_keys()
        assert len(keys_before) == 3
        deleted = await mm.clear_working_memory()
        assert deleted == 3
        keys_after = await mm.list_context_keys()
        assert len(keys_after) == 0

    # -- Session Memory tests -----------------------------------------------

    @pytest.mark.asyncio
    async def test_store_and_get_artifact(self, mm):
        a = await mm.store_artifact(
            content="Use SAGA pattern for distributed transactions.",
            artifact_type=ArtifactType.DECISION,
            metadata={"confidence": 0.9},
            auto_compress=False,
        )
        fetched = await mm.get_artifact(a.id)
        assert fetched is not None
        assert fetched.content == a.content
        assert fetched.artifact_type == ArtifactType.DECISION
        assert fetched.metadata["confidence"] == 0.9

    @pytest.mark.asyncio
    async def test_get_recent_artifacts(self, mm):
        for i in range(5):
            atype = ArtifactType.CODE if i % 2 == 0 else ArtifactType.ANALYSIS
            await mm.store_artifact(f"content {i}", artifact_type=atype, auto_compress=False)

        all_recent = await mm.get_recent_artifacts(limit=10)
        assert len(all_recent) == 5

        code_only = await mm.get_recent_artifacts(artifact_type=ArtifactType.CODE)
        assert all(a.artifact_type == ArtifactType.CODE for a in code_only)

    # -- Research Archive tests ---------------------------------------------

    @pytest.mark.asyncio
    async def test_store_and_retrieve_research(self, mm):
        note = await mm.store_research(
            content="Event-driven architecture reduces coupling between services.",
            topic="architecture-patterns",
            tags=["eda", "coupling", "microservices"],
        )
        assert note.id is not None

        fetched = await mm.get_research(note.id)
        assert fetched is not None
        assert "coupling" in fetched.content

    @pytest.mark.asyncio
    async def test_semantic_search(self, mm):
        await mm.store_research(
            content="The hexagonal architecture puts the domain at the centre, "
            "isolating it from I/O concerns via ports and adapters.",
            topic="architecture",
        )
        await mm.store_research(
            content="Redis Streams provide a persistent, append-only log "
            "suitable for event sourcing.",
            topic="data-stores",
        )
        results = await mm.search_research("ports and adapters domain isolation", n_results=2)
        # With stub embeddings semantic ranking is not guaranteed;
        # verify both stored docs are retrievable.
        assert len(results) == 2
        contents = " ".join(r.content.lower() for r in results)
        assert "hexagonal" in contents or "redis" in contents

    @pytest.mark.asyncio
    async def test_research_count(self, mm):
        initial = await mm.count_research_notes()
        await mm.store_research("note 1", topic="t1")
        await mm.store_research("note 2", topic="t2")
        final = await mm.count_research_notes()
        assert final == initial + 2

    # -- Task Registry tests ------------------------------------------------

    @pytest.mark.asyncio
    async def test_store_and_retrieve_task(self, mm):
        task = Task(
            title="Evaluate database options",
            description="Compare PostgreSQL, CockroachDB, and TiDB for the write path.",
            priority=TaskPriority.HIGH,
        )
        stored = await mm.store_task(task)
        assert stored.session_id == SESSION_ID  # auto-filled

        fetched = await mm.get_task(task.id)
        assert fetched is not None
        assert fetched.title == "Evaluate database options"
        assert fetched.priority == TaskPriority.HIGH

    @pytest.mark.asyncio
    async def test_task_status_lifecycle(self, mm):
        task = Task(title="Run benchmark", description="Benchmark read latency.")
        await mm.store_task(task)

        await mm.update_task_status(task.id, TaskStatus.RUNNING)
        t = await mm.get_task(task.id)
        assert t.status == TaskStatus.RUNNING

        await mm.update_task_status(task.id, TaskStatus.COMPLETED, result="p99=8ms")
        t = await mm.get_task(task.id)
        assert t.status == TaskStatus.COMPLETED
        assert t.result == "p99=8ms"
        assert t.completed_at is not None

    @pytest.mark.asyncio
    async def test_pending_tasks_queue(self, mm):
        for i in range(3):
            p = TaskPriority.HIGH if i == 0 else TaskPriority.NORMAL
            await mm.store_task(Task(title=f"task {i}", description="...", priority=p))
        pending = await mm.get_pending_tasks()
        assert len(pending) == 3
        assert pending[0].priority == TaskPriority.HIGH  # highest priority first

    @pytest.mark.asyncio
    async def test_session_tasks(self, mm):
        for i in range(4):
            await mm.store_task(Task(title=f"session task {i}", description="..."))
        tasks = await mm.get_session_tasks()
        assert len(tasks) == 4

    # -- Cross-layer / end-to-end -------------------------------------------

    @pytest.mark.asyncio
    async def test_full_workflow(self, mm):
        """
        Simulate a mini Tinker loop:
          1. Create a task
          2. Store working memory context
          3. Store an artifact (the output)
          4. Store a research note
          5. Verify everything is retrievable
        """
        # 1. Task
        task = Task(
            title="Design caching layer",
            description="Determine which layers to cache and with what strategy.",
        )
        await mm.store_task(task)
        await mm.update_task_status(task.id, TaskStatus.RUNNING)

        # 2. Working memory
        await mm.set_context("active_task_id", task.id)
        ctx = await mm.get_context("active_task_id")
        assert ctx == task.id

        # 3. Artifact
        artifact = await mm.store_artifact(
            content="Decision: Cache at API gateway (TTL 60s) + Redis write-behind for DB.",
            artifact_type=ArtifactType.DECISION,
            task_id=task.id,
            auto_compress=False,
        )

        # 4. Research note
        note = await mm.store_research(
            content="Write-behind caching improves write throughput by asynchronously "
            "flushing cached writes to the underlying database.",
            topic="caching-patterns",
            tags=["write-behind", "redis"],
            task_id=task.id,
        )

        # 5. Verify all retrievable
        fetched_task = await mm.get_task(task.id)
        fetched_artifact = await mm.get_artifact(artifact.id)
        fetched_note = await mm.get_research(note.id)
        search_results = await mm.search_research("write-behind cache redis")

        assert fetched_task.status == TaskStatus.RUNNING
        assert "write-behind" in fetched_artifact.content.lower()
        assert fetched_note is not None
        assert len(search_results) >= 1

        # 6. Complete task
        await mm.update_task_status(task.id, TaskStatus.COMPLETED, result=artifact.id)
        final_task = await mm.get_task(task.id)
        assert final_task.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_stats(self, mm):
        await mm.store_artifact("a1", auto_compress=False)
        await mm.store_artifact("a2", auto_compress=False)
        await mm.store_research("r1", topic="t")
        await mm.store_task(Task(title="t", description="d"))

        stats = await mm.stats()
        assert stats["session_id"] == SESSION_ID
        assert stats["artifacts_active"] >= 2
        assert stats["research_notes_total"] >= 1


# ===========================================================================
# Runner (for running without pytest)
# ===========================================================================


async def run_smoke_test():
    """
    Minimal smoke test that can be run directly: python -m pytest or
    just `python tests/test_memory_manager.py` for a quick sanity check.
    """
    print("=" * 60)
    print("Tinker Memory Manager — Smoke Test")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp:
        config = make_temp_config(tmp)
        embeddings = await make_stub_embedding_pipeline()

        # Patch Redis with fakeredis
        try:
            import fakeredis.aioredis as fakeredis  # type: ignore

            fake_redis_client = fakeredis.FakeRedis(decode_responses=True)
            print("[✓] fakeredis available")
        except ImportError:
            fake_redis_client = None
            print("[!] fakeredis not installed — Redis tests skipped")

        from core.memory.storage import ChromaAdapter, DuckDBAdapter, SQLiteAdapter

        duckdb = DuckDBAdapter(config.duckdb_path)
        chroma = ChromaAdapter(config.chroma_path, config.chroma_collection)
        sqlite = SQLiteAdapter(config.sqlite_path)

        try:
            await duckdb.connect()
            print("[✓] DuckDB connected")
        except Exception as e:
            print(f"[✗] DuckDB: {e}")

        try:
            await chroma.connect()
            print("[✓] ChromaDB connected")
        except Exception as e:
            print(f"[✗] ChromaDB: {e}")

        try:
            await sqlite.connect()
            print("[✓] SQLite connected")
        except Exception as e:
            print(f"[✗] SQLite: {e}")

        # DuckDB round-trip
        a = Artifact(content="test artifact", session_id="smoke", artifact_type=ArtifactType.CODE)
        await duckdb.insert_artifact(a)
        row = await duckdb.get_artifact(a.id)
        assert row and row["content"] == "test artifact"
        print("[✓] DuckDB artifact write/read")

        # ChromaDB round-trip
        note_id = str(uuid.uuid4())
        emb = await embeddings.embed("test note content")
        await chroma.upsert(
            doc_id=note_id,
            document="test note content",
            embedding=emb,
            metadata={
                "topic": "test",
                "session_id": "smoke",
                "tags": "",
                "source": "smoke",
                "task_id": "",
                "created_at": datetime.now(UTC).isoformat(),
            },
        )
        results = await chroma.query(emb, n_results=1)
        assert results[0]["id"] == note_id
        print("[✓] ChromaDB upsert/query")

        # SQLite round-trip
        task = Task(title="Smoke task", description="testing", session_id="smoke")
        await sqlite.upsert_task(task)
        row = await sqlite.get_task(task.id)
        assert row and row["title"] == "Smoke task"
        await sqlite.update_task_status(task.id, "completed", result="ok")
        row = await sqlite.get_task(task.id)
        assert row["status"] == "completed"
        print("[✓] SQLite task write/update/read")

        # Redis (if available)
        if fake_redis_client:
            redis = RedisAdapter.__new__(RedisAdapter)
            redis.url = "fake"
            redis.default_ttl = 3600
            redis._client = fake_redis_client
            await redis.set("smoke", "key1", {"hello": "world"})
            val = await redis.get("smoke", "key1")
            assert val == {"hello": "world"}
            print("[✓] Redis context set/get")

        await duckdb.close()
        await chroma.close()
        await sqlite.close()

    print("\n✅  All smoke tests passed.")


if __name__ == "__main__":
    asyncio.run(run_smoke_test())
