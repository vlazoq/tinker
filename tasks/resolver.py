"""
tasks/resolver.py — Dependency tracking and cycle detection
============================================================

What this file does
--------------------
The ``DependencyResolver`` manages the relationship between tasks that
depend on each other.

When Tinker plans work, it sometimes creates tasks that must happen in a
specific order.  For example:
  - Task B: "Design the caching layer" might depend on
  - Task A: "Research caching strategies" finishing first.

The resolver enforces these ordering rules by:
  1. Blocking tasks that aren't ready yet (their prerequisites haven't
     finished), setting their status to BLOCKED.
  2. Unblocking tasks as soon as all their prerequisites are COMPLETE,
     setting their status back to PENDING so the queue can schedule them.
  3. Detecting cycles — if Task A depends on B and B depends on A, nothing
     can ever run.  The resolver can find and report such loops.
  4. Producing a topological ordering — a sequence in which every task's
     prerequisites come before the task itself.

Key concept: BLOCKED vs PENDING
---------------------------------
  PENDING  = "ready to be scheduled; the queue can pick this"
  BLOCKED  = "can't run yet; waiting for prerequisites to finish"

A task that depends on nothing starts PENDING.  A task with unfinished
prerequisites starts BLOCKED and automatically moves to PENDING once they
complete.

Graph theory note (for the curious)
--------------------------------------
Dependencies form a Directed Acyclic Graph (DAG) — directed because
"A depends on B" is a one-way relationship, acyclic because cycles would
mean deadlock (no task could ever run).

The resolver uses two standard graph algorithms:
  - DFS (Depth-First Search) to find cycles.
  - Kahn's algorithm to produce a topological (dependency-respecting) order.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import TYPE_CHECKING

from .schema import Task, TaskStatus

# TYPE_CHECKING is True only during static analysis (e.g. mypy), not at
# runtime.  This lets us annotate with TaskRegistry without causing a
# circular import when Python actually executes the code.
if TYPE_CHECKING:
    from .registry import TaskRegistry

log = logging.getLogger(__name__)


# =============================================================================
# Custom exception
# =============================================================================

# DependencyCycleError is defined in the central exceptions module and
# re-exported here so ``from tasks.resolver import DependencyCycleError``
# continues to work.
from exceptions import DependencyCycleError  # noqa: F401  (intentional re-export)


# =============================================================================
# DependencyResolver class
# =============================================================================

class DependencyResolver:
    """
    Manages task blocking/unblocking based on inter-task dependencies.

    This class has no constructor parameters — it holds no state of its own.
    It operates purely by reading and writing to the provided TaskRegistry.

    All methods are synchronous (no async/await) because the TaskRegistry
    uses synchronous SQLite.  The TaskEngine wraps the blocking calls in
    ``asyncio.run_in_executor`` to keep the async event loop unblocked.
    """

    # =========================================================================
    # Public API
    # =========================================================================

    def check_and_block(self, task: Task, registry: "TaskRegistry") -> Task:
        """Check whether a task's prerequisites are all done; block if not.

        Call this right after creating a new task with dependencies.  If any
        dependency task is not yet COMPLETE, this method will:
          1. Set the task's status to BLOCKED.
          2. Record which dependencies are still blocking it in task.metadata.
          3. Save the updated task to the registry.

        If all dependencies are already COMPLETE (or there are no dependencies),
        the task is left as-is (typically PENDING).

        Parameters
        ----------
        task     : The newly created (or re-evaluated) task.
        registry : The live task store to look up dependency statuses.

        Returns
        -------
        The task, possibly with status changed to BLOCKED.
        """
        if not task.dependencies:
            # No prerequisites at all — nothing to block on.
            return task

        # Find which of this task's dependencies are NOT yet COMPLETE
        blocking = self._blocking_deps(task, registry)
        if blocking:
            # At least one prerequisite is unfinished — block this task
            task.status = TaskStatus.BLOCKED
            # Store the blocking IDs in metadata for debugging and display
            task.metadata["blocking_deps"] = blocking
            task.touch()         # Update the updated_at timestamp
            registry.save(task)  # Persist the BLOCKED status
            log.debug(
                "Task '%s' blocked by %d dep(s): %s", task.id, len(blocking), blocking
            )
        return task

    def unblock_dependents(
        self,
        completed_task: Task,
        registry: "TaskRegistry",
    ) -> list[Task]:
        """Check whether completing a task unblocks any other tasks.

        Called immediately after a task reaches COMPLETE status.  This method:
          1. Finds all BLOCKED tasks that listed ``completed_task.id`` as a
             dependency.
          2. Re-checks each one: if this was their *last* remaining blocker,
             they transition from BLOCKED → PENDING.
          3. Saves the newly-PENDING tasks to the registry.

        Tasks that are still waiting on other prerequisites stay BLOCKED.

        Parameters
        ----------
        completed_task : The task that just finished.
        registry       : The live task store.

        Returns
        -------
        List of tasks that were just unblocked (their status is now PENDING).
        """
        unblocked: list[Task] = []
        blocked_tasks = registry.by_status(TaskStatus.BLOCKED)

        for candidate in blocked_tasks:
            # Skip this candidate if it didn't list completed_task as a dep.
            # (Quick early exit — avoids the more expensive _blocking_deps call.)
            if completed_task.id not in candidate.dependencies:
                continue

            # Check whether there are any OTHER unfinished dependencies
            remaining_blockers = self._blocking_deps(candidate, registry)
            if not remaining_blockers:
                # All prerequisites are now done — this task is ready to run
                candidate.status = TaskStatus.PENDING
                # Remove the "blocking_deps" diagnostic entry from metadata
                candidate.metadata.pop("blocking_deps", None)
                candidate.touch()
                registry.save(candidate)
                unblocked.append(candidate)
                log.info("Task '%s' unblocked after completion of '%s'",
                         candidate.id, completed_task.id)

        return unblocked

    def resolve_all(self, registry: "TaskRegistry") -> list[Task]:
        """Re-evaluate every BLOCKED task in the registry.

        This is a full-scan version of ``unblock_dependents``.  It's slower
        than the targeted version (because it checks ALL blocked tasks, not
        just those affected by one completion), but it's useful in two cases:
          1. After a bulk import of tasks, to correct any mis-labelled statuses.
          2. At queue startup, to catch any tasks that became unblocked while
             the system was offline.

        Returns
        -------
        List of tasks that were just unblocked.
        """
        unblocked: list[Task] = []
        for task in registry.by_status(TaskStatus.BLOCKED):
            remaining = self._blocking_deps(task, registry)
            if not remaining:
                # No blockers remain — this task is free to run
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
        """Build a complete dependency adjacency dict for all tasks.

        Returns a dict where:
          key   = task ID
          value = set of task IDs that this task depends ON (its prerequisites)

        Example:
          { "task-B": {"task-A"},   # B depends on A
            "task-A": set(),        # A depends on nothing
            "task-C": {"task-A", "task-B"} }  # C depends on both

        This is useful for visualising the task graph or running graph
        algorithms like cycle detection and topological sort.
        """
        graph: dict[str, set[str]] = {}
        for task in registry.list_all():
            graph[task.id] = set(task.dependencies)
        return graph

    def detect_cycles(self, registry: "TaskRegistry") -> list[list[str]]:
        """Find any circular dependencies in the task graph.

        Uses a depth-first search (DFS) with a "recursion stack" to find
        back-edges — edges that point back to an ancestor in the current
        search path.  A back-edge means there's a cycle.

        Returns
        -------
        A list of cycles.  Each cycle is a list of task IDs that form a
        loop.  Returns an empty list if no cycles are found.

        Example return value for a cycle A → B → A:
          [["task-A", "task-B", "task-A"]]

        Note: this does NOT raise DependencyCycleError — it just reports.
        Use ``topological_order()`` if you want an exception on cycle detection.
        """
        graph = self.build_dependency_graph(registry)
        visited: set[str] = set()     # Nodes we've fully explored
        rec_stack: set[str] = set()   # Nodes in the current DFS path
        cycles: list[list[str]] = []

        def dfs(node: str, path: list[str]) -> None:
            """Recursive DFS helper."""
            visited.add(node)
            rec_stack.add(node)    # This node is now on the current path

            for dep in graph.get(node, set()):
                if dep not in visited:
                    # Haven't seen this node yet — keep exploring
                    dfs(dep, path + [dep])
                elif dep in rec_stack:
                    # We've reached a node that's already on our current path!
                    # That means there's a cycle.
                    cycle_start = path.index(dep) if dep in path else 0
                    cycles.append(path[cycle_start:] + [dep])

            # Remove from recursion stack when we backtrack
            rec_stack.discard(node)

        # Start DFS from every unvisited node (handles disconnected graphs)
        for node in list(graph):
            if node not in visited:
                dfs(node, [node])

        return cycles

    def topological_order(self, registry: "TaskRegistry") -> list[str]:
        """Return task IDs in a valid dependency-respecting execution order.

        "Topological order" means: if task B depends on task A, then A
        appears before B in the returned list.  This gives you a safe
        sequence in which to run all tasks.

        Uses Kahn's algorithm:
          1. Count how many unfinished dependencies each task has ("in-degree").
          2. Add all zero-in-degree tasks (no prerequisites) to a queue.
          3. Repeatedly: pop a task, add it to the result, and decrement the
             in-degree of all its dependents.  Any that reach zero go in the queue.
          4. If we can't process all tasks, there must be a cycle — raise an error.

        Raises
        ------
        DependencyCycleError if any circular dependencies exist.

        Returns
        -------
        List of task IDs in execution order (prerequisites before dependents).
        """
        all_tasks = registry.list_all()

        # deps_of[A] = {B, C} means "A needs B and C to finish before it runs"
        deps_of: dict[str, set[str]] = {t.id: set(t.dependencies) for t in all_tasks}

        # Build the reverse graph: prerequisite → set of tasks that need it.
        # Kahn's algorithm needs to know "when I finish X, which tasks get
        # their in-degree reduced?"
        dependents_of: dict[str, set[str]] = {t.id: set() for t in all_tasks}
        for task_id, deps in deps_of.items():
            for dep in deps:
                if dep in dependents_of:
                    dependents_of[dep].add(task_id)  # dep is a prerequisite of task_id

        # in_degree[task_id] = number of unfinished prerequisites this task has
        in_degree: dict[str, int] = {t.id: len(deps_of[t.id]) for t in all_tasks}

        # Start with all tasks that have NO prerequisites (ready to run immediately)
        queue = deque(tid for tid, deg in in_degree.items() if deg == 0)
        order: list[str] = []

        while queue:
            node = queue.popleft()
            order.append(node)  # This task is safe to run now

            # "Completing" this node: reduce in-degree of all tasks that depended on it
            for dependent in dependents_of.get(node, set()):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    # All prerequisites of this dependent are now satisfied
                    queue.append(dependent)

        # If we couldn't order ALL tasks, some must be in a cycle (they never
        # reached in-degree 0, so they never got added to the queue).
        if len(order) != len(all_tasks):
            raise DependencyCycleError(
                "Cycle detected: could not produce full topological order."
            )
        return order

    # =========================================================================
    # Internal helper
    # =========================================================================

    @staticmethod
    def _blocking_deps(task: Task, registry: "TaskRegistry") -> list[str]:
        """Return the IDs of this task's dependencies that are NOT yet COMPLETE.

        These are the tasks that are "blocking" the given task from running.

        Logic:
          - If a dependency task exists and is COMPLETE, it's not blocking.
          - If a dependency task exists and is NOT COMPLETE, it's blocking.
          - If a dependency task doesn't exist at all (maybe it was deleted),
            we treat it as blocking.  Better to be safe and wait than to run
            prematurely.

        Returns
        -------
        List of dependency task IDs that are still blocking (not yet COMPLETE).
        An empty list means all prerequisites are satisfied.
        """
        blocking: list[str] = []
        for dep_id in task.dependencies:
            dep = registry.get(dep_id)
            if dep is None:
                # Dependency not found in the registry — treat as blocking.
                # This could happen if a dependency was manually deleted.
                log.warning("Dependency %s not found for task %s", dep_id, task.id)
                blocking.append(dep_id)
            elif dep.status != TaskStatus.COMPLETE:
                # Dependency exists but hasn't finished yet — still blocking
                blocking.append(dep_id)
        return blocking
