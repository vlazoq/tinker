"""
tinker/task_engine/scorer.py
─────────────────────────────
PriorityScorer

Computes a single priority_score ∈ [0, 1] for a PENDING task using the
five canonical factors:

  1. Confidence gap      – how uncertain is the current understanding
  2. Recency             – how recently was related work done (inverse)
  3. Staleness           – how long has this task been waiting (boosts)
  4. Dependency depth    – fewer unresolved ancestors → higher score
  5. Exploration bonus   – reserved 5-10% random slot to avoid tunnel vision

Formula (weighted sum, then normalised):
  score = w1*confidence_gap
        + w2*recency_score
        + w3*staleness_score
        + w4*depth_score
        + w5*type_bonus
  + exploration_bump (if is_exploration)

All weights are configurable; the defaults reflect Tinker's priorities.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from .schema import Task, TaskType


@dataclass
class ScorerWeights:
    """Tunable knobs for the five scoring dimensions."""
    confidence_gap:    float = 0.30   # How much to value high-uncertainty tasks
    recency:           float = 0.20   # How much to value not re-working same subsystem
    staleness:         float = 0.20   # Prevent indefinite starvation
    dependency_depth:  float = 0.15   # Prefer shallower (more unblocked) tasks
    type_bonus:        float = 0.15   # Reward higher-value task types
    exploration_bump:  float = 0.10   # Added score for exploration-slot tasks

    # Decay constants
    recency_half_life_hours: float = 4.0    # After 4 h, recency score = 0.5
    staleness_sat_hours:     float = 24.0   # After 24 h, staleness score saturates


# Type-value table — reflects how much each type advances the system
_TYPE_VALUES: dict[TaskType, float] = {
    TaskType.SYNTHESIS:   1.0,
    TaskType.DESIGN:      0.9,
    TaskType.VALIDATION:  0.8,
    TaskType.CRITIQUE:    0.7,
    TaskType.RESEARCH:    0.6,
    TaskType.EXPLORATION: 0.5,
}


class PriorityScorer:
    """
    Stateless scorer — call score(task) and get a float back.

    Design choice: scorer does NOT touch the registry; it works from the
    data already embedded in the Task dataclass so it can be used in tests
    without a live DB.
    """

    def __init__(self, weights: ScorerWeights | None = None):
        self.w = weights or ScorerWeights()

    # ── Public API ────────────────────────────────────────────────────────

    def score(self, task: Task) -> float:
        """Return a priority score ∈ [0, 1] for the given task."""
        components = {
            "confidence_gap":   self.w.confidence_gap   * self._confidence_component(task),
            "recency":          self.w.recency           * self._recency_component(task),
            "staleness":        self.w.staleness         * self._staleness_component(task),
            "dependency_depth": self.w.dependency_depth  * self._depth_component(task),
            "type_bonus":       self.w.type_bonus        * self._type_component(task),
        }
        raw = sum(components.values())

        # Exploration slot bump (additive, not weighted)
        if task.is_exploration:
            raw = min(1.0, raw + self.w.exploration_bump)

        # Light random jitter (±1 %) to break exact ties naturally
        jitter = random.uniform(-0.01, 0.01)
        final = max(0.0, min(1.0, raw + jitter))
        return round(final, 6)

    def score_and_update(self, task: Task) -> Task:
        """Score the task and write the result back to task.priority_score."""
        task.priority_score = self.score(task)
        return task

    def score_all(self, tasks: list[Task]) -> list[Task]:
        """Score a batch of tasks in-place and return them sorted desc."""
        for t in tasks:
            self.score_and_update(t)
        return sorted(tasks, key=lambda t: t.priority_score, reverse=True)

    # ── Scoring components (each returns 0-1) ─────────────────────────────

    @staticmethod
    def _confidence_component(task: Task) -> float:
        """Directly use the confidence_gap field — already ∈ [0, 1]."""
        return max(0.0, min(1.0, task.confidence_gap))

    def _recency_component(self, task: Task) -> float:
        """
        Score is HIGH when the subsystem was last worked on a LONG time ago.
        Uses exponential decay from last_subsystem_work_hours.
        Score → 1.0 as time since last work → ∞
        Score → 0.0 as time since last work → 0
        """
        h = max(0.0, task.last_subsystem_work_hours)
        half_life = self.w.recency_half_life_hours
        # Inverse-decay: long gap → high score
        return 1.0 - math.exp(-math.log(2) * h / half_life)

    def _staleness_component(self, task: Task) -> float:
        """
        Prevent starvation: tasks that have been waiting grow in priority.
        Uses a sigmoid that saturates at staleness_sat_hours.
        """
        h = max(0.0, task.staleness_hours)
        sat = self.w.staleness_sat_hours
        # Sigmoid-like saturation
        return 1.0 - math.exp(-h / sat)

    @staticmethod
    def _depth_component(task: Task) -> float:
        """
        Prefer tasks with fewer ancestors (shallower dependency chains run first).
        depth=0 → 1.0, depth=5+ → ~0.0 (exponential decay)
        """
        d = max(0, task.dependency_depth)
        return math.exp(-0.5 * d)

    @staticmethod
    def _type_component(task: Task) -> float:
        return _TYPE_VALUES.get(task.type, 0.5)

    # ── Diagnostic ────────────────────────────────────────────────────────

    def explain(self, task: Task) -> dict[str, float]:
        """Return a breakdown of each component × weight for debugging."""
        return {
            "confidence_gap":   self.w.confidence_gap   * self._confidence_component(task),
            "recency":          self.w.recency           * self._recency_component(task),
            "staleness":        self.w.staleness         * self._staleness_component(task),
            "dependency_depth": self.w.dependency_depth  * self._depth_component(task),
            "type_bonus":       self.w.type_bonus        * self._type_component(task),
            "exploration_bump": self.w.exploration_bump if task.is_exploration else 0.0,
            "raw_total":        self.score(task),
        }
