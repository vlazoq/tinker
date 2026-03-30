"""
agents/__init__.py
==================

Public surface of the ``agents`` package.

This module is intentionally thin — it re-exports the public symbols from
the individual agent modules so that all existing call sites continue to
work without modification::

    from agents import ArchitectAgent, CriticAgent, SynthesizerAgent
    from agents import _current_trace_id          # used by web_search / llm client
    from agents import ArchitectStrategy, CriticStrategy, SynthesizerStrategy

Concrete agent classes live in their own files:
  * agents/architect.py   — ArchitectAgent
  * agents/critic.py      — CriticAgent
  * agents/synthesizer.py — SynthesizerAgent

Shared utilities (trace ContextVar, prompt builders, response parsers, etc.)
live in agents/_shared.py.

Structural protocols (interfaces) live in agents/protocols.py.

The agent factory (agents/agent_factory.py) is the recommended way to
instantiate agents — it maps AgentRole enums to classes and supports
runtime substitution of custom implementations.
"""

from __future__ import annotations

# ── Shared internals re-exported for backward compatibility ──────────────────
# These are imported directly by tests/test_agents.py, core/tools/web_search.py,
# and core/llm/client.py.  The leading underscore signals that these are
# implementation details — new code should not depend on them directly.
from agents._shared import (
    _build_architect_prompts,
    _build_critic_prompts,
    _build_synthesizer_prompts,
    _current_trace_id,
    _extract_candidate_tasks,
    _extract_knowledge_gaps,
    _extract_score,
    _get_prompt_builder_cls,
    _get_rate_limiter_registry,
    _get_retry_async,
    _parse_architect_structured,
)

# ── Concrete agent classes ────────────────────────────────────────────────────
from agents.architect import ArchitectAgent
from agents.critic import CriticAgent
from agents.fritz.protocol import VCSAgentProtocol

# ── Structural protocols (interfaces) ────────────────────────────────────────
from agents.protocols import ArchitectStrategy, CriticStrategy, SynthesizerStrategy
from agents.synthesizer import SynthesizerAgent

__all__ = [
    # Public: concrete agent classes
    "ArchitectAgent",
    # Public: protocol / interface types
    "ArchitectStrategy",
    "CriticAgent",
    "CriticStrategy",
    "SynthesizerAgent",
    "SynthesizerStrategy",
    "VCSAgentProtocol",
    "_build_architect_prompts",
    "_build_critic_prompts",
    "_build_synthesizer_prompts",
    # Semi-public: re-exported internals (backward compat)
    "_current_trace_id",
    "_extract_candidate_tasks",
    "_extract_knowledge_gaps",
    "_extract_score",
    "_get_prompt_builder_cls",
    "_get_rate_limiter_registry",
    "_get_retry_async",
    "_parse_architect_structured",
]
