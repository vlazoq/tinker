"""
stubs.py
========
Concrete stub implementations of MemoryManager and PromptBuilder used for
local development and testing.  Replace with real implementations when
integrating with the full Tinker system.
"""

from __future__ import annotations

import asyncio
import random
import textwrap
from typing import Any

from .assembler import (
    AgentRole,
    MemoryItem,
    _MemoryManagerProtocol,
    _PromptBuilderProtocol,
)


# ---------------------------------------------------------------------------
# Stub Memory Manager
# ---------------------------------------------------------------------------

_FAKE_ARTIFACTS = [
    MemoryItem(
        id="art-001",
        content=textwrap.dedent("""\
            Architecture v0.3 – Microservices split proposal.
            Introduced an API Gateway to consolidate routing for Auth, Billing,
            and Notification services.  Latency budget: 50 ms p99 per hop.
            Trade-off: adds one network round-trip but simplifies client code.
        """),
        score=0.91,
        source="session",
    ),
    MemoryItem(
        id="art-002",
        content=textwrap.dedent("""\
            Event-sourcing pattern applied to the Order domain.
            All state mutations written as immutable events to Kafka topic
            `orders.v1`.  Consumers project into Postgres read models.
            Concern raised: replay time for large event streams (~48 h).
        """),
        score=0.85,
        source="session",
    ),
    MemoryItem(
        id="art-003",
        content=textwrap.dedent("""\
            CQRS experiment for the Inventory service.
            Write model: DDD aggregates with optimistic locking.
            Read model: denormalized Elasticsearch index refreshed every 5 s.
            Result: 3× query throughput improvement in load test.
        """),
        score=0.78,
        source="session",
    ),
]

_FAKE_RESEARCH = [
    MemoryItem(
        id="res-101",
        content=textwrap.dedent("""\
            [Paper] "Consistency Models in Distributed Databases" (VLDB 2023).
            Key finding: eventual consistency acceptable for >70 % of OLTP
            workloads when compensating transactions are handled client-side.
            Recommends Saga pattern over 2PC for cross-service transactions.
        """),
        score=0.88,
        source="archive",
    ),
    MemoryItem(
        id="res-102",
        content=textwrap.dedent("""\
            [Blog] Shopify's migration to component-based monolith (2022).
            They extracted micro-frontends but kept the backend monolith with
            clear module boundaries.  Reduced deployment complexity by 40 %.
            Key lesson: premature service extraction harms iteration speed.
        """),
        score=0.82,
        source="archive",
    ),
]

_FAKE_CRITIQUE = [
    MemoryItem(
        id="crit-201",
        content=textwrap.dedent("""\
            Critique of arch v0.3 (loop 4, critic role):
            The API Gateway introduces a single point of failure unless
            deployed as a cluster.  Current design lacks a circuit-breaker
            strategy for downstream service failures.  Recommend: integrate
            Resilience4j or Envoy sidecar before promoting to v0.4.
        """),
        score=0.95,
        source="critique",
    ),
]

_FAKE_ARCH_STATE = textwrap.dedent("""\
    ## Architecture State – v0.3  (loop 7)

    **Style**: Microservices (6 services identified)
    **Transport**: REST (external) + gRPC (internal)
    **Data stores**: Postgres (transactional), Redis (cache), Elasticsearch (search)
    **Messaging**: Kafka for async event propagation
    **Deployment target**: Kubernetes (3-node cluster, AWS EKS)

    **Open decisions**:
    - [ ] Circuit-breaker strategy for API Gateway
    - [ ] Saga vs 2PC for cross-service transactions
    - [ ] Schema registry governance policy for Kafka topics

    **Resolved decisions**:
    - [x] Event sourcing for Order domain (art-002)
    - [x] CQRS for Inventory (art-003)
    - [x] JWT-based auth with 15-min TTL
""")


