"""
orchestrator.py — Tinker's heartbeat.

The Orchestrator is pure deterministic Python.  It owns no AI reasoning; it
only routes work to the agents it's handed at construction time.

Usage
-----
    orch = Orchestrator(
        config=OrchestratorConfig(),
        task_engine=...,
        context_assembler=...,
        architect_agent=...,
        critic_agent=...,
        synthesizer_agent=...,
        memory_manager=...,
        tool_layer=...,
        arch_state_manager=...,
    )
    asyncio.run(orch.run())
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time
from typing import Any, Optional

from .config import OrchestratorConfig
from .state import OrchestratorState, LoopLevel, LoopStatus
from .micro_loop import run_micro_loop, MicroLoopError
from .meso_loop import run_meso_loop
from .macro_loop import run_macro_loop

logger = logging.getLogger("tinker.orchestrator")


class Orchestrator:
    """
    Central controller that drives Tinker's three reasoning loops indefinitely.

    All component dependencies are injected; the Orchestrator never imports
    them directly, making it fully testable with fakes/stubs.
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
    ) -> None:
        self.config = config or OrchestratorConfig()
        self.task_engine = task_engine
        self.context_assembler = context_assembler
        self.architect_agent = architect_agent
        self.critic_agent = critic_agent
        self.synthesizer_agent = synthesizer_agent
        self.memory_manager = memory_manager
        self.tool_layer = tool_layer
        self.arch_state_manager = arch_state_manager

        self.state = OrchestratorState()
        self._shutdown_event = asyncio.Event()

    # ── Public API ───────────────────────────────────────────────────────────

    async def run(self) -> None:
        """
        Entry point.  Runs indefinitely until SIGINT/SIGTERM or
        `request_shutdown()` is called.
        """
        self._install_signal_handlers()
        logger.info("Orchestrator starting — PID signals wired, entering main loop")

        try:
            await self._main_loop()
        except asyncio.CancelledError:
            logger.info("Orchestrator task cancelled — treating as shutdown")
        finally:
            await self._on_shutdown()

    def request_shutdown(self) -> None:
        """Programmatic shutdown — useful in tests and the Dashboard."""
        logger.info("Shutdown requested programmatically")
        self.state.shutdown_requested = True
        self._shutdown_event.set()

    @property
    def is_running(self) -> bool:
        return not self._shutdown_event.is_set()

    def get_state_snapshot(self) -> dict:
        """Called by Dashboard — returns a JSON-serialisable dict."""
        return self.state.to_dict()

    # ── Main loop ────────────────────────────────────────────────────────────

    async def _main_loop(self) -> None:
        """
        Drive micro loops indefinitely; escalate to meso/macro as needed.
        Never raises — all errors are caught and handled per-loop.
        """
        while not self._shutdown_event.is_set():
            # ── Macro loop timer check ────────────────────────────────────────
            if self._should_run_macro():
                await self._run_macro()

            # ── Micro loop ────────────────────────────────────────────────────
            self.state.current_level = LoopLevel.MICRO
            micro_succeeded = await self._run_micro()

            if micro_succeeded:
                self.state.consecutive_failures = 0

                # ── Meso check: did any subsystem hit its trigger? ─────────────
                subsystem = self.state.current_subsystem
                if subsystem and self._should_run_meso(subsystem):
                    await self._run_meso(subsystem)

            else:
                self.state.consecutive_failures += 1
                if self.state.consecutive_failures >= self.config.max_consecutive_failures:
                    logger.warning(
                        "Backing off for %.1fs after %d consecutive failures",
                        self.config.failure_backoff_seconds,
                        self.state.consecutive_failures,
                    )
                    await self._interruptible_sleep(self.config.failure_backoff_seconds)
                    self.state.consecutive_failures = 0

            # ── Idle sleep (0 by default — run flat-out) ──────────────────────
            if self.config.micro_loop_idle_seconds > 0:
                await self._interruptible_sleep(self.config.micro_loop_idle_seconds)

            # ── Write state snapshot for Dashboard ────────────────────────────
            self._try_write_snapshot()

        self.state.current_level = LoopLevel.IDLE

    # ── Micro ────────────────────────────────────────────────────────────────

    async def _run_micro(self) -> bool:
        """Execute one micro loop.  Returns True on success."""
        try:
            record = await run_micro_loop(self)

            # Update state from completed record
            self.state.total_micro_loops += 1
            self.state.current_task_id = record.task_id
            self.state.current_subsystem = record.subsystem
            self.state.add_micro_record(record)

            if record.status == LoopStatus.SUCCESS:
                # Increment subsystem counter (used for meso trigger)
                self.state.increment_subsystem(record.subsystem)
                return True

            return False

        except MicroLoopError as exc:
            logger.error("Micro loop failed: %s", exc)
            return False

        except Exception as exc:
            logger.exception("Unexpected error in micro loop: %s", exc)
            return False

    # ── Meso ─────────────────────────────────────────────────────────────────

    def _should_run_meso(self, subsystem: str) -> bool:
        count = self.state.subsystem_micro_counts.get(subsystem, 0)
        return count >= self.config.meso_trigger_count

    async def _run_meso(self, subsystem: str) -> None:
        prev_level = self.state.current_level
        self.state.current_level = LoopLevel.MESO
        logger.info("Escalating to meso loop for subsystem=%s", subsystem)

        try:
            record = await run_meso_loop(self, subsystem, self.state.total_micro_loops)
            self.state.total_meso_loops += 1
            self.state.add_meso_record(record)
        except Exception as exc:
            # Should not propagate — meso_loop handles internally
            logger.exception("Meso loop raised unexpectedly: %s", exc)
        finally:
            self.state.current_level = prev_level

    # ── Macro ─────────────────────────────────────────────────────────────────

    def _should_run_macro(self) -> bool:
        elapsed = time.monotonic() - self.state.last_macro_at
        return elapsed >= self.config.macro_interval_seconds

    async def _run_macro(self) -> None:
        prev_level = self.state.current_level
        self.state.current_level = LoopLevel.MACRO
        logger.info("Triggering macro loop (architectural snapshot)")

        # Reset the timer immediately so a slow macro doesn't cascade
        self.state.last_macro_at = time.monotonic()

        try:
            record = await run_macro_loop(self, self.state.total_micro_loops)
            self.state.total_macro_loops += 1
            self.state.add_macro_record(record)
        except Exception as exc:
            logger.exception("Macro loop raised unexpectedly: %s", exc)
        finally:
            self.state.current_level = prev_level

    # ── Shutdown ──────────────────────────────────────────────────────────────

    async def _on_shutdown(self) -> None:
        """
        Clean up: let the current micro loop finish (it already has by the time
        we get here since the main loop checks the event between iterations),
        write a final state snapshot, and log farewell.
        """
        logger.info(
            "Orchestrator shutting down — micro=%d meso=%d macro=%d",
            self.state.total_micro_loops,
            self.state.total_meso_loops,
            self.state.total_macro_loops,
        )
        self.state.status = LoopStatus.SHUTDOWN
        self._try_write_snapshot()
        logger.info("Orchestrator stopped cleanly.")

    def _install_signal_handlers(self) -> None:
        """
        Wire SIGINT and SIGTERM to request_shutdown().
        Uses asyncio-safe add_signal_handler (Unix only; on Windows the
        shutdown event must be set programmatically).
        """
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._handle_signal, sig)
            logger.debug("Signal handlers installed for SIGINT and SIGTERM")
        except (NotImplementedError, AttributeError):
            # Windows or environments without signal support
            logger.warning("Signal handlers not supported in this environment")

    def _handle_signal(self, sig: signal.Signals) -> None:
        logger.info("Received signal %s — requesting graceful shutdown", sig.name)
        self.request_shutdown()

    # ── Utilities ─────────────────────────────────────────────────────────────

    async def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep that wakes early if shutdown is requested."""
        try:
            await asyncio.wait_for(
                self._shutdown_event.wait(),
                timeout=seconds,
            )
        except asyncio.TimeoutError:
            pass  # Normal path — timeout means we slept the full duration

    def _try_write_snapshot(self) -> None:
        try:
            self.state.write_snapshot(self.config.state_snapshot_path)
        except Exception as exc:
            logger.warning("Could not write state snapshot: %s", exc)
