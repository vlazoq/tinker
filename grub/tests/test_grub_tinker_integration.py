"""
grub/tests/test_grub_tinker_integration.py
===========================================
End-to-end integration tests for the Grub ↔ Tinker feedback loop.

These tests exercise the complete cycle without requiring a live Ollama server,
Redis, or any external service.  All I/O goes to a temporary SQLite database
that pytest creates and destroys for each test.

The loop under test
-------------------
  1. Tinker inserts an "implementation" task into its SQLite database.
  2. Grub reads it via ``TinkerBridge.fetch_implementation_tasks()``.
  3. A Minion runs (mocked here — returns a MinionResult directly).
  4. Grub reports the result via ``TinkerBridge.report_result()``.
  5. The original Tinker task is marked ``status="complete"``.
  6. A new ``type="review"`` Tinker task is created whose metadata embeds
     the MinionResult so Tinker can see exactly what Grub produced.
  7. When Tinker's micro loop picks up that review task, it calls
     ``_enrich_review_context(task, context)`` which surfaces the Grub result
     as a top-level ``grub_implementation`` key — directly readable by the
     Architect without parsing nested JSON.

What is NOT tested here
-----------------------
- Live Ollama calls (use the orchestrator integration test for that)
- Redis or DuckDB (those are unit-tested in memory/test_memory_manager.py)
- The Minion implementation itself (tested in test_minion_base.py)
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from grub.feedback         import TinkerBridge
from grub.contracts.task   import GrubTask, TaskPriority
from grub.contracts.result import MinionResult, ResultStatus, TestSummary

# _enrich_review_context is a module-level utility in the micro loop.
# We import it directly so we can unit-test it in isolation.
from orchestrator.micro_loop import _enrich_review_context


# ===========================================================================
# Shared fixtures
# ===========================================================================

_FULL_SCHEMA = """
    CREATE TABLE IF NOT EXISTS tasks (
        id                      TEXT PRIMARY KEY,
        title                   TEXT NOT NULL,
        description             TEXT DEFAULT '',
        type                    TEXT DEFAULT 'design',
        subsystem               TEXT DEFAULT 'unknown',
        status                  TEXT DEFAULT 'pending',
        priority_score          REAL DEFAULT 0.5,
        metadata                TEXT DEFAULT '{}',
        confidence_gap          REAL DEFAULT 0.5,
        is_exploration          INTEGER DEFAULT 0,
        created_at              TEXT NOT NULL,
        updated_at              TEXT NOT NULL,
        staleness_hours         REAL DEFAULT 0.0,
        dependency_depth        INTEGER DEFAULT 0,
        last_subsystem_work_hours REAL DEFAULT 0.0,
        attempt_count           INTEGER DEFAULT 0,
        dependencies            TEXT DEFAULT '[]',
        outputs                 TEXT DEFAULT '[]',
        tags                    TEXT DEFAULT '[]'
    );
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert_task(
    con: sqlite3.Connection,
    *,
    id: str,
    title: str,
    description: str = "Do the thing.",
    type: str = "implementation",
    subsystem: str = "billing",
    status: str = "pending",
    priority_score: float = 0.5,
    metadata: dict | None = None,
) -> None:
    now = _now()
    con.execute(
        """INSERT INTO tasks
           (id, title, description, type, subsystem, status,
            priority_score, metadata,
            confidence_gap, is_exploration, created_at, updated_at,
            staleness_hours, dependency_depth, last_subsystem_work_hours,
            attempt_count, dependencies, outputs, tags)
           VALUES (?,?,?,?,?,?,?,?,0.5,0,?,?,0.0,0,0.0,0,'[]','[]','[]')""",
        (id, title, description, type, subsystem, status,
         priority_score, json.dumps(metadata or {}), now, now),
    )


@pytest.fixture
def tasks_db(tmp_path) -> Path:
    """An empty Tinker tasks database with the correct schema."""
    db = tmp_path / "tinker_tasks.sqlite"
    con = sqlite3.connect(str(db))
    con.executescript(_FULL_SCHEMA)
    con.commit()
    con.close()
    return db


@pytest.fixture
def bridge(tasks_db, tmp_path) -> TinkerBridge:
    """A TinkerBridge wired to the test database."""
    return TinkerBridge(
        tinker_tasks_db      = str(tasks_db),
        tinker_artifacts_dir = str(tmp_path / "tinker_artifacts"),
        grub_artifacts_dir   = str(tmp_path / "grub_artifacts"),
    )


