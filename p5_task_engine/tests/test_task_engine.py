"""
tinker/task_engine/tests/test_task_engine.py
──────────────────────────────────────────────
Test harness demonstrating a complete Task Engine lifecycle end-to-end.

Run with:
    python -m pytest tinker/task_engine/tests/test_task_engine.py -v

Or directly:
    python tinker/task_engine/tests/test_task_engine.py
"""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import time
import logging
import unittest

from p5_task_engine import (
    Task,
    TaskType,
    TaskStatus,
    Subsystem,
    TaskRegistry,
    TaskGenerator,
    PriorityScorer,
    ScorerWeights,
    DependencyResolver,
    DependencyCycleError,
    TaskQueue,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("test_task_engine")


# ────────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────────

ARCHITECT_OUTPUT = {
    "architect_version": "1.0",
    "subsystem": "memory_manager",
    "outputs": ["artefact:mem_schema_v3"],
    "candidate_tasks": [
        {
            "title": "Evaluate HNSW vs IVF indexing strategies",
            "description": (
                "Compare approximate-nearest-neighbour index algorithms "
                "for the long-term memory store under load."
            ),
            "type": "research",
            "subsystem": "memory_manager",
            "dependencies": [],
            "confidence_gap": 0.85,
            "tags": ["memory", "performance", "indexing"],
            "metadata": {"priority_hint": "high"},
        },
        {
            "title": "Design memory eviction policy",
            "description": "Define LRU / importance-weighted eviction for bounded memory.",
            "type": "design",
            "subsystem": "memory_manager",
            "dependencies": [],  # will be injected in dependency test
            "confidence_gap": 0.60,
            "tags": ["memory", "eviction"],
            "metadata": {},
        },
        {
            "title": "Validate memory schema migration",
            "description": "Ensure v2→v3 schema migration preserves all existing artefacts.",
            "type": "validation",
            "subsystem": "memory_manager",
            "dependencies": [],
            "confidence_gap": 0.40,
            "tags": ["memory", "migration"],
            "metadata": {},
        },
        {
            "title": "INVALID TYPE TASK",
            "type": "does_not_exist",   # Should fallback to DESIGN
            "subsystem": "memory_manager",
            "confidence_gap": "not-a-float",  # Should fallback to 0.5
        },
    ],
}


def make_registry() -> TaskRegistry:
    return TaskRegistry(db_path=":memory:")


# ────────────────────────────────────────────────────────────────────────────
# 1 · Schema tests
# ────────────────────────────────────────────────────────────────────────────

class TestTaskSchema(unittest.TestCase):

    def test_defaults(self):
        t = Task(title="Test", description="desc")
        self.assertEqual(t.status, TaskStatus.PENDING)
        self.assertEqual(t.type, TaskType.DESIGN)
        self.assertIsNotNone(t.id)
        self.assertIsNotNone(t.created_at)

    def test_mark_started(self):
        t = Task(title="T")
        t.mark_started()
        self.assertEqual(t.status, TaskStatus.ACTIVE)
        self.assertIsNotNone(t.started_at)
        self.assertEqual(t.attempt_count, 1)

    def test_mark_complete(self):
        t = Task(title="T")
        t.mark_started()
        t.mark_complete(outputs=["artefact:x"])
        self.assertEqual(t.status, TaskStatus.COMPLETE)
        self.assertIn("artefact:x", t.outputs)

    def test_mark_failed(self):
        t = Task(title="T")
        t.mark_failed("timeout")
        self.assertEqual(t.status, TaskStatus.FAILED)
        self.assertEqual(t.metadata["failure_reason"], "timeout")

    def test_serialisation_roundtrip(self):
        t = Task(
            title="Round-trip test",
            type=TaskType.CRITIQUE,
            subsystem=Subsystem.ORCHESTRATOR,
            dependencies=["id-1", "id-2"],
            tags=["tag-a"],
            metadata={"key": "val"},
        )
        d = t.to_dict()
        t2 = Task.from_dict(d)
        self.assertEqual(t.id, t2.id)
        self.assertEqual(t.type, t2.type)
        self.assertEqual(t.subsystem, t2.subsystem)
        self.assertEqual(t.dependencies, t2.dependencies)
        self.assertEqual(t.tags, t2.tags)
        log.info("✓ Schema round-trip OK")


# ────────────────────────────────────────────────────────────────────────────
# 2 · Registry tests
# ────────────────────────────────────────────────────────────────────────────

class TestTaskRegistry(unittest.TestCase):

    def setUp(self):
        self.reg = make_registry()

    def test_save_and_get(self):
        t = Task(title="Reg test")
        self.reg.save(t)
        fetched = self.reg.get(t.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.title, "Reg test")

    def test_update(self):
        t = Task(title="Before")
        self.reg.save(t)
        t.title = "After"
        self.reg.update(t)
        self.assertEqual(self.reg.get(t.id).title, "After")

    def test_delete(self):
        t = Task(title="Delete me")
        self.reg.save(t)
        self.assertTrue(self.reg.delete(t.id))
        self.assertIsNone(self.reg.get(t.id))

    def test_by_status(self):
        for i in range(3):
            t = Task(title=f"P{i}", status=TaskStatus.PENDING)
            self.reg.save(t)
        for i in range(2):
            t = Task(title=f"C{i}", status=TaskStatus.COMPLETE)
            self.reg.save(t)
        self.assertEqual(len(self.reg.by_status(TaskStatus.PENDING)), 3)
        self.assertEqual(len(self.reg.by_status(TaskStatus.COMPLETE)), 2)

    def test_count_by_status(self):
        self.reg.save(Task(title="A", status=TaskStatus.PENDING))
        self.reg.save(Task(title="B", status=TaskStatus.ACTIVE))
        counts = self.reg.count_by_status()
        self.assertIn("pending", counts)
        log.info("✓ Registry CRUD OK | counts=%s", counts)


# ────────────────────────────────────────────────────────────────────────────
# 3 · TaskGenerator tests
# ────────────────────────────────────────────────────────────────────────────

class TestTaskGenerator(unittest.TestCase):

    def setUp(self):
        self.gen = TaskGenerator()

    def test_basic_generation(self):
        tasks = self.gen.from_architect_output(ARCHITECT_OUTPUT, parent_task_id="parent-1")
        self.assertEqual(len(tasks), 4)
        for t in tasks:
            self.assertIsInstance(t, Task)
            self.assertEqual(t.parent_id, "parent-1")

    def test_type_fallback(self):
        tasks = self.gen.from_architect_output(ARCHITECT_OUTPUT)
        invalid_task = next(t for t in tasks if "INVALID" in t.title)
        self.assertEqual(invalid_task.type, TaskType.DESIGN)  # Fallback

    def test_confidence_gap_fallback(self):
        tasks = self.gen.from_architect_output(ARCHITECT_OUTPUT)
        invalid_task = next(t for t in tasks if "INVALID" in t.title)
        self.assertAlmostEqual(invalid_task.confidence_gap, 0.5, places=1)

    def test_empty_candidates(self):
        tasks = self.gen.from_architect_output({"candidate_tasks": []})
        self.assertEqual(tasks, [])

    def test_missing_candidates_key(self):
        tasks = self.gen.from_architect_output({})
        self.assertEqual(tasks, [])

    def test_exploration_task(self):
        t = self.gen.make_exploration_task(
            title="Explore event-sourcing for state manager",
            description="Investigate whether CQRS+event-sourcing improves Tinker's audit trail.",
        )
        self.assertTrue(t.is_exploration)
        self.assertEqual(t.type, TaskType.EXPLORATION)
        log.info("✓ TaskGenerator OK")


# ────────────────────────────────────────────────────────────────────────────
# 4 · PriorityScorer tests
# ────────────────────────────────────────────────────────────────────────────

class TestPriorityScorer(unittest.TestCase):

    def setUp(self):
        self.scorer = PriorityScorer()

    def test_score_range(self):
        t = Task(title="S", confidence_gap=0.9, staleness_hours=10.0)
        s = self.scorer.score(t)
        self.assertGreaterEqual(s, 0.0)
        self.assertLessEqual(s, 1.0)

    def test_high_confidence_gap_scores_higher(self):
        lo = Task(title="Lo", confidence_gap=0.1)
        hi = Task(title="Hi", confidence_gap=0.9)
        self.assertGreater(self.scorer.score(hi), self.scorer.score(lo))

    def test_stale_task_scores_higher(self):
        fresh = Task(title="Fresh", staleness_hours=0.0,  confidence_gap=0.5)
        stale = Task(title="Stale", staleness_hours=48.0, confidence_gap=0.5)
        self.assertGreater(self.scorer.score(stale), self.scorer.score(fresh))

    def test_exploration_bump(self):
        normal = Task(title="N", is_exploration=False, confidence_gap=0.5)
        expl   = Task(title="E", is_exploration=True,  confidence_gap=0.5)
        self.assertGreater(self.scorer.score(expl), self.scorer.score(normal))

    def test_depth_penalty(self):
        shallow = Task(title="Sh", dependency_depth=0, confidence_gap=0.5)
        deep    = Task(title="De", dependency_depth=5, confidence_gap=0.5)
        self.assertGreater(self.scorer.score(shallow), self.scorer.score(deep))

    def test_score_all_sorted(self):
        tasks = [
            Task(title="A", confidence_gap=0.3),
            Task(title="B", confidence_gap=0.9),
            Task(title="C", confidence_gap=0.6),
        ]
        sorted_tasks = self.scorer.score_all(tasks)
        self.assertEqual(sorted_tasks[0].title, "B")

    def test_explain(self):
        t = Task(title="Explain me", confidence_gap=0.7, is_exploration=True)
        breakdown = self.scorer.explain(t)
        self.assertIn("confidence_gap", breakdown)
        self.assertIn("exploration_bump", breakdown)
        self.assertGreater(breakdown["exploration_bump"], 0)
        log.info("✓ PriorityScorer OK | breakdown=%s", breakdown)


# ────────────────────────────────────────────────────────────────────────────
# 5 · DependencyResolver tests
# ────────────────────────────────────────────────────────────────────────────

class TestDependencyResolver(unittest.TestCase):

    def setUp(self):
        self.reg = make_registry()
        self.resolver = DependencyResolver()

    def _make_complete_task(self, title="Done") -> Task:
        t = Task(title=title, status=TaskStatus.COMPLETE)
        self.reg.save(t)
        return t

    def test_no_deps_not_blocked(self):
        t = Task(title="Free", dependencies=[])
        t = self.resolver.check_and_block(t, self.reg)
        self.assertEqual(t.status, TaskStatus.PENDING)

    def test_incomplete_dep_blocks(self):
        dep = Task(title="Dep", status=TaskStatus.PENDING)
        self.reg.save(dep)
        t = Task(title="Blocked", dependencies=[dep.id])
        t = self.resolver.check_and_block(t, self.reg)
        self.assertEqual(t.status, TaskStatus.BLOCKED)

    def test_complete_dep_does_not_block(self):
        dep = self._make_complete_task("Complete dep")
        t = Task(title="Ready", dependencies=[dep.id])
        t = self.resolver.check_and_block(t, self.reg)
        self.assertEqual(t.status, TaskStatus.PENDING)

    def test_unblock_on_completion(self):
        dep = Task(title="Dep", status=TaskStatus.PENDING)
        self.reg.save(dep)
        child = Task(title="Child", dependencies=[dep.id], status=TaskStatus.BLOCKED)
        self.reg.save(child)

        dep.mark_complete()
        self.reg.save(dep)
        unblocked = self.resolver.unblock_dependents(dep, self.reg)

        self.assertEqual(len(unblocked), 1)
        self.assertEqual(unblocked[0].id, child.id)
        self.assertEqual(self.reg.get(child.id).status, TaskStatus.PENDING)

    def test_resolve_all(self):
        dep1 = self._make_complete_task("D1")
        dep2 = self._make_complete_task("D2")
        blocked = Task(
            title="Unblock me",
            dependencies=[dep1.id, dep2.id],
            status=TaskStatus.BLOCKED,
        )
        self.reg.save(blocked)
        unblocked = self.resolver.resolve_all(self.reg)
        self.assertEqual(len(unblocked), 1)

    def test_topological_order(self):
        a = Task(title="A"); self.reg.save(a)
        b = Task(title="B", dependencies=[a.id]); self.reg.save(b)
        c = Task(title="C", dependencies=[b.id]); self.reg.save(c)
        order = self.resolver.topological_order(self.reg)
        self.assertLess(order.index(a.id), order.index(b.id))
        self.assertLess(order.index(b.id), order.index(c.id))
        log.info("✓ DependencyResolver OK | topo_order=%s", order)


# ────────────────────────────────────────────────────────────────────────────
# 6 · TaskQueue end-to-end lifecycle test
# ────────────────────────────────────────────────────────────────────────────

class TestTaskQueueLifecycle(unittest.TestCase):
    """
    Full end-to-end lifecycle:
    pending → active → critique → complete → (dependents unblocked)
    """

    def setUp(self):
        self.reg   = make_registry()
        self.queue = TaskQueue(self.reg, auto_unblock=True)
        self.gen   = TaskGenerator()

    def test_full_lifecycle(self):
        log.info("=" * 60)
        log.info("E2E LIFECYCLE TEST")
        log.info("=" * 60)

        # ── Step 1: Generate tasks from Architect output ──────────────────
        parent = Task(title="Seed task", status=TaskStatus.COMPLETE)
        self.reg.save(parent)

        tasks = self.gen.from_architect_output(ARCHITECT_OUTPUT, parent_task_id=parent.id)
        log.info("Step 1: Generated %d tasks", len(tasks))

        # Inject a dependency between tasks to test blocking
        dep_task   = tasks[0]  # research task
        child_task = tasks[1]  # design task — will depend on the research
        child_task.dependencies = [dep_task.id]

        # Add an exploration task
        expl = self.gen.make_exploration_task(
            title="Explore append-only log for state versioning",
            description="Investigate if immutable event logs simplify rollback.",
        )
        tasks.append(expl)

        # Save everything; check_and_block the dependent
        for t in tasks:
            self.reg.save(t)

        resolver = DependencyResolver()
        for t in tasks:
            resolver.check_and_block(t, self.reg)

        counts = self.reg.count_by_status()
        log.info("Step 1 counts: %s", counts)
        self.assertIn("pending", counts)

        # ── Step 2: Score all pending tasks ──────────────────────────────
        pending = self.reg.by_status(TaskStatus.PENDING)
        scorer  = PriorityScorer()
        scored  = scorer.score_all(pending)
        log.info("Step 2: Scored %d pending tasks", len(scored))
        for t in scored:
            log.info("  score=%.4f  [%s] %s", t.priority_score, t.type.value, t.title)
            self.reg.save(t)

        # ── Step 3: Dispatch the top task ─────────────────────────────────
        dispatched = self.queue.get_next()
        self.assertIsNotNone(dispatched)
        self.assertEqual(dispatched.status, TaskStatus.ACTIVE)
        log.info("Step 3: Dispatched '%s'", dispatched.title)

        # Verify it's persisted as ACTIVE
        db_task = self.reg.get(dispatched.id)
        self.assertEqual(db_task.status, TaskStatus.ACTIVE)

        # ── Step 4: Push to critique ──────────────────────────────────────
        self.queue.push_to_critique(dispatched.id)
        self.assertEqual(self.reg.get(dispatched.id).status, TaskStatus.CRITIQUE)
        log.info("Step 4: Pushed to critique")

        # ── Step 5: Accept critique → COMPLETE ───────────────────────────
        completed = self.queue.accept_critique(
            dispatched.id,
            notes="Architecture looks solid. Approved.",
        )
        self.assertEqual(completed.status, TaskStatus.COMPLETE)
        log.info("Step 5: Task completed")

        # ── Step 6: Verify dependent unblocking ──────────────────────────
        # If the research task was completed, the design task should now be PENDING
        if dispatched.id == dep_task.id:
            child_in_db = self.reg.get(child_task.id)
            self.assertEqual(child_in_db.status, TaskStatus.PENDING)
            log.info("Step 6: Dependent task unblocked ✓")
        else:
            log.info("Step 6: Skipped (dep task not yet dispatched — expected)")

        # ── Step 7: Stats ────────────────────────────────────────────────
        stats = self.queue.stats()
        log.info("Step 7: Queue stats: %s", stats)
        self.assertIn("counts", stats)

        log.info("=" * 60)
        log.info("E2E LIFECYCLE TEST PASSED")
        log.info("=" * 60)

    def test_exploration_slot_activated(self):
        """With 100% exploration probability, exploration tasks always win."""
        # Override exploration % to guarantee the slot is used
        queue = TaskQueue(
            self.reg,
            exploration_min_pct=1.0,
            exploration_max_pct=1.0,
        )
        gen = TaskGenerator()
        expl = gen.make_exploration_task("Explore something wild", "desc")
        regular = Task(
            title="Normal task",
            type=TaskType.SYNTHESIS,
            confidence_gap=1.0,   # Would normally score very high
        )
        self.reg.save(expl)
        self.reg.save(regular)

        next_task = queue.get_next()
        self.assertIsNotNone(next_task)
        self.assertTrue(next_task.is_exploration)
        log.info("✓ Exploration slot test passed: dispatched '%s'", next_task.title)

    def test_fail_task(self):
        t = Task(title="Doomed task", status=TaskStatus.PENDING)
        self.reg.save(t)
        self.queue.get_next()  # activates it
        failed = self.queue.fail_task(t.id, "Architect timed out")
        self.assertEqual(failed.status, TaskStatus.FAILED)
        self.assertEqual(failed.metadata["failure_reason"], "Architect timed out")

    def test_empty_queue_returns_none(self):
        result = self.queue.get_next()
        self.assertIsNone(result)


# ────────────────────────────────────────────────────────────────────────────
# 7 · Multi-task dependency chain test
# ────────────────────────────────────────────────────────────────────────────

class TestDependencyChain(unittest.TestCase):
    """A → B → C chain: only A can run first; completing A unblocks B; etc."""

    def setUp(self):
        self.reg      = make_registry()
        self.queue    = TaskQueue(self.reg)
        self.resolver = DependencyResolver()

    def test_chain_ordering(self):
        a = Task(title="A – Design schema")
        b = Task(title="B – Implement schema", dependencies=[a.id])
        c = Task(title="C – Validate schema",  dependencies=[b.id])

        for t in (a, b, c):
            self.reg.save(t)
            self.resolver.check_and_block(t, self.reg)

        # Only A is PENDING; B and C are BLOCKED
        self.assertEqual(self.reg.get(a.id).status, TaskStatus.PENDING)
        self.assertEqual(self.reg.get(b.id).status, TaskStatus.BLOCKED)
        self.assertEqual(self.reg.get(c.id).status, TaskStatus.BLOCKED)

        # Run A
        next_t = self.queue.get_next()
        self.assertEqual(next_t.id, a.id)
        self.queue.accept_critique(next_t.id, "Good.")

        # B should now be PENDING; C still BLOCKED
        self.assertEqual(self.reg.get(b.id).status, TaskStatus.PENDING)
        self.assertEqual(self.reg.get(c.id).status, TaskStatus.BLOCKED)

        # Run B
        next_t = self.queue.get_next()
        self.assertEqual(next_t.id, b.id)
        self.queue.accept_critique(next_t.id, "Good.")

        # C should now be PENDING
        self.assertEqual(self.reg.get(c.id).status, TaskStatus.PENDING)

        log.info("✓ Dependency chain A→B→C resolved in correct order")


# ────────────────────────────────────────────────────────────────────────────
# Entry point
# ────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()

    for cls in [
        TestTaskSchema,
        TestTaskRegistry,
        TestTaskGenerator,
        TestPriorityScorer,
        TestDependencyResolver,
        TestTaskQueueLifecycle,
        TestDependencyChain,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
