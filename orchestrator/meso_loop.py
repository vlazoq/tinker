"""
orchestrator/meso_loop.py
=========================

The meso loop — subsystem-level synthesis and summarisation.

What is the meso loop?
-----------------------
"Meso" means "middle" (from the Greek μέσος).  The meso loop sits between the
fast, fine-grained micro loop and the slow, system-wide macro loop.  Its job
is to periodically *step back* and synthesise everything Tinker has learned
about a particular subsystem into one coherent document.

Think of it like this: imagine the micro loop is an engineer jotting down
individual notes while exploring a codebase — one note per observation.
After a while, those notes pile up.  The meso loop is the moment when the
engineer stops, reads all the notes, and writes a proper summary document:
"Here's what we know about the auth_service, the patterns we've found, and
the open questions we still need to address."

When does it fire?
-------------------
The orchestrator tracks how many successful micro loops have run for each
subsystem.  When a subsystem's count reaches ``config.meso_trigger_count``
(default: 5), the orchestrator calls this module.  After a successful meso
run, the counter resets to 0, and the subsystem starts accumulating micro
loops again toward the next synthesis.

What does it produce?
---------------------
A "subsystem design document" stored in memory.  This document:
  * Combines the content of multiple micro-loop artifacts.
  * Identifies patterns and consensus decisions.
  * Notes remaining open questions.
  * Becomes raw material for the macro loop, which synthesises all subsystem
    documents into a full architectural snapshot.

How this file is structured
----------------------------
There is one public function: ``run_meso_loop()``.  All the logic is contained
in that single function (unlike the micro loop, which splits into many helpers)
because the meso loop has fewer, more sequential steps:

  1. Fetch artifacts for the subsystem from memory.
  2. Check there are enough to justify synthesis.
  3. Call the Synthesizer AI.
  4. Store the resulting document.
  5. Reset the subsystem counter.

Error handling
--------------
Errors inside the meso loop are caught, logged, and recorded in the
``MesoLoopRecord``.  They do NOT propagate to the orchestrator — a failed
meso synthesis is unfortunate but should not crash the whole system.  The
orchestrator will continue running micro loops and try again when the subsystem
next hits the trigger count.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from .compat import coroutine_if_needed
from .state import MesoLoopRecord, LoopStatus

# Only imported by type checkers (e.g. mypy, pyright) — not at runtime.
# Avoids a circular import: orchestrator.py imports meso_loop.py, so
# meso_loop.py cannot import orchestrator.py at runtime.
if TYPE_CHECKING:
    from .orchestrator import Orchestrator

# Sub-logger for the meso loop.  Log messages will appear as
# "tinker.orchestrator.meso" in the log output.
logger = logging.getLogger("tinker.orchestrator.meso")


async def run_meso_loop(orch: "Orchestrator", subsystem: str, trigger_iteration: int) -> MesoLoopRecord:
    """
    Execute a meso-level synthesis for ``subsystem``.

    This function is called by the orchestrator (see ``orchestrator.py:_run_meso``)
    after a subsystem's micro-loop counter reaches the configured threshold.

    The function:
      1. Fetches recent artifacts for this subsystem from memory.
      2. Checks whether there are enough artifacts to justify a synthesis.
         (If not, it returns early without calling the Synthesizer AI —
         a synthesis from 0 or 1 artifacts wouldn't be meaningful.)
      3. Calls the Synthesizer AI with all fetched artifacts.
      4. Stores the resulting subsystem design document in memory.
      5. Resets the subsystem's micro-loop counter to 0.

    All exceptions are caught internally and recorded in the returned
    ``MesoLoopRecord``.  They do NOT propagate to the caller — the orchestrator
    is expected to continue regardless of whether meso synthesis succeeds.

    Parameters
    ----------
    orch              : The Orchestrator instance (provides access to all
                        components and config).
    subsystem         : The name of the subsystem to synthesise
                        (e.g. "api_gateway", "auth_service").
    trigger_iteration : The total micro-loop count at the time this meso run
                        was triggered.  Stored in the record for auditing and
                        correlation with the micro-loop history.

    Returns
    -------
    MesoLoopRecord : A fully populated record describing what happened.
    """
    cfg = orch.config

    # Create the record object.  It starts with status=RUNNING and gets
    # updated as each step completes (or fails).
    record = MesoLoopRecord(
        subsystem=subsystem,
        trigger_iteration=trigger_iteration,
        started_at=time.monotonic(),
    )
    logger.info("meso START subsystem=%s", subsystem)

    try:
        # ── 1a. Collect artifacts by subsystem tag ────────────────────────────
        # Primary fetch: ask the memory manager for the most recent artifacts
        # tagged with this subsystem.  ``context_max_artifacts`` caps the total
        # because the Synthesizer has a finite context window.
        artifacts = await asyncio.wait_for(
            coroutine_if_needed(orch.memory_manager.get_artifacts)(
                subsystem=subsystem,
                limit=cfg.context_max_artifacts,
            ),
            timeout=20.0,
        )

        # ── 1b. Supplement with task-id–targeted fetch ────────────────────────
        # Secondary fetch: pull artifacts by the specific task IDs from recent
        # micro-loop records for this subsystem.  This catches artifacts that
        # were stored without correct subsystem metadata (e.g. because the task
        # dict had a missing or misspelled subsystem field) but whose task_id
        # correctly links them to this batch of work.
        #
        # We scan the rolling micro_history (last 100 records) for task_ids
        # that ran on this subsystem and succeeded.  We cap the list at 20 IDs
        # to avoid a huge IN clause.
        try:
            from .state import LoopStatus
            recent_task_ids = [
                r.task_id
                for r in orch.state.micro_history[-20:]
                if (
                    r.subsystem == subsystem
                    and r.status == LoopStatus.SUCCESS
                    and r.task_id is not None
                )
            ]
            if recent_task_ids and hasattr(orch.memory_manager, "get_artifacts_by_task_ids"):
                extra_rows = await asyncio.wait_for(
                    coroutine_if_needed(
                        orch.memory_manager.get_artifacts_by_task_ids
                    )(task_ids=recent_task_ids, limit_each=2),
                    timeout=10.0,
                )
                # Merge: skip any row whose artifact id is already in the
                # primary result so we don't duplicate content for the
                # Synthesizer.
                existing_ids = {a.get("id") for a in artifacts}
                for row in extra_rows:
                    if row.get("id") not in existing_ids:
                        artifacts.append(row)
                        existing_ids.add(row.get("id"))
                if extra_rows:
                    logger.debug(
                        "meso subsystem=%s: added %d task-id–targeted artifact(s)",
                        subsystem,
                        len(extra_rows),
                    )
        except Exception as exc:
            # The secondary fetch is a best-effort enhancement; never let it
            # break a meso synthesis.
            logger.debug("meso task-id supplemental fetch failed (non-fatal): %s", exc)

        # ── 2. Guard: do we have enough to synthesise? ────────────────────────
        # A synthesis built from fewer than ``meso_min_artifacts`` artifacts
        # (default: 2) wouldn't contain enough signal.  Skip it and mark
        # success so the orchestrator doesn't treat this as a failure.
        if len(artifacts) < cfg.meso_min_artifacts:
            logger.info(
                "meso SKIP subsystem=%s — only %d artifact(s), need %d",
                subsystem, len(artifacts), cfg.meso_min_artifacts,
            )
            # Mark SUCCESS (not FAILED) because skipping is intentional.
            # A FAILED status would trigger the orchestrator's failure-counting
            # logic, which would be misleading here.
            record.status = LoopStatus.SUCCESS
            record.artifacts_synthesised = len(artifacts)
            record.finished_at = time.monotonic()
            return record

        # Record how many artifacts we're working with.
        record.artifacts_synthesised = len(artifacts)

        # ── 2. Synthesizer call ───────────────────────────────────────────────
        # Ask the Synthesizer AI to read all the artifacts and produce a
        # coherent subsystem design document.  We pass ``level="meso"`` so the
        # Synthesizer knows what kind of output to produce (as opposed to the
        # system-wide "macro" synthesis it produces for the macro loop).
        synthesis = await asyncio.wait_for(
            coroutine_if_needed(orch.synthesizer_agent.call)(
                level="meso",
                subsystem=subsystem,
                artifacts=artifacts,
            ),
            timeout=cfg.synthesizer_timeout,
        )

        # ── 3. Store subsystem design document ───────────────────────────────
        # Package the synthesis into a document dict and store it.  This
        # document is distinct from the individual "artifacts" produced by
        # micro loops — it's a higher-level, synthesised summary.
        # The macro loop will later collect ALL such documents to build the
        # full architectural snapshot.
        document = {
            "type": "subsystem_design",       # tells the memory manager what kind of document this is
            "subsystem": subsystem,            # so the macro loop can organise by subsystem
            "synthesis": synthesis.get("content", ""),  # the AI-generated text
            "artifact_count": len(artifacts),  # how many artifacts were synthesised
            "trigger_iteration": trigger_iteration,  # which micro loop triggered this
        }
        doc_id = await asyncio.wait_for(
            coroutine_if_needed(orch.memory_manager.store_document)(document),
            timeout=15.0,
        )
        # Record the document ID so it appears in the Dashboard history.
        record.document_id = doc_id
        record.status = LoopStatus.SUCCESS

        # ── 3b. Emit an implementation task for Grub ──────────────────────────
        # If Grub integration is available, create an 'implementation' task so
        # Grub picks up this design and writes code for it.
        # This is safe to skip if the task engine or generator are not wired in.
        try:
            task_engine = getattr(orch, "task_engine", None)
            task_gen    = getattr(orch, "task_generator", None)
            if task_engine is not None and task_gen is not None:
                # Build a path hint — Grub will look for the actual .md file
                # in tinker_artifacts/ matching this subsystem name.
                artifact_hint = f"tinker_artifacts/{subsystem}_design.md"
                impl_task = task_gen.make_implementation_task(
                    title       = f"Implement {subsystem} from meso synthesis",
                    description = (
                        f"Grub: implement the {subsystem} subsystem based on the "
                        f"meso synthesis document. Design artifact: {artifact_hint}. "
                        f"Synthesis summary: {synthesis.get('content', '')[:300]}"
                    ),
                    subsystem     = subsystem,
                    artifact_path = artifact_hint,
                )
                await task_engine.add_task(impl_task)
                logger.info(
                    "meso: emitted implementation task for Grub (subsystem=%s, task=%s)",
                    subsystem, impl_task.id[:8],
                )
        except Exception as exc:
            # Never let Grub integration errors crash the meso loop
            logger.debug("meso: could not emit implementation task: %s", exc)

        # ── 4. Reset the subsystem counter ───────────────────────────────────
        # Now that we've synthesised this batch of micro-loop artifacts, reset
        # the counter to 0.  The subsystem will accumulate another batch of
        # micro loops before the next meso synthesis fires.
        orch.state.reset_subsystem_count(subsystem)
        logger.info(
            "meso END subsystem=%s doc_id=%s artifacts=%d",
            subsystem, doc_id, len(artifacts),
        )

    except asyncio.TimeoutError as exc:
        # An AI call or memory operation timed out.  Record the error and
        # return — do NOT re-raise.  The orchestrator will continue with
        # micro loops.
        msg = f"Timeout in meso loop for subsystem={subsystem}: {exc}"
        logger.warning(msg)
        record.status = LoopStatus.FAILED
        record.error = msg

    except Exception as exc:
        # Unexpected error.  ``logger.exception`` logs the full stack trace,
        # which is invaluable for debugging.  Again, do NOT re-raise.
        msg = f"Error in meso loop for subsystem={subsystem}: {exc}"
        logger.exception(msg)
        record.status = LoopStatus.FAILED
        record.error = msg

    finally:
        # Always record the finish time, whether we succeeded, skipped, or failed.
        # The early-return path above sets finished_at explicitly; this ``finally``
        # block handles the normal and error paths.
        if record.finished_at is None:
            record.finished_at = time.monotonic()

    return record
