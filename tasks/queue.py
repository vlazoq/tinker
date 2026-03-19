"""
tasks/queue.py — Task selection and lifecycle management
=========================================================

What this file does
--------------------
The ``TaskQueue`` is the gatekeeper between the task store and the
Orchestrator.  When the Orchestrator asks "what should I work on next?",
the queue answers.

The queue does three things:
  1. Selects the highest-priority PENDING task to give to an agent
     (using scores from PriorityScorer).
  2. Manages lifecycle transitions: PENDING → ACTIVE → COMPLETE / FAILED.
  3. Provides an exploration slot — a small random chance of picking an
     "exploration" task even if a higher-scored regular task is available.

Why the exploration slot?
--------------------------
Without it, Tinker would always pick the task with the highest score.
Over time, scores converge and the same types of tasks keep winning.
The system would get stuck in a loop of similar work — a phenomenon called
"tunnel vision".

The exploration slot reserves 5-10% of task selections for tasks flagged
as ``is_exploration=True``.  This forces Tinker to occasionally venture
into unexpected territory, which can surface valuable new ideas.

The queue as a "view" over the registry
-----------------------------------------
The queue does not store tasks itself.  All task data lives in the
TaskRegistry (the SQLite database).  The queue is a layer of logic on
top of that store — it reads tasks, applies scoring and selection rules,
updates statuses, and writes the results back.

Think of it like a priority-queue data structure, but backed by a
database rather than an in-memory heap.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .schema import Task, TaskStatus
from .scorer import PriorityScorer
from .resolver import DependencyResolver

# Only imported for type hints (avoids circular imports at runtime)
if TYPE_CHECKING:
    from .registry import TaskRegistry

log = logging.getLogger(__name__)


def _now_str() -> str:
    """Return the current UTC time as an ISO-8601 string.

    Kept as a module-level helper so it can be called from static methods
    without needing ``self``.
    """
    return datetime.now(timezone.utc).isoformat()


# =============================================================================
# TaskQueue class
# =============================================================================


class TaskQueue:
    """
    Selects tasks from the registry and manages their lifecycle transitions.

    Parameters
    ----------
    registry : TaskRegistry
        The live database store for all tasks.
    scorer : PriorityScorer | None
        Computes numeric priority for each pending task.
        If None, a default PriorityScorer with standard weights is created.
    resolver : DependencyResolver | None
        Checks and updates task blocking status.
        If None, a default DependencyResolver is created.
    exploration_min_pct : float
        Minimum probability of picking an exploration task over a regular one.
        Default 0.05 (5%).
    exploration_max_pct : float
        Maximum probability of picking an exploration task over a regular one.
        Default 0.10 (10%).  The actual probability is drawn uniformly from
        [exploration_min_pct, exploration_max_pct] on each call to get_next().
    auto_unblock : bool
        If True, run ``resolver.resolve_all()`` at the start of every
        ``get_next()`` call.  This catches any tasks that became unblocked
        since the last call (e.g. if the system was offline).
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
        # Use provided scorer or create one with default weights
        self.scorer = scorer or PriorityScorer()
        # Use provided resolver or create a stateless one
        self.resolver = resolver or DependencyResolver()

        # Store the exploration band as instance variables
        self._expl_min = exploration_min_pct
        self._expl_max = exploration_max_pct

        # If True, scan for newly unblocked tasks on every get_next() call.
        # Slight overhead, but ensures no task is accidentally left in BLOCKED
        # when it's actually ready to run.
        self._auto_unblock = auto_unblock

    # =========================================================================
    # Core public API
    # =========================================================================

    def get_next(self) -> Task | None:
        """Select the highest-priority PENDING task and mark it ACTIVE.

        This is the main entry point for the Orchestrator.  It:
          1. Optionally runs resolve_all() to unblock any newly-eligible tasks.
          2. Fetches all PENDING tasks.
          3. Refreshes each task's staleness_hours field from its creation time.
          4. Scores all pending tasks.
          5. Picks one (either the top-scored task or, occasionally, a random
             exploration task).
          6. Transitions the chosen task from PENDING → ACTIVE and saves it.

        Returns None if there are no PENDING tasks at all.

        Returns
        -------
        The selected Task (now ACTIVE), or None if the queue is empty.
        """
        # Run a full dependency scan to catch any tasks that became unblocked
        # while we weren't looking (e.g. a task completed in another thread,
        # or the system restarted).
        if self._auto_unblock:
            self.resolver.resolve_all(self.registry)

        # Fetch all PENDING tasks from the database
        pending = self.registry.by_status(TaskStatus.PENDING)
        if not pending:
            log.debug("TaskQueue: no pending tasks")
            return None

        # Update each task's staleness_hours based on how long it has been
        # sitting in PENDING.  We do this here (not at creation time) because
        # staleness is a continuous measure that grows over real clock time.
        self._refresh_staleness(pending)

        # Score all pending tasks and sort them descending by priority_score
        scored = self.scorer.score_all(pending)

        # Pick one task (normal pick or exploration pick)
        task = self._pick_task(scored)
        if task is None:
            return None

        # Transition from PENDING → ACTIVE; increments attempt_count
        task.mark_started()
        self.registry.save(task)  # Persist the new ACTIVE status

        log.info(
            "TaskQueue dispatched task '%s' [%s/%s] score=%.4f",
            task.id,
            task.type.value,
            task.subsystem.value,
            task.priority_score,
        )
        return task

    def complete_task(
        self,
        task_id: str,
        outputs: list[str] | None = None,
    ) -> Task | None:
        """Mark a task as COMPLETE and check whether it unblocks any others.

        Called by the Orchestrator after an agent finishes work on a task.

        Parameters
        ----------
        task_id : str
            The ID of the task to complete.
        outputs : list of artefact keys produced by the agent.
            Stored on the task for downstream tasks to reference.

        Returns
        -------
        The completed Task, or None if the task_id wasn't found.
        """
        task = self.registry.get(task_id)
        if task is None:
            log.warning("complete_task: unknown task %s", task_id)
            return None

        # Mark COMPLETE and record any output artefacts
        task.mark_complete(outputs)
        self.registry.save(task)

        # Completing this task may unblock other tasks that were waiting for it
        unblocked = self.resolver.unblock_dependents(task, self.registry)
        if unblocked:
            # Re-score the newly-unblocked tasks so the queue has fresh scores
            # for them before the next get_next() call.
            self.scorer.score_all(unblocked)
            for t in unblocked:
                self.registry.save(t)

        log.info("Task %s completed; %d task(s) unblocked", task_id, len(unblocked))
        return task

    def fail_task(self, task_id: str, reason: str = "") -> Task | None:
        """Mark a task as FAILED and record the reason.

        Used when an agent tries but cannot produce a usable result.
        The task remains in the DB for auditing; it will not be rescheduled
        automatically (a human or anti-stagnation logic can decide later).

        Parameters
        ----------
        task_id : ID of the task to fail.
        reason  : Human-readable description of why the task failed.
        """
        task = self.registry.get(task_id)
        if task is None:
            return None
        task.mark_failed(reason)  # Sets status=FAILED, stores reason in metadata
        self.registry.save(task)
        log.warning("Task %s failed: %s", task_id, reason)
        return task

    def push_to_critique(self, task_id: str) -> Task | None:
        """Transition a task from ACTIVE → CRITIQUE (awaiting review).

        After an Architect agent finishes, the Orchestrator may route the
        result to a Critic agent or human reviewer.  During that waiting
        period, the task is in the CRITIQUE state — neither ACTIVE nor COMPLETE.

        Parameters
        ----------
        task_id : ID of the task to move to CRITIQUE status.
        """
        task = self.registry.get(task_id)
        if task is None:
            return None
        task.status = TaskStatus.CRITIQUE
        task.touch()  # Update the timestamp
        self.registry.save(task)
        return task

    def accept_critique(self, task_id: str, notes: str = "") -> Task | None:
        """Accept the critique and mark the task COMPLETE.

        Called when a reviewer (human or Critic agent) approves the work.
        The review notes are saved on the task for historical reference,
        then ``complete_task()`` is called to trigger the normal completion
        flow (unblocking dependents, recording outputs, etc.).

        Parameters
        ----------
        task_id : ID of the task under review.
        notes   : Reviewer's comments, stored in task.critique_notes.
        """
        task = self.registry.get(task_id)
        if task is None:
            return None
        task.critique_notes = notes  # Save the reviewer's comments
        # Delegate to complete_task() for the full completion flow
        return self.complete_task(task_id)

    def reject_critique(self, task_id: str, notes: str = "") -> Task | None:
        """Reject the critique and return the task to PENDING for a retry.

        Called when a reviewer decides the work needs to be redone.
        The task goes back to PENDING so the queue can schedule it again.
        The rejection notes are preserved so the next agent can learn from them.

        Parameters
        ----------
        task_id : ID of the task under review.
        notes   : Reviewer's comments explaining why it was rejected.
        """
        task = self.registry.get(task_id)
        if task is None:
            return None
        task.critique_notes = notes  # Save the rejection reason
        task.status = TaskStatus.PENDING  # Put it back in the work queue
        task.touch()
        self.registry.save(task)
        log.info("Task %s returned to PENDING after critique", task_id)
        return task

    # =========================================================================
    # Queue introspection
    # =========================================================================

    def depth(self) -> int:
        """Return the number of PENDING tasks (how much work is waiting)."""
        return len(self.registry.by_status(TaskStatus.PENDING))

    def stats(self) -> dict:
        """Return a summary of the current queue state.

        Returns a dict with:
          - "counts"         : {status_string: count} for all statuses
          - "depth"          : number of pending tasks
          - "top_task"       : title of the highest-scored pending task
          - "top_score"      : the score of that task
          - "exploration_pct": the configured exploration band as a string
        """
        counts = self.registry.count_by_status()
        pending = self.registry.by_status(TaskStatus.PENDING)
        # Score the pending tasks to know which one is currently "top"
        scored = self.scorer.score_all(pending)
        return {
            "counts": counts,
            "depth": counts.get("pending", 0),
            "top_task": scored[0].title if scored else None,
            "top_score": scored[0].priority_score if scored else 0.0,
            # Show the exploration band as a human-readable percentage range
            "exploration_pct": f"{self._expl_min * 100:.0f}-{self._expl_max * 100:.0f}%",
        }

    # =========================================================================
    # Internal helpers
    # =========================================================================

    def _pick_task(self, scored: list[Task]) -> Task | None:
        """Choose which task to dispatch from the scored, sorted list.

        Normally returns ``scored[0]`` — the highest-priority task.
        But with a small random probability (between _expl_min and _expl_max),
        it instead picks a random task from the exploration pool.

        This is the "exploration slot" mechanism described in the module
        docstring.  The randomness here is intentional and deliberate.

        Parameters
        ----------
        scored : All PENDING tasks, scored and sorted (highest first).
                 Already mutated in-place by scorer.score_all().

        Returns
        -------
        The chosen Task, or None if the list is empty.
        """
        if not scored:
            return None

        # Split tasks into exploration vs. regular pools
        exploration_pool = [t for t in scored if t.is_exploration]
        # Roll dice for the exploration slot
        if exploration_pool:
            # Draw the threshold uniformly from [_expl_min, _expl_max]
            exploration_threshold = random.uniform(self._expl_min, self._expl_max)
            # Draw a random number; if it's below the threshold, use exploration
            if random.random() < exploration_threshold:
                # Pick any exploration task at random (not necessarily the
                # highest-scored one — the whole point is to diversify)
                chosen = random.choice(exploration_pool)
                log.debug("Exploration slot activated → '%s'", chosen.title)
                return chosen

        # Default: return the highest-scoring task (first in the sorted list)
        return scored[0]

    @staticmethod
    def _refresh_staleness(tasks: list[Task]) -> None:
        """Update each task's staleness_hours based on its creation time.

        Staleness is the number of hours a task has been sitting in PENDING.
        We calculate it fresh on every get_next() call rather than storing
        a fixed value, because a task's staleness grows continuously with
        real clock time.

        Mutates the tasks in-place — does not save to the registry.
        (The registry will be updated when the task is saved after scoring.)

        Parameters
        ----------
        tasks : The list of PENDING tasks to update.
        """
        now = datetime.now(timezone.utc)
        for task in tasks:
            try:
                # Parse the created_at ISO string back into a datetime
                created = datetime.fromisoformat(task.created_at)

                # If the timestamp has no timezone info (e.g. from old data),
                # assume it was UTC so the subtraction works correctly.
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)

                # Calculate how many hours the task has been waiting
                delta_h = (now - created).total_seconds() / 3600.0
                task.staleness_hours = max(0.0, delta_h)

            except (ValueError, TypeError):
                # If the timestamp is malformed, skip this task silently.
                # A staleness of 0.0 (the default) is a safe fallback.
                pass
