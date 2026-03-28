"""
runtime/orchestrator/_micro_helpers.py
======================================

Standalone helper functions extracted from ``micro_loop.py`` to keep that
module focused on the core step pipeline.

These are *not* step functions — they are utilities used by the step functions
or by ``run_micro_loop`` itself.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

from .state import MicroLoopRecord

if TYPE_CHECKING:
    from .orchestrator import Orchestrator

logger = logging.getLogger("tinker.orchestrator.micro")


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
        from infra.observability.alerting import AlertType, AlertSeverity
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
    # Import _call_architect from micro_loop at call time to avoid circular
    # imports (micro_loop imports from this module at the top level).
    from .micro_loop import _call_architect

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