def _make_success_result(task_id: str = "grub-task-1") -> MinionResult:
    return MinionResult(
        task_id              = task_id,
        minion_name          = "coder",
        status               = ResultStatus.SUCCESS,
        score                = 0.88,
        files_written        = ["billing/module.py", "billing/tests.py"],
        summary              = "Billing module implemented with 100% test coverage.",
        feedback_for_tinker  = "Consider adding idempotency keys to the billing API.",
        test_results         = TestSummary(passed=12, failed=0, errors=0, skipped=1),
        iterations           = 2,
        duration_seconds     = 14.3,
    )


# ===========================================================================
# 1. Full happy-path loop
# ===========================================================================

class TestFullLoop:
    """End-to-end: Tinker → Grub → Tinker with a successful result."""

    def test_fetch_reads_pending_implementation_tasks(self, bridge, tasks_db):
        """Grub can read a task that Tinker created."""
        con = sqlite3.connect(str(tasks_db))
        _insert_task(con, id="t-001", title="Implement billing", subsystem="billing",
                     metadata={"artifact_path": "tinker_artifacts/billing_design.md"})
        con.commit()
        con.close()

        tasks = bridge.fetch_implementation_tasks()

        assert len(tasks) == 1
        assert tasks[0].title         == "Implement billing"
        assert tasks[0].subsystem     == "billing"
        assert tasks[0].tinker_task_id == "t-001"
        assert tasks[0].artifact_path  == "tinker_artifacts/billing_design.md"

    def test_report_result_marks_original_task_complete(self, bridge, tasks_db):
        """After reporting a result the original Tinker task is status='complete'."""
        con = sqlite3.connect(str(tasks_db))
        _insert_task(con, id="t-001", title="Implement billing")
        con.commit()
        con.close()

        result = _make_success_result()
        ok = bridge.report_result(result, tinker_task_id="t-001")

        assert ok is True
        con = sqlite3.connect(str(tasks_db))
        row = con.execute("SELECT status FROM tasks WHERE id='t-001'").fetchone()
        con.close()
        # TaskStatus.COMPLETE = "complete" (NOT "completed")
        assert row[0] == "complete", (
            f"Expected 'complete' but got {row[0]!r}. "
            "Check feedback.py: UPDATE tasks SET status='complete' ..."
        )

    def test_report_result_creates_review_task(self, bridge, tasks_db):
        """After reporting a result, Tinker has a new 'review' task."""
        con = sqlite3.connect(str(tasks_db))
        _insert_task(con, id="t-001", title="Implement billing")
        con.commit()
        con.close()

        result = _make_success_result()
        bridge.report_result(result, tinker_task_id="t-001")

        con = sqlite3.connect(str(tasks_db))
        rows = con.execute(
            "SELECT id, title, type, status, metadata FROM tasks WHERE type='review'"
        ).fetchall()
        con.close()

        assert len(rows) == 1, "Expected exactly one review task"
        review = rows[0]
        assert review[2] == "review"
        assert review[3] == "pending"   # ready for Tinker to pick up

    def test_review_task_metadata_contains_grub_result(self, bridge, tasks_db):
        """The review task's metadata embeds the full MinionResult as JSON."""
        con = sqlite3.connect(str(tasks_db))
        _insert_task(con, id="t-001", title="Implement billing")
        con.commit()
        con.close()

        result = _make_success_result()
        bridge.report_result(result, tinker_task_id="t-001")

        con = sqlite3.connect(str(tasks_db))
        row = con.execute(
            "SELECT metadata FROM tasks WHERE type='review'"
        ).fetchone()
        con.close()

        meta = json.loads(row[0])
        grub_result = meta.get("grub_task_result")
        assert grub_result is not None, "metadata must contain 'grub_task_result'"
        assert grub_result["status"]      == "success"
        assert grub_result["score"]       == pytest.approx(0.88, abs=0.001)
        assert grub_result["minion_name"] == "coder"
        assert "billing/module.py" in grub_result["files_written"]

    def test_full_round_trip_fetch_then_report_then_enrich(self, bridge, tasks_db):
        """
        Complete round-trip:
          1. Tinker creates implementation task
          2. Grub fetches it
          3. Minion produces result
          4. Bridge reports result
          5. Bridge reads back the review task
          6. micro_loop._enrich_review_context surfaces grub_implementation
        """
        # Step 1: Tinker creates an implementation task
        con = sqlite3.connect(str(tasks_db))
        _insert_task(con, id="t-full", title="Implement auth module",
                     subsystem="auth",
                     metadata={"artifact_path": "tinker_artifacts/auth_design.md"})
        con.commit()
        con.close()

        # Step 2: Grub fetches it
        grub_tasks = bridge.fetch_implementation_tasks()
        assert len(grub_tasks) == 1
        grub_task = grub_tasks[0]

        # Step 3: Minion produces result
        result = MinionResult(
            task_id              = grub_task.id,
            minion_name          = "coder",
            status               = ResultStatus.SUCCESS,
            score                = 0.91,
            files_written        = ["auth/handler.py"],
            summary              = "Auth handler implemented.",
            feedback_for_tinker  = "JWT expiry should be configurable.",
        )

        # Step 4: Bridge reports result
        ok = bridge.report_result(result, tinker_task_id=grub_task.tinker_task_id)
        assert ok is True

        # Step 5: Read back the review task from the DB
        con = sqlite3.connect(str(tasks_db))
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM tasks WHERE type='review'"
        ).fetchone()
        con.close()
        assert row is not None

        # Step 6: Simulate micro_loop picking up the review task
        review_task = dict(row)
        initial_context = {"task": review_task, "prompt": "Review this implementation."}
        enriched = _enrich_review_context(review_task, initial_context)

        assert "grub_implementation" in enriched, (
            "_enrich_review_context must add grub_implementation key"
        )
        impl = enriched["grub_implementation"]
        assert impl["status"]  == "success"
        assert impl["score"]   == pytest.approx(0.91, abs=0.001)
        assert "auth/handler.py" in impl["files_written"]


