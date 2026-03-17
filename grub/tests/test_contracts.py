"""
grub/tests/test_contracts.py
============================
Tests for GrubTask and MinionResult data contracts.

These are pure data tests — no LLM calls, no file I/O.
They verify that the contracts serialise/deserialise correctly and that
helper properties work as expected.
"""

import pytest
from grub.contracts.task   import GrubTask, TaskPriority
from grub.contracts.result import MinionResult, ResultStatus, TestSummary


# ═══════════════════════════════════════════════════════════════════════════════
# GrubTask
# ═══════════════════════════════════════════════════════════════════════════════

class TestGrubTask:

    def test_creates_with_required_fields(self):
        task = GrubTask(title="Test task", description="Do something")
        assert task.title       == "Test task"
        assert task.description == "Do something"

    def test_auto_generates_id(self):
        t1 = GrubTask(title="A", description="B")
        t2 = GrubTask(title="A", description="B")
        assert t1.id != t2.id        # each task gets a unique ID
        assert len(t1.id) == 36      # UUID4 format

    def test_default_priority_is_normal(self):
        task = GrubTask(title="T", description="D")
        assert task.priority == TaskPriority.NORMAL

    def test_to_dict_round_trip(self):
        task = GrubTask(
            title         = "Implement router",
            description   = "Write the routing logic",
            artifact_path = "tinker_artifacts/router_design.md",
            target_files  = ["src/router.py"],
            subsystem     = "api_gateway",
            priority      = TaskPriority.HIGH,
            tinker_task_id= "tinker-abc",
            context       = {"framework": "fastapi"},
        )
        d    = task.to_dict()
        task2 = GrubTask.from_dict(d)

        assert task2.title          == task.title
        assert task2.description    == task.description
        assert task2.artifact_path  == task.artifact_path
        assert task2.target_files   == task.target_files
        assert task2.subsystem      == task.subsystem
        assert task2.priority       == TaskPriority.HIGH
        assert task2.tinker_task_id == task.tinker_task_id
        assert task2.context        == task.context
        assert task2.id             == task.id

    def test_from_dict_handles_missing_optional_fields(self):
        """Minimal dict (just required fields) should not raise."""
        d    = {"id": "x", "title": "T", "description": "D", "created_at": "now"}
        task = GrubTask.from_dict(d)
        assert task.title       == "T"
        assert task.target_files == []
        assert task.priority    == TaskPriority.NORMAL


# ═══════════════════════════════════════════════════════════════════════════════
# TestSummary
# ═══════════════════════════════════════════════════════════════════════════════

class TestTestSummary:

    def test_total_sums_all_counts(self):
        ts = TestSummary(passed=5, failed=2, errors=1, skipped=1)
        assert ts.total == 9

    def test_all_passed_when_no_failures_or_errors(self):
        ts = TestSummary(passed=10, failed=0, errors=0)
        assert ts.all_passed is True

    def test_not_all_passed_when_failures(self):
        ts = TestSummary(passed=8, failed=2, errors=0)
        assert ts.all_passed is False

    def test_not_all_passed_when_errors(self):
        ts = TestSummary(passed=8, failed=0, errors=1)
        assert ts.all_passed is False

    def test_to_dict_truncates_long_output(self):
        long_output = "x" * 5000
        ts   = TestSummary(output=long_output)
        d    = ts.to_dict()
        assert len(d["output"]) == 2000   # truncated to 2000 chars


# ═══════════════════════════════════════════════════════════════════════════════
# MinionResult
# ═══════════════════════════════════════════════════════════════════════════════

class TestMinionResult:

    def test_succeeded_property(self):
        r = MinionResult(task_id="t1", minion_name="coder", status=ResultStatus.SUCCESS)
        assert r.succeeded is True

    def test_not_succeeded_when_failed(self):
        r = MinionResult(task_id="t1", minion_name="coder", status=ResultStatus.FAILED)
        assert r.succeeded is False

    def test_needs_retry_for_failed(self):
        r = MinionResult(task_id="t1", minion_name="coder", status=ResultStatus.FAILED)
        assert r.needs_retry is True

    def test_needs_retry_for_needs_retry_status(self):
        r = MinionResult(task_id="t1", minion_name="coder", status=ResultStatus.NEEDS_RETRY)
        assert r.needs_retry is True

    def test_not_needs_retry_for_success(self):
        r = MinionResult(task_id="t1", minion_name="coder", status=ResultStatus.SUCCESS)
        assert r.needs_retry is False

    def test_to_dict_includes_all_fields(self):
        r = MinionResult(
            task_id     = "t1",
            minion_name = "tester",
            status      = ResultStatus.PARTIAL,
            score       = 0.63,
            files_written = ["src/test.py"],
            summary     = "5/8 tests pass",
        )
        d = r.to_dict()
        assert d["task_id"]     == "t1"
        assert d["minion_name"] == "tester"
        assert d["status"]      == "partial"
        assert d["score"]       == 0.63
        assert d["files_written"] == ["src/test.py"]

    def test_score_is_rounded_in_dict(self):
        r = MinionResult(task_id="t", minion_name="r", status=ResultStatus.SUCCESS,
                         score=0.833333333)
        assert r.to_dict()["score"] == 0.833

    def test_notes_truncated_in_dict(self):
        r = MinionResult(task_id="t", minion_name="r", status=ResultStatus.SUCCESS,
                         notes="x" * 5000)
        assert len(r.to_dict()["notes"]) == 4000
