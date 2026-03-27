"""
tinker/anti_stagnation/config.py
─────────────────────────────────
Configuration schema for the Anti-Stagnation System.
All thresholds and window sizes live here so operators can tune
the watchdog without touching detection logic.
"""

from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class SemanticLoopConfig:
    """Detects consecutive outputs that are semantically too similar."""

    # Number of recent outputs to keep in the sliding window
    window_size: int = 6

    # Cosine-similarity threshold above which a pair is "too similar"
    similarity_threshold: float = 0.92

    # How many pairs in the window must breach the threshold to raise a flag
    min_breach_count: int = 3

    # Embedding model identifier (resolved by the EmbeddingBackend)
    embedding_model: str = "nomic-embed-text"

    def __post_init__(self) -> None:
        if self.window_size < 2:
            raise ValueError(f"window_size must be >= 2, got {self.window_size}")
        if not 0.0 < self.similarity_threshold <= 1.0:
            raise ValueError(f"similarity_threshold must be in (0, 1], got {self.similarity_threshold}")
        if self.min_breach_count < 1:
            raise ValueError(f"min_breach_count must be >= 1, got {self.min_breach_count}")


@dataclass
class SubsystemFixationConfig:
    """Detects over-focus on a single architectural subsystem."""

    window_size: int = 10
    fixation_threshold: float = 0.70


@dataclass
class CritiqueCollapseConfig:
    """Detects a Critic that has become too agreeable."""

    window_size: int = 8
    collapse_threshold: float = 0.85
    # Minimum scores needed before the detector fires
    min_samples: int = 4


@dataclass
class ResearchSaturationConfig:
    """Detects the Researcher finding the same sources repeatedly."""

    window_size: int = 6
    # Jaccard overlap fraction that triggers a flag
    overlap_threshold: float = 0.60
    min_url_count: int = 3


@dataclass
class TaskStarvationConfig:
    """Detects the task queue draining without new work being generated."""

    low_depth_threshold: int = 3
    window_size: int = 5
    # Consecutive samples where net generation is negative before flagging
    consecutive_negative_threshold: int = 3


@dataclass
class StagnationMonitorConfig:
    """Root configuration object passed to StagnationMonitor."""

    semantic_loop: SemanticLoopConfig = field(default_factory=SemanticLoopConfig)
    subsystem_fixation: SubsystemFixationConfig = field(
        default_factory=SubsystemFixationConfig
    )
    critique_collapse: CritiqueCollapseConfig = field(
        default_factory=CritiqueCollapseConfig
    )
    research_saturation: ResearchSaturationConfig = field(
        default_factory=ResearchSaturationConfig
    )
    task_starvation: TaskStarvationConfig = field(default_factory=TaskStarvationConfig)

    # Maximum stagnation events to keep in the in-memory log
    event_log_max_size: int = 500

    # If True, all detectors run even after one fires (collect every flag)
    run_all_detectors: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "StagnationMonitorConfig":
        """Shallow-merge a dict of overrides into the default config."""
        cfg = cls()
        sections = {
            "semantic_loop": cfg.semantic_loop,
            "subsystem_fixation": cfg.subsystem_fixation,
            "critique_collapse": cfg.critique_collapse,
            "research_saturation": cfg.research_saturation,
            "task_starvation": cfg.task_starvation,
        }
        for section_name, obj in sections.items():
            if section_name in d:
                for k, v in d[section_name].items():
                    if hasattr(obj, k):
                        setattr(obj, k, v)
        for k in ("event_log_max_size", "run_all_detectors"):
            if k in d:
                setattr(cfg, k, d[k])
        return cfg
