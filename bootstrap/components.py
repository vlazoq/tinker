"""
bootstrap/components.py
=======================

Single responsibility: construct and wire the core AI and storage components.

Extracted from main.py so that component construction can be read and
modified without touching logging or enterprise stack code.

Two public functions mirror the two runtime modes:

  build_real_components(problem)  — real Ollama, Redis, ChromaDB, etc.
  build_stub_components(problem)  — in-process stubs (no external services).

Both return the same dict shape so the rest of the startup code is identical
regardless of mode (OCP / polymorphism).

Usage
-----
::

    from bootstrap.components import build_real_components, build_stub_components

    components = build_real_components("Design a cache layer")
    # or
    components = build_stub_components("Design a cache layer")
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("tinker.bootstrap.components")


def build_real_components(problem: str) -> dict:
    """Construct and wire all real Tinker components.

    All imports are deferred so missing optional packages only fail when the
    relevant component is actually needed, not at module-import time.

    Parameters
    ----------
    problem : str
        The architecture problem statement used to seed the task queue.

    Returns
    -------
    dict
        Keys: router, memory_manager, task_engine, context_assembler,
              architect_agent, critic_agent, synthesizer_agent,
              tool_layer, arch_state_manager, stagnation_monitor.
    """
    # ── LLM / Model Router ───────────────────────────────────────────────────
    from core.llm.router import ModelRouter
    from core.llm.types import AgentRole, Machine, MachineConfig, TaskAwareRoutingStrategy

    # Build a task-aware routing strategy that respects per-role overrides
    # from environment variables.  E.g. setting TINKER_RESEARCHER_MACHINE=secondary
    # would route the Researcher to the lighter model.
    _role_overrides: dict[AgentRole, Machine] = {}
    for _role in AgentRole:
        _env = os.getenv(f"TINKER_{_role.value.upper()}_MACHINE", "")
        if _env:
            try:
                _role_overrides[_role] = Machine(_env)
            except ValueError:
                logger.warning(
                    "Ignoring invalid TINKER_%s_MACHINE=%s (expected 'server' or 'secondary')",
                    _role.value.upper(),
                    _env,
                )

    routing_strategy = TaskAwareRoutingStrategy(model_overrides=_role_overrides)

    router = ModelRouter(
        server_config=MachineConfig.server_defaults(),
        secondary_config=MachineConfig.secondary_defaults(),
        routing_strategy=routing_strategy,
    )

    # ── Memory Manager ────────────────────────────────────────────────────────
    from core.memory.manager import MemoryManager
    from core.memory.schemas import MemoryConfig

    mem_config = MemoryConfig(
        redis_url=os.getenv("TINKER_REDIS_URL", "redis://localhost:6379"),
        duckdb_path=os.getenv("TINKER_DUCKDB_PATH", "tinker_session.duckdb"),
        chroma_path=os.getenv("TINKER_CHROMA_PATH", "./chroma_db"),
        sqlite_path=os.getenv("TINKER_SQLITE_PATH", "tinker_tasks.sqlite"),
    )
    memory_manager = MemoryManager(config=mem_config)

    # ── Tool Layer ────────────────────────────────────────────────────────────
    from core.tools.registry import build_default_registry

    tool_layer = build_default_registry(
        searxng_url=os.getenv("TINKER_SEARXNG_URL", "http://localhost:8080"),
        artifact_output_dir=os.getenv("TINKER_ARTIFACT_DIR", "./tinker_artifacts"),
        diagram_output_dir=os.getenv("TINKER_DIAGRAM_DIR", "./tinker_diagrams"),
        memory_manager=memory_manager,
        search_default_results=int(os.getenv("TINKER_SEARCH_DEFAULT_RESULTS", "10")),
        search_max_results=int(os.getenv("TINKER_SEARCH_MAX_RESULTS", "50")),
    )

    # ── Task Engine ───────────────────────────────────────────────────────────
    from runtime.tasks.engine import TaskEngine

    task_engine = TaskEngine(
        problem_statement=problem,
        db_path=os.getenv("TINKER_TASK_DB", "tinker_tasks_engine.sqlite"),
    )

    # ── Context Assembler ─────────────────────────────────────────────────────
    from core.context.assembler import ContextAssembler
    from core.context.memory_adapter import MemoryAdaptor
    from core.context.prompt_builder_adapter import PromptBuilderAdapter

    context_assembler = ContextAssembler(
        memory_manager=MemoryAdaptor(memory_manager),
        prompt_builder=PromptBuilderAdapter(),
    )

    # ── Agents ────────────────────────────────────────────────────────────────
    from agents import ArchitectAgent, CriticAgent, SynthesizerAgent

    architect_agent = ArchitectAgent(router)
    critic_agent = CriticAgent(router)
    synthesizer_agent = SynthesizerAgent(router)

    # ── Architecture State Manager ────────────────────────────────────────────
    from infra.architecture.manager import ArchitectureStateManager

    arch_state_manager = ArchitectureStateManager(
        workspace=os.getenv("TINKER_WORKSPACE", "./tinker_workspace"),
        system_name=problem[:80],
        auto_git=os.getenv("TINKER_AUTO_GIT", "true").lower() == "true",
    )

    # ── Anti-Stagnation Monitor ───────────────────────────────────────────────
    from runtime.stagnation.config import StagnationMonitorConfig
    from runtime.stagnation.monitor import StagnationMonitor

    stagnation_monitor = StagnationMonitor(config=StagnationMonitorConfig())

    # ── Event Bus (hooks system) ─────────────────────────────────────────────
    from core.events import EventBus

    event_bus = EventBus()

    # ── Research Team (parallel research agents) ────────────────────────────
    from agents.research_team import ResearchTeam

    research_team = ResearchTeam(
        tool_layer=tool_layer,
        memory_manager=memory_manager,
        max_concurrent=int(os.getenv("TINKER_RESEARCH_CONCURRENCY", "3")),
        research_num_results=int(os.getenv("TINKER_RESEARCH_NUM_RESULTS", "10")),
        research_max_scrape=int(os.getenv("TINKER_RESEARCH_MAX_SCRAPE", "5")),
        research_max_content_chars=int(os.getenv("TINKER_RESEARCH_MAX_CONTENT_CHARS", "8000")),
    )

    # ── Auto-Memory (cross-session learning) ─────────────────────────────────
    from core.memory.auto_memory import AutoMemory

    auto_memory = AutoMemory(
        memory_dir=os.getenv("TINKER_MEMORY_DIR", "./tinker_memory"),
        high_score_threshold=float(os.getenv("TINKER_MEMORY_HIGH_THRESHOLD", "0.85")),
        low_score_threshold=float(os.getenv("TINKER_MEMORY_LOW_THRESHOLD", "0.4")),
    )
    auto_memory.attach(event_bus)

    # ── Research Enhancer (query rewriting, memory-first, summarization) ────
    from core.tools.research_enhancer import ResearchEnhancer

    research_enhancer = ResearchEnhancer(
        router=router,
        memory_manager=memory_manager,
        query_rewrite=os.getenv("TINKER_RESEARCH_QUERY_REWRITE", "true").lower() == "true",
        memory_first=os.getenv("TINKER_RESEARCH_MEMORY_FIRST", "true").lower() == "true",
        summarize=os.getenv("TINKER_RESEARCH_SUMMARIZE", "true").lower() == "true",
        iterative_max_rounds=int(os.getenv("TINKER_RESEARCH_ITERATIVE_ROUNDS", "2")),
        summarize_threshold=int(os.getenv("TINKER_RESEARCH_SUMMARIZE_THRESHOLD", "3000")),
        memory_min_score=float(os.getenv("TINKER_RESEARCH_MEMORY_MIN_SCORE", "0.7")),
        llm_max_concurrent=int(os.getenv("TINKER_RESEARCH_LLM_MAX_CONCURRENT", "2")),
    )

    # ── Webhook Dispatcher (n8n / local automation integration) ──────────────
    from core.tools.webhook import WebhookDispatcher

    webhook_dispatcher = WebhookDispatcher(
        timeout=float(os.getenv("TINKER_WEBHOOK_TIMEOUT", "10")),
        max_concurrent=int(os.getenv("TINKER_WEBHOOK_MAX_CONCURRENT", "5")),
    )
    webhook_dispatcher.attach(event_bus)

    return {
        "router": router,
        "memory_manager": memory_manager,
        "task_engine": task_engine,
        "context_assembler": context_assembler,
        "architect_agent": architect_agent,
        "critic_agent": critic_agent,
        "synthesizer_agent": synthesizer_agent,
        "tool_layer": tool_layer,
        "arch_state_manager": arch_state_manager,
        "stagnation_monitor": stagnation_monitor,
        "event_bus": event_bus,
        "research_team": research_team,
        "research_enhancer": research_enhancer,
    }


def build_stub_components(problem: str) -> dict:
    """Build stub (fake) components for smoke-testing without external services.

    Stubs implement the same interfaces as the real components but return
    hardcoded / random data instead of calling Ollama, Redis, etc.

    Parameters
    ----------
    problem : str
        Ignored — stubs use hardcoded behaviour.  The parameter exists so
        callers can switch between real and stub modes without branching.
    """
    from runtime.orchestrator.stubs import build_stub_components as _build

    return _build()
