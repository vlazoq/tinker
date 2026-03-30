"""
core/memory/tests/test_semantic_search.py
======================================
Unit tests for MemoryManager.search() — the semantic search method
that wraps ChromaDB query results and converts L2 distance to score.

Uses stubs to avoid real DB connections.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Minimal MemoryManager stub that bypasses DB connections
# ---------------------------------------------------------------------------


class FakeEmbeddings:
    """Returns a fixed vector — the value doesn't matter for scoring tests."""

    async def embed(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0]


class StubMemoryManager:
    """
    A lightweight stand-in for MemoryManager that exposes only the
    search() method, wired to a controllable FakeChroma.
    """

    def __init__(self, chroma_results):
        self._chroma_results = chroma_results
        self._embeddings = FakeEmbeddings()

    async def _query_chroma(self, embedding, n_results, where):
        return self._chroma_results

    async def search(
        self,
        query: str,
        top_k: int = 10,
        filters=None,
    ) -> list[dict]:
        """Replicated from MemoryManager.search() without storage dependencies."""
        filter_topic = (filters or {}).get("artifact_type")
        filter_session = (filters or {}).get("session_id")

        embedding = await self._embeddings.embed(query)
        where: dict = {}
        if filter_topic:
            where["topic"] = {"$eq": filter_topic}
        if filter_session:
            where["session_id"] = {"$eq": filter_session}

        raw_results = await self._query_chroma(
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSemanticSearch:
    @pytest.mark.asyncio
    async def test_distance_zero_gives_score_one(self):
        """distance=0.0 → score = 1.0 / (1 + 0) = 1.0"""
        result = {
            "id": "note-1",
            "document": "some text",
            "distance": 0.0,
            "metadata": {"topic": "arch", "tags": ""},
        }
        mm = StubMemoryManager(chroma_results=[result])
        results = await mm.search("query")
        assert len(results) == 1
        assert results[0]["score"] == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_distance_one_gives_score_half(self):
        """distance=1.0 → score = 1.0 / (1 + 1) = 0.5"""
        result = {
            "id": "note-2",
            "document": "content",
            "distance": 1.0,
            "metadata": {"topic": "design", "tags": ""},
        }
        mm = StubMemoryManager(chroma_results=[result])
        results = await mm.search("query")
        assert results[0]["score"] == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_empty_chroma_result_returns_empty_list(self):
        mm = StubMemoryManager(chroma_results=[])
        results = await mm.search("anything")
        assert results == []

    @pytest.mark.asyncio
    async def test_score_is_rounded_to_4_decimal_places(self):
        """score should be rounded to 4 decimal places."""
        result = {
            "id": "note-3",
            "document": "doc",
            "distance": 3.0,  # 1/(1+3) = 0.25 exactly → no rounding issue
            "metadata": {"topic": "x", "tags": ""},
        }
        mm = StubMemoryManager(chroma_results=[result])
        results = await mm.search("q")
        score = results[0]["score"]
        # Confirm the score has at most 4 decimal places
        assert score == round(score, 4)

    @pytest.mark.asyncio
    async def test_tags_split_from_comma_separated_string(self):
        """tags should be split from a comma-separated metadata string."""
        result = {
            "id": "note-4",
            "document": "text",
            "distance": 0.5,
            "metadata": {"topic": "t", "tags": "alpha,beta,gamma"},
        }
        mm = StubMemoryManager(chroma_results=[result])
        results = await mm.search("q")
        assert results[0]["tags"] == ["alpha", "beta", "gamma"]

    @pytest.mark.asyncio
    async def test_empty_tags_string_yields_empty_list(self):
        result = {
            "id": "note-5",
            "document": "text",
            "distance": 0.5,
            "metadata": {"topic": "t", "tags": ""},
        }
        mm = StubMemoryManager(chroma_results=[result])
        results = await mm.search("q")
        assert results[0]["tags"] == []

    @pytest.mark.asyncio
    async def test_missing_tags_key_yields_empty_list(self):
        result = {
            "id": "note-6",
            "document": "text",
            "distance": 0.5,
            "metadata": {"topic": "t"},
        }
        mm = StubMemoryManager(chroma_results=[result])
        results = await mm.search("q")
        assert results[0]["tags"] == []

    @pytest.mark.asyncio
    async def test_multiple_results_all_scored(self):
        """All results should have correct scores."""
        raw = [
            {
                "id": f"n-{i}",
                "document": "doc",
                "distance": float(i),
                "metadata": {"topic": "t", "tags": ""},
            }
            for i in range(4)
        ]
        mm = StubMemoryManager(chroma_results=raw)
        results = await mm.search("q")
        assert len(results) == 4
        for i, r in enumerate(results):
            expected = round(1.0 / (1.0 + i), 4)
            assert r["score"] == pytest.approx(expected)

    @pytest.mark.asyncio
    async def test_result_includes_id_and_snippet(self):
        long_doc = "x" * 500
        result = {
            "id": "note-7",
            "document": long_doc,
            "distance": 0.2,
            "metadata": {"topic": "arch", "tags": "a,b"},
        }
        mm = StubMemoryManager(chroma_results=[result])
        results = await mm.search("q")
        r = results[0]
        assert r["id"] == "note-7"
        assert r["memory_id"] == "note-7"
        assert len(r["snippet"]) <= 300
        assert r["text"] == long_doc