# ===========================================================================
# 2. _enrich_review_context — unit tests
# ===========================================================================

class TestEnrichReviewContext:
    """Tests for the micro_loop._enrich_review_context utility."""

    def _make_review_task(self, grub_result: dict | None = None) -> dict:
        """Build a dict that looks like a Tinker task row from SQLite."""
        return {
            "id": str(uuid.uuid4()),
            "type": "review",
            "title": "Review Grub implementation",
            "metadata": json.dumps({
                "grub_task_result": grub_result or {"status": "success", "score": 0.8},
                "source": "grub_feedback",
            }),
        }

    def test_adds_grub_implementation_key(self):
        task    = self._make_review_task({"status": "success", "score": 0.75})
        context = {"prompt": "Review this."}
        enriched = _enrich_review_context(task, context)
        assert "grub_implementation" in enriched

    def test_preserves_existing_context_keys(self):
        task    = self._make_review_task()
        context = {"prompt": "Review.", "prior_artifacts": [{"id": "a1"}]}
        enriched = _enrich_review_context(task, context)
        assert enriched["prompt"]           == "Review."
        assert enriched["prior_artifacts"]  == [{"id": "a1"}]

    def test_extracts_grub_result_fields(self):
        grub_data = {
            "status":        "success",
            "score":         0.92,
            "minion_name":   "coder",
            "files_written": ["src/api.py"],
            "summary":       "API implemented.",
        }
        task     = self._make_review_task(grub_data)
        enriched = _enrich_review_context(task, {})
        impl     = enriched["grub_implementation"]
        assert impl["score"]          == 0.92
        assert impl["minion_name"]    == "coder"
        assert "src/api.py" in impl["files_written"]

    def test_handles_metadata_as_dict(self):
        """metadata may already be a dict (not a JSON string)."""
        task = {
            "id": "t1",
            "type": "review",
            "metadata": {
                "grub_task_result": {"status": "success", "score": 0.5},
            },
        }
        enriched = _enrich_review_context(task, {})
        assert "grub_implementation" in enriched

    def test_handles_metadata_as_json_string(self):
        """metadata stored as JSON string in SQLite should be parsed."""
        task = {
            "id": "t1",
            "type": "review",
            "metadata": json.dumps({"grub_task_result": {"status": "failed", "score": 0.1}}),
        }
        enriched = _enrich_review_context(task, {})
        assert "grub_implementation" in enriched
        assert enriched["grub_implementation"]["status"] == "failed"

    def test_no_grub_result_leaves_context_unchanged(self):
        """Tasks without grub_task_result pass through unchanged."""
        task = {
            "id": "t1",
            "type": "review",
            "metadata": json.dumps({"source": "human_review"}),
        }
        original_context = {"prompt": "Review this.", "tokens": 500}
        enriched = _enrich_review_context(task, original_context)
        assert "grub_implementation" not in enriched
        assert enriched["prompt"]  == original_context["prompt"]
        assert enriched["tokens"]  == 500

    def test_graceful_fallback_on_malformed_metadata(self):
        """Corrupted metadata must not crash the micro loop."""
        task = {
            "id": "t1",
            "type": "review",
            "metadata": "{ this is not valid json {{{{",
        }
        context = {"prompt": "Review."}
        enriched = _enrich_review_context(task, context)
        # Must return context unchanged, not raise
        assert enriched["prompt"] == "Review."
        assert "grub_implementation" not in enriched

    def test_does_not_mutate_original_context(self):
        """_enrich_review_context must return a new dict, not mutate the input."""
        task    = self._make_review_task({"status": "success", "score": 0.7})
        context = {"prompt": "Review."}
        _ = _enrich_review_context(task, context)
        assert "grub_implementation" not in context   # original must be untouched

    def test_missing_metadata_key_is_graceful(self):
        """Task with no 'metadata' key at all must not crash."""
        task     = {"id": "t1", "type": "review"}
        enriched = _enrich_review_context(task, {"prompt": "Review."})
        assert "grub_implementation" not in enriched