class StubMemoryManager(_MemoryManagerProtocol):
    """Returns deterministic fake data; optionally simulates latency."""

    def __init__(self, latency: float = 0.05):
        self.latency = latency

    async def get_arch_state_summary(self) -> str:
        await asyncio.sleep(self.latency)
        return _FAKE_ARCH_STATE

    async def semantic_search_session(
        self, query: str, top_k: int = 5
    ) -> list[MemoryItem]:
        await asyncio.sleep(self.latency)
        # Return copies with slightly shuffled scores to simulate a live
        # semantic search without mutating the global fake data (which would
        # make tests non-deterministic across runs).
        import copy
        items = copy.deepcopy(_FAKE_ARTIFACTS[:top_k])
        for item in items:
            item.score = round(min(1.0, item.score + random.uniform(-0.02, 0.02)), 3)
        return items

    async def semantic_search_archive(
        self, query: str, top_k: int = 5
    ) -> list[MemoryItem]:
        await asyncio.sleep(self.latency)
        return _FAKE_RESEARCH[:top_k]

    async def get_prior_critique(self, task_id: str) -> list[MemoryItem]:
        await asyncio.sleep(self.latency)
        return _FAKE_CRITIQUE


# ---------------------------------------------------------------------------
# Stub Prompt Builder
# ---------------------------------------------------------------------------

_ROLE_IDENTITIES: dict[AgentRole, str] = {
    AgentRole.ARCHITECT: textwrap.dedent("""\
        You are Tinker's **Architect** agent.
        Your purpose: design and evolve the software architecture.
        You think in systems: components, contracts, trade-offs, constraints.
        You produce structured architecture proposals with explicit rationale.
        You are rigorous, creative, and comfortable with uncertainty.
    """),
    AgentRole.CRITIC: textwrap.dedent("""\
        You are Tinker's **Critic** agent.
        Your purpose: identify flaws, risks, and blind spots in proposed architectures.
        You think adversarially but constructively — every critique ends with a suggestion.
        You focus on: correctness, scalability, operability, and security.
    """),
    AgentRole.RESEARCHER: textwrap.dedent("""\
        You are Tinker's **Researcher** agent.
        Your purpose: surface relevant prior art, patterns, and empirical evidence.
        You synthesize research notes into actionable architectural insights.
        You cite sources and flag when evidence is weak or conflicting.
    """),
    AgentRole.SYNTHESIZER: textwrap.dedent("""\
        You are Tinker's **Synthesizer** agent.
        Your purpose: integrate proposals, critiques, and research into a coherent
        architecture decision record (ADR).
        You resolve contradictions, document trade-offs, and advance the architecture state.
    """),
}

_FORMAT_TEMPLATES: dict[AgentRole, str] = {
    AgentRole.ARCHITECT: textwrap.dedent("""\
        ## Output Format (loop {loop_level})
        Respond with a structured architecture proposal:
        1. **Summary** (2-3 sentences)
        2. **Components Changed / Added** (bullet list)
        3. **Rationale** (paragraph per decision)
        4. **Open Questions** (bullet list)
        5. **Proposed State Version** (increment patch: v0.X → v0.X+1)
    """),
    AgentRole.CRITIC: textwrap.dedent("""\
        ## Output Format (loop {loop_level})
        Respond with a structured critique:
        1. **Risk Register** (table: risk | severity | likelihood | mitigation)
        2. **Design Flaws** (bullet list with explanation)
        3. **Unaddressed Requirements** (if any)
        4. **Suggested Fixes** (actionable, prioritized)
    """),
    AgentRole.RESEARCHER: textwrap.dedent("""\
        ## Output Format (loop {loop_level})
        Respond with a research synthesis:
        1. **Relevant Patterns** (name + summary + applicability)
        2. **Empirical Evidence** (findings from research notes)
        3. **Recommended Reading** (if gaps identified)
        4. **Architectural Implications** (concise bullets)
    """),
    AgentRole.SYNTHESIZER: textwrap.dedent("""\
        ## Output Format (loop {loop_level})
        Respond with an Architecture Decision Record (ADR):
        1. **Title** (imperative sentence)
        2. **Status**: Proposed | Accepted | Superseded
        3. **Context** (what forces are at play)
        4. **Decision** (the change we're making)
        5. **Consequences** (good + bad)
        6. **Updated Architecture State** (full summary)
    """),
}


class StubPromptBuilder(_PromptBuilderProtocol):
    def build_system_identity(self, role: AgentRole) -> str:
        return _ROLE_IDENTITIES.get(role, f"You are a Tinker agent with role: {role}.")

    def build_output_format(self, role: AgentRole, loop_level: int) -> str:
        template = _FORMAT_TEMPLATES.get(role, "Respond clearly and concisely.")
        return template.replace("{loop_level}", str(loop_level))

    def render_template(self, template_name: str, **kwargs: Any) -> str:
        # Passthrough stub — real implementation would load Jinja2 templates
        return f"[template:{template_name}] {kwargs}"
