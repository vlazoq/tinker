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
    # Monitor
    "StagnationMonitor",
    # Config
    "StagnationMonitorConfig",
    "SemanticLoopConfig",
    "SubsystemFixationConfig",
    "CritiqueCollapseConfig",
    "ResearchSaturationConfig",
    "TaskStarvationConfig",
    # Models
    "MicroLoopContext",
    "InterventionDirective",
    "InterventionType",
    "StagnationType",
    "StagnationEvent",
    "INTERVENTION_MAP",
    # Embeddings
    "EmbeddingBackend",
    "FallbackTFIDFBackend",
    "OllamaEmbeddingBackend",
    "make_embedding_backend",
    # Log
    "StagnationEventLog",
]
