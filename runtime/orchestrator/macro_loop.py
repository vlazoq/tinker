"""
runtime/orchestrator/macro_loop.py
==========================

The macro loop — a full architectural snapshot committed to version control.

What is the macro loop?
------------------------
"Macro" means "large" or "big-picture".  The macro loop is the highest and
slowest of Tinker's three reasoning levels.  It fires on a timer — by default,
every four hours — and produces a *single, unified document* that describes
the entire software architecture as Tinker understands it right now.

Compared to the other loops:

  Micro loop  Runs constantly.  Works on one task in one subsystem.
              Produces one artifact per iteration.
              Example: "Here's a proposed design for the auth_service
                         login endpoint."

  Meso loop   Runs after several micro loops on the same subsystem.
              Synthesises all those artifacts into one subsystem document.
              Example: "Here is the full design of the auth_service:
                         its patterns, decisions, and open questions."

  Macro loop  Runs every few hours.
              Reads ALL subsystem documents and produces a system-wide view.
              Example: "Here is the architecture of the entire system:
                         how auth_service, api_gateway, data_pipeline, etc.
                         fit together; cross-cutting patterns; global concerns."

The result is committed to the architecture state manager (which in production
is backed by Git), creating a versioned history of how Tinker's understanding
of the architecture has evolved over time.

Why commit to version control?
--------------------------------
The Git history of these snapshots is a log of the AI's architectural reasoning.
Humans can:
  * See how the architecture evolved (diff between versions).
  * Audit specific decisions ("when did Tinker decide to use CQRS?").
  * Roll back to a known-good architectural snapshot.
  * Use the commit hash to correlate a snapshot with the exact micro-loop
    iteration that triggered it.

Error handling
--------------
Like the meso loop, errors inside the macro loop are caught, logged, and
recorded in the ``MacroLoopRecord``.  They do NOT propagate to the orchestrator.
If the macro loop fails, the timer is still reset (this happens in the
orchestrator, not here), so the next attempt won't happen for another full
interval.  This prevents "retry storms" where a persistently-failing macro loop
hammers the Synthesizer AI continuously.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from .compat import coroutine_if_needed
from .state import LoopStatus, MacroLoopRecord

# Only imported by type checkers — not at runtime — to avoid a circular import.
if TYPE_CHECKING:
    from .orchestrator import Orchestrator

# Sub-logger for the macro loop.  Messages appear as "tinker.orchestrator.macro".
logger = logging.getLogger("tinker.orchestrator.macro")


async def run_macro_loop(orch: Orchestrator, trigger_iteration: int) -> MacroLoopRecord:
    """
    Execute a full architectural snapshot and commit it to version control.

    This function is called by the orchestrator (see ``orchestrator.py:_run_macro``)
    when the macro timer fires.

    Steps:
      1. Fetch all subsystem design documents from memory (these were produced
         by previous meso loops).
      2. Call the Synthesizer AI with all documents and ask for a system-wide
         architectural narrative.
      3. Commit the snapshot to the architecture state manager (e.g. Git).

    All exceptions are caught and recorded in the ``MacroLoopRecord``.  They
    do NOT propagate to the orchestrator — the system continues regardless of
    whether the macro snapshot succeeds.

    Parameters
    ----------
    orch              : The Orchestrator instance (provides access to all
                        components, config, and state).
    trigger_iteration : The total micro-loop count at the time this macro run
                        was triggered.  Stored in the record for auditing.

    Returns
    -------
    MacroLoopRecord : A fully populated record describing what happened.
    """
    cfg = orch.config

    # The snapshot version is 1-based: "version 1" is the first ever snapshot,
    # "version 2" is the second, and so on.  We compute it as the current total
    # + 1 because the orchestrator increments total_macro_loops *after* this
    # function returns.
    snapshot_version = orch.state.total_macro_loops + 1

    # Create the record object.  It starts with status=RUNNING.
    record = MacroLoopRecord(
        snapshot_version=snapshot_version,
        trigger_iteration=trigger_iteration,
        started_at=time.monotonic(),
    )
    logger.info("macro START version=%d", snapshot_version)

    try:
        # ── 1. Gather all stored documents ────────────────────────────────────
        # The memory manager holds all the subsystem design documents produced
        # by meso loops.  We fetch ALL of them (no limit) because the macro
        # snapshot is supposed to be comprehensive.  In production, this might
        # be dozens of documents spanning all subsystems.
        documents = await asyncio.wait_for(
            coroutine_if_needed(orch.memory_manager.get_all_documents)(),
            timeout=30.0,
        )
        logger.info("macro: %d document(s) collected", len(documents))

        # ── 2. Full synthesizer pass ──────────────────────────────────────────
        # Ask the Synthesizer AI to read all the subsystem documents and write
        # a single, coherent architectural narrative.  We pass ``level="macro"``
        # so the Synthesizer knows to produce a system-wide view rather than a
        # subsystem-specific one.
        #
        # We also pass the current loop counts so the AI can frame its output
        # in context: "As of micro loop 847, the system looks like this..."
        snapshot = await asyncio.wait_for(
            coroutine_if_needed(orch.synthesizer_agent.call)(
                level="macro",
                documents=documents,
                snapshot_version=snapshot_version,
                total_micro_loops=orch.state.total_micro_loops,
            ),
            timeout=cfg.synthesizer_timeout,
        )

        # ── 3. Commit to Architecture State Manager ───────────────────────────
        # Package the snapshot into a commit payload and hand it to the
        # architecture state manager.  In production, this manager writes the
        # snapshot to a file and calls ``git commit`` (or equivalent).
        # It returns a short commit hash (e.g. "a3f9c21b") which we store in
        # the record for cross-referencing.
        commit_payload = {
            # Version number for this snapshot (1, 2, 3, ...).
            "version": snapshot_version,
            # The AI-generated architectural narrative.
            "content": snapshot.get("content", ""),
            # Counters included for context in the commit message / history.
            "total_micro_loops": orch.state.total_micro_loops,
            "total_meso_loops": orch.state.total_meso_loops,
            # A copy of the subsystem-level micro counts at this moment —
            # useful for understanding which subsystems have been most active.
            "subsystem_counts": dict(orch.state.subsystem_micro_counts),
        }
        commit_hash = await asyncio.wait_for(
            coroutine_if_needed(orch.arch_state_manager.commit)(commit_payload),
            timeout=30.0,
        )
        # Store the commit hash in the record so it appears in the Dashboard
        # and can be used to look up this snapshot in version control.
        record.commit_hash = commit_hash
        record.status = LoopStatus.SUCCESS
        logger.info("macro END version=%d commit=%s", snapshot_version, commit_hash)

        # ── 4. Self-improvement analysis ─────────────────────────────────
        # If the self-improvement engine is available, collect a performance
        # snapshot and let it analyze whether Tinker should adjust its own
        # prompts, config, or generate improvement tasks.
        if hasattr(orch, "_self_improvement") and orch._self_improvement is not None:
            try:
                from .self_improvement import PerformanceSnapshot

                # Collect recent metrics from the orchestrator state
                snap = PerformanceSnapshot(
                    subsystem_scores=dict(getattr(orch.state, "subsystem_critic_scores", {})),
                    stagnation_events=list(getattr(orch.state, "recent_stagnation_events", [])),
                    dlq_entries=[],  # populated below if DLQ available
                    error_counts=dict(getattr(orch.state, "error_counts", {})),
                    loop_durations=list(getattr(orch.state, "recent_loop_durations", [])),
                )

                actions = orch._self_improvement.analyze(snap)
                for action in actions:
                    if action.action_type == "prompt_adjustment":
                        logger.info(
                            "macro: self-improve prompt adjustment — %s",
                            action.description,
                        )
                        # Apply the prompt adjustment to the Architect's
                        # system prompt via the PromptBuilder's global
                        # project instructions (reversible, marker-based).
                        try:
                            from core.prompts.builder import PromptBuilder as _PB

                            current = _PB.get_global_project_instructions()
                            updated = orch._self_improvement.apply_prompt_adjustment(
                                current,
                                action,
                            )
                            _PB.set_global_project_instructions(updated)
                            logger.info(
                                "macro: applied prompt adjustment for '%s'",
                                action.parameters.get("subsystem", "?"),
                            )
                        except Exception as pa_exc:
                            logger.warning(
                                "macro: could not apply prompt adjustment: %s",
                                pa_exc,
                            )

                    elif action.action_type == "config_adjustment":
                        logger.info(
                            "macro: self-improve config adjustment — %s",
                            action.description,
                        )
                        # Apply config adjustment (temperature tuning).
                        try:
                            current_temp = getattr(orch.config, "temperature", 0.7)
                            new_temp = orch._self_improvement.apply_config_adjustment(
                                current_temp,
                                action,
                            )
                            orch.config.temperature = new_temp
                            logger.info(
                                "macro: temperature adjusted %.3f → %.3f",
                                current_temp,
                                new_temp,
                            )
                        except Exception as ca_exc:
                            logger.warning(
                                "macro: could not apply config adjustment: %s",
                                ca_exc,
                            )

                    elif action.action_type == "task_generation":
                        logger.info(
                            "macro: self-improve task generated — %s",
                            action.description,
                        )
                        # Enqueue the self-improvement task so the next
                        # micro loop picks it up.
                        try:
                            await coroutine_if_needed(orch.task_engine.add_task)(
                                {
                                    "type": action.parameters.get("task_type", "self_improvement"),
                                    "title": action.parameters.get(
                                        "task_title", action.description
                                    ),
                                    "description": action.parameters.get(
                                        "task_description", action.description
                                    ),
                                    "subsystem": "self_improvement",
                                    "priority": action.parameters.get("priority", "NORMAL"),
                                    "metadata": {
                                        "source": "self_improvement_engine",
                                        "confidence": action.confidence,
                                        "target": action.target,
                                    },
                                }
                            )
                            logger.info(
                                "macro: enqueued self-improvement task: %s",
                                action.parameters.get("task_title", "?"),
                            )
                        except Exception as tg_exc:
                            logger.warning(
                                "macro: could not enqueue self-improvement task: %s",
                                tg_exc,
                            )
            except Exception as si_exc:
                logger.warning(
                    "macro: self-improvement analysis failed (non-fatal): %s",
                    si_exc,
                )

    except TimeoutError as exc:
        # One of the timed operations (document fetch, Synthesizer call, or
        # commit) took too long.  Record the error and return — do NOT raise.
        msg = f"Timeout in macro loop version={snapshot_version}: {exc}"
        logger.warning(msg)
        record.status = LoopStatus.FAILED
        record.error = msg

    except Exception as exc:
        # Unexpected error — log the full stack trace and return.
        # ``logger.exception`` automatically includes the traceback.
        msg = f"Error in macro loop version={snapshot_version}: {exc}"
        logger.exception(msg)
        record.status = LoopStatus.FAILED
        record.error = msg

    finally:
        # Always record the finish time.  The ``finally`` block runs whether
        # the try block succeeded or raised an exception.
        if record.finished_at is None:
            record.finished_at = time.monotonic()

    return record
