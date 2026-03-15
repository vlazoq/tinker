#!/usr/bin/env python3
"""
main.py — Tinker entrypoint.

Usage:
    python main.py --problem "Design a distributed job queue system"
    python main.py --problem "..." --stubs          # use in-process stubs (no Ollama needed)
    python main.py --problem "..." --no-dashboard   # suppress dashboard state writes

Tinker will run indefinitely (Ctrl-C to stop).
In a second terminal, run:
    python -m p10_observability_dashboard
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Make all component packages importable regardless of working directory
# ---------------------------------------------------------------------------
ROOT = Path(__file__).parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Load .env if present (TINKER_SERVER_URL, TINKER_SECONDARY_URL, etc.)
# ---------------------------------------------------------------------------
_env_file = ROOT / ".env"
if _env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file)
    except ImportError:
        pass  # python-dotenv not installed — env vars must be set externally

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("tinker.main")


# ---------------------------------------------------------------------------
# Component construction helpers
# ---------------------------------------------------------------------------

def _build_real_components(problem: str) -> dict:
    """
    Construct all real Tinker components and wire them together.
    Raises if a required import fails.
    """
    # ── P1: Model Client ─────────────────────────────────────────────────────
    from p1_model_client_n_ollama.router import ModelRouter
    from p1_model_client_n_ollama.types  import MachineConfig

    router = ModelRouter(
        server_config    = MachineConfig.server_defaults(),
        secondary_config = MachineConfig.secondary_defaults(),
    )

    # ── P2: Memory Manager ───────────────────────────────────────────────────
    from p2_memory_manager.manager  import MemoryManager
    from p2_memory_manager.schemas  import MemoryConfig

    mem_config = MemoryConfig(
        redis_url    = os.getenv("TINKER_REDIS_URL", "redis://localhost:6379"),
        duckdb_path  = os.getenv("TINKER_DUCKDB_PATH", "tinker_session.duckdb"),
        chroma_path  = os.getenv("TINKER_CHROMA_PATH", "./chroma_db"),
        sqlite_path  = os.getenv("TINKER_SQLITE_PATH", "tinker_tasks.sqlite"),
    )
    memory_manager = MemoryManager(config=mem_config)

    # ── P3: Tool Layer ────────────────────────────────────────────────────────
    from p3_tool_layer.registry import build_default_registry

    tool_layer = build_default_registry(
        searxng_url        = os.getenv("TINKER_SEARXNG_URL", "http://localhost:8080"),
        artifact_output_dir = os.getenv("TINKER_ARTIFACT_DIR", "./tinker_artifacts"),
        diagram_output_dir  = os.getenv("TINKER_DIAGRAM_DIR", "./tinker_diagrams"),
        memory_manager     = memory_manager,
    )

    # ── P4: Agent Prompts (PromptBuilder) ─────────────────────────────────────
    # Used indirectly by ContextAssembler; agents build their own prompts.

    # ── P5: Task Engine ──────────────────────────────────────────────────────
    from p5_task_engine.engine import TaskEngine

    task_engine = TaskEngine(
        problem_statement = problem,
        db_path           = os.getenv("TINKER_TASK_DB", "tinker_tasks_engine.sqlite"),
    )

    # ── P6: Context Assembler ────────────────────────────────────────────────
    from p6_context_assembler.context_assembler import ContextAssembler, _MemoryManagerProtocol

    # Adapt the real MemoryManager to the ContextAssembler's protocol
    class _MemoryAdaptor(_MemoryManagerProtocol):
        """Bridges MemoryManager (p2) → ContextAssembler's protocol (p6)."""

        def __init__(self, mm: MemoryManager) -> None:
            self._mm = mm

        async def get_arch_state_summary(self) -> str:
            try:
                docs = await self._mm.get_all_documents()
                if not docs:
                    return ""
                latest = docs[-1]
                return latest.get("content", "")[:1500]
            except Exception:
                return ""

        async def semantic_search_session(self, query: str, top_k: int = 5):
            from p6_context_assembler.context_assembler import MemoryItem
            try:
                artifacts = await self._mm.get_recent_artifacts(limit=top_k * 2)
                items = []
                for a in artifacts[:top_k]:
                    items.append(MemoryItem(
                        id=a.id,
                        content=a.content[:500],
                        score=0.8,
                        source="session",
                    ))
                return items
            except Exception:
                return []

        async def semantic_search_archive(self, query: str, top_k: int = 5):
            from p6_context_assembler.context_assembler import MemoryItem
            try:
                notes = await self._mm.search_research(query=query, n_results=top_k)
                return [
                    MemoryItem(id=n.id, content=n.content[:500], score=0.75, source="archive")
                    for n in notes
                ]
            except Exception:
                return []

        async def get_prior_critique(self, task_id: str):
            return []

    class _NullPromptBuilder:
        """Minimal PromptBuilder stub for the ContextAssembler."""
        def build_system_identity(self, role) -> str:
            return f"You are a {role.value} agent in Tinker."
        def build_output_format(self, role, loop_level: int) -> str:
            return "Respond with a JSON object."
        def render_template(self, template_name: str, **kwargs) -> str:
            return ""

    context_assembler = ContextAssembler(
        memory_manager = _MemoryAdaptor(memory_manager),
        prompt_builder = _NullPromptBuilder(),
    )

    # ── P7: Agents (wrap ModelRouter) ────────────────────────────────────────
    from agents import ArchitectAgent, CriticAgent, SynthesizerAgent

    architect_agent   = ArchitectAgent(router)
    critic_agent      = CriticAgent(router)
    synthesizer_agent = SynthesizerAgent(router)

    # ── P8: Architecture State Manager ───────────────────────────────────────
    from p8_architecture_state_manager.manager import ArchitectureStateManager

    arch_state_manager = ArchitectureStateManager(
        workspace   = os.getenv("TINKER_WORKSPACE", "./tinker_workspace"),
        system_name = problem[:80],
        auto_git    = os.getenv("TINKER_AUTO_GIT", "true").lower() == "true",
    )

    # ── P9: Anti-Stagnation (optional — not required by orchestrator directly) ─
    # Wired separately; the orchestrator does not call it directly.

    return {
        "router":           router,
        "memory_manager":   memory_manager,
        "task_engine":      task_engine,
        "context_assembler": context_assembler,
        "architect_agent":  architect_agent,
        "critic_agent":     critic_agent,
        "synthesizer_agent": synthesizer_agent,
        "tool_layer":       tool_layer,
        "arch_state_manager": arch_state_manager,
    }


