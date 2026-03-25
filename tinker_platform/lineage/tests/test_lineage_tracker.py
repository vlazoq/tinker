"""
Tests for lineage/tracker.py
================================

Verifies derivation recording, ancestry/descendant queries, type/operation
filters, stats, and cycle detection.
"""

from __future__ import annotations

import pytest

from tinker_platform.lineage.tracker import LineageTracker


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
        parents = await tracker.get_parents("c")
        assert any(p["parent_id"] == "p" for p in parents)
        # Metadata is deserialized back to dict
        edge = next(p for p in parents if p["parent_id"] == "p")
        assert edge["metadata"]["score"] == 0.85


class TestGetDescendants:
    @pytest.mark.asyncio
    async def test_descendants_of_root(self, tracker):
        # task → artifact → synthesis
        await tracker.record_derivation(
            "task1", "task", "art1", "artifact", "micro_loop"
        )
        await tracker.record_derivation(
            "art1", "artifact", "syn1", "synthesis", "meso_synthesis"
        )
        descendants = await tracker.get_descendants("task1")
        child_ids = {e["child_id"] for e in descendants}
        assert "art1" in child_ids
        assert "syn1" in child_ids

    @pytest.mark.asyncio
    async def test_leaf_has_no_descendants(self, tracker):
        await tracker.record_derivation(
            "task2", "task", "art2", "artifact", "micro_loop"
        )
        descendants = await tracker.get_descendants("art2")
        assert descendants == []


class TestGetByType:
    @pytest.mark.asyncio
    async def test_get_by_parent_type(self, tracker):
        await tracker.record_derivation("t1", "task", "a1", "artifact", "micro_loop")
        await tracker.record_derivation("a1", "artifact", "s1", "synthesis", "meso")
        task_edges = await tracker.get_by_type("task", role="parent")
        assert all(e["parent_type"] == "task" for e in task_edges)
        assert len(task_edges) >= 1

    @pytest.mark.asyncio
    async def test_get_by_child_type(self, tracker):
        await tracker.record_derivation("t2", "task", "a2", "artifact", "micro_loop")
        artifact_edges = await tracker.get_by_type("artifact", role="child")
        assert all(e["child_type"] == "artifact" for e in artifact_edges)

    @pytest.mark.asyncio
    async def test_get_by_either_type(self, tracker):
        await tracker.record_derivation("t3", "task", "a3", "artifact", "micro_loop")
        edges = await tracker.get_by_type("artifact", role="either")
        assert any(e["child_type"] == "artifact" for e in edges)


class TestGetByOperation:
    @pytest.mark.asyncio
    async def test_filter_by_operation(self, tracker):
        await tracker.record_derivation("t1", "task", "a1", "artifact", "micro_loop")
        await tracker.record_derivation(
            "a1", "artifact", "s1", "synthesis", "meso_synthesis"
        )
        micro_edges = await tracker.get_by_operation("micro_loop")
        assert all(e["operation"] == "micro_loop" for e in micro_edges)
        assert len(micro_edges) >= 1

    @pytest.mark.asyncio
    async def test_unknown_operation_returns_empty(self, tracker):
        edges = await tracker.get_by_operation("nonexistent_op_xyz")
        assert edges == []


class TestGetStats:
    @pytest.mark.asyncio
    async def test_stats_empty_graph(self, tracker):
        stats = await tracker.get_stats()
        assert stats["total_edges"] == 0
        assert stats["by_parent_type"] == {}
        assert stats["by_operation"] == {}

    @pytest.mark.asyncio
    async def test_stats_populated_graph(self, tracker):
        await tracker.record_derivation("t1", "task", "a1", "artifact", "micro_loop")
        await tracker.record_derivation("t2", "task", "a2", "artifact", "micro_loop")
        await tracker.record_derivation(
            "a1", "artifact", "s1", "synthesis", "meso_synthesis"
        )
        stats = await tracker.get_stats()
        assert stats["total_edges"] == 3
        assert stats["by_parent_type"]["task"] == 2
        assert stats["by_parent_type"]["artifact"] == 1
        assert stats["by_operation"]["micro_loop"] == 2
        assert stats["by_operation"]["meso_synthesis"] == 1


class TestCycleDetection:
    @pytest.mark.asyncio
    async def test_self_loop_rejected(self, tracker):
        edge_id = await tracker.record_derivation(
            "node1", "artifact", "node1", "artifact", "test_op"
        )
        assert edge_id is None

    @pytest.mark.asyncio
    async def test_cycle_rejected(self, tracker):
        # a → b → c, then attempt c → a (would create cycle)
        await tracker.record_derivation("a", "task", "b", "artifact", "micro_loop")
        await tracker.record_derivation("b", "artifact", "c", "synthesis", "meso")
        edge_id = await tracker.record_derivation(
            "c", "synthesis", "a", "task", "bad_op"
        )
        assert edge_id is None

    @pytest.mark.asyncio
    async def test_valid_dag_accepted(self, tracker):
        # Diamond graph: a → b, a → c, b → d, c → d (valid DAG)
        e1 = await tracker.record_derivation("a", "task", "b", "artifact", "op1")
        e2 = await tracker.record_derivation("a", "task", "c", "artifact", "op1")
        e3 = await tracker.record_derivation("b", "artifact", "d", "synthesis", "op2")
        e4 = await tracker.record_derivation("c", "artifact", "d", "synthesis", "op2")
        assert all(e is not None for e in [e1, e2, e3, e4])
