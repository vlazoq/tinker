"""
macro_loop.py — full architectural snapshot, committed to Git.

Triggered on a wall-clock timer every `macro_interval_seconds`.

Flow:
    memory_manager.get_all_documents()
    → synthesizer_agent.call(level="macro", documents=...)
    → arch_state_manager.commit(snapshot)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from .state import MacroLoopRecord, LoopStatus

if TYPE_CHECKING:
    from .orchestrator import Orchestrator

logger = logging.getLogger("tinker.orchestrator.macro")


async def run_macro_loop(orch: "Orchestrator", trigger_iteration: int) -> MacroLoopRecord:
    """
    Execute a full architectural snapshot.
    Failures are logged; the timer resets regardless so we don't retry-storm.
    """
    cfg = orch.config
    snapshot_version = orch.state.total_macro_loops + 1
    record = MacroLoopRecord(
        snapshot_version=snapshot_version,
        trigger_iteration=trigger_iteration,
        started_at=time.monotonic(),
    )
    logger.info("macro START version=%d", snapshot_version)

    try:
        # ── 1. Gather all stored documents ────────────────────────────────────
        documents = await asyncio.wait_for(
            asyncio.coroutine_if_needed(orch.memory_manager.get_all_documents)(),
            timeout=30.0,
        )
        logger.info("macro: %d document(s) collected", len(documents))

        # ── 2. Full synthesizer pass ──────────────────────────────────────────
        snapshot = await asyncio.wait_for(
            asyncio.coroutine_if_needed(orch.synthesizer_agent.call)(
                level="macro",
                documents=documents,
                snapshot_version=snapshot_version,
                total_micro_loops=orch.state.total_micro_loops,
            ),
            timeout=cfg.synthesizer_timeout,
        )

        # ── 3. Commit to Architecture State Manager ───────────────────────────
        commit_payload = {
            "version": snapshot_version,
            "content": snapshot.get("content", ""),
            "total_micro_loops": orch.state.total_micro_loops,
            "total_meso_loops": orch.state.total_meso_loops,
            "subsystem_counts": dict(orch.state.subsystem_micro_counts),
        }
        commit_hash = await asyncio.wait_for(
            asyncio.coroutine_if_needed(orch.arch_state_manager.commit)(commit_payload),
            timeout=30.0,
        )
        record.commit_hash = commit_hash
        record.status = LoopStatus.SUCCESS
        logger.info("macro END version=%d commit=%s", snapshot_version, commit_hash)

    except asyncio.TimeoutError as exc:
        msg = f"Timeout in macro loop version={snapshot_version}: {exc}"
        logger.warning(msg)
        record.status = LoopStatus.FAILED
        record.error = msg

    except Exception as exc:
        msg = f"Error in macro loop version={snapshot_version}: {exc}"
        logger.exception(msg)
        record.status = LoopStatus.FAILED
        record.error = msg

    finally:
        if record.finished_at is None:
            record.finished_at = time.monotonic()

    return record
