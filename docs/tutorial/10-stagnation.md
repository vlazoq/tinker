# Chapter 10 — Anti-Stagnation

## The Problem

Left unchecked, an AI reasoning loop will stagnate.  It will:

- **Loop semantically** — write essentially the same design decision over
  and over with slightly different wording
- **Fixate on one subsystem** — spend 90% of its time on "api_gateway"
  and ignore "worker_pool"
- **Collapse critique** — the Critic starts awarding every design a 0.9+
  score ("everything is great!") which means no real pressure to improve
- **Saturate research** — keep searching the same web resources over and
  over, discovering nothing new
- **Starve tasks** — run out of high-depth tasks and only do shallow work

These are not hypothetical.  Every long-running AI loop eventually hits them.

---

## The Architecture Decision

We build a `StagnationMonitor` with five independent *detectors*, each
watching for a different pattern.  The monitor is called by the orchestrator
after every micro loop.  When it fires, it returns a `Directive`:

| Directive | What it means | Orchestrator action |
|-----------|---------------|---------------------|
| `FORCE_BRANCH` | Too long on one subsystem | Force meso synthesis on that subsystem → pivot to a different one |
| `INJECT_TASK`  | Task diversity too low | Generate new exploration tasks |
| `REDUCE_TEMP`  | Semantic loop detected | Lower the temperature for the next N loops |
| `ALERT_HUMAN`  | Unrecoverable stagnation | Log a critical warning (human should intervene) |
| `CONTINUE`     | Nothing to do | Carry on |

---

## Step 1 — Directory Structure

```
tinker/
  stagnation/
    __init__.py
    monitor.py
    detectors.py
```

---

## Step 2 — The Detectors

Each detector gets a sliding window of recent data and returns a
severity score (0.0 = no stagnation, 1.0 = definitely stagnating).

```python
# tinker/stagnation/detectors.py

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class DetectorResult:
    """Result from one detector."""
    name:     str
    severity: float   # 0.0 = none, 1.0 = maximum
    details:  str     = ""


# ── Detector 1: Semantic Loop ─────────────────────────────────────────────────
# Detects when recent artifacts are too similar to each other.

def cosine_similarity_simple(a: str, b: str) -> float:
    """
    Very simple word-overlap similarity (not true cosine similarity).
    For production, use an embedding model.  For our purposes this is
    good enough to detect obvious repetition.
    """
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    return len(intersection) / max(len(words_a), len(words_b))


def detect_semantic_loop(
    recent_contents: list[str],
    window_size: int = 6,
    similarity_threshold: float = 0.8,
    min_breaches: int = 3,
) -> DetectorResult:
    """
    Fire if too many recent artifact pairs are too similar to each other.
    """
    contents = recent_contents[-window_size:]
    if len(contents) < 2:
        return DetectorResult("semantic_loop", 0.0, "Not enough history")

    breach_count = 0
    pairs_checked = 0
    for i in range(len(contents)):
        for j in range(i + 1, len(contents)):
            sim = cosine_similarity_simple(contents[i], contents[j])
            pairs_checked += 1
            if sim >= similarity_threshold:
                breach_count += 1

    severity = min(breach_count / max(min_breaches, 1), 1.0)
    return DetectorResult(
        "semantic_loop",
        severity,
        f"{breach_count}/{pairs_checked} pairs above threshold {similarity_threshold}"
    )


# ── Detector 2: Subsystem Fixation ───────────────────────────────────────────
# Detects when recent work is concentrated on one subsystem.

def detect_subsystem_fixation(
    recent_subsystems: list[str],
    window_size:        int   = 10,
    fixation_threshold: float = 0.7,
) -> DetectorResult:
    """
    Fire if one subsystem accounts for more than fixation_threshold of
    recent work.
    """
    window = recent_subsystems[-window_size:]
    if not window:
        return DetectorResult("subsystem_fixation", 0.0)

    counts: dict[str, int] = {}
    for s in window:
        counts[s] = counts.get(s, 0) + 1

    max_fraction = max(counts.values()) / len(window)
    top_subsystem = max(counts, key=counts.__getitem__)

    severity = max(0.0, (max_fraction - fixation_threshold) / (1.0 - fixation_threshold))
    return DetectorResult(
        "subsystem_fixation",
        severity,
        f"{top_subsystem!r} = {max_fraction:.0%} of recent work"
    )


# ── Detector 3: Critique Collapse ────────────────────────────────────────────
# Detects when the Critic always gives high scores (stopped providing value).

def detect_critique_collapse(
    recent_scores: list[float],
    window_size:   int   = 8,
    collapse_threshold: float = 0.85,
    min_samples:   int   = 4,
) -> DetectorResult:
    """
    Fire if recent critic scores are suspiciously high (Critic not challenging).
    """
    scores = recent_scores[-window_size:]
    if len(scores) < min_samples:
        return DetectorResult("critique_collapse", 0.0, "Not enough scores")

    avg = sum(scores) / len(scores)
    severity = max(0.0, (avg - collapse_threshold) / (1.0 - collapse_threshold))
    return DetectorResult(
        "critique_collapse",
        severity,
        f"Avg critic score = {avg:.2f} (threshold {collapse_threshold})"
    )
```

---

## Step 3 — The Monitor

