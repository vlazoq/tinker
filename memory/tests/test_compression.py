"""
memory/tests/test_compression.py
==================================
Unit tests for memory/compression.py.

Uses stub DuckDB/ChromaDB/EmbeddingPipeline to avoid real DB connections.
"""

from __future__ import annotations

import asyncio
import logging
import math

import pytest

from memory.compression import _cosine_similarity, MemoryCompressor
from memory.schemas import MemoryConfig


# ---------------------------------------------------------------------------
# Stub adapters
# ---------------------------------------------------------------------------


class FakeDuckDB:
    async def get_old_artifacts(self, *a, **kw):
        return []

    async def insert_artifact(self, *a):
        pass

    async def mark_archived(self, *a):
        pass

    async def count_session_artifacts(self, *a, **kw):
        return 0

    async def get_recent(self, *a, **kw):
        return []


class FakeChroma:
    async def upsert(self, *a, **kw):
        pass

    async def query(self, *a, **kw):
        return []


class FakeEmbeddings:
    """Returns a controllable fixed vector for every embed() call."""

    def __init__(self, vector: list[float] = None):
        self._vector = vector or [1.0, 0.0, 0.0]
        self.calls = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return list(self._vector)


class OrthogonalEmbeddings:
    """Returns [1,0,0] for 'original' and [0,1,0] for 'summary' calls."""

    def __init__(self):
        self._call_count = 0

    async def embed(self, text: str) -> list[float]:
        self._call_count += 1
        if self._call_count % 2 == 1:
            return [1.0, 0.0, 0.0]
        else:
            return [0.0, 1.0, 0.0]


# ---------------------------------------------------------------------------
# _cosine_similarity
# ---------------------------------------------------------------------------


class TestCosineSimilarity:
    def test_identical_vectors_return_one(self):
        v = [1.0, 2.0, 3.0]
        assert _cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors_return_zero(self):
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        assert _cosine_similarity(a, b) == pytest.approx(0.0)

    def test_empty_vectors_return_zero(self):
        assert _cosine_similarity([], []) == 0.0
        assert _cosine_similarity([1.0], []) == 0.0
        assert _cosine_similarity([], [1.0]) == 0.0

    def test_mismatched_length_returns_zero(self):
        assert _cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0

    def test_antiparallel_vectors_return_minus_one(self):
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert _cosine_similarity(a, b) == pytest.approx(-1.0)

    def test_zero_vector_returns_zero(self):
        assert _cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0

    def test_normalised_diagonal(self):
        a = [1.0, 1.0]
        n = math.sqrt(2)
        expected = (1.0 / n) * (1.0 / n) * 2  # == 1.0
        assert _cosine_similarity(a, a) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# _compress_chunk — summariser receives correct prompt
# ---------------------------------------------------------------------------


class TestCompressChunk:
    @pytest.mark.asyncio
    async def test_summariser_called_with_prompt(self):
        """_compress_chunk should call the summariser with a structured prompt."""
        received_prompts = []

        async def fake_summariser(prompt: str) -> str:
            received_prompts.append(prompt)
            return "test summary text"

        config = MemoryConfig()
        config.compression_summary_chunk = 10  # large enough to hold all test artifacts

        embeddings = FakeEmbeddings(vector=[0.9, 0.1, 0.0])
        compressor = MemoryCompressor(
            duckdb=FakeDuckDB(),
            chroma=FakeChroma(),
            embeddings=embeddings,
            summariser=fake_summariser,
            config=config,
        )

        artifacts = [
            {
                "id": f"art-{i}",
                "content": f"content of artifact {i}",
                "artifact_type": "raw",
                "created_at": "2024-01-01T00:00:00Z",
            }
            for i in range(3)
        ]

        await compressor._compress_chunk("session-1", artifacts, "test-reason")

        assert len(received_prompts) == 1
        prompt = received_prompts[0]
        assert "test-reason" in prompt
        assert "3" in prompt  # artifact count in prompt
        assert "ARTIFACTS" in prompt
        assert "SUMMARY" in prompt

    @pytest.mark.asyncio
    async def test_compress_chunk_calls_duckdb_and_chroma(self):
        """Artifacts should be inserted into DuckDB and upserted into ChromaDB."""
        inserted_artifacts = []
        upserted_docs = []
        archived_ids = []

        class TrackingDuckDB(FakeDuckDB):
            async def insert_artifact(self, artifact):
                inserted_artifacts.append(artifact)

            async def mark_archived(self, ids):
                archived_ids.extend(ids)

        class TrackingChroma(FakeChroma):
            async def upsert(self, **kw):
                upserted_docs.append(kw)

        async def summariser(prompt: str) -> str:
            return "a good summary"

        config = MemoryConfig()
        config.compression_summary_chunk = 10

        compressor = MemoryCompressor(
            duckdb=TrackingDuckDB(),
            chroma=TrackingChroma(),
            embeddings=FakeEmbeddings(vector=[0.8, 0.2, 0.0]),
            summariser=summariser,
            config=config,
        )

        artifacts = [{"id": "a1", "content": "hello", "artifact_type": "raw", "created_at": "now"}]
        count = await compressor._compress_chunk("ses-1", artifacts, "age-based")

        assert count == 1
        assert len(inserted_artifacts) == 1
        assert len(upserted_docs) == 1
        assert "a1" in archived_ids


# ---------------------------------------------------------------------------
# Low / High similarity logging
# ---------------------------------------------------------------------------


class TestSimilarityLogging:
    @pytest.mark.asyncio
    async def test_low_similarity_logs_warning(self, caplog):
        """When cosine similarity < 0.4, a WARNING should be logged."""

        async def summariser(prompt: str) -> str:
            return "completely different output"

        config = MemoryConfig()
        config.compression_summary_chunk = 10

        compressor = MemoryCompressor(
            duckdb=FakeDuckDB(),
            chroma=FakeChroma(),
            embeddings=OrthogonalEmbeddings(),  # produces 0 similarity
            summariser=summariser,
            config=config,
        )

        artifacts = [{"id": "x1", "content": "original", "artifact_type": "raw", "created_at": "now"}]

        with caplog.at_level(logging.WARNING, logger="memory.compression"):
            await compressor._compress_chunk("ses-1", artifacts, "threshold-based")

        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any("Low summary quality" in m or "cosine_similarity" in m for m in warning_messages)

    @pytest.mark.asyncio
    async def test_high_similarity_logs_debug(self, caplog):
        """When cosine similarity >= 0.4, only a DEBUG message should appear."""

        async def summariser(prompt: str) -> str:
            return "a faithful summary"

        config = MemoryConfig()
        config.compression_summary_chunk = 10

        compressor = MemoryCompressor(
            duckdb=FakeDuckDB(),
            chroma=FakeChroma(),
            embeddings=FakeEmbeddings(vector=[1.0, 0.0, 0.0]),  # identical → similarity 1.0
            summariser=summariser,
            config=config,
        )

        artifacts = [{"id": "y1", "content": "content", "artifact_type": "raw", "created_at": "now"}]

        with caplog.at_level(logging.DEBUG, logger="memory.compression"):
            await compressor._compress_chunk("ses-2", artifacts, "age-based")

        # Must have no WARNING-level log about quality
        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert not any("Low summary quality" in m for m in warning_messages)

        # Should have a DEBUG message about quality being OK
        debug_messages = [
            r.message for r in caplog.records if r.levelno == logging.DEBUG
        ]
        assert any("quality OK" in m or "cosine_similarity" in m for m in debug_messages)
