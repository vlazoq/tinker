"""
tinker/architecture/__init__.py
================================

What this package does
-----------------------
This package is the "long-term memory" for Tinker's architectural thinking.
Every time Tinker's AI loops produce a new insight — a new component they've
identified, a design decision they've made, or a question they still need to
answer — those ideas get recorded here as structured data.

Think of it like a living architecture document that automatically grows and
improves over time, instead of a static Word file that goes stale.

How the pieces fit together
----------------------------
1. schema.py   — Defines all the data shapes (what a "Component" looks like,
                 what a "DesignDecision" looks like, etc.).  Like the blueprint
                 for the document.

2. merger.py   — Contains the logic for *adding* new information to an existing
                 document without destroying what was already there.  It never
                 deletes — it only grows the knowledge.

3. manager.py  — The main entry point that orchestrators actually call.  It
                 loads the current state from disk, hands updates to the merger,
                 saves new snapshots, and can optionally commit them to Git so
                 you have a full version history.

Why it exists
--------------
Tinker runs many AI loops continuously.  Each loop might discover something
new about the system it's designing.  Without a structured place to accumulate
those discoveries, every loop would start from scratch — wasting tokens and
losing insights.

This package is the solution: a versioned, queryable, persistent document that
captures everything Tinker has learned about the system so far.

How to use it from outside this package
----------------------------------------
    from infra.architecture import ArchitectureStateManager

    mgr = ArchitectureStateManager(workspace="./my_workspace")
    mgr.apply_update({"components": [{"name": "API Gateway", ...}]})
    print(mgr.summarise())   # compact text you can paste into an LLM prompt

Public API (everything importable from this package)
-----------------------------------------------------
    ArchitectureStateManager  — the one class you'll use most of the time
    merge_update              — the underlying merge function (mostly internal)
    ArchitectureState         — the root document dataclass
    Component                 — a single software component
    ConfidenceScore           — how confident Tinker is about something (0–1)
    ConfidenceTier            — human-readable bucket: speculative / tentative /
                                confident / established
    DesignDecision            — a recorded architectural choice
    DecisionStatus            — proposed / accepted / rejected / revisited
    OpenQuestion              — something Tinker still needs to figure out
    Relationship              — a link between two components
    RelationshipKind          — calls / publishes_to / reads_from / etc.
    RejectedAlternative       — an option Tinker considered and discarded
    SubsystemSummary          — a high-level grouping of related components
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