```python
# tinker/stagnation/monitor.py

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .detectors import (
    detect_semantic_loop,
    detect_subsystem_fixation,
    detect_critique_collapse,
    DetectorResult,
)

logger = logging.getLogger(__name__)


class Directive(str, Enum):
    CONTINUE      = "continue"
    FORCE_BRANCH  = "force_branch"   # force meso on a subsystem to pivot
    INJECT_TASK   = "inject_task"    # add new exploration tasks
    REDUCE_TEMP   = "reduce_temp"    # lower temperature for next N loops
    ALERT_HUMAN   = "alert_human"    # log a critical warning


@dataclass
class StagnationEvent:
    """Fired when stagnation is detected."""
    directive:        Directive
    severity:         float
    detector_name:    str
    details:          str
    target_subsystem: Optional[str] = None   # for FORCE_BRANCH


class StagnationMonitor:
    """
    Watches for stagnation patterns and returns directives to the orchestrator.
    """

    def __init__(
        self,
        # Tuning parameters — all have sensible defaults
        semantic_similarity_threshold: float = 0.8,
        fixation_threshold:            float = 0.7,
        collapse_threshold:            float = 0.85,
        # Minimum severity before firing a directive
        min_severity:                  float = 0.5,
    ) -> None:
        self._sem_threshold  = semantic_similarity_threshold
        self._fix_threshold  = fixation_threshold
        self._col_threshold  = collapse_threshold
        self._min_severity   = min_severity

    def check(
        self,
        recent_contents:   list[str],    # last N artifact content strings
        recent_subsystems: list[str],    # last N subsystem names
        recent_scores:     list[float],  # last N critic scores
    ) -> Optional[StagnationEvent]:
        """
        Run all detectors.  Return a StagnationEvent if any fires, else None.
        """
        results: list[DetectorResult] = [
            detect_semantic_loop(
                recent_contents,
                similarity_threshold=self._sem_threshold,
            ),
            detect_subsystem_fixation(
                recent_subsystems,
                fixation_threshold=self._fix_threshold,
            ),
            detect_critique_collapse(
                recent_scores,
                collapse_threshold=self._col_threshold,
            ),
        ]

        # Find the most severe result
        worst = max(results, key=lambda r: r.severity)
        if worst.severity < self._min_severity:
            return None   # no stagnation detected

        # Map detector to directive
        directive_map = {
            "semantic_loop":      Directive.REDUCE_TEMP,
            "subsystem_fixation": Directive.FORCE_BRANCH,
            "critique_collapse":  Directive.INJECT_TASK,
        }
        directive = directive_map.get(worst.name, Directive.ALERT_HUMAN)

        # For FORCE_BRANCH, find the most-worked subsystem to pivot away from
        target = None
        if directive == Directive.FORCE_BRANCH and recent_subsystems:
            counts: dict[str, int] = {}
            for s in recent_subsystems:
                counts[s] = counts.get(s, 0) + 1
            target = max(counts, key=counts.__getitem__)

        event = StagnationEvent(
            directive        = directive,
            severity         = worst.severity,
            detector_name    = worst.name,
            details          = worst.details,
            target_subsystem = target,
        )
        logger.warning(
            "Stagnation detected: %s (severity=%.2f) — %s. Directive: %s",
            worst.name, worst.severity, worst.details, directive.value,
        )
        return event
```

---

## Step 4 — Integration into the Orchestrator

In `Orchestrator._tick()`, after a successful micro loop:

```python
# After run_micro_loop() succeeds, check for stagnation

from stagnation.monitor import StagnationMonitor, Directive

monitor = StagnationMonitor()   # created once in __init__

# Collect recent data from state history
recent_contents   = [r.get("content", "")[:500]
                     for r in self.state.micro_history[-6:]]
recent_subsystems = [r.get("subsystem", "")
                     for r in self.state.micro_history[-10:]]
recent_scores     = [r.get("critic_score", 0.5)
                     for r in self.state.micro_history[-8:]
                     if r.get("critic_score") is not None]

event = monitor.check(recent_contents, recent_subsystems, recent_scores)
if event:
    self.state.stagnation_events_total += 1
    if event.directive == Directive.FORCE_BRANCH and event.target_subsystem:
        # Force meso synthesis on the over-worked subsystem,
        # which will clear its micro count and give other subsystems a turn
        await self._run_meso(event.target_subsystem)
        self.state.reset_subsystem_count(event.target_subsystem)
    elif event.directive == Directive.INJECT_TASK:
        # Generate new exploration tasks to diversify the queue
        await self._task_gen.seed_from_problem(
            problem    = self._problem,
            subsystems = ["cross_cutting", "observability"],
        )
```

---

## Key Concepts Introduced

| Concept | What it means |
|---------|---------------|
| Sliding window | Only look at the last N data points, not all history |
| Severity score | A continuous 0.0–1.0 score rather than binary yes/no |
| Directive pattern | The monitor tells the orchestrator WHAT happened; the orchestrator decides what to do |
| Fail-safe defaults | Detectors return severity=0.0 (no action) when there's not enough data |

The most important lesson here is the **directive pattern**.  The monitor
returns `Directive.FORCE_BRANCH` — it does not directly call `_run_meso()`.
That separation means you can test the monitor completely independently
of the orchestrator.  Feed it some data, check what directive it returns.
No AI calls needed.

---

→ Next: [Chapter 11 — Observability](./11-observability.md)
