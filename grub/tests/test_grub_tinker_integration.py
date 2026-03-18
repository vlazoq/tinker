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

Test classes
------------
  TestFullLoop                — happy path round-trips
  TestEnrichReviewContext     — _enrich_review_context unit tests
  TestFetchOrdering           — priority + limit + type/status filtering
  TestFailedResults           — failed/partial MinionResult handling
  TestEdgeCases               — missing DB, malformed metadata, idempotency
  TestWALMode                 — SQLite WAL journal mode is active
  TestConcurrentWrites        — thread-safety of report_result under load
  TestConcurrentFetchReport   — interleaved reads + writes don't deadlock
  TestArtifactDiscovery       — _find_artifact file-system lookup
  TestDatabasePersistence     — data survives across separate connections

What is NOT tested here
-----------------------
- Live Ollama calls (use the orchestrator integration test for that)
- Redis or DuckDB (those are unit-tested in memory/test_memory_manager.py)
- The Minion implementation itself (tested in test_minion_base.py)
"""

from __future__ import annotations

import json
import sqlite3
import threading
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
    """
    An empty Tinker tasks database with the correct schema.

    WAL mode is enabled here to match production behaviour: Tinker and Grub
    both access this file from separate processes (or threads in tests), and
    WAL mode is required to avoid "database is locked" errors under concurrent
    reads + writes.
    """
    db = tmp_path / "tinker_tasks.sqlite"
    con = sqlite3.connect(str(db))
    con.execute("PRAGMA journal_mode=WAL")   # enable once; persists in the DB file
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


# ===========================================================================
# 6. WAL mode verification
# ===========================================================================

class TestWALMode:
    """
    Verify that SQLite WAL (Write-Ahead Logging) journal mode is active.

    WAL mode is mandatory for Tinker/Grub production use: both processes
    open the same database file simultaneously.  Default DELETE journal mode
    serialises all access with file-level locks, causing frequent
    "database is locked" errors.  WAL allows concurrent readers + one writer,
    which is the access pattern here.

    These tests confirm that:
    1. The tasks_db fixture creates the DB in WAL mode.
    2. TinkerBridge.report_result() keeps the DB in WAL mode.
    3. TinkerBridge.fetch_implementation_tasks() keeps the DB in WAL mode.
    """

    def _journal_mode(self, db_path: str) -> str:
        con = sqlite3.connect(db_path)
        row = con.execute("PRAGMA journal_mode").fetchone()
        con.close()
        return row[0].lower()

    def test_fixture_creates_wal_database(self, tasks_db):
        assert self._journal_mode(str(tasks_db)) == "wal", (
            "tasks_db fixture must create a WAL-mode database. "
            "Add PRAGMA journal_mode=WAL to the fixture."
        )

    def test_report_result_preserves_wal_mode(self, bridge, tasks_db):
        con = sqlite3.connect(str(tasks_db))
        _insert_task(con, id="t-wal", title="WAL test task")
        con.commit()
        con.close()

        result = MinionResult(
            task_id="g-wal", minion_name="coder",
            status=ResultStatus.SUCCESS, score=0.9, summary="WAL test.",
        )
        bridge.report_result(result, tinker_task_id="t-wal")

        assert self._journal_mode(str(tasks_db)) == "wal", (
            "TinkerBridge.report_result() must not switch the DB out of WAL mode. "
            "Ensure PRAGMA journal_mode=WAL is set on every connection."
        )

    def test_fetch_implementation_tasks_preserves_wal_mode(self, bridge, tasks_db):
        bridge.fetch_implementation_tasks()
        assert self._journal_mode(str(tasks_db)) == "wal"


# ===========================================================================
# 7. Concurrent writes
# ===========================================================================

class TestConcurrentWrites:
    """
    Thread-safety of TinkerBridge under concurrent load.

    Production scenario: Grub may run multiple workers in parallel (one per
    Minion pipeline) all sharing a single TinkerBridge / SQLite file.
    These tests verify that:

    a) N workers each reporting to a DIFFERENT task all succeed — no data
       loss, no corruption, all N tasks reach status='complete'.
    b) N workers all reporting to the SAME task create exactly 1 review task
       — INSERT OR IGNORE deduplication works under concurrent load, not just
       in serial retries.
    c) Interleaved fetch + report from multiple threads never deadlocks or
       raises unexpected exceptions.

    Thread count is intentionally high (10–20) to expose race conditions that
    lower counts might miss.
    """

    @staticmethod
    def _make_bridge(tasks_db: Path, tmp_path: Path) -> TinkerBridge:
        return TinkerBridge(
            tinker_tasks_db      = str(tasks_db),
            tinker_artifacts_dir = str(tmp_path / "ta"),
            grub_artifacts_dir   = str(tmp_path / "ga"),
        )

    def test_concurrent_reports_different_tasks_all_succeed(self, tasks_db, tmp_path):
        """
        20 threads each report to a different task.
        All 20 tasks must reach status='complete' and exactly 20 review tasks
        must exist — no writes lost, no duplicates.
        """
        N = 20
        con = sqlite3.connect(str(tasks_db))
        for i in range(N):
            _insert_task(con, id=f"conc-diff-{i:03d}", title=f"Task {i}")
        con.commit()
        con.close()

        bridge = self._make_bridge(tasks_db, tmp_path)
        errors: list[Exception] = []
        ok_flags: list[bool | None] = [None] * N

        def report(idx: int) -> None:
            try:
                r = MinionResult(
                    task_id     = f"grub-diff-{idx}",
                    minion_name = "coder",
                    status      = ResultStatus.SUCCESS,
                    score       = 0.7 + idx * 0.01,
                    summary     = f"Implemented task {idx}.",
                )
                ok_flags[idx] = bridge.report_result(r, tinker_task_id=f"conc-diff-{idx:03d}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=report, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Exceptions in concurrent threads: {errors}"
        assert all(f is True for f in ok_flags), (
            f"Some report_result() calls returned False: {ok_flags}"
        )

        con = sqlite3.connect(str(tasks_db))
        complete_count = con.execute(
            f"SELECT COUNT(*) FROM tasks WHERE status='complete' AND id LIKE 'conc-diff-%'"
        ).fetchone()[0]
        review_count = con.execute(
            f"SELECT COUNT(*) FROM tasks WHERE type='review' AND id LIKE 'review-conc-diff-%'"
        ).fetchone()[0]
        con.close()

        assert complete_count == N, (
            f"Expected {N} tasks marked complete; got {complete_count}. "
            "Some concurrent writes were lost."
        )
        assert review_count == N, (
            f"Expected {N} review tasks; got {review_count}. "
            "Some concurrent INSERTs were lost or deduplicated incorrectly."
        )

    def test_concurrent_reports_same_task_no_duplicate_review(self, tasks_db, tmp_path):
        """
        10 threads all report results for the same Tinker task simultaneously.
        INSERT OR IGNORE must ensure exactly 1 review task is created — never
        more — regardless of race conditions in the commit order.
        """
        N_THREADS = 10
        con = sqlite3.connect(str(tasks_db))
        _insert_task(con, id="shared-task", title="Shared implementation task")
        con.commit()
        con.close()

        bridge = self._make_bridge(tasks_db, tmp_path)
        errors: list[Exception] = []

        def report() -> None:
            try:
                r = MinionResult(
                    task_id     = "grub-shared",
                    minion_name = "coder",
                    status      = ResultStatus.SUCCESS,
                    score       = 0.85,
                    summary     = "Shared task completed.",
                )
                bridge.report_result(r, tinker_task_id="shared-task")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=report) for _ in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, (
            f"Unexpected exceptions from {N_THREADS} concurrent reports: {errors}"
        )

        con = sqlite3.connect(str(tasks_db))
        review_count = con.execute(
            "SELECT COUNT(*) FROM tasks WHERE type='review'"
        ).fetchone()[0]
        con.close()

        assert review_count == 1, (
            f"Expected exactly 1 review task after {N_THREADS} concurrent reports "
            f"but got {review_count}. "
            "INSERT OR IGNORE deduplication failed under concurrent load. "
            "Verify PRAGMA journal_mode=WAL is active (see TestWALMode)."
        )

    def test_concurrent_reports_data_integrity(self, tasks_db, tmp_path):
        """
        After N concurrent reports, verify that no row is partially written:
        every review task must have valid JSON in the metadata column and a
        non-empty 'grub_task_result' key.
        """
        N = 10
        con = sqlite3.connect(str(tasks_db))
        for i in range(N):
            _insert_task(con, id=f"integ-{i:03d}", title=f"Integrity task {i}")
        con.commit()
        con.close()

        bridge = self._make_bridge(tasks_db, tmp_path)
        errors: list[Exception] = []

        def report(idx: int) -> None:
            try:
                r = MinionResult(
                    task_id     = f"grub-integ-{idx}",
                    minion_name = "coder",
                    status      = ResultStatus.SUCCESS,
                    score       = 0.8,
                    summary     = f"Integrity test {idx}.",
                    files_written = [f"module_{idx}.py"],
                )
                bridge.report_result(r, tinker_task_id=f"integ-{idx:03d}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=report, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Thread exceptions: {errors}"

        con = sqlite3.connect(str(tasks_db))
        rows = con.execute(
            "SELECT id, metadata FROM tasks WHERE type='review'"
        ).fetchall()
        con.close()

        assert len(rows) == N, f"Expected {N} review tasks; got {len(rows)}"

        for row_id, metadata_str in rows:
            # Metadata must be valid JSON
            try:
                meta = json.loads(metadata_str)
            except json.JSONDecodeError as e:
                raise AssertionError(
                    f"Row {row_id} has corrupt (non-JSON) metadata: {metadata_str!r}"
                ) from e

            # The grub_task_result key must be present and non-empty
            grub_result = meta.get("grub_task_result")
            assert grub_result is not None, (
                f"Row {row_id} is missing 'grub_task_result' in metadata"
            )
            assert grub_result.get("status") == "success", (
                f"Row {row_id} has wrong status in grub_task_result: {grub_result}"
            )


# ===========================================================================
# 8. Concurrent fetch + report interleaved
# ===========================================================================

class TestConcurrentFetchReport:
    """
    Interleaved reads (fetch_implementation_tasks) and writes (report_result)
    from multiple threads must not deadlock, corrupt data, or raise unexpected
    exceptions.

    This simulates the production scenario of Tinker continuously reading tasks
    while Grub workers simultaneously write results — all sharing the same SQLite
    file over a WAL-mode connection.
    """

    def test_interleaved_fetch_and_report_no_deadlock(self, tasks_db, tmp_path):
        """
        3 reader threads each doing 10 fetches, and 5 writer threads each
        reporting one task, all running simultaneously.

        No deadlocks (all threads must complete within the timeout), no
        exceptions from either readers or writers.
        """
        N_TASKS     = 5
        N_READERS   = 3
        READS_EACH  = 10

        bridge = TinkerBridge(
            tinker_tasks_db      = str(tasks_db),
            tinker_artifacts_dir = str(tmp_path / "ta"),
            grub_artifacts_dir   = str(tmp_path / "ga"),
        )

        con = sqlite3.connect(str(tasks_db))
        for i in range(N_TASKS):
            _insert_task(con, id=f"inter-{i:03d}", title=f"Interleaved task {i}")
        con.commit()
        con.close()

        fetch_errors:  list[Exception] = []
        report_errors: list[Exception] = []

        def do_fetch() -> None:
            for _ in range(READS_EACH):
                try:
                    bridge.fetch_implementation_tasks()
                except Exception as exc:
                    fetch_errors.append(exc)

        def do_report(idx: int) -> None:
            try:
                r = MinionResult(
                    task_id     = f"grub-inter-{idx}",
                    minion_name = "coder",
                    status      = ResultStatus.SUCCESS,
                    score       = 0.75,
                    summary     = f"Interleaved task {idx} done.",
                )
                bridge.report_result(r, tinker_task_id=f"inter-{idx:03d}")
            except Exception as exc:
                report_errors.append(exc)

        threads = (
            [threading.Thread(target=do_fetch)           for _ in range(N_READERS)] +
            [threading.Thread(target=do_report, args=(i,)) for i in range(N_TASKS)]
        )
        for t in threads:
            t.start()
        alive = [t.join(timeout=30) or t.is_alive() for t in threads]

        assert not any(alive), (
            "At least one thread did not complete within 30 s — possible deadlock. "
            "Check journal_mode=WAL is active on all connections."
        )
        assert not fetch_errors,  f"Reader thread exceptions: {fetch_errors}"
        assert not report_errors, f"Writer thread exceptions: {report_errors}"

    def test_completed_tasks_not_returned_after_concurrent_report(self, tasks_db, tmp_path):
        """
        After concurrent reports mark tasks as complete, subsequent fetches
        must not return those tasks — no stale reads from WAL snapshots.
        """
        N = 8
        bridge = TinkerBridge(
            tinker_tasks_db      = str(tasks_db),
            tinker_artifacts_dir = str(tmp_path / "ta"),
            grub_artifacts_dir   = str(tmp_path / "ga"),
        )

        con = sqlite3.connect(str(tasks_db))
        for i in range(N):
            _insert_task(con, id=f"stale-{i:03d}", title=f"Stale task {i}")
        con.commit()
        con.close()

        # Concurrently report all tasks complete
        def report(idx: int) -> None:
            r = MinionResult(
                task_id="g", minion_name="coder",
                status=ResultStatus.SUCCESS, score=0.8, summary="done",
            )
            bridge.report_result(r, tinker_task_id=f"stale-{idx:03d}")

        threads = [threading.Thread(target=report, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        # All fetches after this point must see zero pending implementation tasks
        remaining = bridge.fetch_implementation_tasks()
        stale = [t for t in remaining if t.tinker_task_id.startswith("stale-")]
        assert not stale, (
            f"After concurrent reports, fetch returned completed tasks: "
            f"{[t.tinker_task_id for t in stale]}"
        )


# ===========================================================================
# 9. Artifact discovery
# ===========================================================================

class TestArtifactDiscovery:
    """
    Tests for TinkerBridge._find_artifact() — the method that locates design
    documents in tinker_artifacts/ when no explicit artifact_path is set in
    the task metadata.

    Tinker writes design artifacts with names like:
      billing_design.md, auth_gateway_design.md, api_design.md
    Grub must be able to find them by subsystem name even when the exact path
    is not stored in the task row.
    """

    def _bridge_with_artifacts(self, tmp_path: Path) -> TinkerBridge:
        """Create a bridge pointing at a real tmp_path artifacts directory."""
        art_dir = tmp_path / "tinker_artifacts"
        art_dir.mkdir(parents=True, exist_ok=True)
        db = tmp_path / "tasks.sqlite"
        con = sqlite3.connect(str(db))
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(_FULL_SCHEMA)
        con.commit()
        con.close()
        return TinkerBridge(
            tinker_tasks_db      = str(db),
            tinker_artifacts_dir = str(art_dir),
            grub_artifacts_dir   = str(tmp_path / "ga"),
        )

    def test_finds_exact_design_file(self, tmp_path):
        bridge = self._bridge_with_artifacts(tmp_path)
        (tmp_path / "tinker_artifacts" / "billing_design.md").write_text("# Billing")
        path = bridge._find_artifact("billing")
        assert path != ""
        assert "billing_design.md" in path

    def test_finds_glob_match(self, tmp_path):
        bridge = self._bridge_with_artifacts(tmp_path)
        (tmp_path / "tinker_artifacts" / "auth_v2_design.md").write_text("# Auth")
        path = bridge._find_artifact("auth")
        assert path != ""
        assert "auth" in path

    def test_subsystem_with_spaces_normalized(self, tmp_path):
        bridge = self._bridge_with_artifacts(tmp_path)
        (tmp_path / "tinker_artifacts" / "api_gateway_design.md").write_text("# API GW")
        path = bridge._find_artifact("api gateway")
        assert path != ""
        assert "api_gateway" in path

    def test_no_match_returns_empty_string(self, tmp_path):
        bridge = self._bridge_with_artifacts(tmp_path)
        path = bridge._find_artifact("nonexistent_module")
        assert path == ""

    def test_empty_subsystem_returns_empty_string(self, tmp_path):
        bridge = self._bridge_with_artifacts(tmp_path)
        assert bridge._find_artifact("") == ""

    def test_missing_artifacts_dir_returns_empty_string(self, tmp_path):
        """If tinker_artifacts/ does not exist, return "" gracefully."""
        db = tmp_path / "tasks.sqlite"
        con = sqlite3.connect(str(db))
        con.executescript(_FULL_SCHEMA)
        con.commit()
        con.close()
        bridge = TinkerBridge(
            tinker_tasks_db      = str(db),
            tinker_artifacts_dir = str(tmp_path / "nonexistent_dir"),
            grub_artifacts_dir   = str(tmp_path / "ga"),
        )
        assert bridge._find_artifact("billing") == ""


# ===========================================================================
# 10. Database persistence
# ===========================================================================

class TestDatabasePersistence:
    """
    Verify that writes to the file-based SQLite database survive across
    separate connection objects — i.e., data is actually persisted to disk
    and not held only in memory or in an uncommitted transaction.

    This distinguishes a real file-based DB from the `:memory:` pattern and
    catches bugs where commits are missing or connections are shared
    incorrectly between threads.
    """

    def test_completed_status_visible_from_new_connection(self, bridge, tasks_db):
        """
        After report_result() closes its connection, a brand-new connection
        opened directly on the same file must see status='complete'.
        """
        con = sqlite3.connect(str(tasks_db))
        _insert_task(con, id="persist-1", title="Persist test")
        con.commit()
        con.close()

        result = MinionResult(
            task_id="g-1", minion_name="coder",
            status=ResultStatus.SUCCESS, score=0.9, summary="Persisted.",
        )
        bridge.report_result(result, tinker_task_id="persist-1")

        # Open a completely new connection — no shared state with bridge
        con2 = sqlite3.connect(str(tasks_db))
        row  = con2.execute("SELECT status FROM tasks WHERE id='persist-1'").fetchone()
        con2.close()
        assert row is not None
        assert row[0] == "complete"

    def test_review_task_visible_from_new_connection(self, bridge, tasks_db):
        con = sqlite3.connect(str(tasks_db))
        _insert_task(con, id="persist-2", title="Persist review test")
        con.commit()
        con.close()

        result = MinionResult(
            task_id="g-2", minion_name="coder",
            status=ResultStatus.SUCCESS, score=0.85, summary="Review test.",
        )
        bridge.report_result(result, tinker_task_id="persist-2")

        con2 = sqlite3.connect(str(tasks_db))
        review = con2.execute(
            "SELECT id, type, status FROM tasks WHERE type='review'"
        ).fetchone()
        con2.close()

        assert review is not None, "Review task not found via fresh connection"
        assert review[1] == "review"
        assert review[2] == "pending"

    def test_fetch_result_persists_across_bridge_instances(self, tasks_db, tmp_path):
        """
        Two separate TinkerBridge instances pointing to the same DB file must
        observe each other's writes — there is no per-instance in-memory cache.
        """
        bridge_a = TinkerBridge(
            tinker_tasks_db      = str(tasks_db),
            tinker_artifacts_dir = str(tmp_path / "ta"),
            grub_artifacts_dir   = str(tmp_path / "ga_a"),
        )
        bridge_b = TinkerBridge(
            tinker_tasks_db      = str(tasks_db),
            tinker_artifacts_dir = str(tmp_path / "ta"),
            grub_artifacts_dir   = str(tmp_path / "ga_b"),
        )

        con = sqlite3.connect(str(tasks_db))
        _insert_task(con, id="cross-1", title="Cross-bridge task")
        con.commit()
        con.close()

        # Bridge A reports
        result = MinionResult(
            task_id="g-cross", minion_name="coder",
            status=ResultStatus.SUCCESS, score=0.8, summary="Cross-bridge done.",
        )
        ok = bridge_a.report_result(result, tinker_task_id="cross-1")
        assert ok is True

        # Bridge B fetches — must see zero pending implementation tasks (the one
        # task is now 'complete') and must see the review task instead.
        pending = bridge_b.fetch_implementation_tasks()
        assert not any(t.tinker_task_id == "cross-1" for t in pending), (
            "bridge_b still sees a task that bridge_a marked complete"
        )

        con2 = sqlite3.connect(str(tasks_db))
        review = con2.execute(
            "SELECT COUNT(*) FROM tasks WHERE type='review'"
        ).fetchone()[0]
        con2.close()
        assert review == 1, "Review task written by bridge_a not visible to bridge_b"
