"""
tinker/task_engine/queue.py
────────────────────────────
TaskQueue

In-memory priority queue backed by the TaskRegistry.

Design decisions
────────────────
• The queue is a *view* over the registry — it does not own tasks.
• get_next() refreshes scores and returns the highest-priority PENDING task,
  with a 5-10 % slot reserved for exploration tasks to prevent tunnel vision.
• The exploration slot is enforced probabilistically:
    - If a random draw lands in the exploration band AND an exploration
      task exists, it is returned regardless of score order.
    - This ensures exploration tasks are never starved but also never
      dominate the queue.
• On return from get_next(), the task is atomically transitioned to ACTIVE.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .schema import Task, TaskStatus
from .scorer import PriorityScorer, ScorerWeights
from .resolver import DependencyResolver

if TYPE_CHECKING:
    from .registry import TaskRegistry

log = logging.getLogger(__name__)


def _now_str() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskQueue:
    """
    Manages task selection and lifecycle transitions for the Orchestrator.

    Parameters
    ──────────
    registry:              The live TaskRegistry instance.
    scorer:                Optional PriorityScorer (default weights used if None).
    resolver:              Optional DependencyResolver.
    exploration_min_pct:   Minimum % of slots reserved for exploration tasks.
    exploration_max_pct:   Maximum % of slots reserved for exploration tasks.
    auto_unblock:          If True, resolver.resolve_all() is called on each
                           get_next() call to catch any newly-unblocked tasks.
    """

    def __init__(
        self,
        registry: "TaskRegistry",
        scorer: PriorityScorer | None = None,
        resolver: DependencyResolver | None = None,
        exploration_min_pct: float = 0.05,
        exploration_max_pct: float = 0.10,
        auto_unblock: bool = True,
    ):
        self.registry = registry
        self.scorer   = scorer or PriorityScorer()
        self.resolver = resolver or DependencyResolver()
        self._expl_min = exploration_min_pct
        self._expl_max = exploration_max_pct
        self._auto_unblock = auto_unblock

    # ── Core API ──────────────────────────────────────────────────────────

    def get_next(self) -> Task | None:
        """
        Return the highest-priority PENDING task and mark it ACTIVE.
        Returns None if no PENDING tasks are available.

        Exploration slot: with probability in [exploration_min_pct,
        exploration_max_pct] the next task is drawn from the exploration
        pool instead of the top of the scored queue.
        """
        if self._auto_unblock:
            self.resolver.resolve_all(self.registry)

        pending = self.registry.by_status(TaskStatus.PENDING)
        if not pending:
            log.debug("TaskQueue: no pending tasks")
            return None

        # Refresh staleness before scoring
        self._refresh_staleness(pending)

        # Score all
        scored = self.scorer.score_all(pending)

        # Decide whether to use the exploration slot
        task = self._pick_task(scored)
        if task is None:
            return None

        # Transition to ACTIVE
        task.mark_started()
        self.registry.save(task)
        log.info(
            "TaskQueue dispatched task '%s' [%s/%s] score=%.4f",
            task.id, task.type.value, task.subsystem.value, task.priority_score,
        )
        return task

    def complete_task(
        self,
        task_id: str,
        outputs: list[str] | None = None,
    ) -> Task | None:
        """Mark a task COMPLETE and unblock any dependents."""
        task = self.registry.get(task_id)
        if task is None:
            log.warning("complete_task: unknown task %s", task_id)
            return None
        task.mark_complete(outputs)
        self.registry.save(task)
        unblocked = self.resolver.unblock_dependents(task, self.registry)
        if unblocked:
            # Re-score newly unblocked tasks
            self.scorer.score_all(unblocked)
            for t in unblocked:
                self.registry.save(t)
        log.info("Task %s completed; %d task(s) unblocked", task_id, len(unblocked))
        return task

    def fail_task(self, task_id: str, reason: str = "") -> Task | None:
        """Mark a task FAILED."""
        task = self.registry.get(task_id)
        if task is None:
            return None
        task.mark_failed(reason)
        self.registry.save(task)
        log.warning("Task %s failed: %s", task_id, reason)
        return task

    def push_to_critique(self, task_id: str) -> Task | None:
        """Transition ACTIVE → CRITIQUE (waiting for human/agent review)."""
        task = self.registry.get(task_id)
        if task is None:
            return None
        task.status = TaskStatus.CRITIQUE
        task.touch()
        self.registry.save(task)
        return task

    def accept_critique(self, task_id: str, notes: str = "") -> Task | None:
        """Transition CRITIQUE → COMPLETE after review."""
        task = self.registry.get(task_id)
        if task is None:
            return None
        task.critique_notes = notes
        return self.complete_task(task_id)

    def reject_critique(self, task_id: str, notes: str = "") -> Task | None:
        """Transition CRITIQUE → PENDING (retry)."""
        task = self.registry.get(task_id)
        if task is None:
            return None
        task.critique_notes = notes
        task.status = TaskStatus.PENDING
        task.touch()
        self.registry.save(task)
        log.info("Task %s returned to PENDING after critique", task_id)
        return task

    # ── Queue introspection ───────────────────────────────────────────────

    def depth(self) -> int:
        """Number of PENDING tasks."""
        return len(self.registry.by_status(TaskStatus.PENDING))

    def stats(self) -> dict:
        counts = self.registry.count_by_status()
        pending = self.registry.by_status(TaskStatus.PENDING)
        scored = self.scorer.score_all(pending)
        return {
            "counts":          counts,
            "depth":           counts.get("pending", 0),
            "top_task":        scored[0].title if scored else None,
            "top_score":       scored[0].priority_score if scored else 0.0,
            "exploration_pct": f"{self._expl_min*100:.0f}-{self._expl_max*100:.0f}%",
        }

    # ── Internals ─────────────────────────────────────────────────────────

    def _pick_task(self, scored: list[Task]) -> Task | None:
        if not scored:
            return None

        exploration_pool = [t for t in scored if t.is_exploration]
        regular_pool     = [t for t in scored if not t.is_exploration]

        # Roll dice for exploration slot
        if exploration_pool:
            exploration_threshold = random.uniform(
                self._expl_min, self._expl_max
            )
            if random.random() < exploration_threshold:
                chosen = random.choice(exploration_pool)
                log.debug("Exploration slot activated → '%s'", chosen.title)
                return chosen

        # Return highest-scoring task (first in scored list, already sorted)
        return scored[0]

    @staticmethod
    def _refresh_staleness(tasks: list[Task]) -> None:
        """
        Recalculate staleness_hours for each task based on created_at.
        Mutates tasks in-place.
        """
        now = datetime.now(timezone.utc)
        for task in tasks:
            try:
                created = datetime.fromisoformat(task.created_at)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                delta_h = (now - created).total_seconds() / 3600.0
                task.staleness_hours = max(0.0, delta_h)
            except (ValueError, TypeError):
                pass
