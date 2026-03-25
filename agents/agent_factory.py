"""
agents/agent_factory.py
========================

Factory for creating Tinker agent instances.

Why a factory?
--------------
Application code should not hardcode which agent class maps to which role.
This factory centralises the mapping (OCP / DIP) so new agent variants
(e.g. a specialised ArchitectAgent for a different domain) can be introduced
by extending the registry without modifying call sites.

Usage
-----
::

    from agents.agent_factory import create_agent, create_all_agents
    from core.llm.types import AgentRole

    # Single agent:
    architect = create_agent(AgentRole.ARCHITECT, router)

    # All agents at once (returns a dict keyed by AgentRole):
    agents = create_all_agents(router)
    architect  = agents[AgentRole.ARCHITECT]
    critic     = agents[AgentRole.CRITIC]
    synthesizer = agents[AgentRole.SYNTHESIZER]
"""

from __future__ import annotations

from typing import Any

from core.llm.types import AgentRole


# ---------------------------------------------------------------------------
# Internal registry — maps AgentRole → constructor callable.
#
# Each entry is a zero-argument lambda that returns the agent *class* (not an
# instance), so new variants can be registered at runtime without re-importing
# this module.
# ---------------------------------------------------------------------------

_AGENT_REGISTRY: dict[AgentRole, type] = {}


def _ensure_registry() -> None:
    """Populate _AGENT_REGISTRY on first use (lazy import)."""
    if _AGENT_REGISTRY:
        return
    from agents import ArchitectAgent, CriticAgent, SynthesizerAgent

    _AGENT_REGISTRY[AgentRole.ARCHITECT] = ArchitectAgent
    _AGENT_REGISTRY[AgentRole.CRITIC] = CriticAgent
    _AGENT_REGISTRY[AgentRole.SYNTHESIZER] = SynthesizerAgent
    # RESEARCHER is an alias for ArchitectAgent in the default implementation
    _AGENT_REGISTRY[AgentRole.RESEARCHER] = ArchitectAgent


def register_agent(role: AgentRole, agent_class: type) -> None:
    """Register a custom agent class for a given role.

    Call this before the first ``create_agent`` invocation to override the
    default implementation for any role.

    Parameters
    ----------
    role        : The AgentRole this class should handle.
    agent_class : A class whose constructor accepts ``(router)`` as its only
                  positional argument.
    """
    _AGENT_REGISTRY[role] = agent_class


def create_agent(role: AgentRole, router: Any) -> Any:
    """Create and return an agent for the given role.

    Parameters
    ----------
    role   : Which reasoning role this agent fulfils.
    router : A ModelRouter (or compatible stub) to inject into the agent.

    Returns
    -------
    The agent instance (ArchitectAgent, CriticAgent, SynthesizerAgent, …).

    Raises
    ------
    ValueError
        If no agent class is registered for the given role.
    """
    _ensure_registry()
    agent_class = _AGENT_REGISTRY.get(role)
    if agent_class is None:
        raise ValueError(
            f"No agent registered for role {role!r}.  "
            f"Known roles: {list(_AGENT_REGISTRY)}.  "
            f"Use register_agent() to add a custom class."
        )
    return agent_class(router)


def create_all_agents(router: Any) -> dict[AgentRole, Any]:
    """Create all standard agents and return them keyed by AgentRole.

    This is a convenience wrapper around :func:`create_agent` for the common
    case where all roles are needed at once.

    Parameters
    ----------
    router : A ModelRouter (or compatible stub).

    Returns
    -------
    dict[AgentRole, agent]
        ``{AgentRole.ARCHITECT: ..., AgentRole.CRITIC: ..., ...}``
    """
    _ensure_registry()
    return {role: create_agent(role, router) for role in _AGENT_REGISTRY}
