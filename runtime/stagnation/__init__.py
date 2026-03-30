"""
tinker/anti_stagnation
──────────────────────
Anti-Stagnation System for the Tinker autonomous architecture engine.

Public API:
    StagnationMonitor     — primary interface for the Orchestrator
    StagnationMonitorConfig — configuration root (all thresholds)
    MicroLoopContext      — payload passed on each micro-loop tick
    InterventionDirective — returned when stagnation is detected
    StagnationType        — enum of detectable failure modes
    InterventionType      — enum of available interventions
"""

from .config import (
    CritiqueCollapseConfig,
    ResearchSaturationConfig,
    SemanticLoopConfig,
    StagnationMonitorConfig,
    SubsystemFixationConfig,
    TaskStarvationConfig,
)
from .embeddings import (
    EmbeddingBackend,
    FallbackTFIDFBackend,
    OllamaEmbeddingBackend,
    make_embedding_backend,
)
from .event_log import StagnationEventLog
from .models import (
    INTERVENTION_MAP,
    InterventionDirective,
    InterventionType,
    MicroLoopContext,
    StagnationEvent,
    StagnationType,
)
from .monitor import StagnationMonitor

__all__ = [
    "INTERVENTION_MAP",
    "CritiqueCollapseConfig",
    # Embeddings
    "EmbeddingBackend",
    "FallbackTFIDFBackend",
    "InterventionDirective",
    "InterventionType",
    # Models
    "MicroLoopContext",
    "OllamaEmbeddingBackend",
    "ResearchSaturationConfig",
    "SemanticLoopConfig",
    "StagnationEvent",
    # Log
    "StagnationEventLog",
    # Monitor
    "StagnationMonitor",
    # Config
    "StagnationMonitorConfig",
    "StagnationType",
    "SubsystemFixationConfig",
    "TaskStarvationConfig",
    "make_embedding_backend",
]
