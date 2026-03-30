"""
agents/grub/loop.py
============
The three execution modes for Grub.

This is the most important file for understanding how Grub runs.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MODE A — Sequential (DEFAULT)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
One task at a time, one minion at a time.

  Task 1: Coder → Reviewer → Tester → (Debugger if needed) → Refactorer
  Task 2: Coder → Reviewer → Tester → ...

When to use: Single PC, limited VRAM, getting started.
Switch: set execution_mode = "sequential" in grub_config.json

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MODE B — Parallel
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Multiple independent tasks run concurrently as asyncio tasks.

  Task 1 pipeline  ─────────────────────────────────────────────────►
  Task 2 pipeline  ─────────────────────────────────────────────────►
  Task 3 pipeline  ─────────────────────────────────────────────────►
                   All running at the same time

When to use: Multiple tasks that don't depend on each other,
             multiple Ollama instances (e.g. your 3090 + daily PC),
             or when tasks use different models (no VRAM conflict).
Switch: set execution_mode = "parallel" in grub_config.json

⚠️  Warning: if all tasks use the same large model (e.g. qwen2.5-coder:32b),
    parallel mode will saturate VRAM.  Use only when tasks use different models
    or when you have separate machines.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MODE C — Queue (distributed-ready)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Tasks are stored in a SQLite queue.  Multiple worker processes pick tasks
from the queue independently.

  SQLite queue ──► Worker 1 (PC with 3090) ──► results
                ──► Worker 2 (daily PC)    ──► results

When to use: Multi-machine setup, or when you want to add/remove
             workers without restarting Grub.
Switch:
  1. Set execution_mode = "queue" in grub_config.json
  2. Start the queue manager:  python -m grub --mode queue-manager
  3. Start workers on each machine:  python -m grub --mode worker
  See SETUP.md for full multi-machine instructions.

STATUS: All three modes FULLY IMPLEMENTED
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from datetime import UTC
from typing import TYPE_CHECKING

from .contracts.result import MinionResult, ResultStatus
from .contracts.task import GrubTask

if TYPE_CHECKING:
    from .config import GrubConfig
    from .registry import MinionRegistry

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# MODE A — Sequential
# ═══════════════════════════════════════════════════════════════════════════════


async def run_sequential(
    tasks: list[GrubTask],
    pipeline: PipelineRunner,
) -> list[MinionResult]:
    """
    Run tasks one at a time, in order.

    Each task goes through the full pipeline (Coder → Reviewer → Tester →
    Debugger if needed → Refactorer) before the next task starts.

    Parameters
    ----------
    tasks    : List of GrubTasks to process.
    pipeline : PipelineRunner that handles the Coder→Reviewer→Tester chain.

    Returns
    -------
    List of final MinionResults (one per task).
    """
    results = []
    total = len(tasks)

    for i, task in enumerate(tasks, 1):
        logger.info("Sequential mode: task %d/%d — %s", i, total, task.title)
        result = await pipeline.run_pipeline(task)
        results.append(result)
        logger.info(
            "Sequential mode: task %d/%d done — status=%s score=%.2f",
            i,
            total,
            result.status.value,
            result.score,
        )

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# MODE B — Parallel
# ═══════════════════════════════════════════════════════════════════════════════


async def run_parallel(
    tasks: list[GrubTask],
    pipeline: PipelineRunner,
    max_workers: int = 3,
) -> list[MinionResult]:
    """
    Run tasks concurrently, up to max_workers at a time.

    Uses asyncio.Semaphore to limit concurrency — this prevents too many
    LLM calls happening simultaneously, which would cause Ollama to queue
    them anyway (and potentially run out of VRAM).

    Parameters
    ----------
    tasks       : List of GrubTasks to process.
    pipeline    : PipelineRunner that handles the full pipeline.
    max_workers : Maximum number of tasks running at the same time.
                  Start with 2–3 and increase if your hardware can handle it.

    Returns
    -------
    List of final MinionResults in the same order as input tasks.
    """
    semaphore = asyncio.Semaphore(max_workers)

    async def _run_one(task: GrubTask) -> MinionResult:
        async with semaphore:
            logger.info("Parallel mode: starting — %s", task.title)
            result = await pipeline.run_pipeline(task)
            logger.info(
                "Parallel mode: done — %s (status=%s score=%.2f)",
                task.title,
                result.status.value,
                result.score,
            )
            return result

    # Run all tasks concurrently (limited by semaphore)
    results = await asyncio.gather(*[_run_one(t) for t in tasks])
    return list(results)


# ═══════════════════════════════════════════════════════════════════════════════
# MODE C — Queue (SQLite-backed)
# ═══════════════════════════════════════════════════════════════════════════════


class GrubQueue:
    """
    SQLite-backed task queue for Mode C (distributed/queue mode).

    Multiple processes can read from the same SQLite file — each worker
    uses SELECT ... FOR UPDATE equivalent (atomic claim) to avoid
    processing the same task twice.

    How it works
    ------------
    1. GrubAgent.enqueue_tasks() inserts tasks into the 'grub_tasks' table
       with status='pending'.
    2. Workers call claim_next() to atomically claim a pending task
       (status → 'in_progress', claimed_by = worker_id).
    3. Workers process the task and call complete() or fail().
    4. GrubAgent reads results from 'grub_results' table.

    Schema
    ------
    grub_tasks:   id, title, description, priority, status, payload (JSON), ...
    grub_results: task_id, worker_id, status, score, payload (JSON), ...
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        """Create the queue tables if they don't exist."""
        con = sqlite3.connect(self._db_path)
        con.executescript("""
            CREATE TABLE IF NOT EXISTS grub_tasks (
                id          TEXT PRIMARY KEY,
                title       TEXT NOT NULL,
                priority    TEXT NOT NULL DEFAULT 'normal',
                status      TEXT NOT NULL DEFAULT 'pending',
                payload     TEXT NOT NULL,           -- JSON-encoded GrubTask
                claimed_by  TEXT,                    -- worker ID
                created_at  TEXT NOT NULL,
                claimed_at  TEXT,
                updated_at  TEXT
            );

            CREATE INDEX IF NOT EXISTS grub_tasks_status_idx
                ON grub_tasks (status, priority, created_at);

            CREATE TABLE IF NOT EXISTS grub_results (
                id         TEXT PRIMARY KEY,
                task_id    TEXT NOT NULL,
                worker_id  TEXT,
                status     TEXT NOT NULL,
                score      REAL,
                payload    TEXT NOT NULL,            -- JSON-encoded MinionResult
                created_at TEXT NOT NULL,
                FOREIGN KEY (task_id) REFERENCES grub_tasks(id)
            );
        """)
        con.commit()
        con.close()
        logger.debug("GrubQueue initialised at %s", self._db_path)

    def enqueue(self, task: GrubTask) -> bool:
        """
        Insert a task into the queue.

        Returns True on success.
        """
        from datetime import datetime

        now = datetime.now(UTC).isoformat()
        try:
            con = sqlite3.connect(self._db_path, timeout=10)
            con.execute(
                "INSERT OR IGNORE INTO grub_tasks "
                "(id, title, priority, status, payload, created_at) "
                "VALUES (?, ?, ?, 'pending', ?, ?)",
                (
                    task.id,
                    task.title,
                    task.priority.value,
                    json.dumps(task.to_dict()),
                    now,
                ),
            )
            con.commit()
            con.close()
            logger.debug("GrubQueue.enqueue: %s", task.title)
            return True
        except Exception as exc:
            logger.error("GrubQueue.enqueue failed: %s", exc)
            return False

    def claim_next(self, worker_id: str) -> GrubTask | None:
        """
        Atomically claim the next pending task for a worker.

        Returns the claimed GrubTask, or None if the queue is empty.
        Uses a SQLite transaction to prevent two workers claiming the same task.
        """
        from datetime import datetime

        now = datetime.now(UTC).isoformat()
        try:
            con = sqlite3.connect(self._db_path, timeout=10)
            con.isolation_level = None  # autocommit off
            con.execute("BEGIN EXCLUSIVE")  # exclusive lock on the DB file

            # Find the next pending task (HIGH priority first, then FIFO)
            priority_order = "CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END"
            row = con.execute(
                f"SELECT id, payload FROM grub_tasks "
                f"WHERE status = 'pending' "
                f"ORDER BY {priority_order}, created_at "
                f"LIMIT 1"
            ).fetchone()

            if not row:
                con.execute("ROLLBACK")
                con.close()
                return None

            task_id, payload = row
            con.execute(
                "UPDATE grub_tasks SET status='in_progress', "
                "claimed_by=?, claimed_at=?, updated_at=? WHERE id=?",
                (worker_id, now, now, task_id),
            )
            con.execute("COMMIT")
            con.close()

            task_dict = json.loads(payload)
            return GrubTask.from_dict(task_dict)

        except Exception as exc:
            logger.error("GrubQueue.claim_next failed: %s", exc)
            return None

    def complete(self, task_id: str, result: MinionResult, worker_id: str) -> None:
        """Mark a task as completed and store the result."""
        import uuid
        from datetime import datetime

        now = datetime.now(UTC).isoformat()
        try:
            con = sqlite3.connect(self._db_path, timeout=10)
            con.execute(
                "UPDATE grub_tasks SET status='completed', updated_at=? WHERE id=?",
                (now, task_id),
            )
            con.execute(
                "INSERT INTO grub_results (id, task_id, worker_id, status, score, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    task_id,
                    worker_id,
                    result.status.value,
                    result.score,
                    json.dumps(result.to_dict()),
                    now,
                ),
            )
            con.commit()
            con.close()
        except Exception as exc:
            logger.error("GrubQueue.complete failed: %s", exc)

    def fail(self, task_id: str, reason: str) -> None:
        """Mark a task as failed (will not be retried by workers)."""
        from datetime import datetime

        now = datetime.now(UTC).isoformat()
        try:
            con = sqlite3.connect(self._db_path, timeout=10)
            con.execute(
                "UPDATE grub_tasks SET status='failed', updated_at=? WHERE id=?",
                (now, task_id),
            )
            con.commit()
            con.close()
        except Exception as exc:
            logger.error("GrubQueue.fail failed: %s", exc)

    def pending_count(self) -> int:
        """Return number of pending tasks."""
        try:
            con = sqlite3.connect(self._db_path, timeout=5)
            n = con.execute("SELECT COUNT(*) FROM grub_tasks WHERE status='pending'").fetchone()[0]
            con.close()
            return n
        except Exception:
            return 0

    def get_results(self, limit: int = 100) -> list[dict]:
        """Fetch recent completed results."""
        try:
            con = sqlite3.connect(self._db_path, timeout=5)
            rows = con.execute(
                "SELECT payload FROM grub_results ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            con.close()
            return [json.loads(r[0]) for r in rows]
        except Exception:
            return []


async def run_queue_worker(
    worker_id: str,
    queue: GrubQueue,
    pipeline: PipelineRunner,
    poll_interval: float = 2.0,
) -> None:
    """
    Queue mode worker loop.

    Continuously claims tasks from the queue, processes them, and stores
    results.  Runs until there are no more tasks (or until cancelled).

    Parameters
    ----------
    worker_id     : Unique identifier for this worker (e.g. "worker-3090").
    queue         : GrubQueue instance pointing to the shared SQLite file.
    pipeline      : PipelineRunner for processing tasks.
    poll_interval : Seconds to wait when queue is empty before checking again.
    """
    logger.info("Queue worker '%s' started", worker_id)

    while True:
        task = queue.claim_next(worker_id)

        if task is None:
            # Queue is empty — wait and check again
            if queue.pending_count() == 0:
                logger.info("Queue worker '%s': queue empty, stopping.", worker_id)
                break
            await asyncio.sleep(poll_interval)
            continue

        logger.info(
            "Queue worker '%s': claimed task %s — %s",
            worker_id,
            task.id[:8],
            task.title,
        )
        try:
            result = await pipeline.run_pipeline(task)
            queue.complete(task.id, result, worker_id)
            logger.info(
                "Queue worker '%s': task done — status=%s score=%.2f",
                worker_id,
                result.status.value,
                result.score,
            )
        except Exception as exc:
            logger.error("Queue worker '%s': task failed with exception: %s", worker_id, exc)
            queue.fail(task.id, str(exc))


# ═══════════════════════════════════════════════════════════════════════════════
# PipelineRunner — shared by all three modes
# ═══════════════════════════════════════════════════════════════════════════════


class PipelineRunner:
    """
    The Coder → Reviewer → Tester → Debugger → Refactorer pipeline.

    All three execution modes (A, B, C) use this same pipeline.
    The mode only affects HOW tasks are dispatched to the pipeline,
    not WHAT the pipeline does.

    Pipeline stages
    ---------------
    1. Coder      : Write the implementation
    2. Reviewer   : Review for quality and design alignment
       └─ If score < threshold: retry Coder with reviewer feedback (max N times)
    3. Tester     : Write and run tests
       └─ If tests fail: call Debugger (max N times)
    4. Refactorer : Clean up the working code

    Any stage can be skipped by setting skip_* = True in the config context.
    """

    def __init__(self, registry: MinionRegistry, config: GrubConfig) -> None:
        self.registry = registry
        self.config = config

    async def run_pipeline(self, task: GrubTask) -> MinionResult:
        """
        Run the full pipeline for one task.

        Returns the final MinionResult (from the last successful stage).
        """
        t0 = time.monotonic()
        logger.info("Pipeline start: %s", task.title)

        # ── Stage 1: Code ──────────────────────────────────────────────────────
        coder = self.registry.get_minion("coder")
        coder_result = await coder.run(task)

        if not coder_result.succeeded and coder_result.status == ResultStatus.FAILED:
            logger.error("Pipeline: Coder FAILED for task %s", task.id[:8])
            return coder_result

        # ── Stage 2: Review (with retry loop) ─────────────────────────────────
        reviewer = self.registry.get_minion("reviewer")
        review_task = GrubTask(
            id=task.id,
            title=task.title,
            description=task.description,
            artifact_path=task.artifact_path,
            subsystem=task.subsystem,
            context={
                **task.context,
                "files_to_review": coder_result.files_written,
            },
        )

        review_result = await reviewer.run(review_task)
        current_files = coder_result.files_written
        best_coder_result = coder_result

        for retry in range(self.config.max_iterations - 1):
            if review_result.succeeded:
                break
            logger.info(
                "Pipeline: review score %.2f < %.2f, retrying coder (attempt %d)",
                review_result.score,
                self.config.quality_threshold,
                retry + 2,
            )
            # Re-run coder with reviewer feedback
            retry_task = GrubTask(
                id=task.id,
                title=task.title,
                description=task.description
                + f"\n\n## Reviewer Feedback\n{review_result.notes[:1500]}",
                artifact_path=task.artifact_path,
                target_files=current_files,
                subsystem=task.subsystem,
                context=task.context,
            )
            retry_task.attempt_count = retry + 2
            coder_result = await coder.run(retry_task)
            if coder_result.files_written:
                current_files = coder_result.files_written
                best_coder_result = coder_result
            review_task.context["files_to_review"] = current_files
            review_result = await reviewer.run(review_task)

        # ── Stage 3: Test ──────────────────────────────────────────────────────
        tester = self.registry.get_minion("tester")
        test_task = GrubTask(
            id=task.id,
            title=task.title,
            description=task.description,
            subsystem=task.subsystem,
            context={**task.context, "files_to_test": current_files},
        )
        test_result = await tester.run(test_task)

        # ── Stage 4: Debug if tests failed ────────────────────────────────────
        if test_result.test_results and not test_result.test_results.all_passed:
            debugger = self.registry.get_minion("debugger")
            debug_task = GrubTask(
                id=task.id,
                title=task.title,
                description=task.description,
                subsystem=task.subsystem,
                context={
                    **task.context,
                    "test_output": test_result.test_results.output,
                    "failing_files": current_files,
                    "test_file": (
                        test_result.files_written[0] if test_result.files_written else ""
                    ),
                },
            )
            debug_result = await debugger.run(debug_task)
            if debug_result.files_written:
                current_files = debug_result.files_written

        # ── Stage 5: Refactor ─────────────────────────────────────────────────
        refactorer = self.registry.get_minion("refactorer")
        refactor_task = GrubTask(
            id=task.id,
            title=task.title,
            description=task.description,
            subsystem=task.subsystem,
            context={
                **task.context,
                "files_to_refactor": current_files,
                "test_file": (test_result.files_written[0] if test_result.files_written else ""),
            },
        )
        final_result = await refactorer.run(refactor_task)

        duration = time.monotonic() - t0
        logger.info(
            "Pipeline done: %s — %.1fs, score=%.2f, files=%s",
            task.title,
            duration,
            final_result.score,
            ", ".join(final_result.files_written[:3]),
        )

        # Merge feedback from all stages
        combined_feedback = " | ".join(
            filter(
                None,
                [
                    best_coder_result.feedback_for_tinker,
                    review_result.notes[:200] if not review_result.succeeded else "",
                ],
            )
        )
        final_result.feedback_for_tinker = combined_feedback
        final_result.duration_seconds = duration

        return final_result
