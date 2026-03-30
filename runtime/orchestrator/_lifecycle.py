"""
runtime/orchestrator/_lifecycle.py
===================================
Lifecycle management methods extracted from the Orchestrator class.

Contains signal handler installation, signal handling, and graceful
shutdown logic.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from .state import LoopStatus

logger = logging.getLogger("tinker.orchestrator")


class LifecycleMixin:
    """
    Mixin providing signal handling and shutdown methods.

    Mixed into the Orchestrator class.  Methods access orchestrator state
    via ``self`` (state, _dlq_replayer, _shutdown_event).
    """

    async def _on_shutdown(self) -> None:
        """
        Perform clean-up when the orchestrator is stopping.

        Steps:
          1. Log a summary of what was accomplished.
          2. Save a checkpoint so the next run can resume.
          3. Flush auto-memory to disk.
          4. Stop the DLQ auto-replayer.
          5. Update the state status to SHUTDOWN.
          6. Write a final snapshot so the Dashboard shows the terminal state.
        """
        logger.info(
            "Orchestrator shutting down — micro=%d meso=%d macro=%d",
            self.state.total_micro_loops,
            self.state.total_meso_loops,
            self.state.total_macro_loops,
        )

        # Save checkpoint so the next run can resume where we left off
        if getattr(self, "checkpoint_manager", None) is not None:
            try:
                self.checkpoint_manager.save(self._build_checkpoint_data())
                logger.info("Checkpoint saved for resume")
            except Exception as exc:
                logger.warning("Checkpoint save failed (non-fatal): %s", exc)

        # Flush auto-memory state to disk
        if getattr(self, "event_bus", None) is not None:
            try:
                # Auto-memory listens on the bus; give it a final flush signal
                from core.events import Event, EventType
                await self.event_bus.publish(Event(
                    type=EventType.SYSTEM_STOPPING,
                    source="orchestrator",
                    payload={"flush": True},
                ))
            except Exception as exc:
                logger.debug("Event bus flush notification failed: %s", exc)

        if self._dlq_replayer is not None:
            try:
                await self._dlq_replayer.stop()
                logger.info("DLQ auto-replayer stopped")
            except Exception as exc:
                logger.warning(
                    "DLQ auto-replayer stop failed (non-fatal): %s", exc
                )

        self.state.status = LoopStatus.SHUTDOWN
        self._try_write_snapshot()
        logger.info("Orchestrator stopped cleanly.")

    def _install_signal_handlers(self) -> None:
        """
        Tell the OS to call ``_handle_signal`` when SIGINT or SIGTERM arrives.

        Uses ``loop.add_signal_handler()`` on Unix (safe with asyncio).
        Falls back to ``signal.signal()`` on Windows for SIGINT only.
        """
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._handle_signal, sig)
            logger.debug("Signal handlers installed for SIGINT and SIGTERM")
        except (NotImplementedError, AttributeError):
            import sys

            if sys.platform == "win32":
                try:
                    signal.signal(
                        signal.SIGINT,
                        lambda _sig, _frame: self.request_shutdown(),
                    )
                    logger.debug(
                        "Windows SIGINT handler installed via signal.signal() — "
                        "press Ctrl-C to shut down gracefully"
                    )
                except Exception as exc:
                    logger.warning(
                        "Could not install Windows SIGINT handler (%s) — "
                        "Ctrl-C will still stop the process via KeyboardInterrupt",
                        exc,
                    )
            else:
                logger.warning(
                    "Signal handlers not supported in this environment — "
                    "Ctrl-C will still shut down cleanly via KeyboardInterrupt"
                )

    def _handle_signal(self, sig: signal.Signals) -> None:
        """
        Called by the event loop when SIGINT or SIGTERM is received.

        Delegates to ``request_shutdown()`` which sets the shutdown event.
        """
        logger.info("Received signal %s — requesting graceful shutdown", sig.name)
        self.request_shutdown()
