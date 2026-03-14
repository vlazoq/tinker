"""
tinker/architecture_state/__init__.py
Exposes the public API surface for the Architecture State subsystem.
"""

from .manager import ArchitectureStateManager
from .merger import merge_update
from .schema import (
    ArchitectureState,
    Component,
    ConfidenceScore,
    ConfidenceTier,
    DesignDecision,
    DecisionStatus,
    OpenQuestion,
    Relationship,
    RelationshipKind,
    RejectedAlternative,
    SubsystemSummary,
)

__all__ = [
    "ArchitectureStateManager",
    "merge_update",
    "ArchitectureState",
    "Component",
    "ConfidenceScore",
    "ConfidenceTier",
    "DesignDecision",
    "DecisionStatus",
    "OpenQuestion",
    "Relationship",
    "RelationshipKind",
    "RejectedAlternative",
    "SubsystemSummary",
]
