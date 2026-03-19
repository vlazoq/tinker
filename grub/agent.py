"""
grub/agent.py
=============
GrubAgent — the main orchestrator for the implementation system.

This is the top-level class.  You create one GrubAgent, call run(), and
it handles everything:

  1. Loads config and initialises the registry
  2. Fetches implementation tasks from Tinker (or uses tasks you provide)
  3. Dispatches tasks through the pipeline in the configured execution mode
  4. Reports results back to Tinker
  5. Loops until there are no more tasks (or until you Ctrl-C)

Usage
-----
::

    # Programmatic use (from main.py or scripts)
    agent = GrubAgent.from_config()
    await agent.run()

    # Or inject specific tasks (bypassing Tinker)
    tasks = [GrubTask(title="Implement router", ...)]
    results = await agent.run_tasks(tasks)

STATUS: FULLY IMPLEMENTED
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .config import GrubConfig
from .registry import MinionRegistry
from .feedback import TinkerBridge
from .loop import PipelineRunner, GrubQueue
from .loop import run_sequential, run_parallel, run_queue_worker
from .contracts.task import GrubTask
from .contracts.result import MinionResult

logger = logging.getLogger(__name__)


class GrubAgent:
    """
    The Grub orchestrator.

    Responsibilities
    ----------------
    - Owns the config, registry, pipeline, and Tinker bridge
    - Decides which execution mode to use
    - Runs the main loop (poll Tinker → run tasks → report back)
    - Handles graceful shutdown on Ctrl-C

    Parameters
    ----------
    config   : GrubConfig.  Load with GrubConfig.load() or pass directly.
    registry : MinionRegistry.  Created automatically if not provided.
    """

    def __init__(
        self,
        config: Optional[GrubConfig] = None,
        registry: Optional[MinionRegistry] = None,
    ) -> None:
        self.config = config or GrubConfig()
        self.registry = registry or MinionRegistry(self.config)
        self.pipeline = PipelineRunner(self.registry, self.config)
        self.bridge = TinkerBridge(
            tinker_tasks_db=self.config.tinker_tasks_db,
            tinker_artifacts_dir=self.config.tinker_artifacts_dir,
            grub_artifacts_dir=self.config.grub_artifacts_dir,
        )
        self._shutdown = False

    @classmethod
    def from_config(cls, config_path: str = "grub_config.json") -> "GrubAgent":
        """
        Create a GrubAgent by loading config from a JSON file.

        If the file doesn't exist, a default config is created and saved.
        """
        config = GrubConfig.load(config_path)
        errors = config.validate()
        if errors:
            for e in errors:
                logger.error("Config error: %s", e)
            raise ValueError(f"Invalid GrubConfig: {errors}")

        registry = MinionRegistry(config)
        registry.load_defaults()

        logger.info(
            "GrubAgent created: mode=%s, minions=%s",
            config.execution_mode,
            ", ".join(registry.list_minions()),
        )
        return cls(config=config, registry=registry)

    # ── Main entry point ──────────────────────────────────────────────────────

    async def run(self, poll_interval: float = 30.0) -> None:
        """
        Main loop: poll Tinker for tasks, run them, report back.

        Runs indefinitely.  Stop with Ctrl-C or by calling request_shutdown().

        Parameters
        ----------
        poll_interval : Seconds to wait between polls when no tasks are found.
        """
        self._install_signal_handlers()
        logger.info("GrubAgent started (mode=%s)", self.config.execution_mode)

        while not self._shutdown:
            # Fetch pending implementation tasks from Tinker
            tasks = self.bridge.fetch_implementation_tasks(limit=10)

            if not tasks:
                logger.debug("GrubAgent: no tasks, sleeping %.0fs", poll_interval)
                await self._interruptible_sleep(poll_interval)
                continue

            logger.info("GrubAgent: processing %d tasks", len(tasks))
            results = await self.run_tasks(tasks)

            # Report each result back to Tinker
            for task, result in zip(tasks, results):
                self.bridge.report_result(result, task.tinker_task_id)
                self.bridge.write_implementation_note(result, task)

            logger.info(
                "GrubAgent: batch done — %d/%d succeeded",
                sum(1 for r in results if r.succeeded),
                len(results),
            )

        logger.info("GrubAgent: shutdown complete")

    async def run_tasks(self, tasks: list[GrubTask]) -> list[MinionResult]:
        """
        Run a list of tasks using the configured execution mode.

        This is the mode-dispatcher:
          - "sequential" → run_sequential()
          - "parallel"   → run_parallel()
          - "queue"      → GrubQueue + run_queue_worker()

        Parameters
        ----------
        tasks : List of GrubTasks to process.

        Returns
        -------
        List of MinionResults (one per task, same order as input).
        """
        mode = self.config.execution_mode

        if mode == "sequential":
            return await run_sequential(tasks, self.pipeline)

        elif mode == "parallel":
            return await run_parallel(
                tasks,
                self.pipeline,
                max_workers=3,
            )

        elif mode == "queue":
            # Enqueue all tasks into the SQLite queue
            queue = GrubQueue(self.config.queue_db_path)
            for task in tasks:
                queue.enqueue(task)

            # Start workers (one per configured worker slot)
            worker_coros = [
                run_queue_worker(
                    worker_id=f"worker-{i}",
                    queue=queue,
                    pipeline=self.pipeline,
                )
                for i in range(self.config.queue_workers)
            ]
            await asyncio.gather(*worker_coros)

            # Collect results from the queue DB
            raw_results = queue.get_results(limit=len(tasks))
            # Build MinionResult objects from stored dicts
            results = []
            from .contracts.result import ResultStatus

            for r in raw_results:
                results.append(
                    MinionResult(
                        task_id=r["task_id"],
                        minion_name=r.get("minion_name", "unknown"),
                        status=ResultStatus(r.get("status", "failed")),
                        score=float(r.get("score", 0.0)),
                        summary=r.get("summary", ""),
                        notes=r.get("notes", ""),
                        files_written=r.get("files_written", []),
                    )
                )
            return results

        else:
            raise ValueError(
                f"Unknown execution_mode '{mode}'. "
                "Valid values: 'sequential', 'parallel', 'queue'"
            )

    def request_shutdown(self) -> None:
        """Signal the main loop to stop after the current batch."""
        logger.info("GrubAgent: shutdown requested")
        self._shutdown = True

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep that can be interrupted by request_shutdown()."""
        step = 1.0
        elapsed = 0.0
        while elapsed < seconds and not self._shutdown:
            await asyncio.sleep(min(step, seconds - elapsed))
            elapsed += step

    def _install_signal_handlers(self) -> None:
        """Install Ctrl-C handler for graceful shutdown."""
        import signal
        import sys

        def _handler(_sig, _frame):
            logger.info("GrubAgent: received interrupt, shutting down...")
            self.request_shutdown()

        try:
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGINT, self.request_shutdown)
            loop.add_signal_handler(signal.SIGTERM, self.request_shutdown)
        except (NotImplementedError, RuntimeError):
            # Windows: ProactorEventLoop doesn't support add_signal_handler.
            # Fall back to signal.signal() for SIGINT only (SIGTERM is not a
            # real signal on Windows).
            if sys.platform == "win32":
                signal.signal(signal.SIGINT, _handler)
                # SIGTERM does not exist on Windows — skip it silently.
                sigterm = getattr(signal, "SIGTERM", None)
                if sigterm is not None:
                    try:
                        signal.signal(sigterm, _handler)
                    except (OSError, ValueError):
                        pass
            else:
                logger.warning("GrubAgent: could not install signal handlers")