def _build_stub_components(problem: str) -> dict:
    """
    Use the in-process stubs from p7_orchestrator/stubs.py.
    No external services required.  Useful for smoke-testing the wiring.
    """
    from p7_orchestrator.stubs import build_stub_components
    return build_stub_components()


# ---------------------------------------------------------------------------
# Main async entrypoint
# ---------------------------------------------------------------------------

async def _async_main(problem: str, use_stubs: bool) -> None:
    from p7_orchestrator.orchestrator import Orchestrator
    from p7_orchestrator.config import OrchestratorConfig

    config = OrchestratorConfig(
        macro_interval_seconds = float(os.getenv("TINKER_MACRO_INTERVAL", str(4 * 3600))),
        meso_trigger_count     = int(os.getenv("TINKER_MESO_TRIGGER", "5")),
        architect_timeout      = float(os.getenv("TINKER_ARCHITECT_TIMEOUT", "120")),
        critic_timeout         = float(os.getenv("TINKER_CRITIC_TIMEOUT", "60")),
    )

    if use_stubs:
        logger.info("Running with IN-PROCESS STUBS — no Ollama or external services needed")
        components = _build_stub_components(problem)
    else:
        logger.info("Building real components (Ollama required at %s)",
                    os.getenv("TINKER_SERVER_URL", "http://localhost:11434"))
        components = _build_real_components(problem)

    # Start the ModelRouter HTTP session (no-op for stubs)
    router = components.pop("router", None)
    if router is not None:
        await router.start()

    # Connect the MemoryManager storage backends (no-op for stubs)
    memory_manager = components.get("memory_manager")
    if hasattr(memory_manager, "connect"):
        try:
            await memory_manager.connect()
            logger.info("MemoryManager connected to all storage backends")
        except Exception as exc:
            logger.warning("MemoryManager connect failed (%s) — some features may be limited", exc)

    orchestrator = Orchestrator(
        config              = config,
        task_engine         = components["task_engine"],
        context_assembler   = components["context_assembler"],
        architect_agent     = components["architect_agent"],
        critic_agent        = components["critic_agent"],
        synthesizer_agent   = components["synthesizer_agent"],
        memory_manager      = components["memory_manager"],
        tool_layer          = components["tool_layer"],
        arch_state_manager  = components["arch_state_manager"],
    )

    logger.info("=" * 60)
    logger.info("TINKER starting")
    logger.info("Problem: %s", problem)
    logger.info("Mode   : %s", "STUBS" if use_stubs else "REAL")
    logger.info("=" * 60)

    try:
        await orchestrator.run()
    finally:
        if router is not None:
            await router.shutdown()
        if memory_manager is not None and hasattr(memory_manager, "close"):
            await memory_manager.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Tinker — Autonomous Architecture Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --problem "Design a distributed job queue system"
  python main.py --problem "Design a real-time analytics pipeline" --stubs
  python main.py --problem "..." --stubs  # smoke-test without Ollama

Press Ctrl-C to stop.  Run the dashboard in a separate terminal:
  python -m p10_observability_dashboard
""",
    )
    parser.add_argument(
        "--problem", "-p",
        default="Design a robust, scalable software architecture",
        help="The architectural design problem Tinker will work on.",
    )
    parser.add_argument(
        "--stubs",
        action="store_true",
        default=False,
        help="Use in-process stubs instead of real Ollama models (no external services needed).",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("TINKER_LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.getLogger().setLevel(args.log_level)

    try:
        asyncio.run(_async_main(problem=args.problem, use_stubs=args.stubs))
    except KeyboardInterrupt:
        logger.info("Tinker stopped by user (Ctrl-C).")


if __name__ == "__main__":
    main()
