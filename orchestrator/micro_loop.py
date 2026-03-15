"""
micro_loop.py — drives one complete micro-loop iteration.

Flow:
    task_engine.select_task()
    → context_assembler.build()
    → architect_agent.call()
    → [optional researcher routing if knowledge gaps flagged]
    → critic_agent.call()
    → memory_manager.store_artifact()
    → task_engine.complete_task()
    → task_engine.generate_tasks()
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Optional

from .state import MicroLoopRecord, LoopStatus

if TYPE_CHECKING:
    from .orchestrator import Orchestrator

logger = logging.getLogger("tinker.orchestrator.micro")


async def run_micro_loop(orch: "Orchestrator") -> MicroLoopRecord:
    """
    Execute one full micro-loop iteration.
    Raises MicroLoopError on unrecoverable failure (caller handles backoff).
    All sub-step errors are caught and surfaced through the record.
    """
    cfg = orch.config
    state = orch.state
    iteration = state.total_micro_loops + 1

    # ── 1. Task Selection ────────────────────────────────────────────────────
    task = await _select_task(orch)
    if task is None:
        raise MicroLoopError("No tasks available — task engine returned None")

    record = MicroLoopRecord(
        iteration=iteration,
        task_id=task["id"],
        subsystem=task.get("subsystem", "unknown"),
        started_at=time.monotonic(),
    )
    logger.info("micro[%d] START task=%s subsystem=%s", iteration, task["id"], task.get("subsystem"))

    try:
        # ── 2. Context Assembly ──────────────────────────────────────────────
        context = await _assemble_context(orch, task)

        # ── 3. Architect Call ────────────────────────────────────────────────
        architect_result = await _call_architect(orch, task, context, cfg.architect_timeout)
        record.architect_tokens = architect_result.get("tokens_used", 0)

        # ── 4. Researcher Routing (optional) ─────────────────────────────────
        researcher_calls = 0
        if architect_result.get("knowledge_gaps"):
            enriched_context, researcher_calls = await _route_researcher(
                orch, task, context, architect_result["knowledge_gaps"], cfg
            )
            record.researcher_calls = researcher_calls
            # re-run architect with enriched context if we got new info
            if researcher_calls > 0:
                architect_result = await _call_architect(
                    orch, task, enriched_context, cfg.architect_timeout
                )
                record.architect_tokens += architect_result.get("tokens_used", 0)

        # ── 5. Critic Call ───────────────────────────────────────────────────
        critic_result = await _call_critic(orch, task, architect_result, cfg.critic_timeout)
        record.critic_tokens = critic_result.get("tokens_used", 0)

        # ── 6. Artifact Storage ──────────────────────────────────────────────
        artifact_id = await _store_artifact(orch, task, architect_result, critic_result)
        record.artifact_id = artifact_id

        # ── 7. Task Completion ───────────────────────────────────────────────
        await _complete_task(orch, task, artifact_id)

        # ── 8. New Task Generation ───────────────────────────────────────────
        new_count = await _generate_tasks(orch, task, architect_result, critic_result)
        record.new_tasks_generated = new_count

        record.status = LoopStatus.SUCCESS

    except asyncio.TimeoutError as exc:
        msg = f"Timeout in micro loop iteration {iteration}: {exc}"
        logger.warning(msg)
        record.status = LoopStatus.FAILED
        record.error = msg
        raise MicroLoopError(msg) from exc

    except Exception as exc:
        msg = f"Error in micro loop iteration {iteration}: {exc}"
        logger.exception(msg)
        record.status = LoopStatus.FAILED
        record.error = msg
        raise MicroLoopError(msg) from exc

    finally:
        record.finished_at = time.monotonic()
        logger.info(
            "micro[%d] END status=%s duration=%.2fs artifact=%s",
            iteration, record.status.value, record.duration(), record.artifact_id
        )

    return record


# ── Step helpers ─────────────────────────────────────────────────────────────

async def _select_task(orch: "Orchestrator") -> Optional[dict]:
    """Delegate to the Task Engine."""
    try:
        return await asyncio.wait_for(
            asyncio.coroutine_if_needed(orch.task_engine.select_task)(),
            timeout=10.0,
        )
    except Exception as exc:
        raise MicroLoopError(f"task_engine.select_task failed: {exc}") from exc


async def _assemble_context(orch: "Orchestrator", task: dict) -> dict:
    try:
        return await asyncio.wait_for(
            asyncio.coroutine_if_needed(orch.context_assembler.build)(
                task=task,
                max_artifacts=orch.config.context_max_artifacts,
            ),
            timeout=15.0,
        )
    except Exception as exc:
        raise MicroLoopError(f"context_assembler.build failed: {exc}") from exc


async def _call_architect(
    orch: "Orchestrator", task: dict, context: dict, timeout: float
) -> dict:
    try:
        return await asyncio.wait_for(
            asyncio.coroutine_if_needed(orch.architect_agent.call)(
                task=task, context=context
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
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
    For each knowledge gap the Architect flagged, ask the Tool Layer to
    research it and inject the result back into the context.
    Returns (enriched_context, number_of_calls_made).
    """
    calls_made = 0
    enriched = dict(context)
    research_results = []

    for gap in knowledge_gaps[: cfg.max_researcher_calls_per_loop]:
        try:
            result = await asyncio.wait_for(
                asyncio.coroutine_if_needed(orch.tool_layer.research)(query=gap),
                timeout=cfg.tool_timeout,
            )
            research_results.append({"gap": gap, "result": result})
            calls_made += 1
            logger.debug("researcher: gap=%r resolved (%d chars)", gap, len(str(result)))
        except asyncio.TimeoutError:
            logger.warning("researcher: timeout on gap=%r — skipping", gap)
        except Exception as exc:
            logger.warning("researcher: error on gap=%r: %s — skipping", gap, exc)

    if research_results:
        enriched["research"] = research_results

    return enriched, calls_made


