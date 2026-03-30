#!/usr/bin/env python3
"""
main.py — The single entry point for Tinker.
=============================================

What this file does
--------------------
1. Reads configuration from the .env file and command-line arguments.
2. Delegates startup concerns to the bootstrap/ package (SRP):
     bootstrap.logging_config  — configure loguru / stdlib logging
     bootstrap.components      — build core AI / storage components
     bootstrap.enterprise_stack — build resilience / observability stack
     bootstrap.health          — pre-flight health checks
3. Starts the Orchestrator, which runs the micro/meso/macro loops indefinitely.

Usage
-----
    python main.py --problem "Design a distributed job queue system"
    python main.py --problem "..." --stubs          # no Ollama needed
    python main.py --problem "..." --dashboard      # TUI dashboard in-process

Press Ctrl-C to stop.  Run the dashboard in a separate terminal:
    python -m dashboard
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure all Tinker packages are on the Python import path.
# ---------------------------------------------------------------------------
ROOT = Path(__file__).parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Load .env (if present) before anything else reads env vars.
# ---------------------------------------------------------------------------
_env_file = ROOT / ".env"
if _env_file.exists():
    try:
        from dotenv import load_dotenv

        load_dotenv(_env_file)
    except ImportError:
        pass

logger = logging.getLogger("tinker.main")


# ---------------------------------------------------------------------------
# Dashboard state translation
# ---------------------------------------------------------------------------


def _make_dashboard_patch(orch_state_dict: dict) -> dict:
    """Convert OrchestratorState.to_dict() into the dashboard patch format."""
    totals = orch_state_dict.get("totals", {})
    patch = {
        "connected": True,
        "loop_level": orch_state_dict.get("current_level", "micro"),
        "micro_count": totals.get("micro", 0),
        "meso_count": totals.get("meso", 0),
        "macro_count": totals.get("macro", 0),
    }

    task_id = orch_state_dict.get("current_task_id")
    subsystem = orch_state_dict.get("current_subsystem", "")
    if task_id:
        patch["active_task"] = {
            "id": task_id,
            "type": "design",
            "subsystem": subsystem or "",
            "description": f"Task {task_id[:8]}… (subsystem: {subsystem or 'unknown'})",
            "status": "active",
        }

    subsystem_counts = orch_state_dict.get("subsystem_micro_counts", {})
    if subsystem_counts:
        patch["queue_stats"] = {
            "total_depth": sum(subsystem_counts.values()),
            "by_status": {},
            "by_type": subsystem_counts,
        }

    return patch


# ---------------------------------------------------------------------------
# Async main
# ---------------------------------------------------------------------------


async def _async_main(problem: str, use_stubs: bool, dashboard: bool) -> None:
    """Everything from here runs inside asyncio's event loop."""
    from bootstrap.components import build_real_components, build_stub_components
    from bootstrap.enterprise_stack import build_enterprise_stack
    from bootstrap.health import asyncio_exception_handler, run_health_check

    asyncio.get_event_loop().set_exception_handler(asyncio_exception_handler)

    from runtime.orchestrator.config import OrchestratorConfig
    from runtime.orchestrator.orchestrator import Orchestrator

    config = OrchestratorConfig(
        macro_interval_seconds=float(os.getenv("TINKER_MACRO_INTERVAL", str(4 * 3600))),
        meso_trigger_count=int(os.getenv("TINKER_MESO_TRIGGER", "5")),
        architect_timeout=float(os.getenv("TINKER_ARCHITECT_TIMEOUT", "120")),
        critic_timeout=float(os.getenv("TINKER_CRITIC_TIMEOUT", "60")),
    )

    # ── TINKER.md project instructions ───────────────────────────────────────
    from core.prompts.builder import PromptBuilder as _PromptBuilder

    _instructions_path = Path(config.project_instructions_path)
    if _instructions_path.exists():
        try:
            _instructions_content = _instructions_path.read_text(encoding="utf-8")
            _PromptBuilder.set_global_project_instructions(_instructions_content)
            logger.info(
                "Project instructions loaded from %s (%d chars)",
                _instructions_path,
                len(_instructions_content),
            )
        except Exception as _exc:
            logger.warning(
                "Could not read %s: %s — running without project instructions",
                _instructions_path,
                _exc,
            )
    else:
        logger.info(
            "No TINKER.md found at %s — running without project instructions",
            _instructions_path,
        )

    # ── Checkpoint manager ────────────────────────────────────────────────────
    from runtime.orchestrator.checkpoint import CheckpointManager

    checkpoint_manager = CheckpointManager(
        path=config.checkpoint_path,
        enabled=config.checkpoint_enabled,
    )
    prior_checkpoint = checkpoint_manager.load()
    if prior_checkpoint:
        logger.info(
            "Found checkpoint from micro iteration %d — will resume",
            prior_checkpoint.get("micro_iteration", 0),
        )

    # ── Build components ──────────────────────────────────────────────────────
    if use_stubs:
        logger.info("Running with IN-PROCESS STUBS — no Ollama or external services needed")
        components = build_stub_components(problem)
    else:
        logger.info(
            "Building real components (Ollama required at %s)",
            os.getenv("TINKER_SERVER_URL", "http://localhost:11434"),
        )
        components = build_real_components(problem)

    # ── Enterprise stack ──────────────────────────────────────────────────────
    enterprise = build_enterprise_stack()

    await enterprise["dlq"].connect()
    await enterprise["audit_log"].connect()
    await enterprise["lineage_tracker"].connect()

    from infra.observability.audit_log import AuditEventType

    await enterprise["audit_log"].log(
        event_type=AuditEventType.SYSTEM_START,
        actor="main",
        resource="tinker",
        outcome="started",
        details={"problem": problem[:100], "mode": "stubs" if use_stubs else "real"},
    )

    if not use_stubs:
        from core.validation.input_validator import validate_problem_statement

        problem = validate_problem_statement(problem)
        await run_health_check()

    # ── Connect router and memory ─────────────────────────────────────────────
    router = components.pop("router", None)
    if router is not None:
        await router.start()

    memory_manager = components.get("memory_manager")
    if hasattr(memory_manager, "connect"):
        try:
            await memory_manager.connect()
            logger.info("MemoryManager connected to all storage backends")
        except Exception as exc:
            logger.warning("MemoryManager connect failed (%s) — limited functionality", exc)

    # ── Metrics ───────────────────────────────────────────────────────────────
    from metrics import TinkerMetrics

    metrics = TinkerMetrics()

    # ── Auto-recovery ─────────────────────────────────────────────────────────
    from infra.resilience.auto_recovery import AutoRecoveryManager

    auto_recovery = AutoRecoveryManager(
        memory_manager=components.get("memory_manager"),
        circuit_registry=enterprise["circuit_registry"],
    )
    for name in ("redis", "chromadb"):
        cb = enterprise["circuit_registry"].get_or_default(name)
        if cb:
            cb.on_state_change(auto_recovery.on_circuit_open)
    enterprise["auto_recovery"] = auto_recovery

    # ── Health server ─────────────────────────────────────────────────────────
    health_port = int(os.getenv("TINKER_HEALTH_PORT", "8080"))
    if os.getenv("TINKER_HEALTH_ENABLED", "true").lower() != "false":
        from infra.health.http_server import HealthServer

        health_server = HealthServer(
            orchestrator=None,
            memory_manager=components.get("memory_manager"),
            circuit_registry=enterprise["circuit_registry"],
            rate_registry=enterprise["rate_registry"],
            sla_tracker=enterprise["sla_tracker"],
            dlq=enterprise["dlq"],
        )
        enterprise["health_server"] = health_server
        try:
            await health_server.start(port=health_port)
            logger.info("Health server started on port %d", health_port)
        except Exception as exc:
            logger.warning("Health server failed to start: %s", exc)

    # ── Dashboard snapshot callback ───────────────────────────────────────────
    from ui.dashboard.subscriber import publish_state as _publish_state

    # Try to import the web UI StatePublisher for SSE push notifications.
    # If the web UI is not running in-process, this is a no-op.
    _web_notify: object = None
    try:
        from ui.web.app import _publisher as _web_publisher
        from ui.web.app import notify_state_change as _notify_web

        _web_notify = (_web_publisher, _notify_web)
    except ImportError:
        pass

    def _dashboard_snapshot_cb() -> None:
        state_dict = orchestrator.state.to_dict()
        _publish_state(_make_dashboard_patch(state_dict))
        # Also push to the web UI StatePublisher for SSE clients.
        if _web_notify is not None:
            pub, notify_fn = _web_notify
            try:
                asyncio.get_event_loop().create_task(notify_fn(pub, state_dict))
            except Exception:
                pass  # Event loop may not be running yet

    # ── Create Orchestrator ───────────────────────────────────────────────────
    orchestrator = Orchestrator(
        config=config,
        task_engine=components["task_engine"],
        context_assembler=components["context_assembler"],
        architect_agent=components["architect_agent"],
        critic_agent=components["critic_agent"],
        synthesizer_agent=components["synthesizer_agent"],
        memory_manager=components["memory_manager"],
        tool_layer=components["tool_layer"],
        arch_state_manager=components["arch_state_manager"],
        stagnation_monitor=components.get("stagnation_monitor"),
        metrics=metrics,
        snapshot_callback=_dashboard_snapshot_cb,
        checkpoint_manager=checkpoint_manager,
        event_bus=components.get("event_bus"),
        research_team=components.get("research_team"),
        research_enhancer=components.get("research_enhancer"),
    )

    # ── Self-improvement engine ─────────────────────────────────────────────
    _self_improvement_engine = None
    if os.getenv("TINKER_SELF_IMPROVE_ENABLED", "false").lower() == "true":
        from runtime.orchestrator.self_improvement import SelfImprovementEngine

        _self_improvement_engine = SelfImprovementEngine(
            baseline_temperature=float(os.getenv("TINKER_TEMPERATURE", "0.7")),
            self_improve_branch=os.getenv("TINKER_SELF_IMPROVE_BRANCH", "tinker/self-improve"),
            enabled=True,
        )
        logger.info("Self-improvement engine enabled")

    # ── MCP ───────────────────────────────────────────────────────────────────
    try:
        from core.mcp.bridge import MCPBridge as _MCPBridge
        from core.mcp.config import MCPConfig as _MCPConfig

        _mcp_config = _MCPConfig.from_env()
        if _mcp_config.enabled:
            _mcp_bridge = _MCPBridge(_mcp_config, components["tool_layer"])
            try:
                from ui.web.app import app as _webui_app

                _mcp_bridge.mount_server(_webui_app)
                _webui_app.state.mcp_bridge = _mcp_bridge
            except Exception as _exc:
                logger.warning("Could not mount MCP server on webui app: %s", _exc)
            await _mcp_bridge.connect_clients()
            logger.info("MCP subsystem ready")
    except ImportError as _exc:
        logger.debug("MCP subsystem not available: %s", _exc)

    # Attach optional self-improvement engine to the orchestrator so
    # macro_loop.py can access it via orch._self_improvement.
    orchestrator._self_improvement = _self_improvement_engine

    if enterprise.get("health_server") is not None:
        enterprise["health_server"]._orchestrator = orchestrator

    logger.info("=" * 60)
    logger.info("TINKER starting")
    logger.info("Problem: %s", problem)
    logger.info("Mode   : %s", "STUBS" if use_stubs else "REAL")
    logger.info("Health endpoint: http://localhost:%d/health", health_port)
    logger.info("=" * 60)

    # ── Run orchestrator ──────────────────────────────────────────────────────

    async def _run_orchestrator() -> None:
        try:
            await orchestrator.run()
        finally:
            if enterprise.get("health_server"):
                await enterprise["health_server"].stop()

            with contextlib.suppress(Exception):
                await enterprise["audit_log"].log(
                    event_type=AuditEventType.SYSTEM_STOP,
                    actor="main",
                    resource="tinker",
                    outcome="stopped",
                    details={
                        "micro_loops": orchestrator.state.total_micro_loops,
                        "meso_loops": orchestrator.state.total_meso_loops,
                        "macro_loops": orchestrator.state.total_macro_loops,
                    },
                )

            if router is not None:
                await router.shutdown()

            if memory_manager is not None and hasattr(memory_manager, "close"):
                await memory_manager.close()

            await enterprise["dlq"].close()
            await enterprise["audit_log"].close()
            await enterprise["lineage_tracker"].close()
            if hasattr(enterprise["dist_lock"], "close"):
                await enterprise["dist_lock"].close()
            if hasattr(enterprise["idempotency_cache"], "close"):
                await enterprise["idempotency_cache"].close()

    if dashboard:
        from ui.dashboard.app import TinkerDashboard
        from ui.dashboard.subscriber import QueueSubscriber

        sub = QueueSubscriber()
        app = TinkerDashboard(subscriber=sub)
        orch_task = asyncio.create_task(_run_orchestrator())
        try:
            await app.run_async()
        finally:
            orchestrator.request_shutdown()
            try:
                await asyncio.wait_for(orch_task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                orch_task.cancel()
    else:
        await _run_orchestrator()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Synchronous CLI entry point — called by ``python main.py``."""
    parser = argparse.ArgumentParser(
        description="Tinker — Autonomous Architecture Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --problem "Design a distributed job queue system"
  python main.py --problem "Design a real-time analytics pipeline" --stubs
  python main.py --problem "..." --stubs --dashboard

Press Ctrl-C to stop.  Dashboard in a separate terminal: python -m dashboard
""",
    )
    parser.add_argument(
        "--problem",
        "-p",
        default="Design a robust, scalable software architecture",
        help="The architectural design problem Tinker will work on.",
    )
    parser.add_argument(
        "--stubs",
        action="store_true",
        default=False,
        help="Use in-process stubs (no Ollama or external services needed).",
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        default=False,
        help="Launch the Textual TUI dashboard in-process.",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("TINKER_LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )

    args = parser.parse_args()

    from bootstrap.logging_config import setup_logging

    setup_logging(args.log_level)

    # Install trace context filter on the root logger so every log record
    # (from any module) carries trace_id, loop_level, task_id, etc.
    from infra.observability.structured_logging import install_trace_filter

    install_trace_filter()

    try:
        asyncio.run(
            _async_main(
                problem=args.problem,
                use_stubs=args.stubs,
                dashboard=args.dashboard,
            ),
            debug=(args.log_level == "DEBUG"),
        )
    except KeyboardInterrupt:
        logger.info("Tinker stopped by user (Ctrl-C).")


if __name__ == "__main__":
    main()
