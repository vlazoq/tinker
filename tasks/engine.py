"""
engine.py — TaskEngine façade
──────────────────────────────
Wraps TaskRegistry, TaskQueue, and TaskGenerator behind the simple interface
the Orchestrator (and its micro/meso/macro loops) expects:

    engine = TaskEngine(problem_statement="Design a distributed job queue")
    task_dict = await engine.select_task()         # dict or None
    await engine.complete_task(task_id, artifact_id)
    new_tasks = await engine.generate_tasks(parent_task, architect_result, critic_result)

All methods are async so the Orchestrator can await them uniformly, even
though the underlying registry is synchronous SQLite.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from .schema import Task, TaskStatus, TaskType, Subsystem
from .registry import TaskRegistry
from .queue import TaskQueue
from .generator import TaskGenerator
from .scorer import PriorityScorer

log = logging.getLogger(__name__)


class TaskEngine:
    """
    Thin async façade over the Task Engine's internal components.

    Dependency Injection (DIP — "D" in SOLID)
    ------------------------------------------
    All internal components can be injected at construction time.  This
    makes the engine testable without real SQLite and swappable without
    subclassing.  Pass concrete implementations for production, stubs for
    tests::

        # Production
        engine = TaskEngine(problem_statement="Design X")

        # Test with injected doubles
        engine = TaskEngine(
            problem_statement="Design X",
            registry=FakeRegistry(),
            scorer=FakeScorer(),
            queue=FakeQueue(),
            generator=FakeGenerator(),
        )

    Parameters
    ----------
    problem_statement : str
        The top-level design problem Tinker is working on.
        Used to seed an initial task if the registry is empty.
    db_path : str
        Path to the SQLite file for persistent task storage.
        Defaults to ":memory:" for ephemeral runs.
        Ignored when *registry* is injected directly.
    registry : TaskRegistry or None
        Inject a pre-built registry.  When None (default), a new
        TaskRegistry is created from *db_path*.
    scorer : PriorityScorer or None
        Inject a custom scorer.  When None (default), PriorityScorer() is used.
    queue : TaskQueue or None
        Inject a pre-built queue.  When None (default), a new TaskQueue is
        constructed from *registry* and *scorer*.
    generator : TaskGenerator or None
        Inject a custom generator.  When None (default), TaskGenerator() is used.
    """

    def __init__(
        self,
        problem_statement: str = "Design a robust software architecture",
        db_path: str = ":memory:",
        registry: Optional[TaskRegistry] = None,
        scorer: Optional[PriorityScorer] = None,
        queue: Optional[TaskQueue] = None,
        generator: Optional[TaskGenerator] = None,
    ) -> None:
        self._problem = problem_statement
        self.registry = registry if registry is not None else TaskRegistry(db_path=db_path)
        self.scorer = scorer if scorer is not None else PriorityScorer()
        self.queue = queue if queue is not None else TaskQueue(
            registry=self.registry, scorer=self.scorer
        )
        self.generator = generator if generator is not None else TaskGenerator()

        # Seed with an initial task so the orchestrator always has work
        self._seed_initial_task()

    # ── Public async API ──────────────────────────────────────────────────

    async def select_task(self) -> Optional[dict]:
        """
        Return the highest-priority PENDING task as a plain dict, marking it
        ACTIVE in the registry.  Returns None if there are no tasks.
        """
        task = await asyncio.get_running_loop().run_in_executor(
            None, self.queue.get_next
        )
        if task is None:
            return None
        return self._task_to_orchestrator_dict(task)

    async def complete_task(
        self,
        task_id: str,
        artifact_id: Optional[str] = None,
        outputs: Optional[list[str]] = None,
        tokens_used: int = 0,
        duration_seconds: float = 0.0,
    ) -> None:
        """
        Mark a task as COMPLETE.

        Accepts *artifact_id* (the micro loop's convention) OR *outputs*
        (the TaskQueue's native convention).  Either form works.

        Parameters
        ----------
        tokens_used      : LLM tokens consumed while completing this task.
        duration_seconds : Wall-clock time taken to complete this task.
        """
        out = outputs or ([artifact_id] if artifact_id else [])
        await asyncio.get_running_loop().run_in_executor(
            None, self.queue.complete_task, task_id, out
        )
        if tokens_used or duration_seconds:
            await asyncio.get_running_loop().run_in_executor(
                None,
                self.registry.complete_task,
                task_id,
                tokens_used,
                duration_seconds,
            )

    async def fail_task(
        self,
        task_id: str,
        reason: str = "",
    ) -> None:
        """
        Mark a task as FAILED with an optional reason.

        Called by the orchestrator when an agent produces an unusable result
        or an exception is raised during execution.

        Parameters
        ----------
        task_id : ID of the task to fail.
        reason  : Human-readable explanation stored in task metadata.
        """
        await asyncio.get_running_loop().run_in_executor(
            None, self.queue.fail_task, task_id, reason
        )

    async def generate_tasks(
        self,
        parent_task: dict,
        architect_result: dict,
        critic_result: dict,
    ) -> list[dict]:
        """
        Parse the Architect's JSON output and enqueue new child tasks.
        Returns the new tasks as plain dicts.
        """
        parent_id = parent_task.get("id")

        def _generate() -> list[Task]:
            new_tasks = self.generator.from_architect_output(
                architect_result, parent_task_id=parent_id
            )
            for t in new_tasks:
                self.registry.save(t)
            return new_tasks

        new_tasks = await asyncio.get_running_loop().run_in_executor(None, _generate)
        return [self._task_to_orchestrator_dict(t) for t in new_tasks]

    # ── Helpers ───────────────────────────────────────────────────────────

    def _seed_initial_task(self) -> None:
        """Insert a root design task if the registry is empty."""
        existing = self.registry.by_status(TaskStatus.PENDING)
        if existing:
            return
        root = Task(
            title="Initial architecture design",
            description=self._problem,
            type=TaskType.DESIGN,
            subsystem=Subsystem.CROSS_CUTTING,
            status=TaskStatus.PENDING,
            confidence_gap=0.9,
        )
        self.registry.save(root)
        log.info("TaskEngine seeded with initial task '%s'", root.title)

    @staticmethod
    def _task_to_orchestrator_dict(task: Task) -> dict:
        """
        Convert a Task dataclass to the flat dict the Orchestrator uses
        (task["id"], task.get("subsystem"), task.get("tags", []), …).
        """
        d = task.to_dict()
        # Ensure the most-used keys are always present at the top level
        d.setdefault("subsystem", Subsystem.CROSS_CUTTING.value)
        d.setdefault("tags", [])
        d.setdefault("description", "")
        return d

    async def enqueue_exploration_task(
        self,
        title: str = "Explore an under-researched architectural area",
        description: str = (
            "The system has shown signs of research saturation or task starvation. "
            "Identify a part of the design that has received little attention and "
            "propose one or more concrete investigative questions to break the loop."
        ),
        subsystem: "Subsystem | None" = None,
    ) -> dict:
        """
        Create and immediately enqueue an exploration task.

        Called by the orchestrator when the StagnationMonitor fires a
        SPAWN_EXPLORATION or ESCALATE_LOOP directive.  The exploration task
        has a high confidence_gap (0.9) so the priority scorer will surface
        it quickly, injecting fresh exploratory work into the queue.

        Parameters
        ----------
        title       : Short human-readable title for the task.
        description : What the exploration should focus on.
        subsystem   : Optional subsystem to target; defaults to CROSS_CUTTING.

        Returns
        -------
        dict : The newly queued task in orchestrator-dict format.
        """
        target_subsystem = (
            subsystem if subsystem is not None else Subsystem.CROSS_CUTTING
        )
        task = self.generator.make_exploration_task(
            title=title,
            description=description,
            subsystem=target_subsystem,
        )

        def _save() -> None:
            self.registry.save(task)

        await asyncio.get_running_loop().run_in_executor(None, _save)
        log.info(
            "TaskEngine enqueued exploration task '%s' (subsystem=%s)",
            task.title,
            task.subsystem.value,
        )
        return self._task_to_orchestrator_dict(task)

    @property
    def queue_depth(self) -> int:
        return self.queue.depth()

    def stats(self) -> dict[str, Any]:
        return self.queue.stats()

    def get_cost_report(self) -> dict:
        """
        Return aggregate cost statistics across all completed tasks.

        Returns
        -------
        dict with keys:
          - total_tasks_completed  : int
          - total_tokens           : int
          - total_duration_seconds : float
          - by_subsystem           : dict mapping subsystem name to
                                     {"tasks": N, "tokens": T, "duration": D}
        """
        return self.registry.cost_report()
