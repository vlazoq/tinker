"""
agents/protocols.py
===================

Structural protocols (interfaces) for Tinker's AI agent roles.

Why protocols?
--------------
The orchestrator depends on three distinct agent roles — Architect, Critic,
and Synthesizer — but it should not be coupled to the concrete implementations
in architect.py, critic.py, and synthesizer.py.  By depending on protocols
instead of classes, we gain:

  * **Substitutability** — swap any agent for a different implementation
    (domain-specialised variant, a test double, a remote agent over HTTP)
    without touching the orchestrator or the bootstrap layer.

  * **Testability** — unit-test the orchestrator with a lightweight mock that
    satisfies the protocol, rather than a full ArchitectAgent instance.

  * **Documentation** — the protocol is a single source of truth for what
    the orchestrator expects from each role.

Usage
-----
The orchestrator, context assembler, and task engine all receive injected
agents.  Type hints should reference the protocol, not the concrete class::

    def __init__(self, architect: ArchitectStrategy, critic: CriticStrategy, ...):
        ...

To verify that a concrete class satisfies the protocol at runtime::

    assert isinstance(my_agent, ArchitectStrategy)   # True for ArchitectAgent

To substitute a custom implementation::

    from agents.agent_factory import register_agent
    from core.llm.types import AgentRole

    register_agent(AgentRole.ARCHITECT, MyCustomArchitect)

Relationship to concrete classes
---------------------------------
``ArchitectAgent``  → satisfies ``ArchitectStrategy``
``CriticAgent``     → satisfies ``CriticStrategy``
``SynthesizerAgent``→ satisfies ``SynthesizerStrategy``
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ArchitectStrategy(Protocol):
    """
    Protocol for the Architect role.

    The Architect receives a task and an assembled context, then returns
    a design proposal dict with at minimum: content, knowledge_gaps,
    candidate_tasks, and trace_id.

    The result is passed directly to CriticStrategy.call() and later stored
    as an artifact in MemoryManager — both consumers rely on the dict shape
    documented here.

    Minimum response shape
    ----------------------
    ::

        {
            "content":          str,   # design narrative
            "tokens_used":      int,   # LLM token consumption
            "knowledge_gaps":   list,  # topics to research further
            "decisions":        list,  # key architectural decisions
            "open_questions":   list,  # unresolved questions
            "candidate_tasks":  list,  # follow-up tasks to create
            "trace_id":         str,   # correlation ID
        }
    """

    async def call(self, task: dict, context: dict) -> dict:
        """Run one architect turn and return a design proposal."""
        ...


@runtime_checkable
class CriticStrategy(Protocol):
    """
    Protocol for the Critic role.

    The Critic receives the original task and the Architect's result, then
    returns an evaluation dict with at minimum: content, score, flags,
    and trace_id.

    The score (0.0–1.0) drives the refinement loop in the micro loop:
    scores below ``cfg.min_critic_score`` trigger re-runs of the Architect
    with the Critic's feedback injected as context.

    Minimum response shape
    ----------------------
    ::

        {
            "content":      str,    # critique narrative
            "tokens_used":  int,    # LLM token consumption
            "score":        float,  # 0.0 (terrible) to 1.0 (excellent)
            "flags":        list,   # specific issues to address
            "trace_id":     str,    # correlation ID (propagated from architect)
        }
    """

    async def call(self, task: dict, architect_result: dict) -> dict:
        """Run one critic turn and return an evaluation of the design proposal."""
        ...


@runtime_checkable
class SynthesizerStrategy(Protocol):
    """
    Protocol for the Synthesizer role.

    The Synthesizer reads a collection of artifacts (meso level) or documents
    (macro level) and produces a coherent summary document.

    ``level`` must be either ``"meso"`` or ``"macro"``.  Additional kwargs
    depend on the level (see SynthesizerAgent.call() for the full signature).

    Minimum response shape
    ----------------------
    ::

        {
            "content":      str,  # synthesis document (prose)
            "tokens_used":  int,  # LLM token consumption
            "level":        str,  # echoes back "meso" or "macro"
            "trace_id":     str,  # correlation ID
        }
    """

    async def call(self, level: str, **kwargs) -> dict:
        """Run one synthesis pass at the given level and return a summary document."""
        ...
