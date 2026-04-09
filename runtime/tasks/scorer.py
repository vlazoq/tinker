"""
runtime/tasks/scorer.py — Priority scoring for the task queue
======================================================

What this file does
--------------------
The ``PriorityScorer`` takes a Task and returns a single number between 0
and 1.  Higher scores mean "work on this sooner".  The TaskQueue uses these
scores to sort the work pile and pick the most important next task.

Why do we need scoring?
------------------------
At any given moment Tinker may have dozens of pending tasks across many
subsystems.  Without a principled way of choosing which task to do next,
Tinker might work on easy/unimportant things and ignore critical unknowns.

The scoring formula blends five signals, each capturing a different reason
why a task might deserve priority:

  1. Confidence gap   — how uncertain is the current architecture in this area?
                        (uncertain = urgent, we need answers)
  2. Recency          — how long ago did we work on this subsystem?
                        (long gap = we should revisit it)
  3. Staleness        — how long has this specific task been waiting?
                        (long wait = danger of starvation — it might never run)
  4. Dependency depth — how many prerequisites does this task have?
                        (fewer prerequisites = readier to run)
  5. Task-type bonus  — what kind of task is it?
                        (synthesis > design > validation > critique > research > exploration)

Formula (simplified):
  score = (w1 × gap) + (w2 × recency) + (w3 × staleness)
        + (w4 × depth) + (w5 × type_bonus)
  + exploration_bump   (if task.is_exploration)
  + tiny_jitter        (random ±1% to break exact ties)

All weights are configurable through ``ScorerWeights``.

Why keep the scorer stateless?
--------------------------------
The scorer only reads from the Task object itself — it never touches the
database.  This makes it:
  - Easy to test: just create a Task with known values and call score().
  - Easy to reason about: the score is fully determined by the task's fields.
  - Fast: no I/O or network calls.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from .schema import Task, TaskType

# =============================================================================
# ScorerWeights — configuration knobs
# =============================================================================


@dataclass
class ScorerWeights:
    """
    Tunable weights for the five scoring dimensions.

    Each weight is a fraction; together they represent how much each signal
    contributes to the final score.  The default values were chosen to
    reflect Tinker's priorities:
      - Uncertainty (confidence_gap) is the most important signal: we want
        Tinker to explore what it doesn't know.
      - Staleness and recency are equally important anti-stagnation mechanisms.
      - Dependency depth is a tiebreaker for equally-weighted tasks.
      - Task type adds a modest bonus for higher-value work.

    The weights don't have to sum to 1.0 — the final score is clamped to
    [0, 1] after summing.

    Fields
    ------
    confidence_gap       : Weight for "how uncertain is this area?" signal.
    recency              : Weight for "how long since we worked on this subsystem?" signal.
    staleness            : Weight for "how long has this task been waiting?" signal.
    dependency_depth     : Weight for "how shallow is this task in the dep tree?" signal.
    type_bonus           : Weight for the task-type value lookup.
    exploration_bump     : Additive bonus for tasks flagged as exploration tasks.
    recency_half_life_hours : The recency score decays to 0.5 after this many hours.
    staleness_sat_hours  : The staleness score saturates (plateaus) after this many hours.
    """

    confidence_gap: float = 0.30  # Most important: explore what we don't know
    recency: float = 0.20  # Revisit neglected subsystems
    staleness: float = 0.20  # Prevent any task from waiting forever
    dependency_depth: float = 0.15  # Prefer shallower (less blocked) tasks
    type_bonus: float = 0.15  # Reward higher-value task types

    # This is additive (not a weight), applied after the weighted sum.
    # It gives exploration tasks a small push so they aren't always beaten
    # by highly-scored regular tasks.
    exploration_bump: float = 0.10

    # Decay/saturation constants used in the math below.
    recency_half_life_hours: float = 4.0  # After 4 hours, recency score = 0.5
    staleness_sat_hours: float = 24.0  # After 24 hours, staleness score saturates


# =============================================================================
# Task-type value table
# =============================================================================
# Different task types have different "strategic value" to Tinker.
# A SYNTHESIS that merges multiple proposals is more valuable than basic
# EXPLORATION.  This table encodes that intuition.

_TYPE_VALUES: dict[TaskType, float] = {
    TaskType.SYNTHESIS: 1.0,  # Highest value: brings strands of work together
    TaskType.DESIGN: 0.9,  # Core architectural output
    TaskType.VALIDATION: 0.8,  # Confirming or refuting claims
    TaskType.CRITIQUE: 0.7,  # Identifying weaknesses
    TaskType.RESEARCH: 0.6,  # Background investigation
    TaskType.EXPLORATION: 0.5,  # Lowest value: open-ended, unpredictable
}


# =============================================================================
# PriorityScorer class
# =============================================================================


class PriorityScorer:
    """
    Computes a priority score in [0, 1] for a single PENDING Task.

    Usage
    -----
        scorer = PriorityScorer()
        score = scorer.score(task)          # Returns a float
        tasks = scorer.score_all(task_list) # Scores and sorts a list

    Stateless design
    ----------------
    The scorer holds only its weights (``self.w``) and an optional focus
    subsystem.  Every score is computed fresh from the data already stored
    on the Task object.

    Parameters
    ----------
    weights : ScorerWeights | None
        Custom weights.  If None, the default weights are used.
    depth_first_weight : float
        Additive bonus for tasks whose subsystem matches
        ``focus_subsystem``.  0.0 disables depth-first mode.
    focus_subsystem : str | None
        The subsystem to prioritise.  Updated by the orchestrator after
        each micro loop so the scorer keeps working on the same area.
    """

    def __init__(
        self,
        weights: ScorerWeights | None = None,
        seed: int | None = None,
        depth_first_weight: float = 0.0,
        focus_subsystem: str | None = None,
    ):
        # Use the provided weights, or fall back to the defaults
        self.w = weights or ScorerWeights()
        # Optional seed for reproducible jitter (useful in tests)
        self._rng = random.Random(seed)
        # Depth-first mode: prefer tasks in the same subsystem
        self.depth_first_weight = depth_first_weight
        self.focus_subsystem = focus_subsystem

    # =========================================================================
    # Public API
    # =========================================================================

    def score(self, task: Task) -> float:
        """Compute and return a priority score for the given task.

        The score is a float in [0, 1].  Higher = pick this task sooner.

        Steps:
          1. Compute each of the five weighted components.
          2. Sum them.
          3. Add an exploration bump if the task is marked for exploration.
          4. Add a tiny random jitter to break exact ties.
          5. Clamp to [0, 1] and round to 6 decimal places.

        Returns
        -------
        float in [0.0, 1.0]
        """
        # Calculate each component multiplied by its weight.
        # Each _*_component() method returns a raw value in [0, 1],
        # so multiplying by the weight scales it to [0, weight].
        components = {
            "confidence_gap": self.w.confidence_gap * self._confidence_component(task),
            "recency": self.w.recency * self._recency_component(task),
            "staleness": self.w.staleness * self._staleness_component(task),
            "dependency_depth": self.w.dependency_depth * self._depth_component(task),
            "type_bonus": self.w.type_bonus * self._type_component(task),
        }

        # Sum all five weighted components into a single raw score
        raw = sum(components.values())

        # Exploration bump: add a flat bonus ON TOP of the weighted sum.
        # This is additive (not weighted) so it always gives exploration tasks
        # a push regardless of how their other signals look.
        if task.is_exploration:
            raw = min(1.0, raw + self.w.exploration_bump)

        # Depth-first bonus: if we're currently focused on a subsystem,
        # give tasks in that same subsystem an additive boost.  This keeps
        # the system working on one area until it's solid, rather than
        # constantly switching between subsystems.
        if (
            self.depth_first_weight > 0.0
            and self.focus_subsystem is not None
            and self._subsystem_match(task)
        ):
            raw = min(1.0, raw + self.depth_first_weight)

        # Random jitter of ±1% to break exact ties naturally.
        # Without this, two tasks with identical inputs would always be
        # returned in the same (arbitrary) order, making the queue less diverse.
        jitter = self._rng.uniform(-0.01, 0.01)

        # Clamp to [0, 1] — jitter could push a score just outside the range
        final = max(0.0, min(1.0, raw + jitter))
        return round(final, 6)  # 6 decimal places is plenty of precision

    def score_and_update(self, task: Task) -> Task:
        """Score the task and store the result in task.priority_score.

        This mutates the task in-place.  After calling this, you should
        save the task to the registry so the DB reflects the new score.

        Returns the same task object for convenient chaining.
        """
        task.priority_score = self.score(task)
        return task

    def score_all(self, tasks: list[Task]) -> list[Task]:
        """Score every task in the list in-place, then return them sorted.

        The returned list is sorted descending by priority_score, so
        ``result[0]`` is always the highest-priority task.

        Mutates the ``priority_score`` field on each task.
        """
        for t in tasks:
            self.score_and_update(t)
        # sorted() with reverse=True puts the highest score first
        return sorted(tasks, key=lambda t: t.priority_score, reverse=True)

    # =========================================================================
    # Scoring components — each returns a value in [0, 1]
    # =========================================================================
    # These are "pure" functions: given the same task, they always return
    # the same value.  No randomness, no external state.

    @staticmethod
    def _confidence_component(task: Task) -> float:
        """Return the confidence-gap signal, clamped to [0, 1].

        ``task.confidence_gap`` is set by whoever creates the task (usually
        the TaskGenerator parsing Architect output).  A value of 0.9 means
        "we're very unsure about this"; 0.1 means "we're fairly confident".

        We just pass it through (clamped for safety); no transformation needed.
        """
        return max(0.0, min(1.0, task.confidence_gap))

    def _recency_component(self, task: Task) -> float:
        """Return a score based on how long since we worked on this subsystem.

        The intuition: if we worked on the memory_manager just 30 minutes ago,
        we probably shouldn't immediately work on it again — give other
        subsystems a chance.

        Math: exponential decay starting from 1.0 as time-since-last-work
        grows from 0.  The score reaches 0.5 at ``recency_half_life_hours``.

            score = 1 - e^(-ln(2) * hours / half_life)

        This gives us a smooth curve:
          - 0 hours since last work  → score ≈ 0.0 (don't prioritise)
          - 4 hours since last work  → score ≈ 0.5
          - 12 hours since last work → score ≈ 0.87
          - Very long gap            → score → 1.0
        """
        h = max(0.0, task.last_subsystem_work_hours)
        half_life = self.w.recency_half_life_hours
        # Inverse-decay: the longer the gap, the higher the score.
        # math.log(2) ≈ 0.693 — this ensures the score hits 0.5 at half_life.
        return 1.0 - math.exp(-math.log(2) * h / half_life)

    def _staleness_component(self, task: Task) -> float:
        """Return a score based on how long this task has been waiting.

        Purpose: prevent "starvation" — a low-priority task should eventually
        get picked even if higher-priority tasks keep arriving, otherwise
        it might sit in the queue forever.

        Math: exponential saturation.  The score rises quickly at first, then
        levels off as staleness grows (the task doesn't become infinitely
        important just because it's old):

            score = 1 - e^(-hours / saturation_hours)

          - 0 hours waiting  → score ≈ 0.0
          - 24 hours waiting → score ≈ 0.63
          - 48 hours waiting → score ≈ 0.86
          - Very long wait   → score → 1.0 (saturates)
        """
        h = max(0.0, task.staleness_hours)
        sat = self.w.staleness_sat_hours
        # Sigmoid-like saturation: approaches 1.0 but never exceeds it
        return 1.0 - math.exp(-h / sat)

    @staticmethod
    def _depth_component(task: Task) -> float:
        """Return a score that favours shallower tasks.

        A task at depth 0 has no prerequisites — it's completely free to run.
        A task at depth 5 is five levels deep in a dependency chain, meaning
        several other tasks must finish before this one can start.

        We prefer to work shallow-to-deep (finish prerequisites first), so
        shallower tasks get higher scores.

        Math: exponential decay as depth increases.

            score = e^(-0.5 * depth)

          - depth 0 → score = 1.0
          - depth 2 → score ≈ 0.37
          - depth 5 → score ≈ 0.08
          - depth → ∞ → score → 0.0
        """
        d = max(0, task.dependency_depth)
        # -0.5 controls how fast the score drops; adjust the weight if needed
        return math.exp(-0.5 * d)

    @staticmethod
    def _type_component(task: Task) -> float:
        """Return the strategic-value score for this task's type.

        Looks up the task type in the _TYPE_VALUES table.
        If the type isn't in the table for some reason, defaults to 0.5
        (middle of the road).
        """
        return _TYPE_VALUES.get(task.type, 0.5)

    def _subsystem_match(self, task: Task) -> bool:
        """Return True if the task belongs to the current focus subsystem."""
        task_sub = getattr(task, "subsystem", None)
        if task_sub is None:
            return False
        # Subsystem may be an enum or a plain string
        task_sub_str = task_sub.value if hasattr(task_sub, "value") else str(task_sub)
        focus_str = (
            self.focus_subsystem.value
            if hasattr(self.focus_subsystem, "value")
            else str(self.focus_subsystem)
        )
        return task_sub_str == focus_str

    # =========================================================================
    # Diagnostic helper
    # =========================================================================

    def explain(self, task: Task) -> dict[str, float]:
        """Return a human-readable breakdown of how the score was calculated.

        Useful for debugging, logging, and understanding why the queue
        picked one task over another.

        Example output:
            {
              "confidence_gap":   0.27,
              "recency":          0.14,
              "staleness":        0.08,
              "dependency_depth": 0.15,
              "type_bonus":       0.135,
              "exploration_bump": 0.0,
              "raw_total":        0.762
            }
        """
        return {
            "confidence_gap": self.w.confidence_gap * self._confidence_component(task),
            "recency": self.w.recency * self._recency_component(task),
            "staleness": self.w.staleness * self._staleness_component(task),
            "dependency_depth": self.w.dependency_depth * self._depth_component(task),
            "type_bonus": self.w.type_bonus * self._type_component(task),
            # Show 0.0 for exploration_bump when the task isn't an exploration task
            "exploration_bump": self.w.exploration_bump if task.is_exploration else 0.0,
            "depth_first_bonus": (
                self.depth_first_weight
                if self.depth_first_weight > 0.0
                and self.focus_subsystem is not None
                and self._subsystem_match(task)
                else 0.0
            ),
            "raw_total": self.score(task),
        }
