"""
runtime/orchestrator/orchestrator.py
============================

The Orchestrator — Tinker's heartbeat and central controller.

This file contains the ``Orchestrator`` class, which drives Tinker's three
reasoning loops indefinitely.  Implementation details are split across
focused mixin modules:

  _loop_runners.py  — micro/meso/macro loop execution + model preset reload
  _resilience.py    — backpressure, capacity planning, DLQ replay
  _stagnation.py    — anti-stagnation detection and intervention
  _lifecycle.py     — signal handling and graceful shutdown

The Orchestrator itself contains only the core wiring: ``__init__``, the main
loop, pause/resume, and utility methods.  All intelligence lives in the
injected components; all operational concerns live in the mixins.

The three-loop architecture
----------------------------
  Micro loop  (fastest, most frequent)
    Picks one task → gathers context → calls Architect AI → optionally fills
    knowledge gaps via Tool Layer → calls Critic AI → stores the artifact →
    marks task done → generates follow-up tasks.

  Meso loop   (medium frequency)
    Fires when a single subsystem has accumulated ``meso_trigger_count``
    successful micro loops.  The Synthesizer AI produces a coherent
    subsystem design document.

  Macro loop  (slowest, on a timer)
    Fires every ``macro_interval_seconds`` (default: 4 hours).  The
    Synthesizer produces a system-wide architectural snapshot, committed
    to version control.

How to run it
--------------
    from runtime.orchestrator import Orchestrator, OrchestratorConfig
    from runtime.orchestrator.stubs import build_stub_components
    import asyncio

    components = build_stub_components()
    orch = Orchestrator(config=OrchestratorConfig(), **components)
    asyncio.run(orch.run())
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from core.events import EventBus, Event, EventType
from .config import OrchestratorConfig
from .state import OrchestratorState, LoopLevel

# Mixin classes — each provides a focused group of methods
from ._loop_runners import LoopRunnerMixin
from ._resilience import ResilienceMixin
from ._stagnation import StagnationMixin
from ._lifecycle import LifecycleMixin

try:
    from infra.observability.audit_log import AuditEventType

    _AUDIT_AVAILABLE = True
except ImportError:
    AuditEventType = None  # type: ignore[assignment,misc]
    _AUDIT_AVAILABLE = False

logger = logging.getLogger("tinker.orchestrator")


class Orchestrator(
    LoopRunnerMixin,
    ResilienceMixin,
    StagnationMixin,
    LifecycleMixin,
):
    """
    Central controller that drives Tinker's three reasoning loops indefinitely.

    The Orchestrator is intentionally "dumb" — it contains no AI reasoning of
    its own.  Its only job is to decide *when* to run each loop and to route
    the results between components.  All the intelligence lives in the injected
    components.

    Operational concerns are delegated to focused mixins:
      LoopRunnerMixin  — _run_micro, _run_meso, _run_macro, _check_model_preset
      ResilienceMixin  — _apply_backpressure, _update_capacity_planner, _setup_dlq_replayer
      StagnationMixin  — _check_stagnation, _apply_stagnation_directive
      LifecycleMixin   — _install_signal_handlers, _handle_signal, _on_shutdown
    """

    def __init__(
        self,
        *,
        config: Optional[OrchestratorConfig] = None,
        task_engine: Any,
        context_assembler: Any,
        architect_agent: Any,
        critic_agent: Any,
        synthesizer_agent: Any,
        memory_manager: Any,
        tool_layer: Any,
        arch_state_manager: Any,
        stagnation_monitor: Any = None,
        metrics: Any = None,
        snapshot_callback: Optional[Any] = None,
        checkpoint_manager: Optional[Any] = None,
        event_bus: Optional[Any] = None,
        research_team: Optional[Any] = None,
    ) -> None:
        self.config = config or OrchestratorConfig()

        # Store all injected components as instance attributes so the loop
        # functions can access them via ``orch.component_name``.
        self.task_engine = task_engine
        self.context_assembler = context_assembler
        self.architect_agent = architect_agent
        self.critic_agent = critic_agent
        self.synthesizer_agent = synthesizer_agent
        self.memory_manager = memory_manager
        self.tool_layer = tool_layer
        self.arch_state_manager = arch_state_manager

        # Optional components
        self.stagnation_monitor = stagnation_monitor
        self.metrics = metrics
        self._snapshot_callback = snapshot_callback

        # Event bus for hooks — enables decoupled reactions to lifecycle events.
        # When provided, the orchestrator emits events at key points (task
        # selection, agent calls, loop completion, stagnation, etc.) so
        # external handlers can react without touching orchestrator code.
        self.event_bus = event_bus

        # Research team — optional parallel research agent coordinator.
        # When provided, knowledge-gap research runs concurrently instead
        # of sequentially.  Created automatically in bootstrap if tool_layer
        # is available.
        self.research_team = research_team

        # Enterprise components dictionary — populated by bootstrap layer.
        self.enterprise: dict = {}

        if stagnation_monitor is not None:
            logger.info("StagnationMonitor wired — anti-stagnation detection active")
        if metrics is not None:
            logger.info("Metrics wired — Prometheus counters active")
        if event_bus is not None:
            logger.info("EventBus wired — lifecycle hooks active")

        # Checkpoint manager for pause/resume support.
        self.checkpoint_manager = checkpoint_manager

        # Create a fresh state object — the single source of truth.
        self.state = OrchestratorState()

        # Asyncio events for shutdown and pause coordination.
        self._shutdown_event = asyncio.Event()
        self._pause_event = asyncio.Event()

        # Confirmation gate
        from .confirmation import ConfirmationGate
        self.confirmation_gate = ConfirmationGate(self.config, self.state)

        # DLQ auto-replayer — initialised lazily during run().
        self._dlq_replayer: Any = None

    # ── Event helpers ────────────────────────────────────────────────────────

    async def emit_event(
        self,
        event_type: EventType,
        payload: dict | None = None,
        source: str = "orchestrator",
    ) -> None:
        """Publish an event on the bus if one is wired, swallowing errors."""
        if self.event_bus is None:
            return
        try:
            await self.event_bus.publish(
                Event(type=event_type, payload=payload or {}, source=source)
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("EventBus publish failed (non-fatal): %s", exc)

    # ── Public API ───────────────────────────────────────────────────────────

    async def run(self) -> None:
        """
        Entry point — start the orchestrator and run until told to stop.

        Installs signal handlers, starts the DLQ replayer, enters the main
        loop, and guarantees shutdown cleanup via try/finally.
        """
        self._install_signal_handlers()
        logger.info("Orchestrator starting — PID signals wired, entering main loop")

        await self._setup_dlq_replayer()

        await self.emit_event(EventType.SYSTEM_STARTED, {
            "config": {
                "meso_trigger_count": self.config.meso_trigger_count,
                "macro_interval_seconds": self.config.macro_interval_seconds,
            },
        })

        try:
            await self._main_loop()
        except asyncio.CancelledError:
            logger.info("Orchestrator task cancelled — treating as shutdown")
        finally:
            await self.emit_event(EventType.SYSTEM_STOPPING, {
                "total_micro_loops": self.state.total_micro_loops,
                "total_meso_loops": self.state.total_meso_loops,
                "total_macro_loops": self.state.total_macro_loops,
            })
            await self._on_shutdown()

    def request_shutdown(self) -> None:
        """
        Ask the orchestrator to stop at the end of the current micro loop.

        Safe to call from tests, the Dashboard, or signal handlers.
        """
        logger.info("Shutdown requested programmatically")
        self.state.shutdown_requested = True
        self._shutdown_event.set()

    def pause(self) -> None:
        """
        Pause the orchestrator between micro loops.

        The orchestrator finishes the current micro loop, then waits until
        resume() is called.  Saves a checkpoint so the run can survive a
        process kill while paused.
        """
        logger.info("Pause requested")
        self.state.paused = True
        self._pause_event.set()

        if self.checkpoint_manager:
            self.checkpoint_manager.save(self._build_checkpoint_data())

    def resume(self) -> None:
        """Resume the orchestrator after a pause."""
        logger.info("Resume requested")
        self.state.paused = False
        self._pause_event.clear()

        if self.checkpoint_manager:
            self.checkpoint_manager.clear()

    def _build_checkpoint_data(self) -> dict:
        """Build the dict that CheckpointManager saves to disk."""
        return {
            "micro_iteration": self.state.total_micro_loops,
            "current_task_id": self.state.current_task_id,
            "current_subsystem": self.state.current_subsystem,
            "subsystem_counts": dict(self.state.subsystem_micro_counts),
            "micro_history_tail": [
                {
                    "iteration": r.iteration,
                    "task_id": r.task_id,
                    "subsystem": r.subsystem,
                    "status": r.status.value,
                    "critic_score": r.critic_score,
                }
                for r in self.state.micro_history[-10:]
            ],
        }

    @property
    def is_running(self) -> bool:
        """True if the orchestrator has not yet been asked to stop."""
        return not self._shutdown_event.is_set()

    def get_state_snapshot(self) -> dict:
        """Return a JSON-serialisable snapshot of the current state."""
        return self.state.to_dict()

    # ── Main loop ────────────────────────────────────────────────────────────

    async def _main_loop(self) -> None:
        """
        The central "forever" loop that drives all three reasoning levels.

        Each iteration: macro check → backpressure → micro → meso check →
        failure backoff → idle sleep → snapshot write → shutdown check.
        """
        _preset_mtime: float = 0.0

        while not self._shutdown_event.is_set():
            # Model preset hot-reload check
            try:
                _preset_mtime = await self._check_model_preset(_preset_mtime)
            except Exception as _preset_exc:
                logger.warning("Preset check error (non-fatal): %s", _preset_exc)

            # Pause check
            if self._pause_event.is_set():
                self.state.current_level = LoopLevel.IDLE
                self._try_write_snapshot()
                logger.info("Orchestrator paused — waiting for resume()")
                while self._pause_event.is_set() and not self._shutdown_event.is_set():
                    await self._interruptible_sleep(1.0)
                if self._shutdown_event.is_set():
                    break
                logger.info("Orchestrator resuming")

            # Macro loop timer check
            if self._should_run_macro():
                await self._run_macro()

            # Backpressure check
            await self._apply_backpressure()

            # Micro loop
            self.state.current_level = LoopLevel.MICRO
            micro_succeeded = await self._run_micro()

            if micro_succeeded:
                self.state.consecutive_failures = 0

                # Meso check
                subsystem = self.state.current_subsystem
                if subsystem and self._should_run_meso(subsystem):
                    await self._run_meso(subsystem)

            else:
                self.state.consecutive_failures += 1

                if (
                    self.state.consecutive_failures
                    >= self.config.max_consecutive_failures
                ):
                    logger.warning(
                        "Backing off for %.1fs after %d consecutive failures",
                        self.config.failure_backoff_seconds,
                        self.state.consecutive_failures,
                    )
                    await self._interruptible_sleep(self.config.failure_backoff_seconds)
                    self.state.consecutive_failures = 0

            # Idle sleep
            if self.config.micro_loop_idle_seconds > 0:
                await self._interruptible_sleep(self.config.micro_loop_idle_seconds)

            # Write state snapshot
            self._try_write_snapshot()

        self.state.current_level = LoopLevel.IDLE

    # ── Utilities ─────────────────────────────────────────────────────────────

    async def _interruptible_sleep(self, seconds: float) -> None:
        """
        Sleep for up to ``seconds`` seconds, but wake early if shutdown is
        requested.

        Uses ``asyncio.wait_for(event.wait(), timeout=seconds)`` so the
        orchestrator stays responsive to shutdown requests during any sleep.
        """
        try:
            await asyncio.wait_for(
                self._shutdown_event.wait(),
                timeout=seconds,
            )
        except asyncio.TimeoutError:
            pass

    def _try_write_snapshot(self) -> None:
        """
        Attempt to write the current state to disk, ignoring any errors.

        After a successful write, invokes ``_snapshot_callback`` (if set)
        so in-process observers can react to the new state.
        """
        try:
            self.state.write_snapshot(self.config.state_snapshot_path)
        except Exception as exc:
            logger.warning("Could not write state snapshot: %s", exc)
            return

        if self._snapshot_callback is not None:
            try:
                self._snapshot_callback()
            except Exception as exc:
                logger.debug("snapshot_callback failed (non-fatal): %s", exc)
