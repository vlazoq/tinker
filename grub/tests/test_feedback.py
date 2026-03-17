"""
grub/tests/test_feedback.py
============================
Tests for TinkerBridge — SQLite integration between Tinker and Grub.
"""

import json
import sqlite3
import uuid
import pytest
from datetime import datetime, timezone
from pathlib import Path

from grub.feedback         import TinkerBridge
from grub.contracts.task   import GrubTask, TaskPriority
from grub.contracts.result import MinionResult, ResultStatus


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_tasks_db(path: Path) -> None:
    """Create a minimal Tinker tasks DB with one implementation task."""
    con = sqlite3.connect(str(path))
    con.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            title TEXT,
            description TEXT,
            type TEXT,
            subsystem TEXT,
            status TEXT,
            priority_score REAL DEFAULT 0.5,
            metadata TEXT DEFAULT '{}',
            confidence_gap REAL DEFAULT 0.5,
            is_exploration INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT,
            staleness_hours REAL DEFAULT 0,
            dependency_depth INTEGER DEFAULT 0,
            last_subsystem_work_hours REAL DEFAULT 0,
            attempt_count INTEGER DEFAULT 0,
            dependencies TEXT DEFAULT '[]',
            outputs TEXT DEFAULT '[]',
            tags TEXT DEFAULT '[]'
        );
    """)
    now = datetime.now(timezone.utc).isoformat()
    con.execute(
        "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("task-001", "Implement billing module",
         "Write the billing module based on the design.",
         "implementation", "billing", "pending",
         0.8, json.dumps({"artifact_path": "tinker_artifacts/billing.md"}),
         0.5, 0, now, now, 0, 0, 0, 0, "[]", "[]", "[]")
    )
    con.commit()
    con.close()


# ═══════════════════════════════════════════════════════════════════════════════

class TestTinkerBridge:

    @pytest.fixture
    def bridge(self, tmp_path):
        db_path = tmp_path / "tinker_tasks.sqlite"
        _make_tasks_db(db_path)
        return TinkerBridge(
            tinker_tasks_db      = str(db_path),
            tinker_artifacts_dir = str(tmp_path / "artifacts"),
            grub_artifacts_dir   = str(tmp_path / "grub_artifacts"),
        )

    def test_fetch_returns_pending_implementation_tasks(self, bridge):
        tasks = bridge.fetch_implementation_tasks()
        assert len(tasks) == 1
        assert tasks[0].title    == "Implement billing module"
        assert tasks[0].subsystem == "billing"

    def test_fetch_preserves_artifact_path_from_metadata(self, bridge):
        tasks = bridge.fetch_implementation_tasks()
        assert tasks[0].artifact_path == "tinker_artifacts/billing.md"

    def test_fetch_returns_empty_when_no_implementation_tasks(self, tmp_path):
        db_path = tmp_path / "empty.sqlite"
        # DB with only design tasks
        con = sqlite3.connect(str(db_path))
        con.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, title TEXT, description TEXT,
                type TEXT, subsystem TEXT, status TEXT,
                priority_score REAL DEFAULT 0.5, metadata TEXT DEFAULT '{}'
            );
        """)
        now = datetime.now(timezone.utc).isoformat()
        con.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)",
                    ("d1","Design X","Desc","design","api","pending",0.5,"{}"))
        con.commit()
        con.close()

        b     = TinkerBridge(tinker_tasks_db=str(db_path))
        tasks = b.fetch_implementation_tasks()
        assert tasks == []

    def test_fetch_returns_empty_when_db_missing(self, tmp_path):
        b     = TinkerBridge(tinker_tasks_db=str(tmp_path / "ghost.sqlite"))
        tasks = b.fetch_implementation_tasks()
        assert tasks == []

    def test_report_result_marks_task_completed(self, bridge, tmp_path):
        result = MinionResult(
            task_id     = "t1",
            minion_name = "coder",
            status      = ResultStatus.SUCCESS,
            score       = 0.85,
            summary     = "Router implemented.",
            feedback_for_tinker = "Please review the router implementation.",
        )
        task = GrubTask(title="T", description="D", tinker_task_id="task-001",
                        subsystem="billing")
        ok = bridge.report_result(result, tinker_task_id="task-001")
        assert ok is True

        # Verify original task is marked "complete" (matches TaskStatus.COMPLETE = "complete")
        import sqlite3 as _sq
        db_path = Path(bridge._tasks_db)
        con = _sq.connect(str(db_path))
        row = con.execute("SELECT status FROM tasks WHERE id='task-001'").fetchone()
        con.close()
        assert row[0] == "complete"   # TaskStatus.COMPLETE value, NOT "completed"

    def test_report_result_creates_review_task(self, bridge):
        result = MinionResult(
            task_id              = "t1",
            minion_name          = "pipeline",
            status               = ResultStatus.SUCCESS,
            score                = 0.9,
            summary              = "Done",
            feedback_for_tinker  = "Check the billing module code.",
        )
        bridge.report_result(result, tinker_task_id="task-001")

        import sqlite3 as _sq
        db_path = Path(bridge._tasks_db)
        con = _sq.connect(str(db_path))
        rows = con.execute(
            "SELECT title, type FROM tasks WHERE type='review'"
        ).fetchall()
        con.close()
        assert len(rows) == 1
        assert "review" in rows[0][0].lower() or "Grub" in rows[0][0]

    def test_write_implementation_note_creates_file(self, bridge, tmp_path):
        result = MinionResult(
            task_id       = "t1",
            minion_name   = "pipeline",
            status        = ResultStatus.SUCCESS,
            score         = 0.88,
            summary       = "Billing module implemented",
            files_written = ["billing/module.py"],
        )
        task = GrubTask(title="Impl billing", description="D",
                        subsystem="billing", id="t1")
        path = bridge.write_implementation_note(result, task)
        assert path != ""
        assert Path(path).exists()
        content = Path(path).read_text()
        assert "billing" in content
        assert "0.88" in content