async def _call_critic(
    orch: "Orchestrator", task: dict, architect_result: dict, timeout: float
) -> dict:
    try:
        return await asyncio.wait_for(
            asyncio.coroutine_if_needed(orch.critic_agent.call)(
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
    try:
        # Build a human-readable content string from the architect + critic outputs
        content = (
            f"## Task: {task.get('description', task['id'])}\n"
            f"Subsystem: {task.get('subsystem', 'unknown')}\n\n"
            f"## Architect Output\n{architect_result.get('content', '')}\n\n"
            f"## Critic Output\n{critic_result.get('content', '')}\n"
            f"Critic Score: {critic_result.get('score', 'N/A')}"
        )
        metadata = {
            "subsystem": task.get("subsystem", "unknown"),
            "critic_score": critic_result.get("score"),
            "tags": task.get("tags", []),
        }
        # store_artifact returns an Artifact object; extract its .id
        artifact = await asyncio.wait_for(
            asyncio.coroutine_if_needed(orch.memory_manager.store_artifact)(
                content=content,
                task_id=task["id"],
                metadata=metadata,
            ),
            timeout=15.0,
        )
        # Support both Artifact objects (real MM) and plain string IDs (stub MM)
        return artifact.id if hasattr(artifact, "id") else str(artifact)
    except Exception as exc:
        raise MicroLoopError(f"memory_manager.store_artifact failed: {exc}") from exc


async def _complete_task(orch: "Orchestrator", task: dict, artifact_id: str) -> None:
    try:
        await asyncio.wait_for(
            asyncio.coroutine_if_needed(orch.task_engine.complete_task)(
                task_id=task["id"], artifact_id=artifact_id
            ),
            timeout=10.0,
        )
    except TypeError:
        # Fallback: some task engine implementations use (task_id, outputs=[...])
        try:
            await asyncio.wait_for(
                asyncio.coroutine_if_needed(orch.task_engine.complete_task)(
                    task_id=task["id"], outputs=[artifact_id]
                ),
                timeout=10.0,
            )
        except Exception as exc2:
            logger.warning("task_engine.complete_task failed (non-fatal): %s", exc2)
    except Exception as exc:
        # Non-fatal: artifact is stored, just couldn't mark task done.
        logger.warning("task_engine.complete_task failed (non-fatal): %s", exc)


async def _generate_tasks(
    orch: "Orchestrator",
    task: dict,
    architect_result: dict,
    critic_result: dict,
) -> int:
    try:
        new_tasks = await asyncio.wait_for(
            asyncio.coroutine_if_needed(orch.task_engine.generate_tasks)(
                parent_task=task,
                architect_result=architect_result,
                critic_result=critic_result,
            ),
            timeout=20.0,
        )
        return len(new_tasks) if new_tasks else 0
    except Exception as exc:
        logger.warning("task_engine.generate_tasks failed (non-fatal): %s", exc)
        return 0


# ── asyncio compatibility helper ─────────────────────────────────────────────

def _coroutine_if_needed(fn):
    """Wrap a sync function so we can await it in the event loop."""
    import inspect
    if inspect.iscoroutinefunction(fn):
        return fn
    async def wrapper(*args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))
    return wrapper

# Monkey-patch onto asyncio namespace for cleaner call sites above
asyncio.coroutine_if_needed = _coroutine_if_needed


class MicroLoopError(Exception):
    """Raised when a micro loop cannot complete — orchestrator should back off."""
