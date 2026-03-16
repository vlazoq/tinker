#!/usr/bin/env python3
"""
main.py — The single entry point for Tinker.
=============================================

What this file does
--------------------
This is the file you run to start Tinker. It does three things:

1. **Reads configuration** from the .env file and command-line arguments.
2. **Builds all the components** — connects to Ollama, Redis, databases, etc.
   (or creates stub replacements if you pass --stubs, for testing without real services).
3. **Starts the Orchestrator**, which then runs the micro/meso/macro loops forever.

Think of this file as the "wiring diagram" of the system. Every component is
imported and connected here. The components themselves (llm/, memory/, tools/, etc.)
don't know about each other — they only know about the interfaces they expose.
This pattern is called "dependency injection" and makes the system easy to test.

Usage:
    python main.py --problem "Design a distributed job queue system"
    python main.py --problem "..." --stubs          # use in-process stubs (no Ollama needed)
    python main.py --problem "..." --dashboard      # launch TUI dashboard in-process

Tinker will run indefinitely (Ctrl-C to stop).
Alternatively, run the dashboard in a separate terminal:
    python -m dashboard
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure all Tinker packages are on the Python import path.
#
# When you run "python main.py" from the tinker/ directory, Python only looks
# for packages in the current directory. This line makes that explicit and
# ensures it works even if you run main.py from a different directory.
# ---------------------------------------------------------------------------
ROOT = Path(__file__).parent.resolve()  # Absolute path to this file's directory
if str(ROOT) not in sys.path:
    # Insert at position 0 so our packages take priority over any system packages
    # with the same name (unlikely but safe practice).
    sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Load environment variables from the .env file if one exists.
#
# The .env file (see .env.example) lets you configure Tinker without editing
# code. Variables like TINKER_SERVER_URL, TINKER_REDIS_URL, etc. are set there.
# If python-dotenv isn't installed, we skip this silently — environment
# variables set in the shell will still work.
# ---------------------------------------------------------------------------
_env_file = ROOT / ".env"
if _env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file)
    except ImportError:
        pass  # python-dotenv not installed — env vars must be set externally

# ---------------------------------------------------------------------------
# Set up logging so we can see what Tinker is doing.
#
# The format is: "10:00:01  INFO      tinker.main  Starting up..."
# Every module creates its own logger (e.g. "tinker.orchestrator.micro")
# which appears in this format so you know which part of the system logged it.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("tinker.main")


# ---------------------------------------------------------------------------
# Component construction
# ---------------------------------------------------------------------------

def _build_real_components(problem: str) -> dict:
    """
    Construct and wire together all real Tinker components.

    This is the "real" mode that uses actual AI models, Redis, ChromaDB, etc.
    All components are imported and constructed here, then returned as a dict.
    The Orchestrator receives this dict and uses the components via the
    interfaces they expose.

    Note: We use late imports (inside the function) so that if a package is
    missing (e.g. textual not installed), it only fails when you actually try
    to use that component, not when Python first loads main.py.

    Parameters:
        problem — the architecture problem statement (e.g. "Design a cache layer")

    Returns:
        A dict of component instances keyed by name.
        Keys: router, memory_manager, task_engine, context_assembler,
              architect_agent, critic_agent, synthesizer_agent,
              tool_layer, arch_state_manager
    """

    # ── LLM / Model Router ───────────────────────────────────────────────────
    # The ModelRouter handles all communication with Ollama (the local AI server).
    # It knows about two machines: "server" (big model, does architecture design)
    # and "secondary" (smaller model, does critiques).
    # MachineConfig.server_defaults() and secondary_defaults() read the
    # TINKER_SERVER_URL / TINKER_SECONDARY_URL env vars, falling back to
    # localhost if not set.
    from llm.router import ModelRouter
    from llm.types  import MachineConfig

    router = ModelRouter(
        server_config    = MachineConfig.server_defaults(),
        secondary_config = MachineConfig.secondary_defaults(),
    )

    # ── Memory Manager ────────────────────────────────────────────────────────
    # The MemoryManager is a unified interface over four storage backends:
    # - Redis: fast, ephemeral working memory (lost when Redis restarts)
    # - DuckDB: columnar database for session artifacts (survives restarts)
    # - ChromaDB: vector database for semantic search over research notes
    # - SQLite: reliable task registry (survives restarts)
    # All paths and URLs come from environment variables, with sensible defaults.
    from memory.manager  import MemoryManager
    from memory.schemas  import MemoryConfig

    mem_config = MemoryConfig(
        redis_url    = os.getenv("TINKER_REDIS_URL", "redis://localhost:6379"),
        duckdb_path  = os.getenv("TINKER_DUCKDB_PATH", "tinker_session.duckdb"),
        chroma_path  = os.getenv("TINKER_CHROMA_PATH", "./chroma_db"),
        sqlite_path  = os.getenv("TINKER_SQLITE_PATH", "tinker_tasks.sqlite"),
    )
    memory_manager = MemoryManager(config=mem_config)

    # ── Tool Layer ────────────────────────────────────────────────────────────
    # The ToolRegistry holds all callable tools the AI agents can use:
    # web_search, web_scraper, memory_query, artifact_writer, diagram_generator.
    # build_default_registry() creates one pre-wired with all default tools.
    # We pass memory_manager so the memory_query tool can search Tinker's archive.
    from tools.registry import build_default_registry

    tool_layer = build_default_registry(
        searxng_url         = os.getenv("TINKER_SEARXNG_URL", "http://localhost:8080"),
        artifact_output_dir = os.getenv("TINKER_ARTIFACT_DIR", "./tinker_artifacts"),
        diagram_output_dir  = os.getenv("TINKER_DIAGRAM_DIR", "./tinker_diagrams"),
        memory_manager      = memory_manager,
    )

    # ── Task Engine ───────────────────────────────────────────────────────────
    # The TaskEngine manages the work queue — what should Tinker think about next?
    # It wraps TaskRegistry (SQLite), TaskQueue (priority queue), and
    # TaskGenerator (parses Architect output to create follow-up tasks).
    # If the database is empty, it seeds an initial task from the problem statement.
    from tasks.engine import TaskEngine

    task_engine = TaskEngine(
        problem_statement = problem,
        db_path           = os.getenv("TINKER_TASK_DB", "tinker_tasks_engine.sqlite"),
    )

    # ── Context Assembler ─────────────────────────────────────────────────────
    # The ContextAssembler builds the "background information" section of each
    # prompt. Before calling the Architect, we fetch relevant past results from
    # memory and package them up with a token budget.
    #
    # The ContextAssembler was designed with a specific memory interface
    # (methods like get_arch_state_summary, semantic_search_session, etc.)
    # that doesn't exactly match the real MemoryManager's API. We bridge the
    # gap with a small _MemoryAdaptor class defined right here.
    from context.assembler import ContextAssembler, _MemoryManagerProtocol

    class _MemoryAdaptor(_MemoryManagerProtocol):
        """
        Bridges the real MemoryManager (memory/manager.py) to the interface
        that ContextAssembler expects (_MemoryManagerProtocol).

        The ContextAssembler was built with a specific set of method names
        (get_arch_state_summary, semantic_search_session, etc.) that don't
        exist on the real MemoryManager. Rather than changing either component,
        we adapt between them here with a thin wrapper class.

        This pattern is called the "Adapter" design pattern.
        """

        def __init__(self, mm: MemoryManager) -> None:
            self._mm = mm  # Store a reference to the real MemoryManager

        async def get_arch_state_summary(self) -> str:
            """Return a short summary of the current architecture state."""
            try:
                # get_all_documents() returns stored meso/macro synthesis documents.
                # We return the content of the most recent one ([-1] = last item).
                docs = await self._mm.get_all_documents()
                if not docs:
                    return ""
                latest = docs[-1]
                return latest.get("content", "")[:1500]  # Truncate to avoid huge prompts
            except Exception:
                return ""  # Graceful degradation: return empty if anything goes wrong

        async def semantic_search_session(self, query: str, top_k: int = 5):
            """Find recent session artifacts relevant to the query."""
            from context.assembler import MemoryItem
            try:
                # get_recent_artifacts() returns the most recent DuckDB artifacts.
                # We fetch 2x more than needed to have some to filter from.
                artifacts = await self._mm.get_recent_artifacts(limit=top_k * 2)
                items = []
                for a in artifacts[:top_k]:
                    items.append(MemoryItem(
                        id=a.id,
                        content=a.content[:500],
                        score=0.8,         # Fixed score — we're not doing real ranking here
                        source="session",
                    ))
                return items
            except Exception:
                return []

        async def semantic_search_archive(self, query: str, top_k: int = 5):
            """Search the research archive (ChromaDB) for relevant notes."""
            from context.assembler import MemoryItem
            try:
                # search_research() does a vector similarity search in ChromaDB.
                notes = await self._mm.search_research(query=query, n_results=top_k)
                return [
                    MemoryItem(id=n.id, content=n.content[:500], score=0.75, source="archive")
                    for n in notes
                ]
            except Exception:
                return []

        async def get_prior_critique(self, task_id: str):
            """Retrieve previous Architect+Critic artifacts stored under this task."""
            from context.assembler import MemoryItem
            try:
                artifacts = await self._mm.get_artifacts_by_task(task_id, limit=3)
                return [
                    MemoryItem(
                        id=a.id,
                        content=a.content[:800],
                        score=1.0,
                        source="critique",
                    )
                    for a in artifacts
                ]
            except Exception:
                return []

    from context.stubs import StubPromptBuilder

    # Create the ContextAssembler with our adapted memory interface
    context_assembler = ContextAssembler(
        memory_manager = _MemoryAdaptor(memory_manager),
        prompt_builder = StubPromptBuilder(),
    )

    # ── Agents ────────────────────────────────────────────────────────────────
    # These three classes (from agents.py) wrap ModelRouter calls with
    # structured prompts and response parsing.
    # All three agents receive the same router — the router decides which model
    # to use based on the AgentRole in each request.
    from agents import ArchitectAgent, CriticAgent, SynthesizerAgent

    architect_agent   = ArchitectAgent(router)
    critic_agent      = CriticAgent(router)
    synthesizer_agent = SynthesizerAgent(router)

    # ── Architecture State Manager ────────────────────────────────────────────
    # Tracks the evolving architecture as a series of versioned JSON snapshots.
    # Stores them in the workspace directory (default: ./tinker_workspace/).
    # If auto_git is True, commits each snapshot to a local git repository.
    from architecture.manager import ArchitectureStateManager

    arch_state_manager = ArchitectureStateManager(
        workspace   = os.getenv("TINKER_WORKSPACE", "./tinker_workspace"),
        system_name = problem[:80],  # First 80 chars of the problem as the system name
        auto_git    = os.getenv("TINKER_AUTO_GIT", "true").lower() == "true",
    )

    # ── Anti-Stagnation Manager (wired optionally) ────────────────────────────
    # The anti-stagnation manager monitors for repetitive loops and intervenes.
    # It's not directly called by the Orchestrator — it would be checked separately
    # at the start of each micro loop in a more complete integration.
    # Currently it's not wired in here; the Orchestrator handles basic backoff itself.

    # Return all components as a flat dict.
    # The Orchestrator receives these via keyword arguments.
    return {
        "router":            router,
        "memory_manager":    memory_manager,
        "task_engine":       task_engine,
        "context_assembler": context_assembler,
        "architect_agent":   architect_agent,
        "critic_agent":      critic_agent,
        "synthesizer_agent": synthesizer_agent,
        "tool_layer":        tool_layer,
        "arch_state_manager": arch_state_manager,
    }


def _build_stub_components(problem: str) -> dict:
    """
    Build stub (fake) components for smoke-testing without any real services.

    Stubs live in orchestrator/stubs.py. They implement the same interfaces as
    the real components but return hardcoded or random data instead of calling
    Ollama, Redis, etc.

    Use this with: python main.py --problem "..." --stubs

    This is useful for:
    - Verifying that all the wiring is correct without setting up Ollama/Redis
    - Running automated tests in CI/CD environments
    - Quickly checking that a code change doesn't break the orchestration logic
    """
    from orchestrator.stubs import build_stub_components
    return build_stub_components()


# ---------------------------------------------------------------------------
# Dashboard state translation
# ---------------------------------------------------------------------------

def _make_dashboard_patch(orch_state_dict: dict) -> dict:
    """
    Convert an OrchestratorState.to_dict() snapshot into the patch format
    that the dashboard's subscriber understands.

    The Orchestrator's state dict uses field names like 'totals.micro' and
    'current_level'. The dashboard subscriber expects 'micro_count' and
    'loop_level'. This function translates between the two formats.

    This is called after every micro loop and the result is put on the
    dashboard's asyncio queue so the TUI updates in real-time.
    """
    totals = orch_state_dict.get("totals", {})

    # Build the core patch that the dashboard always needs
    patch = {
        "connected":   True,                                           # Marks us as live
        "loop_level":  orch_state_dict.get("current_level", "micro"), # "micro"/"meso"/"macro"
        "micro_count": totals.get("micro", 0),                        # Total micro loops done
        "meso_count":  totals.get("meso", 0),                         # Total meso loops done
        "macro_count": totals.get("macro", 0),                        # Total macro loops done
    }

    # Add the active task info if there is one
    task_id   = orch_state_dict.get("current_task_id")
    subsystem = orch_state_dict.get("current_subsystem", "")
    if task_id:
        patch["active_task"] = {
            "id":          task_id,
            "type":        "design",
            "subsystem":   subsystem or "",
            # Show a short description since we don't have the full task text here
            "description": f"Task {task_id[:8]}… (subsystem: {subsystem or 'unknown'})",
            "status":      "active",
        }

    # Add queue depth statistics derived from the per-subsystem counters
    subsystem_counts = orch_state_dict.get("subsystem_micro_counts", {})
    if subsystem_counts:
        patch["queue_stats"] = {
            "total_depth": sum(subsystem_counts.values()),  # Total work done across all subsystems
            "by_status":   {},                              # Not available at this level
            "by_type":     subsystem_counts,                # Work count per subsystem name
        }

    return patch


# ---------------------------------------------------------------------------
# Startup health check
# ---------------------------------------------------------------------------

async def _health_check() -> None:
    """
    Verify that required external services are reachable.

    Called once at startup before any AI loops begin.  Logs a clear WARNING
    for each service that is down so the user knows what to fix, rather than
    seeing a cryptic timeout error during the first Architect call.

    Services checked:
      - Ollama (primary model server)
      - Redis (working memory)
    """
    import asyncio

    server_url = os.getenv("TINKER_SERVER_URL", "http://localhost:11434")
    redis_url  = os.getenv("TINKER_REDIS_URL",  "redis://localhost:6379")

    # --- Ollama ---
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{server_url.rstrip('/')}/api/tags", timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    logger.info("Health check OK: Ollama reachable at %s", server_url)
                else:
                    logger.warning(
                        "Health check WARN: Ollama at %s returned HTTP %d — "
                        "model calls will likely fail", server_url, resp.status
                    )
    except Exception as exc:
        logger.warning(
            "Health check WARN: Ollama NOT reachable at %s (%s) — "
            "start Ollama before running Tinker", server_url, exc
        )

    # --- Redis ---
    try:
        import aioredis  # type: ignore
        client = aioredis.from_url(redis_url, socket_connect_timeout=3)
        await client.ping()
        await client.aclose()
        logger.info("Health check OK: Redis reachable at %s", redis_url)
    except ImportError:
        pass  # aioredis not installed; skip check (connection will fail later)
    except Exception as exc:
        logger.warning(
            "Health check WARN: Redis NOT reachable at %s (%s) — "
            "working memory will be unavailable", redis_url, exc
        )


# ---------------------------------------------------------------------------
# Main async function
# ---------------------------------------------------------------------------

async def _async_main(problem: str, use_stubs: bool, dashboard: bool) -> None:
    """
    The async entry point — everything from here runs inside asyncio's event loop.

    asyncio is Python's way of doing multiple things "at the same time" without
    using threads. Instead of blocking (waiting), async functions yield control
    to other tasks while they wait for I/O (network, disk). This is perfect for
    Tinker, which constantly waits for AI model responses over HTTP.

    Parameters:
        problem    — the architecture problem to think about
        use_stubs  — if True, use fake components (no real AI/Redis needed)
        dashboard  — if True, launch the TUI dashboard in this same process
    """
    # Import here (inside the async function) so these are only loaded
    # after sys.path is set up and env vars are loaded.
    from orchestrator.orchestrator import Orchestrator
    from orchestrator.config import OrchestratorConfig

    # OrchestratorConfig holds all the tunable settings.
    # Each one reads from an env var with a sensible default.
    config = OrchestratorConfig(
        macro_interval_seconds = float(os.getenv("TINKER_MACRO_INTERVAL", str(4 * 3600))),
        meso_trigger_count     = int(os.getenv("TINKER_MESO_TRIGGER", "5")),
        architect_timeout      = float(os.getenv("TINKER_ARCHITECT_TIMEOUT", "120")),
        critic_timeout         = float(os.getenv("TINKER_CRITIC_TIMEOUT", "60")),
    )

    # Build either real or stub components depending on the --stubs flag
    if use_stubs:
        logger.info("Running with IN-PROCESS STUBS — no Ollama or external services needed")
        components = _build_stub_components(problem)
    else:
        logger.info("Building real components (Ollama required at %s)",
                    os.getenv("TINKER_SERVER_URL", "http://localhost:11434"))
        components = _build_real_components(problem)

    # ── Pre-flight health check ───────────────────────────────────────────────
    # Verify that required external services are reachable before we start.
    # We warn (not crash) so the user gets a clear message instead of a
    # cryptic failure ten seconds into the first Architect loop.
    if not use_stubs:
        await _health_check()

    # Start the HTTP session inside the ModelRouter.
    # The router opens a connection pool to Ollama here. We pop() it from
    # components so we can manage its lifecycle (start/shutdown) separately.
    router = components.pop("router", None)
    if router is not None:
        await router.start()

    # Connect the MemoryManager to all its storage backends.
    # This opens connections to Redis, DuckDB, ChromaDB, and SQLite.
    # If this fails, we log a warning but continue — some features will
    # be limited (e.g. no semantic search) but the core loops still run.
    memory_manager = components.get("memory_manager")
    if hasattr(memory_manager, "connect"):
        try:
            await memory_manager.connect()
            logger.info("MemoryManager connected to all storage backends")
        except Exception as exc:
            logger.warning("MemoryManager connect failed (%s) — some features may be limited", exc)

    # Create the Orchestrator with all the wired-up components.
    # Note: the Orchestrator receives components by keyword argument and never
    # imports them directly. This is "dependency injection" — the Orchestrator
    # doesn't care where components come from, only what interface they expose.
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
    logger.info("Dashboard: %s", "in-process TUI" if dashboard else "separate terminal (python -m dashboard)")
    logger.info("=" * 60)

    # ── Dashboard integration ─────────────────────────────────────────────────
    # The orchestrator writes a state snapshot file after each loop. We also
    # want to push state updates into the dashboard's asyncio queue in real time
    # (when the dashboard runs in the same process).
    #
    # We do this by monkey-patching the orchestrator's _try_write_snapshot method:
    # we save a reference to the original method, then replace it with our version
    # that calls the original AND also calls publish_state().
    #
    # "Monkey-patching" means replacing a method at runtime without changing
    # the source file. It's a pragmatic trick to add behaviour without modifying
    # the Orchestrator class itself.
    from dashboard.subscriber import publish_state as _publish_state

    _orig_try_write = orchestrator._try_write_snapshot  # Save the original method

    def _hooked_write_snapshot() -> None:
        """
        Replacement for orchestrator._try_write_snapshot.
        Does everything the original did, plus publishes state to the dashboard queue.
        """
        _orig_try_write()   # Call the original: writes state to the JSON file on disk
        try:
            # Also push a state patch to the dashboard's in-process queue.
            # _make_dashboard_patch() translates the orchestrator's state format
            # into the format the dashboard subscriber expects.
            _publish_state(_make_dashboard_patch(orchestrator.state.to_dict()))
        except Exception as _exc:
            # Never crash the orchestrator because the dashboard had a problem
            logger.debug("Dashboard publish_state failed: %s", _exc)

    # Replace the method on this specific instance (not the class)
    orchestrator._try_write_snapshot = _hooked_write_snapshot

    # ── Run the orchestrator (with or without the in-process dashboard) ────────

    async def _run_orchestrator() -> None:
        """Run the orchestrator and clean up connections when it stops."""
        try:
            await orchestrator.run()  # Blocks until shutdown is requested
        finally:
            # Always clean up, even if something crashed
            if router is not None:
                await router.shutdown()
            if memory_manager is not None and hasattr(memory_manager, "close"):
                await memory_manager.close()

    if dashboard:
        # Run the dashboard and orchestrator concurrently in the same asyncio loop.
        #
        # asyncio.create_task() starts the orchestrator as a background task.
        # The dashboard runs in the "foreground" (we await it).
        # When the user quits the dashboard (presses 'q'), we tell the orchestrator
        # to shut down gracefully, then wait up to 5 seconds for it to finish.
        from dashboard.app import TinkerDashboard
        from dashboard.subscriber import QueueSubscriber

        # QueueSubscriber reads from the shared asyncio.Queue that publish_state() writes to.
        sub = QueueSubscriber()
        app = TinkerDashboard(subscriber=sub)

        orch_task = asyncio.create_task(_run_orchestrator())  # Start orchestrator in background
        try:
            await app.run_async()   # Block until user quits dashboard
        finally:
            orchestrator.request_shutdown()   # Ask orchestrator to stop
            try:
                await asyncio.wait_for(orch_task, timeout=5.0)  # Wait up to 5s
            except (asyncio.TimeoutError, asyncio.CancelledError):
                orch_task.cancel()  # Force cancel if it doesn't stop in time
    else:
        # Headless mode: just run the orchestrator until Ctrl-C
        await _run_orchestrator()


# ---------------------------------------------------------------------------
# Command-line interface
# ---------------------------------------------------------------------------

def main() -> None:
    """
    The synchronous CLI entry point — called when you run `python main.py`.

    This function:
    1. Parses command-line arguments (--problem, --stubs, --dashboard, --log-level)
    2. Configures logging
    3. Calls asyncio.run() to start the async event loop

    asyncio.run() is the bridge between synchronous Python (regular scripts)
    and asynchronous Python (async/await). Everything inside _async_main runs
    in an asyncio event loop.
    """
    parser = argparse.ArgumentParser(
        description="Tinker — Autonomous Architecture Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --problem "Design a distributed job queue system"
  python main.py --problem "Design a real-time analytics pipeline" --stubs
  python main.py --problem "..." --stubs --dashboard   # stubs + TUI dashboard

Press Ctrl-C to stop.  Run the dashboard in a separate terminal:
  python -m dashboard
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
        "--dashboard",
        action="store_true",
        default=False,
        help=(
            "Launch the Textual TUI dashboard in-process, fed by the live orchestrator. "
            "Requires `textual` to be installed. "
            "Alternatively run `python -m dashboard` in a second terminal."
        ),
    )

    parser.add_argument(
        "--log-level",
        default=os.getenv("TINKER_LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity. DEBUG shows everything; ERROR shows only failures.",
    )

    args = parser.parse_args()

    # Apply the log level to the root logger (affects all sub-loggers)
    logging.getLogger().setLevel(args.log_level)

    try:
        # asyncio.run() creates a new event loop, runs _async_main until it returns,
        # then closes the loop. KeyboardInterrupt (Ctrl-C) propagates out of run().
        asyncio.run(_async_main(
            problem=args.problem,
            use_stubs=args.stubs,
            dashboard=args.dashboard,
        ))
    except KeyboardInterrupt:
        # This is the expected way to stop Tinker: press Ctrl-C.
        # The orchestrator installs signal handlers that trigger a graceful shutdown.
        logger.info("Tinker stopped by user (Ctrl-C).")


# When Python runs a file directly (python main.py), __name__ equals "__main__".
# When the file is imported by another module, __name__ equals "main".
# This guard ensures main() is only called when the file is run directly.
if __name__ == "__main__":
    main()
