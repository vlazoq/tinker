"""
Tests for agents/agent_factory.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agents.agent_factory import (
    create_agent,
    create_all_agents,
    register_agent,
    _AGENT_REGISTRY,
    _ensure_registry,
)
from core.llm.types import AgentRole


@pytest.fixture(autouse=True)
def clear_registry():
    """Reset the agent registry before each test to avoid cross-test pollution."""
    _AGENT_REGISTRY.clear()
    yield
    _AGENT_REGISTRY.clear()


def make_stub_router():
    return MagicMock(name="StubRouter")


class TestCreateAgent:
    def test_architect_agent_created(self):
        router = make_stub_router()
        agent = create_agent(AgentRole.ARCHITECT, router)
        assert agent is not None

    def test_critic_agent_created(self):
        router = make_stub_router()
        agent = create_agent(AgentRole.CRITIC, router)
        assert agent is not None

    def test_synthesizer_agent_created(self):
        router = make_stub_router()
        agent = create_agent(AgentRole.SYNTHESIZER, router)
        assert agent is not None

    def test_router_injected(self):
        router = make_stub_router()

        class SpyAgent:
            def __init__(self, r):
                self.router = r

        register_agent(AgentRole.ARCHITECT, SpyAgent)
        agent = create_agent(AgentRole.ARCHITECT, router)
        assert agent.router is router

    def test_unknown_role_raises_when_manually_removed(self):
        # Populate defaults then remove a role to simulate a missing registration
        _ensure_registry()
        del _AGENT_REGISTRY[AgentRole.CRITIC]
        with pytest.raises(ValueError, match="No agent registered"):
            create_agent(AgentRole.CRITIC, make_stub_router())


class TestCreateAllAgents:
    def test_returns_dict_keyed_by_role(self):
        agents = create_all_agents(make_stub_router())
        assert isinstance(agents, dict)
        assert AgentRole.ARCHITECT in agents
        assert AgentRole.CRITIC in agents
        assert AgentRole.SYNTHESIZER in agents

    def test_all_agents_are_distinct_instances(self):
        agents = create_all_agents(make_stub_router())
        values = list(agents.values())
        # No two entries should be the same object
        for i, a in enumerate(values):
            for b in values[i + 1:]:
                assert a is not b


class TestRegisterAgent:
    def test_custom_class_used(self):
        class CustomArchitect:
            tag = "custom"

            def __init__(self, r):
                pass

        register_agent(AgentRole.ARCHITECT, CustomArchitect)
        agent = create_agent(AgentRole.ARCHITECT, make_stub_router())
        assert isinstance(agent, CustomArchitect)

    def test_overrides_default(self):
        _ensure_registry()  # populate defaults first

        class SpecialCritic:
            def __init__(self, r):
                pass

        register_agent(AgentRole.CRITIC, SpecialCritic)
        agent = create_agent(AgentRole.CRITIC, make_stub_router())
        assert isinstance(agent, SpecialCritic)
