"""
tinker/task_engine/resolver.py
───────────────────────────────
DependencyResolver

Manages the PENDING → BLOCKED ↔ PENDING transition.

Rules
──────
1.  When a task is saved with non-empty ``dependencies``, check each dep.
    If any dep is NOT COMPLETE → block the task.

2.  When a task is marked COMPLETE, scan the registry for tasks that were
    BLOCKED solely because of that task and potentially unblock them.

3.  Circular dependency detection: raises DependencyCycleError.

Public API
──────────
resolver.check_and_block(task, registry)   → Task (possibly BLOCKED)
resolver.unblock_dependents(completed_task, registry)  → list[Task] unblocked
resolver.resolve_all(registry)             → list[Task] newly unblocked
resolver.build_dependency_graph(registry)  → dict[id → set[id]]
"""

from __future__ import annotations

import logging
from collections import deque
from typing import TYPE_CHECKING

from .schema import Task, TaskStatus

if TYPE_CHECKING:
    from .registry import TaskRegistry

log = logging.getLogger(__name__)


class DependencyCycleError(Exception):
    pass


class DependencyResolver:

    # ── Public API ────────────────────────────────────────────────────────

    def check_and_block(self, task: Task, registry: "TaskRegistry") -> Task:
        """
        Inspect the task's dependencies.  If any are not COMPLETE, set the
        task status to BLOCKED and persist.  Returns the (possibly mutated) task.
        """
        if not task.dependencies:
            return task

        blocking = self._blocking_deps(task, registry)
        if blocking:
            task.status = TaskStatus.BLOCKED
            task.metadata["blocking_deps"] = blocking
            task.touch()
            registry.save(task)
            log.debug(
                "Task '%s' blocked by %d dep(s): %s", task.id, len(blocking), blocking
            )
        return task

    def unblock_dependents(
        self,
        completed_task: Task,
        registry: "TaskRegistry",
    ) -> list[Task]:
        """
        Called after a task reaches COMPLETE.
        Scans BLOCKED tasks whose only remaining blocker was ``completed_task``.
        Returns the list of newly-unblocked tasks.
        """
        unblocked: list[Task] = []
        blocked_tasks = registry.by_status(TaskStatus.BLOCKED)

        for candidate in blocked_tasks:
            if completed_task.id not in candidate.dependencies:
                continue  # Not relevant to this completion

            remaining_blockers = self._blocking_deps(candidate, registry)
            if not remaining_blockers:
                candidate.status = TaskStatus.PENDING
                candidate.metadata.pop("blocking_deps", None)
                candidate.touch()
                registry.save(candidate)
                unblocked.append(candidate)
                log.info("Task '%s' unblocked after completion of '%s'",
                         candidate.id, completed_task.id)

        return unblocked

    def resolve_all(self, registry: "TaskRegistry") -> list[Task]:
        """
        Full scan: re-evaluate every BLOCKED task.
        Useful after a batch import or a bulk status change.
        Returns all tasks that were unblocked.
        """
        unblocked: list[Task] = []
        for task in registry.by_status(TaskStatus.BLOCKED):
            remaining = self._blocking_deps(task, registry)
            if not remaining:
                task.status = TaskStatus.PENDING
                task.metadata.pop("blocking_deps", None)
                task.touch()
                registry.save(task)
                unblocked.append(task)
        log.info("resolve_all unblocked %d task(s)", len(unblocked))
        return unblocked

    def build_dependency_graph(
        self, registry: "TaskRegistry"
    ) -> dict[str, set[str]]:
        """
        Returns adjacency dict: { task_id → set of task_ids it depends on }.
        """
        graph: dict[str, set[str]] = {}
        for task in registry.list_all():
            graph[task.id] = set(task.dependencies)
        return graph

    def detect_cycles(self, registry: "TaskRegistry") -> list[list[str]]:
        """
        DFS-based cycle detection.  Returns a list of cycles (each cycle
        is a list of task IDs forming the loop).  Empty list = no cycles.
        """
        graph = self.build_dependency_graph(registry)
        visited: set[str] = set()
        rec_stack: set[str] = set()
        cycles: list[list[str]] = []

        def dfs(node: str, path: list[str]) -> None:
            visited.add(node)
            rec_stack.add(node)
            for dep in graph.get(node, set()):
                if dep not in visited:
                    dfs(dep, path + [dep])
                elif dep in rec_stack:
                    # Found a cycle
                    cycle_start = path.index(dep) if dep in path else 0
                    cycles.append(path[cycle_start:] + [dep])
            rec_stack.discard(node)

        for node in list(graph):
            if node not in visited:
                dfs(node, [node])

        return cycles

    def topological_order(self, registry: "TaskRegistry") -> list[str]:
        """
        Kahn's algorithm — returns task IDs in dependency-safe execution order.
        Raises DependencyCycleError if cycles are present.

        Graph convention: deps_of[A] = {B, C} means B and C must run before A.
        Kahn's needs edges pointing prerequisite → dependent, so we build the
        reverse (dependents_of) and count in-degree as number of unmet deps.
        """
        all_tasks = registry.list_all()
        deps_of: dict[str, set[str]] = {t.id: set(t.dependencies) for t in all_tasks}

        # Reverse graph: prerequisite → set of tasks that need it
        dependents_of: dict[str, set[str]] = {t.id: set() for t in all_tasks}
        for task_id, deps in deps_of.items():
            for dep in deps:
                if dep in dependents_of:
                    dependents_of[dep].add(task_id)

        in_degree: dict[str, int] = {t.id: len(deps_of[t.id]) for t in all_tasks}
        queue = deque(tid for tid, deg in in_degree.items() if deg == 0)
        order: list[str] = []

        while queue:
            node = queue.popleft()
            order.append(node)
            for dependent in dependents_of.get(node, set()):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        if len(order) != len(all_tasks):
            raise DependencyCycleError(
                "Cycle detected: could not produce full topological order."
            )
        return order

    # ── Internal helpers ──────────────────────────────────────────────────

    @staticmethod
    def _blocking_deps(task: Task, registry: "TaskRegistry") -> list[str]:
        """Return the IDs of dependency tasks that are NOT yet COMPLETE."""
        blocking: list[str] = []
        for dep_id in task.dependencies:
            dep = registry.get(dep_id)
            if dep is None:
                log.warning("Dependency %s not found for task %s", dep_id, task.id)
                blocking.append(dep_id)   # Unknown dep counts as blocking
            elif dep.status != TaskStatus.COMPLETE:
                blocking.append(dep_id)
        return blocking
