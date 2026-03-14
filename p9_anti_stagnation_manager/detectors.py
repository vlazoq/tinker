"""
tinker/anti_stagnation/detectors.py
─────────────────────────────────────
The five stagnation detectors.  Each detector:
  - Maintains its own rolling state (deque-based window)
  - Exposes a single `check(context) -> Optional[DetectionResult]` method
  - Returns None when no stagnation is detected
  - Is independently resettable

DetectionResult carries the evidence dict consumed by the monitor and
eventually written to the event log.
"""

from __future__ import annotations

import itertools
import math
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

from .config import (
    CritiqueCollapseConfig,
    ResearchSaturationConfig,
    SemanticLoopConfig,
    SubsystemFixationConfig,
    TaskStarvationConfig,
)
from .embeddings import EmbeddingBackend
from .models import MicroLoopContext, StagnationType


# ─────────────────────────────────────────────────────────────
# Shared result type
# ─────────────────────────────────────────────────────────────

@dataclass
class DetectionResult:
    stagnation_type: StagnationType
    severity: float                      # 0.0 – 1.0
    evidence: Dict[str, Any] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────
# 1. Semantic Loop Detector
# ─────────────────────────────────────────────────────────────

class SemanticLoopDetector:
    """
    Embeds each output text and maintains a sliding window of vectors.
    Computes pairwise cosine similarity over all consecutive pairs in the
    window; flags when >= min_breach_count pairs exceed the threshold.

    Severity = fraction of breaching pairs / total pairs in window.
    """

    def __init__(self, cfg: SemanticLoopConfig, backend: EmbeddingBackend):
        self.cfg = cfg
        self.backend = backend
        self._window: Deque[Tuple[int, List[float]]] = deque(
            maxlen=cfg.window_size
        )  # (loop_index, embedding)

    def check(self, ctx: MicroLoopContext) -> Optional[DetectionResult]:
        if not ctx.output_text:
            return None

        vec = self.backend.embed(ctx.output_text)
        self._window.append((ctx.loop_index, vec))

        if len(self._window) < 2:
            return None

        pairs = list(itertools.combinations(self._window, 2))
        breaches: List[Tuple[int, int, float]] = []

        for (idx_a, vec_a), (idx_b, vec_b) in pairs:
            # Ensure vectors are the same dimension before comparing
            if len(vec_a) != len(vec_b):
                continue
            sim = self.backend.cosine_similarity(vec_a, vec_b)
            if sim >= self.cfg.similarity_threshold:
                breaches.append((idx_a, idx_b, round(sim, 4)))

        if len(breaches) < self.cfg.min_breach_count:
            return None

        severity = len(breaches) / max(len(pairs), 1)
        return DetectionResult(
            stagnation_type=StagnationType.SEMANTIC_LOOP,
            severity=severity,
            evidence={
                "breach_count": len(breaches),
                "total_pairs": len(pairs),
                "threshold": self.cfg.similarity_threshold,
                "breaching_pairs": breaches[: 5],  # cap for log size
                "window_loop_indices": [i for i, _ in self._window],
            },
        )

    def reset(self) -> None:
        self._window.clear()


# ─────────────────────────────────────────────────────────────
# 2. Subsystem Fixation Detector
# ─────────────────────────────────────────────────────────────

class SubsystemFixationDetector:
    """
    Tracks the subsystem_tag of each loop in a sliding window.
    Flags when any single subsystem appears in more than
    fixation_threshold * window_size of the recent tasks.

    Severity = fraction of the dominant subsystem in the window.
    """

    def __init__(self, cfg: SubsystemFixationConfig):
        self.cfg = cfg
        self._window: Deque[str] = deque(maxlen=cfg.window_size)

    def check(self, ctx: MicroLoopContext) -> Optional[DetectionResult]:
        if not ctx.subsystem_tag:
            return None

        self._window.append(ctx.subsystem_tag)

        if len(self._window) < self.cfg.window_size:
            # Wait for a full window before evaluating
            return None

        counts = Counter(self._window)
        dominant_subsystem, dominant_count = counts.most_common(1)[0]
        fraction = dominant_count / len(self._window)

        if fraction < self.cfg.fixation_threshold:
            return None

        severity = (fraction - self.cfg.fixation_threshold) / (
            1.0 - self.cfg.fixation_threshold + 1e-9
        )
        severity = min(1.0, severity)

        return DetectionResult(
            stagnation_type=StagnationType.SUBSYSTEM_FIXATION,
            severity=severity,
            evidence={
                "dominant_subsystem": dominant_subsystem,
                "dominant_fraction": round(fraction, 4),
                "threshold": self.cfg.fixation_threshold,
                "window_distribution": dict(counts),
                "avoid_subsystem_hint": dominant_subsystem,
            },
        )

    def reset(self) -> None:
        self._window.clear()


# ─────────────────────────────────────────────────────────────
# 3. Critique Collapse Detector
# ─────────────────────────────────────────────────────────────

