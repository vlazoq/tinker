"""
agents/grub/feedback.py
================
Tinker ↔ Grub integration layer.

This file is the bridge between the two systems.

Flow
----
TINKER → GRUB:
  1. Tinker creates a task with type="implementation" and
     artifact_path pointing to a design doc.
  2. Grub's TinkerBridge.fetch_implementation_tasks() reads these tasks
     from Tinker's SQLite database and converts them to GrubTasks.

GRUB → TINKER:
  3. After Grub finishes implementing a task, it calls
     TinkerBridge.report_result() which:
       a. Marks the original Tinker task as status="complete"
       b. Injects a new Tinker task like "Review implementation of X"
          so Tinker knows to review the code and possibly redesign.

This closes the loop:
  Tinker designs → Grub implements → Tinker reviews → Tinker redesigns → ...

STATUS: FULLY IMPLEMENTED
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .contracts.task import GrubTask, TaskPriority
from .contracts.result import MinionResult

logger = logging.getLogger(__name__)


class TinkerBridge:
    """
    Reads implementation tasks from Tinker's database and writes results back.

    Parameters
    ----------
    tinker_tasks_db      : Path to Tinker's SQLite task database.
    tinker_artifacts_dir : Directory where Tinker writes design documents.
    grub_artifacts_dir   : Directory where Grub writes implementation notes.
    """

    def __init__(
        self,
        tinker_tasks_db: str = "tinker_tasks_engine.sqlite",
        tinker_artifacts_dir: str = "./tinker_artifacts",
        grub_artifacts_dir: str = "./grub_artifacts",
    ) -> None:
        self._tasks_db = Path(tinker_tasks_db)
        self._artifacts = Path(tinker_artifacts_dir)
        self._grub_arts = Path(grub_artifacts_dir)
        self._grub_arts.mkdir(parents=True, exist_ok=True)

    # ── Tinker → Grub ─────────────────────────────────────────────────────────

    def fetch_implementation_tasks(self, limit: int = 10) -> list[GrubTask]:
        """
        Read pending 'implementation' tasks from Tinker's task database.

        Tinker marks tasks ready for implementation by setting
        type='implementation' and status='pending'.

        Parameters
        ----------
        limit : Maximum number of tasks to fetch at once.

        Returns
        -------
        List of GrubTasks converted from Tinker tasks.
        """
        if not self._tasks_db.exists():
            logger.debug("TinkerBridge: tasks DB not found at %s", self._tasks_db)
            return []

        try:
            con = sqlite3.connect(str(self._tasks_db), timeout=5)
            # WAL mode: allows concurrent readers + one writer without "database is
            # locked" errors.  Tinker (reader/writer) and Grub (writer) share this
            # file; WAL makes that safe.  The mode persists in the DB file — setting
            # it here is idempotent if Tinker already set it.
            con.execute("PRAGMA journal_mode=WAL")
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT id, title, description, subsystem, metadata "
                "FROM tasks "
                "WHERE type='implementation' AND status='pending' "
                "ORDER BY priority_score DESC "
                "LIMIT ?",
                (limit,),
            ).fetchall()
            con.close()
        except Exception as exc:
            logger.warning("TinkerBridge.fetch_implementation_tasks: %s", exc)
            return []

        tasks = []
        for row in rows:
            meta = {}
            try:
                meta = json.loads(row["metadata"] or "{}")
            except Exception as exc:
                logger.debug("TinkerBridge: malformed metadata for row %s: %s", row["id"] if "id" in row.keys() else "?", exc)

            artifact_path = meta.get("artifact_path", "")
            if not artifact_path:
                # Try to find the design artifact by subsystem name
                artifact_path = self._find_artifact(row["subsystem"] or "")

            tasks.append(
                GrubTask(
                    title=row["title"],
                    description=row["description"] or "",
                    artifact_path=artifact_path,
                    target_files=meta.get("target_files", []),
                    subsystem=row["subsystem"] or "unknown",
                    tinker_task_id=row["id"],
                    priority=TaskPriority.NORMAL,
                    context=meta,
                )
            )
            logger.debug(
                "TinkerBridge: fetched task '%s' (tinker_id=%s)",
                row["title"],
                row["id"][:8],
            )

        logger.info(
            "TinkerBridge: fetched %d implementation tasks from Tinker", len(tasks)
        )
        return tasks

    def _find_artifact(self, subsystem: str) -> str:
        """
        Look for a design artifact matching the subsystem name.

        Checks the Tinker artifacts directory for files like
        'api_gateway_design.md' or 'api_gateway*.md'.

        Returns the first matching path, or "" if nothing found.
        """
        if not subsystem or not self._artifacts.exists():
            return ""

        subsystem_clean = subsystem.lower().replace(" ", "_")
        for pattern in [
            f"{subsystem_clean}_design.md",
            f"{subsystem_clean}*.md",
            f"*{subsystem_clean}*.md",
        ]:
            matches = list(self._artifacts.glob(pattern))
            if matches:
                return str(matches[0])
        return ""

    # ── Grub → Tinker ─────────────────────────────────────────────────────────

    def report_result(self, result: MinionResult, tinker_task_id: str) -> bool:
        """
        Write Grub's result back to Tinker.

        Does two things:
        1. Marks the original Tinker task as ``status='complete'``.
        2. Creates a new Tinker task: "Review implementation of X" so Tinker
           knows to look at what Grub produced and decide what to do next.

        Parameters
        ----------
        result         : The MinionResult from the pipeline.
        tinker_task_id : The Tinker task ID to mark as complete.

        Returns
        -------
        True if both operations succeeded.
        """
        if not self._tasks_db.exists():
            logger.warning("TinkerBridge.report_result: tasks DB not found")
            return False

        try:
            now = datetime.now(timezone.utc).isoformat()
            con = sqlite3.connect(str(self._tasks_db), timeout=10)
            # WAL mode: see fetch_implementation_tasks for rationale.
            con.execute("PRAGMA journal_mode=WAL")

            # 1. Mark original task as complete (matches TaskStatus.COMPLETE = "complete")
            con.execute(
                "UPDATE tasks SET status='complete', updated_at=? WHERE id=?",
                (now, tinker_task_id),
            )
            logger.info(
                "TinkerBridge: marked Tinker task %s as complete", tinker_task_id[:8]
            )

            # 2. Always create a follow-up review task for Tinker so it knows
            #    what Grub produced and can decide whether to redesign.
            #
            # Use a DETERMINISTIC id derived from the original tinker_task_id so
            # that INSERT OR IGNORE deduplicates correctly on crash-recovery.
            # If report_result is called twice for the same task (e.g. after a
            # crash), the second INSERT is silently ignored and no duplicate task
            # is created.
            new_id = f"review-{tinker_task_id}"
            feedback_title = f"Review Grub implementation: {result.summary[:80]}"
            notes = result.feedback_for_tinker or "(no additional notes)"
            feedback_desc = (
                f"Grub has implemented a task. Review the output and decide "
                f"if the architecture needs updating.\n\n"
                f"Implementation summary: {result.summary}\n\n"
                f"Files produced: {', '.join(result.files_written[:5])}\n\n"
                f"Notes: {notes}\n\n"
                f"Grub score: {result.score:.2f}"
            )
            meta = json.dumps(
                {
                    "grub_task_result": result.to_dict(),
                    "source": "grub_feedback",
                }
            )
            con.execute(
                """INSERT OR IGNORE INTO tasks
                   (id, title, description, type, subsystem, status,
                    confidence_gap, is_exploration, created_at, updated_at,
                    priority_score, staleness_hours, dependency_depth,
                    last_subsystem_work_hours, attempt_count,
                    dependencies, outputs, tags, metadata)
                   VALUES (?,?,?,'review','cross_cutting','pending',
                           0.6, 0, ?,?, 0.6, 0.0, 0, 0.0, 0,
                           '[]','[]','["grub_feedback"]',?)""",
                (new_id, feedback_title, feedback_desc, now, now, meta),
            )
            logger.info("TinkerBridge: created Tinker review task %s", new_id[:8])

            con.commit()
            con.close()
            return True

        except Exception as exc:
            logger.error("TinkerBridge.report_result failed: %s", exc)
            return False

    def write_implementation_note(
        self,
        result: MinionResult,
        task: GrubTask,
    ) -> str:
        """
        Write a human-readable implementation note to grub_artifacts/.

        This creates a Markdown file summarising what Grub produced,
        which Tinker can then read as additional context.

        Returns the path of the written file.
        """
        note_path = self._grub_arts / f"impl_{task.subsystem}_{task.id[:8]}.md"
        now = datetime.now(timezone.utc).isoformat()

        content = "\n".join(
            [
                f"# Implementation Note: {task.title}",
                "",
                f"**Date**: {now}",
                f"**Subsystem**: {task.subsystem}",
                f"**Status**: {result.status.value}",
                f"**Quality Score**: {result.score:.2f}",
                "",
                "## Summary",
                result.summary,
                "",
                "## Files Produced",
                *[f"- `{f}`" for f in result.files_written],
                "",
            ]
        )

        if result.test_results:
            tr = result.test_results
            content += (
                f"\n## Test Results\n"
                f"- Passed: {tr.passed}\n"
                f"- Failed: {tr.failed}\n"
                f"- Errors: {tr.errors}\n"
            )

        if result.notes:
            content += f"\n## Notes\n{result.notes[:2000]}\n"

        try:
            note_path.write_text(content, encoding="utf-8")
            logger.info("TinkerBridge: wrote implementation note to %s", note_path)
            return str(note_path)
        except Exception as exc:
            logger.warning("TinkerBridge: could not write note: %s", exc)
            return ""
