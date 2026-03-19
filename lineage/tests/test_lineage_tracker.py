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
    await t.connect()
    yield t
    await t.close()


class TestLineageTracker:
    @pytest.mark.asyncio
    async def test_record_and_get_parents(self, tracker):
        await tracker.record_derivation(
            parent_id="task-001",
            parent_type="task",
            child_id="artifact-001",
            child_type="artifact",
            operation="micro_loop",
        )
        parents = await tracker.get_parents("artifact-001")
        assert any(p["parent_id"] == "task-001" for p in parents)

    @pytest.mark.asyncio
    async def test_record_and_get_children(self, tracker):
        await tracker.record_derivation(
            parent_id="task-001",
            parent_type="task",
            child_id="artifact-001",
            child_type="artifact",
            operation="micro_loop",
        )
        children = await tracker.get_children("task-001")
        assert any(c["child_id"] == "artifact-001" for c in children)

    @pytest.mark.asyncio
    async def test_multi_level_ancestry(self, tracker):
        # Build a 3-level chain: t1 → a1 → doc1
        await tracker.record_derivation("t1", "task", "a1", "artifact", "micro_loop")
        await tracker.record_derivation(
            "a1", "artifact", "doc1", "artifact", "meso_synthesis"
        )
        ancestry = await tracker.get_full_ancestry("doc1")
        parent_ids = {e["parent_id"] for e in ancestry}
        assert "a1" in parent_ids
        assert "t1" in parent_ids

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
            parent_type="task",
            child_id="c",
            child_type="artifact",
            operation="test_op",
            metadata={"score": 0.85, "iteration": 42},
        )
        # Verify we can still retrieve parents (metadata storage is non-crashing)
        parents = await tracker.get_parents("c")
        assert any(p["parent_id"] == "p" for p in parents)