# ===========================================================================
# 3. Priority and ordering
# ===========================================================================

class TestFetchOrdering:
    """Verify that higher-priority tasks are returned first."""

    def test_high_priority_task_returned_before_low(self, bridge, tasks_db):
        con = sqlite3.connect(str(tasks_db))
        _insert_task(con, id="low-001",  title="Low priority task",  priority_score=0.2)
        _insert_task(con, id="high-001", title="High priority task", priority_score=0.9)
        _insert_task(con, id="mid-001",  title="Medium priority task", priority_score=0.5)
        con.commit()
        con.close()

        tasks = bridge.fetch_implementation_tasks()

        assert len(tasks) == 3
        assert tasks[0].tinker_task_id == "high-001", (
            f"Expected high-priority task first but got {tasks[0].tinker_task_id}"
        )
        assert tasks[-1].tinker_task_id == "low-001"

    def test_fetch_limit_is_respected(self, bridge, tasks_db):
        con = sqlite3.connect(str(tasks_db))
        for i in range(5):
            _insert_task(con, id=f"t-{i}", title=f"Task {i}")
        con.commit()
        con.close()

        tasks = bridge.fetch_implementation_tasks(limit=2)
        assert len(tasks) == 2

    def test_non_implementation_tasks_are_not_fetched(self, bridge, tasks_db):
        """Tasks of type 'design' or 'review' must be ignored."""
        con = sqlite3.connect(str(tasks_db))
        _insert_task(con, id="impl-1", title="Implement X", type="implementation")
        _insert_task(con, id="design-1", title="Design Y",  type="design")
        _insert_task(con, id="review-1", title="Review Z",  type="review")
        con.commit()
        con.close()

        tasks = bridge.fetch_implementation_tasks()
        assert len(tasks) == 1
        assert tasks[0].tinker_task_id == "impl-1"

    def test_completed_tasks_are_not_refetched(self, bridge, tasks_db):
        """Tasks already marked complete must not be returned again."""
        con = sqlite3.connect(str(tasks_db))
        _insert_task(con, id="done-1", title="Already done",
                     type="implementation", status="complete")
        _insert_task(con, id="todo-1", title="Still pending",
                     type="implementation", status="pending")
        con.commit()
        con.close()

        tasks = bridge.fetch_implementation_tasks()
        assert len(tasks) == 1
        assert tasks[0].tinker_task_id == "todo-1"


# ===========================================================================
# 4. Failed / partial results
# ===========================================================================