class CritiqueCollapseDetector:
    """
    Tracks Critic confidence scores in a rolling window.
    Flags when the rolling average exceeds collapse_threshold AND
    the trend is consistently high (std deviation is low).

    Severity blends the excess above threshold with score uniformity.
    """

    def __init__(self, cfg: CritiqueCollapseConfig):
        self.cfg = cfg
        self._scores: Deque[float] = deque(maxlen=cfg.window_size)

    def check(self, ctx: MicroLoopContext) -> Optional[DetectionResult]:
        if ctx.critic_score is None:
            return None

        score = max(0.0, min(1.0, ctx.critic_score))
        self._scores.append(score)

        if len(self._scores) < self.cfg.min_samples:
            return None

        rolling_mean = sum(self._scores) / len(self._scores)

        if rolling_mean <= self.cfg.collapse_threshold:
            return None

        # Compute stddev to measure how "flat" the scores are
        variance = sum((s - rolling_mean) ** 2 for s in self._scores) / len(
            self._scores
        )
        std_dev = math.sqrt(variance)

        # Severity: higher excess + lower stddev → more severe
        excess = (rolling_mean - self.cfg.collapse_threshold) / (
            1.0 - self.cfg.collapse_threshold + 1e-9
        )
        uniformity = 1.0 - min(1.0, std_dev / 0.1)  # 0.1 = "flat enough"
        severity = min(1.0, (excess + uniformity) / 2.0)

        return DetectionResult(
            stagnation_type=StagnationType.CRITIQUE_COLLAPSE,
            severity=severity,
            evidence={
                "rolling_mean": round(rolling_mean, 4),
                "std_dev": round(std_dev, 4),
                "threshold": self.cfg.collapse_threshold,
                "sample_count": len(self._scores),
                "recent_scores": list(self._scores)[-5:],
            },
        )

    def reset(self) -> None:
        self._scores.clear()


# ─────────────────────────────────────────────────────────────
# 4. Research Saturation Detector
# ─────────────────────────────────────────────────────────────

class ResearchSaturationDetector:
    """
    Maintains a sliding window of URL sets from recent Researcher outputs.
    Computes average pairwise Jaccard similarity between consecutive pairs.
    Flags when the average exceeds overlap_threshold.

    Severity = (average_jaccard - threshold) normalised to [0, 1].
    """

    def __init__(self, cfg: ResearchSaturationConfig):
        self.cfg = cfg
        self._window: Deque[Tuple[int, Set[str]]] = deque(
            maxlen=cfg.window_size
        )  # (loop_index, url_set)

    @staticmethod
    def _jaccard(a: Set[str], b: Set[str]) -> float:
        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    def check(self, ctx: MicroLoopContext) -> Optional[DetectionResult]:
        if not ctx.research_urls:
            return None

        urls = set(ctx.research_urls)
        total_unique = sum(len(s) for _, s in self._window) + len(urls)

        if total_unique < self.cfg.min_url_count:
            self._window.append((ctx.loop_index, urls))
            return None

        self._window.append((ctx.loop_index, urls))

        if len(self._window) < 2:
            return None

        # Consecutive-pair Jaccard scores
        pairs = list(zip(self._window, list(self._window)[1:]))
        jaccard_scores = [
            self._jaccard(a, b) for (_, a), (_, b) in pairs
        ]
        avg_jaccard = sum(jaccard_scores) / len(jaccard_scores)

        if avg_jaccard < self.cfg.overlap_threshold:
            return None

        severity = (avg_jaccard - self.cfg.overlap_threshold) / (
            1.0 - self.cfg.overlap_threshold + 1e-9
        )
        severity = min(1.0, severity)

        # Find the most repeated URLs across the window
        all_urls: Counter = Counter()
        for _, url_set in self._window:
            all_urls.update(url_set)
        repeated_urls = [u for u, c in all_urls.most_common(10) if c > 1]

        return DetectionResult(
            stagnation_type=StagnationType.RESEARCH_SATURATION,
            severity=severity,
            evidence={
                "avg_jaccard": round(avg_jaccard, 4),
                "threshold": self.cfg.overlap_threshold,
                "pair_scores": [round(s, 4) for s in jaccard_scores],
                "repeated_urls": repeated_urls[:5],
                "window_size": len(self._window),
            },
        )

    def reset(self) -> None:
        self._window.clear()


# ─────────────────────────────────────────────────────────────
# 5. Task Starvation Detector
# ─────────────────────────────────────────────────────────────

class TaskStarvationDetector:
    """
    Tracks the task queue depth and net generation rate over a sliding window.
    Two conditions must BOTH hold to trigger:
      (a) Current queue depth <= low_depth_threshold
      (b) Net generation (generated - consumed) has been negative for
          >= consecutive_negative_threshold consecutive samples

    Severity = 1 - (queue_depth / low_depth_threshold), clipped to [0, 1].
    """

    def __init__(self, cfg: TaskStarvationConfig):
        self.cfg = cfg
        self._net_history: Deque[int] = deque(maxlen=cfg.window_size)
        self._consecutive_negative: int = 0

    def check(self, ctx: MicroLoopContext) -> Optional[DetectionResult]:
        if ctx.queue_depth is None:
            return None

        queue_depth = ctx.queue_depth
        generated = ctx.tasks_generated or 0
        consumed = ctx.tasks_consumed or 0
        net = generated - consumed

        self._net_history.append(net)

        if net < 0:
            self._consecutive_negative += 1
        else:
            self._consecutive_negative = 0

        depth_critical = queue_depth <= self.cfg.low_depth_threshold
        rate_critical = (
            self._consecutive_negative
            >= self.cfg.consecutive_negative_threshold
        )

        if not (depth_critical and rate_critical):
            return None

        severity = max(
            0.0,
            1.0 - queue_depth / max(self.cfg.low_depth_threshold, 1),
        )
        severity = min(1.0, severity)

        return DetectionResult(
            stagnation_type=StagnationType.TASK_STARVATION,
            severity=severity,
            evidence={
                "queue_depth": queue_depth,
                "low_depth_threshold": self.cfg.low_depth_threshold,
                "consecutive_negative": self._consecutive_negative,
                "net_history": list(self._net_history),
                "last_generated": generated,
                "last_consumed": consumed,
            },
        )

    def reset(self) -> None:
        self._net_history.clear()
        self._consecutive_negative = 0
