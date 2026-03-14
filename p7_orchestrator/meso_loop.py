"""
meso_loop.py — subsystem-level synthesis.

Triggered when a subsystem has accumulated `meso_trigger_count` micro loops.

Flow:
    memory_manager.get_artifacts(subsystem, limit=N)
    → synthesizer_agent.call(level="meso", artifacts=...)
    → memory_manager.store_document(subsystem_design)
    → state.reset_subsystem_count(subsystem)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from .state import MesoLoopRecord, LoopStatus

if TYPE_CHECKING:
    from .orchestrator import Orchestrator

logger = logging.getLogger("tinker.orchestrator.meso")


async def run_meso_loop(orch: "Orchestrator", subsystem: str, trigger_iteration: int) -> MesoLoopRecord:
    """
    Execute meso-level synthesis for `subsystem`.
    Failures are logged but do NOT propagate — the orchestrator continues.
    """
    cfg = orch.config
    record = MesoLoopRecord(
        subsystem=subsystem,
        trigger_iteration=trigger_iteration,
        started_at=time.monotonic(),
    )
    logger.info("meso START subsystem=%s", subsystem)

    try:
        # ── 1. Collect artifacts for this subsystem ───────────────────────────
        artifacts = await asyncio.wait_for(
            asyncio.coroutine_if_needed(orch.memory_manager.get_artifacts)(
                subsystem=subsystem,
                limit=cfg.context_max_artifacts,
            ),
            timeout=20.0,
        )

        if len(artifacts) < cfg.meso_min_artifacts:
            logger.info(
                "meso SKIP subsystem=%s — only %d artifact(s), need %d",
                subsystem, len(artifacts), cfg.meso_min_artifacts,
            )
            record.status = LoopStatus.SUCCESS
            record.artifacts_synthesised = len(artifacts)
            record.finished_at = time.monotonic()
            return record

        record.artifacts_synthesised = len(artifacts)

        # ── 2. Synthesizer call ───────────────────────────────────────────────
        synthesis = await asyncio.wait_for(
            asyncio.coroutine_if_needed(orch.synthesizer_agent.call)(
                level="meso",
                subsystem=subsystem,
                artifacts=artifacts,
            ),
            timeout=cfg.synthesizer_timeout,
        )

        # ── 3. Store subsystem design document ───────────────────────────────
        document = {
            "type": "subsystem_design",
            "subsystem": subsystem,
            "synthesis": synthesis.get("content", ""),
            "artifact_count": len(artifacts),
            "trigger_iteration": trigger_iteration,
        }
        doc_id = await asyncio.wait_for(
            asyncio.coroutine_if_needed(orch.memory_manager.store_document)(document),
            timeout=15.0,
        )
        record.document_id = doc_id
        record.status = LoopStatus.SUCCESS

        # ── 4. Reset the subsystem counter ───────────────────────────────────
        orch.state.reset_subsystem_count(subsystem)
        logger.info(
            "meso END subsystem=%s doc_id=%s artifacts=%d",
            subsystem, doc_id, len(artifacts),
        )

    except asyncio.TimeoutError as exc:
        msg = f"Timeout in meso loop for subsystem={subsystem}: {exc}"
        logger.warning(msg)
        record.status = LoopStatus.FAILED
        record.error = msg

    except Exception as exc:
        msg = f"Error in meso loop for subsystem={subsystem}: {exc}"
        logger.exception(msg)
        record.status = LoopStatus.FAILED
        record.error = msg

    finally:
        if record.finished_at is None:
            record.finished_at = time.monotonic()

    return record
