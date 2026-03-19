"""
orchestrator/orchestrator.py
============================

The Orchestrator — Tinker's heartbeat and central controller.

What does this file do?
------------------------
This file contains the ``Orchestrator`` class, which is the engine that drives
Tinker's three reasoning loops indefinitely.  Think of it as a very disciplined
project manager:

  1. It asks the task engine: "What should we work on next?"
  2. It hands the task to the Architect AI (via the micro loop).
  3. After enough micro loops on the same subsystem, it pauses and asks the
     Synthesizer AI to write a subsystem summary (the meso loop).
  4. Every few hours, it asks the Synthesizer for a full architectural snapshot
     (the macro loop) and commits it to version control.
  5. It monitors its own health, backs off when things go wrong, writes its
     state to disk so a Dashboard can display it, and shuts down cleanly when
     asked.

Critically: the Orchestrator contains *no AI reasoning itself*.  It is pure
deterministic Python — a traffic director.  All the intelligence lives in the
components that are injected into it.

The three-loop architecture
----------------------------
  Micro loop  (fastest, most frequent)
    Picks one task → gathers context → calls Architect AI → optionally fills
    knowledge gaps via Tool Layer → calls Critic AI → stores the artifact →
    marks task done → generates follow-up tasks.
    Runs as fast as the AI will respond.  Might complete hundreds of times
    in an hour.

  Meso loop   (medium frequency)
    Fires when a single subsystem has accumulated ``meso_trigger_count``
    successful micro loops.  The Synthesizer AI reads all recent artifacts
    for that subsystem and produces a coherent subsystem design document.
    Might fire a handful of times per hour.

  Macro loop  (slowest, on a timer)
    Fires every ``macro_interval_seconds`` (default: 4 hours).  The
    Synthesizer reads ALL subsystem documents and produces a system-wide
    architectural snapshot, which is then committed to version control.

What is dependency injection?
------------------------------
The ``Orchestrator.__init__`` method accepts all the AI components (architect,
critic, synthesizer, …) as arguments rather than importing or instantiating
them itself.  This is called *dependency injection*.

Benefits:
  * In production, you pass real AI-backed components.
  * In tests, you pass stubs (see ``stubs.py``) and the orchestrator behaves
    identically without making any real AI calls.
  * The orchestrator never needs to change when you swap one AI provider for
    another.

How to run it
--------------
    from orchestrator import Orchestrator, OrchestratorConfig
    from orchestrator.stubs import build_stub_components
    import asyncio

    components = build_stub_components()          # or your real components
    orch = Orchestrator(config=OrchestratorConfig(), **components)
    asyncio.run(orch.run())                        # blocks until Ctrl-C

What is asyncio?
-----------------
``asyncio`` is Python's built-in library for writing code that can do many
things "at the same time" without using multiple threads.  Instead of truly
running in parallel, it takes turns: while one coroutine (an ``async def``
function) is waiting for a network response, Python switches to another
coroutine and makes progress there.

In Tinker, every AI call is a network call that can take seconds.  Using
asyncio means the orchestrator can manage timeouts, check the shutdown flag,
and write state snapshots without getting stuck waiting.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from typing import Any, Optional

from .config import OrchestratorConfig
from .state import OrchestratorState, LoopLevel, LoopStatus

# run_micro_loop contains the detailed step-by-step logic for one micro iteration.
# MicroLoopError is raised when a micro loop fails unrecoverably.
from .micro_loop import run_micro_loop, MicroLoopError

# run_meso_loop synthesises artifacts for one subsystem.
from .meso_loop import run_meso_loop

# run_macro_loop produces and commits the full architectural snapshot.
from .macro_loop import run_macro_loop

# Backpressure is an optional enterprise dependency.  Import lazily so the
# orchestrator works in minimal deployments without the resilience package.
try:
    from resilience.backpressure import BackpressureController, BackpressureAction

    _BACKPRESSURE_AVAILABLE = True
except ImportError:
    BackpressureController = None  # type: ignore[assignment,misc]
    BackpressureAction = None  # type: ignore[assignment,misc]
    _BACKPRESSURE_AVAILABLE = False

try:
    from observability.audit_log import AuditEventType

    _AUDIT_AVAILABLE = True
except ImportError:
    AuditEventType = None  # type: ignore[assignment,misc]
    _AUDIT_AVAILABLE = False

try:
    from capacity.planner import CapacityPlanner

    _CAPACITY_AVAILABLE = True
except ImportError:
    CapacityPlanner = None  # type: ignore[assignment,misc]
    _CAPACITY_AVAILABLE = False

# Logger specific to the orchestrator — messages will appear as
# "tinker.orchestrator" in log output, making them easy to filter.
logger = logging.getLogger("tinker.orchestrator")


class Orchestrator:
    """
    Central controller that drives Tinker's three reasoning loops indefinitely.

    The Orchestrator is intentionally "dumb" in the sense that it contains no
    AI reasoning of its own.  Its only job is to decide *when* to run each
    loop and to route the results between components.  All the intelligence
    lives in the injected components.

    Attributes
    ----------
    config             : All tuneable parameters (timeouts, trigger counts, …).
    task_engine        : Selects tasks, marks them complete, generates new ones.
    context_assembler  : Fetches prior artifacts to give the Architect context.
    architect_agent    : The main reasoning AI — proposes architectural designs.
    critic_agent       : Reviews and scores the Architect's output.
    synthesizer_agent  : Produces higher-level summaries (meso and macro loops).
    memory_manager     : Stores and retrieves artifacts and documents.
    tool_layer         : Provides the Architect with research capabilities.
    arch_state_manager : Commits architectural snapshots (e.g. to Git).
    state              : Live state, updated after every loop iteration.
    """

    def __init__(
        self,
        *,  # everything after * must be a keyword argument
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
    ) -> None:
        """
        Initialise the orchestrator with all of its components.

        The ``*`` in the parameter list forces callers to use keyword arguments,
        which makes the call-site self-documenting:

            Orchestrator(
                config=my_config,
                task_engine=my_engine,
                # ...
            )

        rather than the confusing positional form ``Orchestrator(cfg, eng, ...)``.

        Parameters
        ----------
        config             : Configuration object.  If None, production defaults
                             are used.
        task_engine        : Must implement ``select_task()``, ``complete_task()``,
                             and ``generate_tasks()``.
        context_assembler  : Must implement ``build(task, max_artifacts)``.
        architect_agent    : Must implement ``call(task, context)``.
        critic_agent       : Must implement ``call(task, architect_result)``.
        synthesizer_agent  : Must implement ``call(level, **kwargs)``.
        memory_manager     : Must implement ``store_artifact()``, ``get_artifacts()``,
                             ``store_document()``, ``get_all_documents()``.
        tool_layer         : Must implement ``research(query)``.
        arch_state_manager : Must implement ``commit(payload)``.
        stagnation_monitor : Optional StagnationMonitor instance.  If provided,
                             it is called after every successful micro loop to
                             detect reasoning loops and trigger interventions.
                             Pass None to disable anti-stagnation monitoring.
        metrics            : Optional TinkerMetrics instance (from metrics.py).
                             If provided, counters and gauges are updated after
                             each loop.  Pass None to disable metrics.
        snapshot_callback  : Optional zero-argument callable invoked after every
                             successful state snapshot write.  Use this to push
                             live state to an in-process dashboard or test
                             harness without monkey-patching _try_write_snapshot.
        """
        # If the caller didn't provide a config, use the production defaults.
        self.config = config or OrchestratorConfig()

        # Store all injected components as instance attributes so the loop
        # functions (micro_loop.py, meso_loop.py, macro_loop.py) can access
        # them via ``orch.architect_agent``, ``orch.memory_manager``, etc.
        self.task_engine = task_engine
        self.context_assembler = context_assembler
        self.architect_agent = architect_agent
        self.critic_agent = critic_agent
        self.synthesizer_agent = synthesizer_agent
        self.memory_manager = memory_manager
        self.tool_layer = tool_layer
        self.arch_state_manager = arch_state_manager

        # Optional components — all default to None, meaning the feature is
        # disabled if not wired in.
        self.stagnation_monitor = stagnation_monitor
        self.metrics = metrics
        # Callback invoked after each snapshot write (e.g. to push state to
        # an in-process dashboard).  Replaces the previous pattern of
        # monkey-patching _try_write_snapshot at the call site in main.py.
        self._snapshot_callback = snapshot_callback

        # Enterprise components dictionary — populated by ``_build_enterprise_stack()``
        # in main.py after the Orchestrator is constructed.  Stores all optional
        # enterprise features (circuit breakers, rate limiters, idempotency cache,
        # backpressure controller, capacity planner, etc.) keyed by component name.
        # Defaults to an empty dict so enterprise references in micro_loop.py and
        # elsewhere are safe no-ops when running without the enterprise stack.
        self.enterprise: dict = {}

        if stagnation_monitor is not None:
            logger.info("StagnationMonitor wired — anti-stagnation detection active")
        if metrics is not None:
            logger.info("Metrics wired — Prometheus counters active")

        # Create a fresh state object.  This is the single source of truth
        # for everything the orchestrator knows about itself.
        self.state = OrchestratorState()

        # An asyncio Event acts like a flag: initially "not set" (False).
        # When we call _shutdown_event.set(), it becomes True and anything
        # waiting on it (``await _shutdown_event.wait()``) wakes up immediately.
        self._shutdown_event = asyncio.Event()

    # ── Public API ───────────────────────────────────────────────────────────

    async def run(self) -> None:
        """
        Entry point — start the orchestrator and run until told to stop.

        This is an ``async def`` function, meaning it must be run inside an
        asyncio event loop.  The typical call is::

            asyncio.run(orch.run())

        The function:
          1. Installs signal handlers so Ctrl-C or ``kill`` gracefully stops
             the orchestrator instead of crashing it mid-loop.
          2. Enters the main loop (which runs forever).
          3. On exit (any cause), runs the shutdown cleanup.

        The ``try/finally`` block guarantees that ``_on_shutdown()`` always
        runs, even if the loop is cancelled externally.
        """
        # Wire up OS signals (SIGINT = Ctrl-C, SIGTERM = ``kill`` command).
        self._install_signal_handlers()
        logger.info("Orchestrator starting — PID signals wired, entering main loop")

        try:
            # This call blocks (from the caller's perspective) until the
            # shutdown event is set or the task is cancelled.
            await self._main_loop()
        except asyncio.CancelledError:
            # CancelledError is raised when someone cancels the asyncio Task
            # from outside.  Treat it like a graceful shutdown.
            logger.info("Orchestrator task cancelled — treating as shutdown")
        finally:
            # Always clean up, regardless of how we exited the try block.
            await self._on_shutdown()

    def request_shutdown(self) -> None:
        """
        Ask the orchestrator to stop at the end of the current micro loop.

        This is a *graceful* shutdown request, not an immediate kill.  The
        orchestrator will finish whatever step of the micro loop it's currently
        on, then stop before starting the next iteration.

        Safe to call from:
          * Tests (to stop after N loops)
          * The Dashboard (via a "Stop" button)
          * Signal handlers (SIGINT, SIGTERM)
        """
        logger.info("Shutdown requested programmatically")
        # Set the flag in the state object so it appears in the Dashboard.
        self.state.shutdown_requested = True
        # Set the asyncio event so _interruptible_sleep and the main while-loop
        # condition both notice immediately.
        self._shutdown_event.set()

    @property
    def is_running(self) -> bool:
        """
        True if the orchestrator has not yet been asked to stop.

        A property (not a method) so callers can write ``orch.is_running``
        rather than ``orch.is_running()``.
        """
        # The shutdown event starts unset (is_set() == False), so
        # not is_set() == True means "still running".
        return not self._shutdown_event.is_set()

    def get_state_snapshot(self) -> dict:
        """
        Return a JSON-serialisable snapshot of the current state.

        Called by the Dashboard to get a point-in-time picture without
        needing to share memory or acquire a lock.  The orchestrator itself
        also calls ``write_snapshot()`` after every micro loop to keep the
        on-disk version up to date.
        """
        return self.state.to_dict()

    # ── Main loop ────────────────────────────────────────────────────────────

    async def _main_loop(self) -> None:
        """
        The central "forever" loop that drives all three reasoning levels.

        Structure of each iteration:
          1. Check if the macro timer has fired → if so, run the macro loop.
          2. Run one micro loop.
          3. If the micro loop succeeded, check if the current subsystem has
             hit its meso trigger threshold → if so, run the meso loop.
          4. If the micro loop failed too many times in a row, sleep briefly.
          5. If configured, sleep a short idle period (0 by default).
          6. Write the state snapshot to disk for the Dashboard.
          7. Check the shutdown event → if set, exit.

        Design note: why check macro *before* micro?
        Running the macro check first means a freshly-started orchestrator
        will not immediately run a macro loop (the timer starts at "now"),
        but a long-running orchestrator will handle the macro at the top of
        the next iteration, before doing any new micro work.  This keeps
        the main loop simple and predictable.

        This function never raises — all errors are caught inside the helper
        methods it calls.
        """
        while not self._shutdown_event.is_set():
            # ── Macro loop timer check ────────────────────────────────────────
            # The macro loop fires on a wall-clock timer, not after a fixed
            # number of micro loops.  Check elapsed time here.
            if self._should_run_macro():
                await self._run_macro()

            # ── Backpressure check ────────────────────────────────────────────
            # Before starting a micro loop, ask the backpressure controller
            # whether the system is healthy enough to proceed.  If the queue
            # is too deep or the failure streak is high, it may recommend
            # slowing down or pausing task generation to let the system recover.
            #
            # This prevents runaway task accumulation and protects downstream
            # services from being overwhelmed when Tinker is falling behind.
            await self._apply_backpressure()

            # ── Micro loop ────────────────────────────────────────────────────
            # Tell the state object what level we're at so the Dashboard shows
            # "micro" as the current activity.
            self.state.current_level = LoopLevel.MICRO
            # Run one full micro loop (task → architect → critic → store → next tasks).
            micro_succeeded = await self._run_micro()

            if micro_succeeded:
                # A success resets the consecutive-failure counter.
                # (It was incremented in previous failed iterations.)
                self.state.consecutive_failures = 0

                # ── Meso check ────────────────────────────────────────────────
                # After a successful micro loop, check whether the subsystem
                # that was just worked on has now accumulated enough micro-loop
                # artifacts to justify a meso synthesis.
                subsystem = self.state.current_subsystem
                if subsystem and self._should_run_meso(subsystem):
                    await self._run_meso(subsystem)

            else:
                # The micro loop failed.  Increment the failure streak counter.
                self.state.consecutive_failures += 1

                # If we've hit the failure threshold, something is seriously
                # wrong (network issue, quota exceeded, etc.).  Sleep before
                # hammering the API again.
                if (
                    self.state.consecutive_failures
                    >= self.config.max_consecutive_failures
                ):
                    logger.warning(
                        "Backing off for %.1fs after %d consecutive failures",
                        self.config.failure_backoff_seconds,
                        self.state.consecutive_failures,
                    )
                    # Use _interruptible_sleep so a shutdown request during the
                    # sleep still wakes us up promptly.
                    await self._interruptible_sleep(self.config.failure_backoff_seconds)
                    # Reset so we get another full budget of retries.
                    self.state.consecutive_failures = 0

            # ── Idle sleep ────────────────────────────────────────────────────
            # By default, micro_loop_idle_seconds is 0 — run flat-out.
            # Set it to a non-zero value to throttle the loop (useful in
            # development or to reduce API costs).
            if self.config.micro_loop_idle_seconds > 0:
                await self._interruptible_sleep(self.config.micro_loop_idle_seconds)

            # ── Write state snapshot ──────────────────────────────────────────
            # Persist the current state to disk so the Dashboard can read it.
            # This is non-blocking and swallows errors (a failed snapshot write
            # should never stop the orchestrator from doing its main job).
            self._try_write_snapshot()

        # We've exited the while loop — the shutdown event was set.
        # Mark ourselves as idle before the finally block in run() calls
        # _on_shutdown().
        self.state.current_level = LoopLevel.IDLE

    # ── Backpressure ──────────────────────────────────────────────────────────

    async def _apply_backpressure(self) -> None:
        """
        Evaluate system load and apply any recommended backpressure actions.

        The BackpressureController examines three signals:
          - ``queue_depth``     : How many tasks are waiting to be processed.
          - ``failure_streak``  : How many consecutive micro loop failures.
          - ``artifact_count``  : Total artifacts in memory (memory pressure).

        Based on these, it may recommend:
          - NONE               : All clear — proceed normally.
          - WARN               : Log a warning but continue at full speed.
          - SLOW_DOWN          : Insert a short sleep before the next loop.
          - PAUSE_GENERATION   : Stop the task engine from generating new tasks
                                 until the queue drains to a healthy level.
          - COMPRESS_MEMORY    : Signal the memory manager to archive or evict
                                 old artifacts to free up space.

        This is a no-op when:
          - No backpressure controller is wired (enterprise dict is empty).
          - The backpressure package is not installed.
          - Any exception occurs inside the controller (non-fatal guard).

        All sleeps use ``_interruptible_sleep`` so a shutdown request wakes
        the orchestrator immediately rather than blocking for the full duration.
        """
        if not _BACKPRESSURE_AVAILABLE:
            return

        bp_controller = self.enterprise.get("backpressure")
        if bp_controller is None:
            return

        try:
            # Gather current system signals
            queue_depth = getattr(self.task_engine, "queue_depth", 0) or 0
            failure_streak = self.state.consecutive_failures
            artifact_count = sum(
                1 for r in self.state.micro_history if r.artifact_id is not None
            )

            recommendation = bp_controller.evaluate(
                queue_depth=queue_depth,
                failure_streak=failure_streak,
                artifact_count=artifact_count,
            )

            action = recommendation.action

            if action == BackpressureAction.NONE:
                return  # All clear

            if action == BackpressureAction.WARN:
                logger.warning(
                    "Backpressure WARN: %s (queue=%d, failures=%d)",
                    recommendation.reason,
                    queue_depth,
                    failure_streak,
                )
                return

            if action == BackpressureAction.SLOW_DOWN:
                logger.warning(
                    "Backpressure SLOW_DOWN: sleeping %.1fs — %s",
                    recommendation.wait_seconds,
                    recommendation.reason,
                )
                await self._interruptible_sleep(recommendation.wait_seconds)

            elif action == BackpressureAction.PAUSE_GENERATION:
                logger.warning(
                    "Backpressure PAUSE_GENERATION: pausing task generation "
                    "for %.1fs — %s",
                    recommendation.wait_seconds,
                    recommendation.reason,
                )
                # Tell the task engine to pause if it supports the flag.
                # The finally block guarantees the flag is cleared even if
                # _interruptible_sleep is cancelled or raises.
                _has_pause_flag = hasattr(self.task_engine, "pause_generation")
                if _has_pause_flag:
                    self.task_engine.pause_generation = True
                try:
                    await self._interruptible_sleep(recommendation.wait_seconds)
                finally:
                    if _has_pause_flag:
                        self.task_engine.pause_generation = False

            elif action == BackpressureAction.COMPRESS_MEMORY:
                logger.warning(
                    "Backpressure COMPRESS_MEMORY: requesting memory compression — %s",
                    recommendation.reason,
                )
                if hasattr(self.memory_manager, "compress"):
                    try:
                        await self.memory_manager.compress()
                    except Exception as exc:
                        logger.warning("Memory compression failed: %s", exc)

        except Exception as exc:
            # Backpressure errors are never fatal — the orchestrator must
            # keep running even if the backpressure controller misbehaves.
            logger.warning("Backpressure evaluation failed (non-fatal): %s", exc)

    # ── Capacity planner update ───────────────────────────────────────────────

    async def _update_capacity_planner(self, record: Any) -> None:
        """
        Record resource usage for the just-completed micro loop.

        Feeds token counts, artifact count, and disk usage into the
        CapacityPlanner so it can track growth rates and project forward.
        Called after every successful micro loop.

        This is a no-op when no capacity planner is wired in.
        """
        if not _CAPACITY_AVAILABLE:
            return

        planner = self.enterprise.get("capacity_planner")
        if planner is None:
            return

        try:
            # Record LLM token consumption from this micro loop
            planner.record_tokens(
                micro_tokens=(
                    (record.architect_tokens or 0) + (record.critic_tokens or 0)
                )
            )
            # Record current artifact count from state
            total_artifacts = self.state.total_micro_loops  # rough proxy
            planner.record_artifact_count(total=total_artifacts)

            # Check thresholds and log any alerts
            alerts = planner.check_thresholds()
            for alert in alerts:
                logger.warning("CapacityPlanner: %s", alert)
        except Exception as exc:
            logger.debug("Capacity planner update failed (non-fatal): %s", exc)

    # ── Micro ────────────────────────────────────────────────────────────────

    async def _run_micro(self) -> bool:
        """
        Execute one micro loop iteration and return True on success.

        Delegates the actual work to ``run_micro_loop()`` in micro_loop.py.
        This method's job is only to:
          * Handle any exceptions that escape micro_loop.py
          * Update the orchestrator state with the result
          * Return a simple True/False for the main loop to act on

        Returns
        -------
        True  if the micro loop completed successfully.
        False if it failed for any reason.
        """
        try:
            # run_micro_loop() does the heavy lifting: task selection, AI calls,
            # artifact storage, and task generation.  It returns a MicroLoopRecord.
            record = await run_micro_loop(self)

            # Update the orchestrator's counters and current-task tracking.
            self.state.total_micro_loops += 1
            self.state.current_task_id = record.task_id
            self.state.current_subsystem = record.subsystem

            # ── Per-loop cost attribution ───────────────────────────────────
            # Log token consumption for this micro loop so operators can
            # track per-iteration cost without querying the metrics system.
            arch_tokens = record.architect_tokens or 0
            critic_tokens = record.critic_tokens or 0
            total_tokens = arch_tokens + critic_tokens
            logger.info(
                "micro[%d] cost — architect_tokens=%d critic_tokens=%d "
                "total_tokens=%d task=%s subsystem=%s",
                self.state.total_micro_loops,
                arch_tokens,
                critic_tokens,
                total_tokens,
                record.task_id,
                record.subsystem,
            )

            # Add the record to the rolling history (capped at 100 entries).
            self.state.add_micro_record(record)

            if record.status == LoopStatus.SUCCESS:
                # Only count the subsystem for meso-trigger purposes if the
                # loop truly succeeded end-to-end.
                self.state.increment_subsystem(record.subsystem)

                # ── Metrics ────────────────────────────────────────────────
                # Update Prometheus counters / gauges if a metrics object was
                # injected.  This is a no-op when metrics=None.
                if self.metrics is not None:
                    self.metrics.on_micro_loop(record)

                # ── Anti-stagnation check ──────────────────────────────────
                # Ask the StagnationMonitor whether the system is looping.
                # If directives come back, apply the most severe one.
                # This is opt-in: if no stagnation_monitor was injected, skip.
                if self.stagnation_monitor is not None:
                    directives = await self._check_stagnation(record)
                    if directives:
                        await self._apply_stagnation_directive(directives[0])

                # ── Capacity planner update ─────────────────────────────────
                # Record token/artifact usage for growth-rate projections.
                await self._update_capacity_planner(record)

                return True

            # The record exists but shows FAILED — still return False.
            return False

        except MicroLoopError as exc:
            # MicroLoopError is the "expected" failure type — it means the
            # micro loop hit a problem but handled it cleanly.
            logger.error("Micro loop failed: %s", exc)
            return False

        except Exception as exc:
            # Any other exception is unexpected — log the full traceback.
            logger.exception("Unexpected error in micro loop: %s", exc)
            return False

    # ── Meso ─────────────────────────────────────────────────────────────────

    def _should_run_meso(self, subsystem: str) -> bool:
        """
        Return True if ``subsystem`` has accumulated enough micro loops to
        justify a meso synthesis.

        The threshold is ``config.meso_trigger_count`` (default 5).  So after
        5 successful micro loops on "api_gateway", this returns True and the
        orchestrator will pause micro work and run a meso synthesis.

        After the meso loop runs, it resets the counter to 0 (via
        ``state.reset_subsystem_count``), so the next batch of micro loops
        can accumulate before the next synthesis.
        """
        count = self.state.subsystem_micro_counts.get(subsystem, 0)
        return count >= self.config.meso_trigger_count

    async def _run_meso(self, subsystem: str) -> None:
        """
        Execute a meso-level synthesis for ``subsystem``.

        Temporarily switches the orchestrator's reported level to MESO (so the
        Dashboard shows "meso" as the current activity), then delegates to
        ``run_meso_loop()`` in meso_loop.py.

        Errors are logged but NOT re-raised.  A failed meso synthesis is
        unfortunate but not fatal — the orchestrator resumes micro loops.

        Parameters
        ----------
        subsystem : The name of the subsystem to synthesise (e.g. "api_gateway").
        """
        # Remember what we were doing before (almost certainly MICRO) so we
        # can restore it after the meso loop finishes.
        prev_level = self.state.current_level
        self.state.current_level = LoopLevel.MESO
        logger.info("Escalating to meso loop for subsystem=%s", subsystem)

        try:
            # run_meso_loop() fetches artifacts, calls the Synthesizer AI,
            # stores the resulting document, and resets the subsystem counter.
            record = await run_meso_loop(self, subsystem, self.state.total_micro_loops)
            self.state.total_meso_loops += 1
            self.state.add_meso_record(record)
            if self.metrics is not None:
                self.metrics.on_meso_loop(record)
        except Exception as exc:
            # run_meso_loop is supposed to handle its own exceptions and never
            # raise.  If something *does* escape, log it and carry on.
            logger.exception("Meso loop raised unexpectedly: %s", exc)
        finally:
            # Always restore the loop level, even if meso_loop raised.
            self.state.current_level = prev_level

    # ── Macro ─────────────────────────────────────────────────────────────────

    def _should_run_macro(self) -> bool:
        """
        Return True if enough time has passed since the last macro snapshot.

        Uses monotonic time (never goes backwards) to measure elapsed seconds
        since ``state.last_macro_at``.  Compares to ``config.macro_interval_seconds``
        (default: 4 hours = 14400 seconds).
        """
        elapsed = time.monotonic() - self.state.last_macro_at
        return elapsed >= self.config.macro_interval_seconds

    async def _run_macro(self) -> None:
        """
        Execute a full macro architectural snapshot.

        Temporarily switches the reported level to MACRO, resets the macro
        timer *immediately* (so a slow macro run doesn't cascade into a second
        immediate macro), and delegates to ``run_macro_loop()`` in macro_loop.py.

        Errors are logged but NOT re-raised — the orchestrator resumes.

        Why reset the timer before the loop runs?
        ------------------------------------------
        If the macro loop takes 3 minutes and we reset the timer *after*, the
        next check would see "4 hours elapsed" immediately if it ran right at
        the boundary.  Resetting first means the next macro won't fire for
        another 4 hours regardless of how long this one takes.
        """
        prev_level = self.state.current_level
        self.state.current_level = LoopLevel.MACRO
        logger.info("Triggering macro loop (architectural snapshot)")

        # Reset the timer immediately so a slow macro doesn't cascade into
        # triggering another macro on the very next iteration.
        self.state.last_macro_at = time.monotonic()

        try:
            # run_macro_loop() collects all subsystem documents, calls the
            # Synthesizer AI for a full architectural narrative, and commits
            # the result to version control via arch_state_manager.
            record = await run_macro_loop(self, self.state.total_micro_loops)
            self.state.total_macro_loops += 1
            self.state.add_macro_record(record)
            if self.metrics is not None:
                self.metrics.on_macro_loop(record)
        except Exception as exc:
            # run_macro_loop is supposed to handle its own exceptions.
            # Log any that escape and continue.
            logger.exception("Macro loop raised unexpectedly: %s", exc)
        finally:
            self.state.current_level = prev_level

    # ── Anti-stagnation ───────────────────────────────────────────────────────

    async def _check_stagnation(self, record: Any) -> list:
        """
        Run the StagnationMonitor against the just-completed micro loop record.

        Builds a ``MicroLoopContext`` from the record and runs all five
        detectors (SemanticLoop, SubsystemFixation, CritiqueCollapse,
        ResearchSaturation, TaskStarvation).  Returns a list of
        ``InterventionDirective`` objects sorted by severity (highest first).
        Returns an empty list if nothing is detected or if the monitor raises.

        The monitor is synchronous (CPU-only computation, no I/O) but we run
        it inside ``run_in_executor`` so any unexpectedly heavy computation
        doesn't block the event loop.

        Parameters
        ----------
        record : MicroLoopRecord from the just-completed micro loop.
        """
        from stagnation.models import MicroLoopContext

        ctx = MicroLoopContext(
            loop_index=record.iteration,
            # Architect output is not stored on the record to keep it small.
            # A future improvement would pass it through for semantic checks.
            output_text=None,
            subsystem_tag=record.subsystem,
            # The critic score is now stored on the record (see state.py).
            critic_score=record.critic_score,
            # Queue depth from the task engine if it exposes it.
            queue_depth=getattr(self.task_engine, "queue_depth", None),
            tasks_generated=record.new_tasks_generated,
            # Each micro loop consumes exactly one task.
            tasks_consumed=1,
        )
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self.stagnation_monitor.check, ctx)
        except Exception as exc:
            logger.warning("StagnationMonitor.check() raised unexpectedly: %s", exc)
            return []

    async def _apply_stagnation_directive(self, directive: Any) -> None:
        """
        Act on the highest-severity stagnation directive.

        Each ``InterventionType`` maps to a concrete action:

        FORCE_BRANCH
            Bump the stagnation-flagged subsystem's meso counter to the
            trigger threshold.  This causes the orchestrator to run a meso
            synthesis on that subsystem *before* the next micro loop, which
            naturally transitions work to a different area of the design.

        ALTERNATIVE_FORCING / INJECT_CONTRADICTION
            Logged and recorded in state for now.  A future improvement
            would inject a prompt hint into the next Architect or Critic call.

        SPAWN_EXPLORATION
            Enqueue a fresh exploration task with high confidence_gap so it
            surfaces quickly in priority scoring.

        ESCALATE_LOOP
            Enqueue an exploration task (same as SPAWN_EXPLORATION) — the
            intent is to break a starvation spiral by injecting new work.

        NO_ACTION
            Nothing to do; the directive is informational only.

        Parameters
        ----------
        directive : An ``InterventionDirective`` from the StagnationMonitor.
        """
        from stagnation.models import InterventionType

        if not directive.is_actionable():
            return

        # Increment the global stagnation event counter for the Dashboard and
        # for Prometheus metrics.
        self.state.stagnation_events_total += 1
        if self.metrics is not None:
            self.metrics.on_stagnation(directive)

        logger.warning(
            "[Stagnation] %s directive triggered (type=%s, severity=%.2f)",
            directive.intervention_type.value,
            directive.stagnation_type.value,
            directive.severity,
        )

        itype = directive.intervention_type

        if itype == InterventionType.FORCE_BRANCH:
            # Identify which subsystem to pivot away from.
            avoid = (
                directive.metadata.get("avoid_subsystem")
                or self.state.current_subsystem
            )
            if avoid:
                # Bump the counter to the trigger so a meso synthesis fires
                # before the next micro loop.  After synthesis, the counter
                # resets to 0 and the system naturally gravitates toward
                # other tasks that haven't been synthesised yet.
                target = self.config.meso_trigger_count
                self.state.subsystem_micro_counts[avoid] = target
                logger.info(
                    "[Stagnation] Forced early meso on subsystem=%s to pivot away",
                    avoid,
                )

        elif itype in (
            InterventionType.ALTERNATIVE_FORCING,
            InterventionType.INJECT_CONTRADICTION,
        ):
            # Build a one-shot prompt hint that micro_loop.py will inject into
            # the Architect's context on the next call, then clear automatically.
            if itype == InterventionType.ALTERNATIVE_FORCING:
                hint = (
                    "[STAGNATION INTERVENTION — ALTERNATIVE FORCING] "
                    "You have been cycling through similar solutions. "
                    "Deliberately propose an approach you have NOT tried before. "
                    "Reject any solution that resembles previous proposals and "
                    "instead explore a fundamentally different design direction."
                )
            else:  # INJECT_CONTRADICTION
                hint = (
                    "[STAGNATION INTERVENTION — INJECT CONTRADICTION] "
                    "Actively challenge your current assumptions. "
                    "Identify the core hypothesis behind your recent proposals "
                    "and argue the opposite: what if that hypothesis is wrong? "
                    "Build your next proposal around the counter-hypothesis."
                )
            self.state.pending_stagnation_hint = hint
            logger.info(
                "[Stagnation] %s — prompt hint queued for next micro loop",
                itype.value,
            )

        elif itype in (
            InterventionType.SPAWN_EXPLORATION,
            InterventionType.ESCALATE_LOOP,
        ):
            # Inject a fresh exploration task to break the saturation / starvation.
            if hasattr(self.task_engine, "enqueue_exploration_task"):
                await self.task_engine.enqueue_exploration_task(
                    title="Stagnation-break: explore a new design direction",
                    description=(
                        f"The system detected {directive.stagnation_type.value} "
                        f"(severity={directive.severity:.2f}).  Investigate a part of "
                        "the architecture that has not been explored recently and "
                        "propose a new line of inquiry."
                    ),
                )
                logger.info(
                    "[Stagnation] Exploration task enqueued to break %s",
                    directive.stagnation_type.value,
                )

    # ── Shutdown ──────────────────────────────────────────────────────────────

    async def _on_shutdown(self) -> None:
        """
        Perform clean-up when the orchestrator is stopping.

        By the time this is called, the main loop has already exited (the
        ``while not self._shutdown_event.is_set()`` condition was False).
        The current micro loop iteration, if any, has already completed
        because the shutdown event is only checked *between* iterations.

        Steps:
          1. Log a summary of what was accomplished.
          2. Update the state status to SHUTDOWN.
          3. Write a final snapshot so the Dashboard shows the terminal state.
        """
        logger.info(
            "Orchestrator shutting down — micro=%d meso=%d macro=%d",
            self.state.total_micro_loops,
            self.state.total_meso_loops,
            self.state.total_macro_loops,
        )
        # Mark the overall orchestrator as shut down.
        self.state.status = LoopStatus.SHUTDOWN
        # Write one last snapshot so the Dashboard shows "shutdown" and the
        # final loop counts.
        self._try_write_snapshot()
        logger.info("Orchestrator stopped cleanly.")

    def _install_signal_handlers(self) -> None:
        """
        Tell the OS to call ``_handle_signal`` when SIGINT or SIGTERM arrives.

        What are signals?
        -----------------
        Signals are messages the operating system can send to a running process.
        SIGINT is sent when you press Ctrl-C in the terminal.  SIGTERM is sent
        by ``kill <pid>`` or orchestration tools like Docker or Kubernetes when
        they want the process to stop.

        Why use ``loop.add_signal_handler`` instead of ``signal.signal``?
        -----------------------------------------------------------------
        The standard ``signal.signal()`` is not safe to use with asyncio because
        signal handlers can interrupt the event loop at any point, potentially
        corrupting its internal state.  ``loop.add_signal_handler()`` schedules
        the callback to run *between* event loop iterations, which is safe.

        Compatibility note:
        ``add_signal_handler`` is only available on Unix (Linux, macOS).
        On Windows the ProactorEventLoop raises NotImplementedError, so we fall
        back to ``signal.signal()`` for SIGINT (Ctrl-C).  SIGTERM doesn't exist
        as a real signal on Windows, so it is skipped in the fallback.
        """
        try:
            loop = asyncio.get_running_loop()
            # Wire both Ctrl-C and the standard "please stop" signal.
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._handle_signal, sig)
            logger.debug("Signal handlers installed for SIGINT and SIGTERM")
        except (NotImplementedError, AttributeError):
            # Windows: ProactorEventLoop does not support add_signal_handler.
            # Use signal.signal() as a fallback for SIGINT only.
            # signal.signal() callbacks run in the main OS thread between
            # Python bytecode instructions, so calling request_shutdown()
            # (which sets a threading.Event) is safe — it won't corrupt the
            # event loop the way a raw signal.signal() callback might on Unix.
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
                # Non-Windows environment with no signal support (e.g. a
                # restricted container).  Ctrl-C raises KeyboardInterrupt which
                # asyncio.run() converts to CancelledError; the run() try/finally
                # block calls _on_shutdown() so the final snapshot is written.
                logger.warning(
                    "Signal handlers not supported in this environment — "
                    "Ctrl-C will still shut down cleanly via KeyboardInterrupt"
                )

    def _handle_signal(self, sig: signal.Signals) -> None:
        """
        Called by the event loop when SIGINT or SIGTERM is received.

        This is a *synchronous* function (no ``async``) because signal handlers
        must not be coroutines.  It simply delegates to ``request_shutdown()``,
        which sets the shutdown event the main loop checks.

        Parameters
        ----------
        sig : The signal that was received (SIGINT or SIGTERM).
        """
        logger.info("Received signal %s — requesting graceful shutdown", sig.name)
        self.request_shutdown()

    # ── Utilities ─────────────────────────────────────────────────────────────

    async def _interruptible_sleep(self, seconds: float) -> None:
        """
        Sleep for up to ``seconds`` seconds, but wake early if shutdown is
        requested.

        The problem with ``asyncio.sleep(seconds)``
        -------------------------------------------
        A plain ``await asyncio.sleep(10)`` will sleep for the full 10 seconds
        even if ``request_shutdown()`` is called at second 1.  That means the
        orchestrator would be unresponsive for up to 10 seconds after a shutdown
        request, which feels broken.

        The solution
        ------------
        ``asyncio.wait_for(event.wait(), timeout=seconds)`` waits until EITHER
        the shutdown event is set OR the timeout expires — whichever comes first.
        If the event is set (shutdown requested), we return immediately.
        If the timeout fires first (normal case), a ``TimeoutError`` is raised,
        which we catch and ignore (it just means we slept the full duration).

        Parameters
        ----------
        seconds : Maximum time to sleep in seconds.
        """
        try:
            await asyncio.wait_for(
                self._shutdown_event.wait(),
                timeout=seconds,
            )
            # If we reach here without a TimeoutError, the shutdown event was
            # set — we woke up early.  That's fine; the main loop will see
            # the shutdown event on its next iteration and exit cleanly.
        except asyncio.TimeoutError:
            # Normal path — we slept the full requested duration.
            pass  # Nothing to do; just return.

    def _try_write_snapshot(self) -> None:
        """
        Attempt to write the current state to disk, ignoring any errors.

        Writing the snapshot is a best-effort operation.  If the disk is full,
        the path is read-only, or any other I/O error occurs, we log a warning
        and keep running.  The Dashboard losing its state feed is unfortunate
        but should not crash the orchestrator.

        After a successful write, ``_snapshot_callback`` (if set) is called so
        in-process observers (e.g. a TUI dashboard) can react to the new state
        without requiring any monkey-patching of this method.
        """
        try:
            self.state.write_snapshot(self.config.state_snapshot_path)
        except Exception as exc:
            # Log at WARNING level (not ERROR) because this is non-fatal.
            logger.warning("Could not write state snapshot: %s", exc)
            return

        if self._snapshot_callback is not None:
            try:
                self._snapshot_callback()
            except Exception as exc:
                logger.debug("snapshot_callback failed (non-fatal): %s", exc)