class TestFailedResults:
    """Verify that Grub reports failed and partial results correctly."""

    def test_failed_result_still_marks_original_complete(self, bridge, tasks_db):
        """Even when a Minion fails, the original task is marked complete to
        prevent it from being re-fetched in an infinite loop."""
        con = sqlite3.connect(str(tasks_db))
        _insert_task(con, id="t-fail", title="Implement X")
        con.commit()
        con.close()

        result = MinionResult(
            task_id     = "grub-1",
            minion_name = "coder",
            status      = ResultStatus.FAILED,
            score       = 0.0,
            summary     = "Could not parse design artifact.",
            notes       = "FileNotFoundError: billing_design.md not found",
        )
        ok = bridge.report_result(result, tinker_task_id="t-fail")
        assert ok is True

        con = sqlite3.connect(str(tasks_db))
        row = con.execute("SELECT status FROM tasks WHERE id='t-fail'").fetchone()
        con.close()
        assert row[0] == "complete"

    def test_failed_result_creates_review_task_so_tinker_can_decide(self, bridge, tasks_db):
        """Even on failure, Tinker gets a review task so it can decide to
        redesign or provide more context."""
        con = sqlite3.connect(str(tasks_db))
        _insert_task(con, id="t-fail", title="Implement X")
        con.commit()
        con.close()

        result = MinionResult(
            task_id     = "grub-1",
            minion_name = "coder",
            status      = ResultStatus.FAILED,
            score       = 0.0,
            summary     = "Failed: design artifact missing.",
        )
        bridge.report_result(result, tinker_task_id="t-fail")

        con = sqlite3.connect(str(tasks_db))
        count = con.execute(
            "SELECT COUNT(*) FROM tasks WHERE type='review'"
        ).fetchone()[0]
        con.close()
        assert count == 1, "Tinker must always get a review task to close the loop"

    def test_partial_result_score_preserved_in_review_task(self, bridge, tasks_db):
        """A partial result's score must be preserved in the review metadata."""
        con = sqlite3.connect(str(tasks_db))
        _insert_task(con, id="t-partial", title="Implement Y")
        con.commit()
        con.close()

        result = MinionResult(
            task_id     = "grub-2",
            minion_name = "coder",
            status      = ResultStatus.PARTIAL,
            score       = 0.45,
            summary     = "3 of 7 requirements implemented.",
        )
        bridge.report_result(result, tinker_task_id="t-partial")

        con = sqlite3.connect(str(tasks_db))
        row = con.execute(
            "SELECT metadata FROM tasks WHERE type='review'"
        ).fetchone()
        con.close()

        meta   = json.loads(row[0])
        gr     = meta["grub_task_result"]
        assert gr["status"]  == "partial"
        assert gr["score"]   == pytest.approx(0.45, abs=0.001)


# ===========================================================================
# 5. Edge cases / error handling
# ===========================================================================

class TestEdgeCases:
    """Verify graceful behaviour under abnormal conditions."""

    def test_report_to_missing_db_returns_false(self, tmp_path):
        bridge = TinkerBridge(
            tinker_tasks_db = str(tmp_path / "ghost.sqlite"),
        )
        result = MinionResult(
            task_id="t", minion_name="coder", status=ResultStatus.SUCCESS,
        )
        ok = bridge.report_result(result, tinker_task_id="any-id")
        assert ok is False

    def test_fetch_from_missing_db_returns_empty_list(self, tmp_path):
        bridge = TinkerBridge(tinker_tasks_db=str(tmp_path / "ghost.sqlite"))
        assert bridge.fetch_implementation_tasks() == []

    def test_write_implementation_note_creates_markdown_file(self, bridge, tasks_db):
        result = MinionResult(
            task_id       = "grub-1",
            minion_name   = "coder",
            status        = ResultStatus.SUCCESS,
            score         = 0.88,
            summary       = "Payment module implemented.",
            files_written = ["payments/core.py", "payments/tests.py"],
            test_results  = TestSummary(passed=8, failed=0, errors=0),
        )
        task = GrubTask(title="Implement payments", description="D",
                        subsystem="payments", id="grub-1")
        path = bridge.write_implementation_note(result, task)

        assert path != ""
        content = Path(path).read_text(encoding="utf-8")
        assert "Payment module implemented" in content
        assert "0.88" in content
        assert "payments/core.py" in content
        assert "Passed: 8" in content

    def test_idempotent_report_does_not_duplicate_review_tasks(self, bridge, tasks_db):
        """Calling report_result twice for the same Tinker task must not create
        two review tasks (INSERT OR IGNORE handles this)."""
        con = sqlite3.connect(str(tasks_db))
        _insert_task(con, id="t-001", title="Implement billing")
        con.commit()
        con.close()

        result = _make_success_result()
        bridge.report_result(result, tinker_task_id="t-001")

        # Simulate a retry / crash-recovery scenario
        bridge.report_result(result, tinker_task_id="t-001")

        con = sqlite3.connect(str(tasks_db))
        count = con.execute(
            "SELECT COUNT(*) FROM tasks WHERE type='review'"
        ).fetchone()[0]
        con.close()
        # INSERT OR IGNORE in feedback.py prevents duplicates
        assert count == 1, (
            f"Expected 1 review task after two reports but got {count}. "
            "Check INSERT OR IGNORE in TinkerBridge.report_result()."
        )
