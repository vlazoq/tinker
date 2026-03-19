"""
orchestrator/micro_loop.py
==========================

The micro loop — Tinker's smallest, fastest, most frequent unit of work.

What is the micro loop?
------------------------
The micro loop is the workhorse of the Tinker system.  It runs as fast as the
AI components will allow — potentially hundreds of times per hour — and each
iteration does one complete cycle of architectural reasoning:

  1. Pick a task from the queue.
  2. Assemble context (prior knowledge about this task's area).
  3. Ask the Architect AI to reason about the task.
  4. Optionally: if the Architect flagged knowledge gaps, look them up via the
     Tool Layer and ask the Architect again with enriched context.
  5. Ask the Critic AI to review and score the Architect's output.
  6. Store the combined result as an "artifact" in memory.
  7. Mark the task as complete in the task engine.
  8. Ask the task engine to generate follow-up tasks.

At the end of a successful micro loop, the orchestrator checks whether the
subsystem just worked on has now accumulated enough artifacts to justify a
meso synthesis (which happens in meso_loop.py).

How this file is structured
----------------------------
The main entry point is ``run_micro_loop()``.  It calls a series of private
helper functions (prefixed with ``_``) — one per step above.  Each helper:
  * Has exactly one job.
  * Wraps its call in ``asyncio.wait_for()`` with a timeout.
  * Raises ``MicroLoopError`` on failure so the top-level function can catch
    everything in one place.

The helper functions use ``coroutine_if_needed()`` (imported from
``orchestrator.compat``), which lets the orchestrator work with both async and
sync component implementations without the callers needing to know which they're
dealing with.

What is TYPE_CHECKING?
-----------------------
At the bottom of the imports you'll see:

    if TYPE_CHECKING:
        from .orchestrator import Orchestrator

``TYPE_CHECKING`` is False at runtime, so the import never actually executes.
This avoids a circular import (orchestrator.py imports micro_loop.py, so
micro_loop.py cannot import orchestrator.py at runtime).  The import is there
only to help type checkers and IDEs understand what ``orch`` is.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Optional

from .compat import coroutine_if_needed
from .state import MicroLoopRecord, LoopStatus
from exceptions import MicroLoopError  # noqa: F401  (re-exported for callers)

# This import only happens when a static analysis tool runs, not at runtime.
# It allows us to annotate function parameters with the Orchestrator type
# without creating a circular import.
if TYPE_CHECKING:
    from .orchestrator import Orchestrator

# ── Enterprise component helpers ─────────────────────────────────────────────
# These are imported lazily (try/except) so the micro loop works in minimal
# deployments where the resilience/observability packages aren't installed.

try:
    from resilience.idempotency import IdempotencyCache, idempotency_key  # noqa: F401

    _IDEMPOTENCY_AVAILABLE = True
except ImportError:
    _IDEMPOTENCY_AVAILABLE = False

try:
    from resilience.rate_limiter import RateLimiterRegistry  # noqa: F401

    _RATE_LIMITER_AVAILABLE = True
except ImportError:
    _RATE_LIMITER_AVAILABLE = False

try:
    from observability.tracing import default_tracer  # noqa: F401

    _TRACING_AVAILABLE = True
except ImportError:
    _TRACING_AVAILABLE = False

try:
    from lineage.tracker import LineageTracker  # noqa: F401

    _LINEAGE_AVAILABLE = True
except ImportError:
    _LINEAGE_AVAILABLE = False

# This logger's name "tinker.orchestrator.micro" means log messages from this
# module appear under a sub-category of the main orchestrator logger.
# You can set the log level separately for "tinker.orchestrator.micro" if
# you want fine-grained control (e.g., DEBUG here, INFO elsewhere).
logger = logging.getLogger("tinker.orchestrator.micro")


async def run_micro_loop(orch: "Orchestrator") -> MicroLoopRecord:
    """
    Execute one complete micro-loop iteration from task selection to new tasks.

    This is the only public function in this module.  The ``Orchestrator``
    class calls it in a tight loop (see ``orchestrator.py``).

    The function creates a ``MicroLoopRecord`` at the start and fills it in
    as each step completes.  The record is returned to the orchestrator at the
    end so it can be stored in the history and used to update state.

    On fatal failure, ``MicroLoopError`` is raised.  The orchestrator catches
    this and handles backoff (see ``orchestrator.py:_run_micro``).

    Parameters
    ----------
    orch : The Orchestrator instance, passed in so this function can access
           all injected components (architect_agent, critic_agent, etc.) and
           the config.

    Returns
    -------
    MicroLoopRecord : A fully populated record describing what happened.

    Raises
    ------
    MicroLoopError : If any step fails unrecoverably.
    """
    cfg = orch.config
    state = orch.state
    # The iteration number is "how many micro loops have run so far" + 1.
    # We add 1 because total_micro_loops is incremented by the caller *after*
    # this function returns, so right now it still reflects the previous count.
    iteration = state.total_micro_loops + 1

    # Retrieve enterprise components attached to the orchestrator (if any).
    # The ``enterprise`` dict is populated by ``_build_enterprise_stack()`` in
    # main.py.  When running without enterprise wiring (e.g. in unit tests),
    # it defaults to an empty dict and all enterprise features are no-ops.
    enterprise: dict = getattr(orch, "enterprise", {})
    rate_limiters: Optional[object] = enterprise.get("rate_limiters")
    idempotency_cache: Optional[object] = enterprise.get("idempotency_cache")
    lineage_tracker: Optional[object] = enterprise.get("lineage_tracker")
    _audit_log: Optional[object] = enterprise.get("audit_log")
    alerter: Optional[object] = enterprise.get("alerter")

    # ── 1. Task Selection ────────────────────────────────────────────────────
    # Ask the task engine for the next task to work on.  If there's nothing
    # in the queue, the task engine is expected to create one.
    task = await _select_task(orch)
    if task is None:
        # No task available at all — this is unusual.  Raise immediately.
        raise MicroLoopError("No tasks available — task engine returned None")

    # ── Idempotency check ────────────────────────────────────────────────────
    # Before doing any expensive AI work, check if we've already successfully
    # processed this exact task in this session.  This protects against
    # duplicate work when the orchestrator is restarted mid-run or when the
    # same task is re-queued by the task engine.
    #
    # The idempotency key is a SHA-256 hash of the operation + task ID, so
    # two calls with the same task produce the same key deterministically.
    # If the key exists in the cache, we know this task has already been
    # processed and we can skip it safely.
    if _IDEMPOTENCY_AVAILABLE and idempotency_cache is not None:
        idem_key = idempotency_key("micro_loop", task_id=task["id"])
        try:
            if await idempotency_cache.exists(idem_key):
                logger.info(
                    "micro[%d] SKIP task=%s (idempotency hit — already processed)",
                    iteration,
                    task["id"],
                )
                # Build a minimal SUCCESS record so the orchestrator's
                # counters increment correctly without doing redundant work.
                skip_record = MicroLoopRecord(
                    iteration=iteration,
                    task_id=task["id"],
                    subsystem=task.get("subsystem", "unknown"),
                    started_at=time.monotonic(),
                )
                skip_record.status = LoopStatus.SUCCESS
                skip_record.finished_at = time.monotonic()
                return skip_record
        except Exception as exc:
            # Idempotency cache errors are non-fatal — just proceed normally.
            logger.debug("Idempotency cache lookup failed (non-fatal): %s", exc)

    # Create the record object now that we know which task we're working on.
    # It starts with status=RUNNING and gets updated as steps complete.
    record = MicroLoopRecord(
        iteration=iteration,
        task_id=task["id"],
        subsystem=task.get("subsystem", "unknown"),
        started_at=time.monotonic(),
    )
    logger.info(
        "micro[%d] START task=%s subsystem=%s",
        iteration,
        task["id"],
        task.get("subsystem"),
    )

    try:
        # ── 2. Context Assembly ──────────────────────────────────────────────
        # Before asking the Architect anything, we give it some background:
        # recent artifacts related to this subsystem, the task description,
        # and any other relevant system context.
        context = await _assemble_context(orch, task)

        # ── 2b. Review-task enrichment ───────────────────────────────────────
        # When Grub writes back a 'review' task, the Grub result data lives
        # inside task["metadata"]["grub_task_result"].  Extract it and surface
        # it as a top-level context key so the Architect can see it clearly
        # without having to dig through nested metadata JSON.
        if task.get("type") == "review":
            context = _enrich_review_context(task, context)

        # ── 3. Architect Call ────────────────────────────────────────────────
        # The Architect AI reads the task and context and proposes an
        # architectural design.  This is typically the slowest and most
        # expensive step (the AI is doing real reasoning here).
        #
        # Rate limiting: acquire a token from the architect rate limiter before
        # calling.  If the rate is exceeded, this waits until a token is
        # available.  This prevents runaway cost when the loop runs very fast.
        if _RATE_LIMITER_AVAILABLE and rate_limiters is not None:
            try:
                arch_limiter = rate_limiters.get("architect")
                if arch_limiter is not None:
                    await arch_limiter.acquire()
            except Exception as exc:
                logger.warning(
                    "Architect rate limiter acquire failed (continuing unthrottled): %s",
                    exc,
                )

        architect_result = await _call_architect_with_validation_retry(
            orch, task, context, cfg.architect_timeout, cfg.max_validation_retries
        )
        # Record how many tokens the Architect used — useful for cost tracking.
        # _call_architect_with_validation_retry accumulates tokens across all
        # retry attempts into the returned dict's tokens_used field.
        record.architect_tokens = architect_result.get("tokens_used", 0)

        # Track token cost in the rate limiter for usage reporting.
        if _RATE_LIMITER_AVAILABLE and rate_limiters is not None:
            try:
                arch_limiter = rate_limiters.get("architect")
                if arch_limiter is not None:
                    arch_limiter.record_tokens(record.architect_tokens)
            except Exception as exc:
                logger.debug(
                    "Architect rate limiter record_tokens failed (non-fatal): %s", exc
                )

        # ── 4. Researcher Routing (optional) ─────────────────────────────────
        # The Architect may say "I'm not sure about X — I have a knowledge gap."
        # When that happens, we ask the Tool Layer to look X up, then re-run
        # the Architect with the new information in its context.
        #
        # _refinement_context tracks whichever context was used for the most
        # recent Architect call so the refinement loop (step 5) can build on
        # it when injecting Critic feedback.
        _refinement_context = context
        researcher_calls = 0
        if architect_result.get("knowledge_gaps"):
            enriched_context, researcher_calls = await _route_researcher(
                orch, task, context, architect_result["knowledge_gaps"], cfg
            )
            record.researcher_calls = researcher_calls
            # Only re-run the Architect if the Tool Layer actually found
            # something useful (i.e., at least one call succeeded).
            if researcher_calls > 0:
                _refinement_context = enriched_context
                architect_result = await _call_architect_with_validation_retry(
                    orch, task, _refinement_context, cfg.architect_timeout,
                    cfg.max_validation_retries,
                )
                # Add the second call's token count to the running total.
                record.architect_tokens += architect_result.get("tokens_used", 0)

        # ── 5. Refinement loop (Architect → Critic until threshold met) ──────
        # The Critic scores the Architect's proposal.  If the score is below
        # cfg.min_critic_score and iterations remain, the Critic's feedback is
        # injected into the Architect's context and both run again.
        #
        # When cfg.min_critic_score == 0.0 (the default), the loop runs
        # exactly once, preserving the original single-pass behaviour.
        record.critic_tokens = 0
        _refinement_iter = 0
        while True:
            # Acquire rate-limiter token before each Critic call.
            if _RATE_LIMITER_AVAILABLE and rate_limiters is not None:
                try:
                    critic_limiter = rate_limiters.get("critic")
                    if critic_limiter is not None:
                        await critic_limiter.acquire()
                except Exception as exc:
                    logger.debug(
                        "Critic rate limiter acquire failed (non-fatal): %s", exc
                    )

            critic_result = await _call_critic(
                orch, task, architect_result, cfg.critic_timeout
            )
            _call_tokens = critic_result.get("tokens_used", 0)
            record.critic_tokens += _call_tokens
            record.critic_score = critic_result.get("score")
            _refinement_iter += 1

            if _RATE_LIMITER_AVAILABLE and rate_limiters is not None:
                try:
                    critic_limiter = rate_limiters.get("critic")
                    if critic_limiter is not None:
                        critic_limiter.record_tokens(_call_tokens)
                except Exception as exc:
                    logger.debug(
                        "Critic rate limiter record_tokens failed (non-fatal): %s",
                        exc,
                    )

            _score = record.critic_score or 0.0
            _min_score = cfg.min_critic_score

            # Exit: threshold disabled, score acceptable, or iterations exhausted.
            if (
                _min_score <= 0.0
                or _score >= _min_score
                or _refinement_iter >= cfg.max_refinement_iterations
            ):
                if _min_score > 0.0 and _score < _min_score:
                    logger.warning(
                        "micro[%d] refinement exhausted after %d iteration(s) "
                        "(best score=%.2f < threshold=%.2f) — proceeding",
                        iteration,
                        _refinement_iter,
                        _score,
                        _min_score,
                    )
                break

            # Score below threshold and iterations remain — re-run Architect
            # with Critic feedback injected, then loop back to re-score.
            logger.info(
                "micro[%d] refinement iter %d: score=%.2f < %.2f — re-running Architect",
                iteration,
                _refinement_iter,
                _score,
                _min_score,
            )
            _refinement_context = dict(_refinement_context)
            _refinement_context["critic_feedback"] = {
                "score": _score,
                "content": critic_result.get("content", ""),
                "iteration": _refinement_iter,
                "message": (
                    "Your previous proposal scored below the quality threshold. "
                    "Address the weaknesses identified above and produce an improved design."
                ),
            }
            architect_result = await _call_architect_with_validation_retry(
                orch, task, _refinement_context, cfg.architect_timeout,
                cfg.max_validation_retries,
            )
            record.architect_tokens += architect_result.get("tokens_used", 0)

        # ── Quality gate ─────────────────────────────────────────────────────
        # Alert operators when critic quality drops below the configured threshold.
        # Consecutive sub-threshold scores escalate from WARNING to ERROR.
        _maybe_fire_quality_gate(orch, record, alerter, iteration)

        # ── 6. Artifact Storage ──────────────────────────────────────────────
        # Combine the Architect's proposal and the Critic's review into a
        # single "artifact" and save it to long-term memory.  This artifact
        # will be used as context in future micro loops and as raw material
        # for the next meso synthesis.
        artifact_id = await _store_artifact(orch, task, architect_result, critic_result)
        record.artifact_id = artifact_id

        # ── Lineage tracking ─────────────────────────────────────────────────
        # Record the derivation relationship: this artifact was derived from
        # the task.  This builds a directed graph of provenance that can be
        # queried to trace where any artifact came from.
        if _LINEAGE_AVAILABLE and lineage_tracker is not None:
            try:
                await lineage_tracker.record_derivation(
                    parent_id=task["id"],
                    child_id=artifact_id,
                    operation="micro_loop",
                    metadata={
                        "subsystem": task.get("subsystem", "unknown"),
                        "critic_score": critic_result.get("score"),
                        "iteration": iteration,
                    },
                )
            except Exception as exc:
                logger.debug("Lineage tracking failed (non-fatal): %s", exc)

        # ── 7. Task Completion ───────────────────────────────────────────────
        # Tell the task engine that this task has been worked on and link it
        # to the artifact we just created.  This allows the task engine to
        # track what has been done and avoid re-queuing the same task.
        await _complete_task(orch, task, artifact_id)

        # ── 8. New Task Generation ───────────────────────────────────────────
        # The Architect's output may have raised new questions or identified
        # new areas to explore.  Ask the task engine to generate follow-up
        # tasks based on what we just learned.
        new_count = await _generate_tasks(orch, task, architect_result, critic_result)
        record.new_tasks_generated = new_count

        # ── Mark idempotency ─────────────────────────────────────────────────
        # Now that the task has been fully processed, record its idempotency
        # key so future runs know this task has already been done.  We do this
        # *after* all steps succeed — we never cache a partially-processed task.
        if _IDEMPOTENCY_AVAILABLE and idempotency_cache is not None:
            try:
                idem_key = idempotency_key("micro_loop", task_id=task["id"])
                await idempotency_cache.set(idem_key, artifact_id, ttl=3600)
            except Exception as exc:
                logger.debug("Idempotency cache set failed (non-fatal): %s", exc)

        # All steps succeeded — mark the record as a success.
        record.status = LoopStatus.SUCCESS

    except asyncio.TimeoutError as exc:
        # An AI call timed out.  Log it, mark the record as failed, and
        # re-raise as MicroLoopError so the orchestrator can handle backoff.
        msg = f"Timeout in micro loop iteration {iteration}: {exc}"
        logger.warning(msg)
        record.status = LoopStatus.FAILED
        record.error = msg
        raise MicroLoopError(msg) from exc

    except Exception as exc:
        # Any other unexpected error.  ``logger.exception`` logs the full
        # stack trace in addition to the message, which is essential for
        # debugging unexpected failures.
        msg = f"Error in micro loop iteration {iteration}: {exc}"
        logger.exception(msg)
        record.status = LoopStatus.FAILED
        record.error = msg
        raise MicroLoopError(msg) from exc

    finally:
        # ``finally`` always runs, whether the try block succeeded or raised.
        # Record the finish time no matter what happened.
        record.finished_at = time.monotonic()
        logger.info(
            "micro[%d] END status=%s duration=%.2fs artifact=%s",
            iteration,
            record.status.value,
            record.duration(),
            record.artifact_id,
        )

    return record


# ── Step helpers ─────────────────────────────────────────────────────────────
# Each function below handles exactly one step of the micro loop.
# They are all private (prefixed with _) because only run_micro_loop should
# call them — they are implementation details, not part of any public API.


async def _select_task(orch: "Orchestrator") -> Optional[dict]:
    """
    Ask the task engine for the next task to work on.

    The task engine is responsible for prioritisation — we just accept whatever
    it gives us.  In the stub implementation, it pops from a FIFO queue and
    creates a new random task if the queue is empty.

    A timeout of 10 seconds is more than enough for any reasonable task
    selection strategy (even one that queries a remote database).

    Returns None if the engine has genuinely nothing to offer (rare).
    Raises MicroLoopError if the engine itself throws an exception.
    """
    try:
        return await asyncio.wait_for(
            # coroutine_if_needed wraps sync functions so they can be awaited
            # without blocking the event loop (see orchestrator/compat.py).
            coroutine_if_needed(orch.task_engine.select_task)(),
            timeout=10.0,
        )
    except Exception as exc:
        raise MicroLoopError(f"task_engine.select_task failed: {exc}") from exc


async def _assemble_context(orch: "Orchestrator", task: dict) -> dict:
    """
    Build the context bundle that will be passed to the Architect AI.

    The context assembler fetches:
      * The task itself (description, subsystem, tags, etc.)
      * Up to ``context_max_artifacts`` prior artifacts from memory that are
        relevant to this task (typically retrieved by similarity search in the
        real implementation).
      * Any other system-level information the Architect needs.

    The 15-second timeout is generous but bounded — context assembly should
    be fast (it's mostly a memory lookup), but we don't want to block forever
    if the memory store is slow.

    Raises MicroLoopError on any failure, including timeout.
    """
    try:
        ctx = await asyncio.wait_for(
            coroutine_if_needed(orch.context_assembler.build)(
                task=task,
                # Limit the number of prior artifacts to keep the context
                # window manageable and avoid overwhelming the Architect.
                max_artifacts=orch.config.context_max_artifacts,
            ),
            timeout=15.0,
        )
    except Exception as exc:
        raise MicroLoopError(f"context_assembler.build failed: {exc}") from exc

    # Inject a one-shot stagnation hint if one was queued by the orchestrator.
    # The hint tells the Architect to break out of a detected loop pattern.
    # We clear it immediately so subsequent loops are not affected.
    hint = getattr(orch.state, "pending_stagnation_hint", None)
    if hint:
        orch.state.pending_stagnation_hint = None
        ctx = dict(ctx)  # shallow copy so we don't mutate the original
        ctx["stagnation_hint"] = hint
        logger.info(
            "micro[%d] Stagnation hint injected into Architect context",
            orch.state.total_micro_loops + 1,
        )

    return ctx


async def _call_architect(
    orch: "Orchestrator", task: dict, context: dict, timeout: float
) -> dict:
    """
    Call the Architect AI and return its response.

    The Architect is the main reasoning engine in the micro loop.  It receives
    the task and all assembled context, then produces:
      * ``content``        — a detailed architectural proposal in Markdown
      * ``tokens_used``    — how many tokens this call consumed
      * ``knowledge_gaps`` — topics it wasn't sure about (may be empty list)
      * ``decisions``      — key architectural decisions made
      * ``open_questions`` — questions that remain unresolved

    The ``timeout`` parameter is passed in (rather than hard-coded) because
    this function is called twice when there are knowledge gaps: once with the
    original context and once with the enriched context.  Both calls use the
    same configured timeout.

    ``asyncio.TimeoutError`` is re-raised rather than wrapped in MicroLoopError
    because the top-level ``run_micro_loop`` has a specific handler for it.
    """
    try:
        return await asyncio.wait_for(
            coroutine_if_needed(orch.architect_agent.call)(task=task, context=context),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        # Let TimeoutError bubble up unchanged — the caller distinguishes it
        # from other exceptions to produce a cleaner error message.
        raise
    except Exception as exc:
        raise MicroLoopError(f"architect_agent.call failed: {exc}") from exc


async def _route_researcher(
    orch: "Orchestrator",
    task: dict,
    context: dict,
    knowledge_gaps: list[str],
    cfg,
) -> tuple[dict, int]:
    """
    Fill knowledge gaps flagged by the Architect using the Tool Layer.

    When the Architect says "I'm not sure about X", this function:
      1. Takes each gap (up to ``cfg.max_researcher_calls_per_loop`` of them).
      2. Calls ``tool_layer.research(query=gap)`` to look it up.
      3. Collects the results.
      4. Returns an enriched copy of the context with the research added.

    Individual gap lookups that time out or fail are skipped with a warning —
    we want to make as much progress as possible even if one lookup fails.
    After this function, the caller re-runs the Architect with the enriched
    context so it can produce a better-informed proposal.

    Parameters
    ----------
    orch            : The Orchestrator (for access to tool_layer and config).
    task            : The current task (not used directly, but available for
                      future extensions that want task-aware research).
    context         : The original context dict from the context assembler.
    knowledge_gaps  : A list of strings describing what the Architect didn't know.
    cfg             : The OrchestratorConfig (for timeout and call-count limits).

    Returns
    -------
    (enriched_context, calls_made) where:
      enriched_context : A copy of ``context`` with a "research" key added.
      calls_made       : How many Tool Layer calls succeeded.
    """
    calls_made = 0
    # Make a shallow copy of the context dict so we don't mutate the original.
    enriched = dict(context)
    research_results = []

    # Only process up to max_researcher_calls_per_loop gaps.  The slice
    # ``[:N]`` takes only the first N items from the list.
    for gap in knowledge_gaps[: cfg.max_researcher_calls_per_loop]:
        try:
            result = await asyncio.wait_for(
                coroutine_if_needed(orch.tool_layer.research)(query=gap),
                timeout=cfg.tool_timeout,
            )
            research_results.append({"gap": gap, "result": result})
            calls_made += 1
            logger.debug(
                "researcher: gap=%r resolved (%d chars)", gap, len(str(result))
            )
        except asyncio.TimeoutError:
            # This particular gap lookup was too slow — skip it and try the next.
            logger.warning("researcher: timeout on gap=%r — skipping", gap)
        except Exception as exc:
            # Any other error with this gap lookup — log and skip.
            logger.warning("researcher: error on gap=%r: %s — skipping", gap, exc)

    if research_results:
        # Only add the "research" key if we actually got some results.
        # An empty research list would just confuse the Architect.
        enriched["research"] = research_results

    return enriched, calls_made


async def _call_critic(
    orch: "Orchestrator", task: dict, architect_result: dict, timeout: float
) -> dict:
    """
    Call the Critic AI and return its review of the Architect's proposal.

    The Critic receives the original task and the Architect's full output,
    then produces:
      * ``content``     — a written review with strengths and weaknesses
      * ``tokens_used`` — how many tokens this call consumed
      * ``score``       — a float between 0.0 and 1.0 (1.0 = perfect)
      * ``flags``       — any serious issues that need attention

    The Critic is lighter-weight than the Architect (it reviews rather than
    creates), hence the shorter default timeout (60s vs 120s).

    ``asyncio.TimeoutError`` is re-raised unchanged so the top-level handler
    can produce the right error message.
    """
    try:
        return await asyncio.wait_for(
            coroutine_if_needed(orch.critic_agent.call)(
                task=task, architect_result=architect_result
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        raise
    except Exception as exc:
        raise MicroLoopError(f"critic_agent.call failed: {exc}") from exc


async def _store_artifact(
    orch: "Orchestrator", task: dict, architect_result: dict, critic_result: dict
) -> str:
    """
    Combine the Architect's proposal and the Critic's review into one artifact
    and save it to the memory manager.

    Why combine them?
    -----------------
    Future micro loops will use this artifact as context when working on related
    tasks.  Having both the proposal *and* the review in one place means future
    iterations can learn from both the ideas generated AND the critique of those
    ideas.

    The memory manager is expected to return an object with an ``.id`` attribute
    (the real implementation returns a proper Artifact object from a vector DB).
    The stub returns a ``StubArtifact``, which also has ``.id``.

    Returns
    -------
    str : The unique ID of the stored artifact.

    Raises
    ------
    MicroLoopError : If the memory manager fails to store the artifact.
    """
    try:
        # Build a human-readable Markdown string combining both AI outputs.
        # This is what gets stored and what future iterations will read.
        content = (
            f"## Task: {task.get('description', task['id'])}\n"
            f"Subsystem: {task.get('subsystem', 'unknown')}\n\n"
            f"## Architect Output\n{architect_result.get('content', '')}\n\n"
            f"## Critic Output\n{critic_result.get('content', '')}\n"
            f"Critic Score: {critic_result.get('score', 'N/A')}"
        )
        # Metadata is stored alongside the content for later retrieval and
        # filtering.  The subsystem field is especially important — it's how
        # the meso loop fetches all artifacts for a given subsystem.
        metadata = {
            "subsystem": task.get("subsystem", "unknown"),
            # The critic score is stored in metadata so you can filter or
            # rank artifacts by quality without re-reading the full content.
            "critic_score": critic_result.get("score"),
            "tags": task.get("tags", []),
        }
        # store_artifact returns an Artifact object; we need its .id string.
        artifact = await asyncio.wait_for(
            coroutine_if_needed(orch.memory_manager.store_artifact)(
                content=content,
                task_id=task["id"],
                metadata=metadata,
            ),
            timeout=15.0,
        )
        # The real MemoryManager returns an Artifact object with a .id attribute.
        # Older stub implementations returned a plain string.  We handle both.
        return artifact.id if hasattr(artifact, "id") else str(artifact)
    except Exception as exc:
        raise MicroLoopError(f"memory_manager.store_artifact failed: {exc}") from exc


async def _complete_task(orch: "Orchestrator", task: dict, artifact_id: str) -> None:
    """
    Tell the task engine that this task has been completed.

    This is a *non-fatal* step — even if it fails, the artifact is already
    stored and the orchestrator can continue.  At worst, the task might be
    queued again in a future micro loop (duplicate work, not a crash).

    Compatibility note:
    Different task engine implementations have different signatures for
    ``complete_task``.  The preferred signature is ``(task_id, artifact_id)``.
    Some older implementations use ``(task_id, outputs=[artifact_id])``.
    We try the preferred form first, and fall back to the legacy form.
    """
    try:
        await asyncio.wait_for(
            coroutine_if_needed(orch.task_engine.complete_task)(
                task_id=task["id"], artifact_id=artifact_id
            ),
            timeout=10.0,
        )
    except TypeError:
        # TypeError means the task engine didn't accept ``artifact_id`` as a
        # keyword argument — try the legacy signature with ``outputs`` list.
        try:
            await asyncio.wait_for(
                coroutine_if_needed(orch.task_engine.complete_task)(
                    task_id=task["id"], outputs=[artifact_id]
                ),
                timeout=10.0,
            )
        except Exception as exc2:
            # Even the legacy signature failed — log and move on.
            logger.warning("task_engine.complete_task failed (non-fatal): %s", exc2)
    except Exception as exc:
        # Any other error — non-fatal, just log it.
        # The artifact is already stored, so we haven't lost any work.
        logger.warning("task_engine.complete_task failed (non-fatal): %s", exc)


async def _generate_tasks(
    orch: "Orchestrator",
    task: dict,
    architect_result: dict,
    critic_result: dict,
) -> int:
    """
    Ask the task engine to create follow-up tasks based on this iteration's output.

    The task engine receives the original task and the full outputs from both
    the Architect and the Critic.  It can use this information to:
      * Generate tasks that explore the Architect's open questions.
      * Create tasks to address weaknesses the Critic flagged.
      * Break down high-level recommendations into specific action items.

    This is also a *non-fatal* step — failing to generate new tasks doesn't
    affect the artifact we've already stored.  The queue might run dry
    eventually, but the task engine can always fall back to generating synthetic
    tasks (as the stub does).

    Returns
    -------
    int : The number of new tasks that were added to the queue (0 on failure).
    """
    try:
        new_tasks = await asyncio.wait_for(
            coroutine_if_needed(orch.task_engine.generate_tasks)(
                parent_task=task,
                architect_result=architect_result,
                critic_result=critic_result,
            ),
            timeout=20.0,
        )
        # Return the count of new tasks, or 0 if the engine returned None.
        return len(new_tasks) if new_tasks else 0
    except Exception as exc:
        logger.warning("task_engine.generate_tasks failed (non-fatal): %s", exc)
        return 0


# coroutine_if_needed is imported from orchestrator.compat at the top of this
# file.  It was previously defined here and monkey-patched onto the asyncio
# module; it now lives in its own module to keep the standard library clean.


def _enrich_review_context(task: dict, context: dict) -> dict:
    """
    For review tasks written by Grub, extract the implementation result from
    task metadata and add it as a prominent top-level key in the context.

    This ensures the Architect's prompt clearly shows what Grub produced
    (files written, test results, score, feedback) without requiring the
    Architect to parse nested JSON metadata.

    Returns a shallow copy of context with a ``grub_implementation`` key added.
    """
    import json as _json

    enriched = dict(context)
    try:
        meta = task.get("metadata") or {}
        # SQLite stores metadata as a JSON string; parse it if needed.
        if isinstance(meta, str):
            meta = _json.loads(meta)
        grub_result = meta.get("grub_task_result")
        if grub_result:
            if isinstance(grub_result, str):
                grub_result = _json.loads(grub_result)
            enriched["grub_implementation"] = grub_result
            logger.debug(
                "micro: enriched review task %s with grub_implementation (score=%.2f)",
                task["id"],
                grub_result.get("score", 0.0),
            )
    except Exception as exc:
        logger.debug(
            "_enrich_review_context: could not parse grub result (non-fatal): %s", exc
        )
    return enriched


def _maybe_fire_quality_gate(
    orch: "Orchestrator",
    record: "MicroLoopRecord",
    alerter: Optional[object],
    iteration: int,
) -> None:
    """
    Check whether the critic score falls below the quality gate threshold and,
    if so, fire an alert via the configured alerter.

    The orchestrator tracks the count of consecutive sub-threshold scores on
    a private attribute ``_quality_gate_fails`` so that repeated failures
    escalate from WARNING to ERROR severity.

    This function is synchronous and non-blocking: the actual alert is
    dispatched as a fire-and-forget asyncio Task.
    """
    threshold = getattr(getattr(orch, "config", None), "quality_gate_threshold", 0.4)
    escalation_count = getattr(
        getattr(orch, "config", None), "quality_gate_escalation_count", 3
    )
    if threshold <= 0.0 or alerter is None:
        return

    score = record.critic_score
    if score is None or score >= threshold:
        # Score acceptable — reset the consecutive-fail counter.
        orch.__dict__["_quality_gate_fails"] = 0
        return

    fails = orch.__dict__.get("_quality_gate_fails", 0) + 1
    orch.__dict__["_quality_gate_fails"] = fails

    try:
        from observability.alerting import AlertType, AlertSeverity
    except ImportError:
        return

    severity = (
        AlertSeverity.ERROR
        if fails >= escalation_count
        else AlertSeverity.WARNING
    )
    asyncio.create_task(
        alerter.alert(  # type: ignore[union-attr]
            alert_type=AlertType.CUSTOM,
            title=f"Quality gate breach: critic score {score:.2f} < {threshold:.2f}",
            message=(
                f"Micro loop {iteration} produced a critic score of {score:.2f}, "
                f"which is below the quality gate threshold of {threshold:.2f}. "
                f"This is the {fails} consecutive sub-threshold result."
            ),
            severity=severity,
            context={
                "iteration": iteration,
                "critic_score": score,
                "threshold": threshold,
                "consecutive_failures": fails,
                "task_id": record.task_id,
                "subsystem": record.subsystem,
            },
        )
    )
    logger.warning(
        "Quality gate breach: micro[%d] critic_score=%.2f < threshold=%.2f "
        "(consecutive=%d, severity=%s)",
        iteration,
        score,
        threshold,
        fails,
        severity.value,
    )


def _architect_result_is_thin(result: dict) -> bool:
    """Return True if the Architect output looks incomplete or degraded.

    A result is considered thin when its ``content`` field is shorter than
    50 characters — too short to be a genuine architectural proposal.  This
    catches generation failures (empty replies, refusals, truncated output)
    without false-positives on real proposals.
    """
    return len(result.get("content", "").strip()) < 50


async def _call_architect_with_validation_retry(
    orch: "Orchestrator",
    task: dict,
    context: dict,
    timeout: float,
    max_retries: int,
) -> dict:
    """Call the Architect and retry up to *max_retries* times if the output
    looks incomplete.

    On each retry, a ``validation_feedback`` key is added to the context so
    the model knows its previous attempt was inadequate.  Token counts from
    all attempts are accumulated in the returned dict's ``tokens_used`` field
    so the caller's token accounting stays correct.

    When *max_retries* is 0 this behaves identically to ``_call_architect``.
    """
    result = await _call_architect(orch, task, context, timeout)
    total_tokens = result.get("tokens_used", 0)

    for attempt in range(max_retries):
        if not _architect_result_is_thin(result):
            break
        logger.warning(
            "architect: output appears incomplete (attempt %d/%d) — retrying with feedback",
            attempt + 1,
            max_retries,
        )
        retry_context = dict(context)
        retry_context["validation_feedback"] = (
            "Your previous response was incomplete or too short. "
            "Please produce a complete response that strictly follows the output schema."
        )
        result = await _call_architect(orch, task, retry_context, timeout)
        total_tokens += result.get("tokens_used", 0)

    result["tokens_used"] = total_tokens
    return result


# MicroLoopError was previously defined here.  It now lives in exceptions.py
# (as part of the unified TinkerError hierarchy) and is imported at the top of
# this file.  This comment is kept so that ``git blame`` explains the move.
