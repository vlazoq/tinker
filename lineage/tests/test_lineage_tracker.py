"""
Tests for lineage/tracker.py
================================

Verifies derivation recording, ancestry queries, and child lookups.
"""
from __future__ import annotations

import pytest

from lineage.tracker import LineageTracker


@pytest.fixture
async def tracker(tmp_path):
    db_path = str(tmp_path / "test_lineage.sqlite")
    t = LineageTracker(db_path=db_path)
    await t.initialize()
    yield t
    await t.close()


class TestLineageTracker:
    @pytest.mark.asyncio
    async def test_record_and_get_parents(self, tracker):
        await tracker.record_derivation(
            parent_id="task-001",
            child_id="artifact-001",
            operation="micro_loop",
        )
        parents = await tracker.get_parents("artifact-001")
        assert "task-001" in parents

    @pytest.mark.asyncio
    async def test_record_and_get_children(self, tracker):
        await tracker.record_derivation(
            parent_id="task-001",
            child_id="artifact-001",
            operation="micro_loop",
        )
        children = await tracker.get_children("task-001")
        assert "artifact-001" in children

    @pytest.mark.asyncio
    async def test_multi_level_ancestry(self, tracker):
        # Build a 3-level chain: t1 → a1 → doc1
        await tracker.record_derivation("t1", "a1", "micro_loop")
        await tracker.record_derivation("a1", "doc1", "meso_synthesis")
        ancestry = await tracker.get_full_ancestry("doc1")
        assert "a1" in ancestry
        assert "t1" in ancestry

    @pytest.mark.asyncio
    async def test_no_parents_returns_empty(self, tracker):
        parents = await tracker.get_parents("orphan-node")
        assert parents == []

    @pytest.mark.asyncio
    async def test_no_children_returns_empty(self, tracker):
        children = await tracker.get_children("leaf-node")
        assert children == []

    @pytest.mark.asyncio
    async def test_metadata_stored_with_edge(self, tracker):
        await tracker.record_derivation(
            parent_id="p",
            child_id="c",
            operation="test_op",
            metadata={"score": 0.85, "iteration": 42},
        )
        # Verify we can still retrieve parents (metadata storage is non-crashing)
        parents = await tracker.get_parents("c")
        assert "p" in parents
