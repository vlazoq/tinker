"""
runtime/orchestrator/self_improvement.py
==========================

Tinker Self-Improvement Engine — lets Tinker improve itself.

What this file does
-------------------
This module enables Tinker to analyze its own performance metrics,
identify weaknesses, and generate improvement tasks targeting its own
codebase.  Think of it as a "retrospective" that runs automatically
at the end of each macro loop.

How it works
------------
1. **Prompt auto-tuning**: After each macro loop, compares critic scores
   over time.  If scores trend downward for a subsystem, adjusts the
   Architect's system prompt to focus on that weakness.

2. **Config auto-tuning**: Tracks stagnation events.  If the same
   detector fires repeatedly, adjusts orchestrator parameters (e.g.
   temperature, meso trigger frequency) to break the pattern.

3. **Self-targeting task generation**: Analyzes the DLQ, error logs, and
   low-scoring artifacts to generate tasks like "refactor module X" or
   "improve error handling in Y".

Safety rails
------------
- Self-modifications ONLY happen on a dedicated branch (never main).
- All tests must pass before Fritz commits any self-improvement.
- A confirmation gate blocks merges into main — human review required.
- Temperature adjustments are capped (±0.1 from baseline).
- Max 3 self-improvement tasks per macro cycle to prevent runaway changes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Maximum temperature deviation from baseline (prevents wild outputs)
_MAX_TEMP_DELTA = 0.1
# Maximum self-improvement tasks generated per macro cycle
_MAX_SELF_TASKS_PER_CYCLE = 3
# Score trend window — how many recent scores to consider
_SCORE_WINDOW = 10
# Stagnation threshold — if the same detector fires this many times in a
# row, trigger config auto-tuning
_STAGNATION_REPEAT_THRESHOLD = 3


@dataclass
class PerformanceSnapshot:
    """
    A snapshot of Tinker's recent performance metrics.

    Collected at the end of each macro loop and fed into the self-improvement
    engine to decide whether adjustments are needed.

    Fields
    ------
    subsystem_scores : dict[str, list[float]]
        Recent critic scores per subsystem (last N iterations).
    stagnation_events : list[dict[str, Any]]
        Recent anti-stagnation detector firings.
    dlq_entries : list[dict[str, Any]]
        Recent dead-letter queue entries (failed operations).
    error_counts : dict[str, int]
        Count of errors by exception class name.
    loop_durations : list[float]
        Recent micro loop durations in seconds.
    """

    subsystem_scores: dict[str, list[float]] = field(default_factory=dict)
    stagnation_events: list[dict[str, Any]] = field(default_factory=list)
    dlq_entries: list[dict[str, Any]] = field(default_factory=list)
    error_counts: dict[str, int] = field(default_factory=dict)
    loop_durations: list[float] = field(default_factory=list)


@dataclass
class SelfImprovementAction:
    """
    A single improvement action that Tinker will take on itself.

    Fields
    ------
    action_type : str
        One of: "prompt_adjustment", "config_adjustment", "task_generation".
    description : str
        Human-readable explanation of what this action does and why.
    target : str
        What is being modified (e.g. "architect_system_prompt", "temperature",
        "module:core/llm/router.py").
    parameters : dict[str, Any]
        Action-specific parameters (e.g. {"delta": 0.05} for temp adjustment).
    confidence : float
        How confident the engine is that this action will help (0.0–1.0).
    """

    action_type: str
    description: str
    target: str
    parameters: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.5


class SelfImprovementEngine:
    """
    Analyzes Tinker's performance and generates self-improvement actions.

    This engine runs at the end of each macro loop (every ~4 hours).  It
    looks at recent performance data and decides whether Tinker needs to
    adjust its own behaviour.

    The engine follows a conservative philosophy:
    - Small, incremental adjustments (not radical rewrites).
    - All changes are reversible.
    - Human approval required before merging self-improvements to main.
    - Caps on how much any single parameter can drift from its baseline.

    Parameters
    ----------
    baseline_temperature : float
        The original temperature setting.  Auto-tuning won't deviate more
        than ±_MAX_TEMP_DELTA from this value.
    self_improve_branch : str
        Git branch name for self-improvement commits.  Default:
        "tinker/self-improve".
    enabled : bool
        Master switch.  When False, analyze() returns an empty list.
    """

    def __init__(
        self,
        baseline_temperature: float = 0.7,
        self_improve_branch: str = "tinker/self-improve",
        enabled: bool = True,
    ) -> None:
        self._baseline_temp = baseline_temperature
        self._branch = self_improve_branch
        self._enabled = enabled
        # Track consecutive stagnation events by detector name
        self._stagnation_streak: dict[str, int] = {}
        # Track previous prompt adjustments to avoid oscillation
        self._prompt_history: list[str] = []
        logger.info(
            "SelfImprovementEngine initialized (enabled=%s, branch=%s)",
            enabled,
            self_improve_branch,
        )

    def analyze(self, snapshot: PerformanceSnapshot) -> list[SelfImprovementAction]:
        """
        Analyze recent performance and return a list of improvement actions.

        This is the main entry point.  Call it at the end of each macro loop
        with a fresh PerformanceSnapshot.

        Returns at most _MAX_SELF_TASKS_PER_CYCLE actions to prevent
        runaway self-modification.

        Parameters
        ----------
        snapshot : PerformanceSnapshot
            Recent performance data collected by the orchestrator.

        Returns
        -------
        list[SelfImprovementAction]
            Actions to take.  Empty list if no improvements needed or if
            the engine is disabled.
        """
        if not self._enabled:
            return []

        actions: list[SelfImprovementAction] = []

        # --- 1. Prompt auto-tuning ---
        prompt_actions = self._analyze_score_trends(snapshot.subsystem_scores)
        actions.extend(prompt_actions)

        # --- 2. Config auto-tuning ---
        config_actions = self._analyze_stagnation(snapshot.stagnation_events)
        actions.extend(config_actions)

        # --- 3. Self-targeting task generation ---
        task_actions = self._analyze_failures(snapshot.dlq_entries, snapshot.error_counts)
        actions.extend(task_actions)

        # Cap the number of actions per cycle
        if len(actions) > _MAX_SELF_TASKS_PER_CYCLE:
            # Sort by confidence (highest first) and take the top N
            actions.sort(key=lambda a: a.confidence, reverse=True)
            actions = actions[:_MAX_SELF_TASKS_PER_CYCLE]
            logger.info(
                "Capped self-improvement actions to %d (had %d candidates)",
                _MAX_SELF_TASKS_PER_CYCLE,
                len(actions),
            )

        if actions:
            logger.info(
                "Self-improvement engine produced %d action(s): %s",
                len(actions),
                [a.action_type for a in actions],
            )

        return actions

    def _analyze_score_trends(
        self, subsystem_scores: dict[str, list[float]]
    ) -> list[SelfImprovementAction]:
        """
        Detect declining critic scores and suggest prompt adjustments.

        For each subsystem, compares the average score of the first half
        of the window to the second half.  If the second half is
        significantly lower (>0.1 drop), suggests a prompt adjustment
        telling the Architect to focus on that subsystem's weaknesses.
        """
        actions: list[SelfImprovementAction] = []

        for subsystem, scores in subsystem_scores.items():
            if len(scores) < _SCORE_WINDOW:
                continue  # not enough data to judge trends

            recent = scores[-_SCORE_WINDOW:]
            first_half = recent[: _SCORE_WINDOW // 2]
            second_half = recent[_SCORE_WINDOW // 2 :]

            avg_first = sum(first_half) / len(first_half)
            avg_second = sum(second_half) / len(second_half)
            delta = avg_first - avg_second  # positive = declining

            if delta > 0.1:
                # Scores are declining — suggest prompt adjustment
                adjustment = (
                    f"Focus extra attention on the '{subsystem}' subsystem. "
                    f"Recent critic scores have declined by {delta:.2f}. "
                    f"Prioritize addressing identified weaknesses in this area."
                )
                actions.append(
                    SelfImprovementAction(
                        action_type="prompt_adjustment",
                        description=(
                            f"Inject focus directive for '{subsystem}' into "
                            f"Architect system prompt (score declined {delta:.2f})"
                        ),
                        target="architect_system_prompt",
                        parameters={
                            "subsystem": subsystem,
                            "injection": adjustment,
                            "score_delta": round(delta, 3),
                        },
                        confidence=min(0.9, 0.5 + delta),
                    )
                )
                logger.info(
                    "Score trend declining for '%s': %.2f → %.2f (Δ=%.2f)",
                    subsystem,
                    avg_first,
                    avg_second,
                    delta,
                )

        return actions

    def _analyze_stagnation(self, events: list[dict[str, Any]]) -> list[SelfImprovementAction]:
        """
        Detect repeated stagnation and suggest config adjustments.

        If the same anti-stagnation detector fires >= _STAGNATION_REPEAT_THRESHOLD
        times consecutively, the current parameters aren't working.  Suggest
        a temperature bump (capped at baseline ± _MAX_TEMP_DELTA).
        """
        actions: list[SelfImprovementAction] = []

        # Count consecutive firings per detector
        for event in events:
            detector = event.get("detector", "unknown")
            self._stagnation_streak[detector] = self._stagnation_streak.get(detector, 0) + 1

        for detector, streak in self._stagnation_streak.items():
            if streak >= _STAGNATION_REPEAT_THRESHOLD:
                actions.append(
                    SelfImprovementAction(
                        action_type="config_adjustment",
                        description=(
                            f"Increase temperature by 0.05 — {detector} fired "
                            f"{streak}x consecutively"
                        ),
                        target="temperature",
                        parameters={
                            "delta": 0.05,
                            "max_delta": _MAX_TEMP_DELTA,
                            "reason": f"{detector} streak={streak}",
                        },
                        confidence=0.6,
                    )
                )
                # Reset the streak after generating an action
                self._stagnation_streak[detector] = 0
                logger.info(
                    "Stagnation streak for '%s' hit %d — suggesting temp bump",
                    detector,
                    streak,
                )

        return actions

    def _analyze_failures(
        self,
        dlq_entries: list[dict[str, Any]],
        error_counts: dict[str, int],
    ) -> list[SelfImprovementAction]:
        """
        Analyze DLQ entries and error patterns to generate self-targeting tasks.

        If a particular module or error type appears repeatedly in the DLQ,
        generate a task to improve error handling or refactor that module.
        """
        actions: list[SelfImprovementAction] = []

        # Group DLQ entries by error type or module
        error_modules: dict[str, int] = {}
        for entry in dlq_entries:
            module = entry.get("module", entry.get("source", "unknown"))
            error_modules[module] = error_modules.get(module, 0) + 1

        # Generate tasks for modules with repeated failures
        for module, count in sorted(error_modules.items(), key=lambda x: x[1], reverse=True):
            if count >= 2:
                actions.append(
                    SelfImprovementAction(
                        action_type="task_generation",
                        description=(
                            f"Improve error handling in '{module}' — "
                            f"{count} DLQ entries in this cycle"
                        ),
                        target=f"module:{module}",
                        parameters={
                            "task_type": "self_improvement",
                            "task_title": f"Improve error handling in {module}",
                            "task_description": (
                                f"Module '{module}' produced {count} dead-letter "
                                f"entries in the last macro cycle.  Investigate "
                                f"root causes and add better error handling, "
                                f"retries, or fallbacks."
                            ),
                            "priority": "HIGH" if count >= 5 else "NORMAL",
                        },
                        confidence=min(0.8, 0.4 + count * 0.1),
                    )
                )

        # Check for high-frequency error classes
        for error_class, count in sorted(error_counts.items(), key=lambda x: x[1], reverse=True):
            if count >= 5:
                actions.append(
                    SelfImprovementAction(
                        action_type="task_generation",
                        description=(
                            f"Investigate frequent '{error_class}' errors ({count} occurrences)"
                        ),
                        target=f"error:{error_class}",
                        parameters={
                            "task_type": "self_improvement",
                            "task_title": f"Reduce {error_class} frequency",
                            "task_description": (
                                f"'{error_class}' occurred {count} times in "
                                f"the last macro cycle.  Analyze the root cause "
                                f"and implement a fix or mitigation."
                            ),
                            "priority": "HIGH",
                        },
                        confidence=0.7,
                    )
                )

        return actions

    def apply_prompt_adjustment(
        self,
        current_prompt: str,
        action: SelfImprovementAction,
    ) -> str:
        """
        Apply a prompt adjustment action to the current system prompt.

        Appends a focus directive to the end of the system prompt.  If the
        same subsystem already has a directive, replaces it instead of
        stacking multiple directives.

        Parameters
        ----------
        current_prompt : str
            The current Architect system prompt.
        action : SelfImprovementAction
            Must have action_type == "prompt_adjustment".

        Returns
        -------
        str
            The modified system prompt with the focus directive appended.
        """
        if action.action_type != "prompt_adjustment":
            raise ValueError(f"Expected prompt_adjustment, got {action.action_type}")

        injection = action.parameters.get("injection", "")
        subsystem = action.parameters.get("subsystem", "")

        # Remove any existing directive for this subsystem
        marker_start = f"[SELF-IMPROVE:{subsystem}]"
        marker_end = f"[/SELF-IMPROVE:{subsystem}]"

        if marker_start in current_prompt:
            # Replace existing directive
            start_idx = current_prompt.index(marker_start)
            end_idx = current_prompt.index(marker_end) + len(marker_end)
            current_prompt = (current_prompt[:start_idx] + current_prompt[end_idx:]).strip()

        # Append the new directive with markers for future replacement
        directive = f"\n\n{marker_start}\n{injection}\n{marker_end}"

        result = current_prompt.rstrip() + directive
        self._prompt_history.append(f"{subsystem}: {injection[:100]}")

        logger.info(
            "Applied prompt adjustment for '%s' (confidence=%.2f)",
            subsystem,
            action.confidence,
        )
        return result

    def apply_config_adjustment(
        self,
        current_temperature: float,
        action: SelfImprovementAction,
    ) -> float:
        """
        Apply a config adjustment action (currently: temperature only).

        Clamps the result to baseline ± _MAX_TEMP_DELTA so the temperature
        can never drift too far from the original setting.

        Parameters
        ----------
        current_temperature : float
            The current temperature value.
        action : SelfImprovementAction
            Must have action_type == "config_adjustment".

        Returns
        -------
        float
            The adjusted temperature, clamped within safe bounds.
        """
        if action.action_type != "config_adjustment":
            raise ValueError(f"Expected config_adjustment, got {action.action_type}")

        delta = action.parameters.get("delta", 0.0)
        new_temp = current_temperature + delta

        # Clamp to safe range
        min_temp = max(0.0, self._baseline_temp - _MAX_TEMP_DELTA)
        max_temp = min(1.0, self._baseline_temp + _MAX_TEMP_DELTA)
        clamped = max(min_temp, min(max_temp, new_temp))

        if clamped != new_temp:
            logger.info(
                "Temperature clamped: requested %.3f, clamped to %.3f "
                "(baseline=%.2f, max_delta=%.2f)",
                new_temp,
                clamped,
                self._baseline_temp,
                _MAX_TEMP_DELTA,
            )
        else:
            logger.info(
                "Temperature adjusted: %.3f → %.3f (delta=%.3f)",
                current_temperature,
                clamped,
                delta,
            )

        return clamped

    @property
    def branch(self) -> str:
        """The git branch where self-improvement commits are made."""
        return self._branch

    @property
    def enabled(self) -> bool:
        """Whether the self-improvement engine is active."""
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value
        logger.info("SelfImprovementEngine enabled=%s", value)
