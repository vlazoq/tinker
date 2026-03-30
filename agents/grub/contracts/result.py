"""
agents/grub/contracts/result.py
========================
MinionResult — what every Minion returns to Grub after running.

Why a standard result shape?
-----------------------------
Every Minion (Coder, Tester, Reviewer, Debugger, Refactorer) returns a
MinionResult.  Grub doesn't need to know which Minion ran — it just reads
the result.  This is the 'uniform interface' pattern.

Fields explained
----------------
task_id         : The GrubTask.id this result is for.
minion_name     : Which Minion produced this (e.g. "coder", "tester").
status          : SUCCESS, PARTIAL, FAILED, or NEEDS_RETRY.
score           : 0.0–1.0 quality score (from Reviewer Minion or self-assessed).
files_written   : List of file paths that were created or modified.
test_results    : dict with keys 'passed', 'failed', 'errors' (from Tester).
summary         : Short human-readable summary of what was done.
notes           : Longer notes, reviewer feedback, error messages, etc.
artifacts       : Additional output files (diagrams, reports, etc.).
feedback_for_tinker: Text to inject as a new Tinker task (closes the loop).
iterations      : How many retry loops the Minion needed.
duration_seconds: Wall-clock time taken.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class ResultStatus(StrEnum):
    """
    The outcome of a Minion's run.

    SUCCESS      : Task completed, quality threshold met.
    PARTIAL      : Some work done but not all requirements met.
                   Grub may retry or accept with caveats.
    FAILED       : Minion could not complete the task.
                   Grub will retry up to max_iterations, then DLQ.
    NEEDS_RETRY  : Minion explicitly requests another attempt
                   (e.g. "I need more context to continue").
    SKIPPED      : Task was skipped (e.g. file already exists and is correct).
    """

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    NEEDS_RETRY = "needs_retry"
    SKIPPED = "skipped"


@dataclass
class TestSummary:
    """
    Structured test run results from the Tester Minion.

    Attributes
    ----------
    passed  : Number of tests that passed.
    failed  : Number of tests that failed.
    errors  : Number of tests that errored (different from failed).
    skipped : Number of tests that were skipped.
    output  : Full test runner output (pytest stdout/stderr).
    """

    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    output: str = ""

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errors + self.skipped

    @property
    def all_passed(self) -> bool:
        return self.failed == 0 and self.errors == 0

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "skipped": self.skipped,
            "total": self.total,
            "output": self.output[:2000],  # truncate long output
        }


@dataclass
class MinionResult:
    """
    The standard return value from every Minion.

    Grub reads this to decide:
      - Accept the result (score >= threshold, status == SUCCESS)
      - Retry with feedback (status == NEEDS_RETRY or FAILED)
      - Move to DLQ (max retries exceeded)
      - Feed back to Tinker (feedback_for_tinker is set)

    Example
    -------
    ::

        return MinionResult(
            task_id      = task.id,
            minion_name  = "coder",
            status       = ResultStatus.SUCCESS,
            score        = 0.82,
            files_written= ["api_gateway/router.py"],
            summary      = "Implemented request router with 3 route handlers.",
            feedback_for_tinker = "API gateway router implemented. "
                                  "Consider adding rate limiting middleware.",
        )
    """

    # Mandatory
    task_id: str
    minion_name: str
    status: ResultStatus

    # Quality
    score: float = 0.0  # 0.0 = unusable, 1.0 = perfect

    # Outputs
    files_written: list[str] = field(default_factory=list)
    test_results: TestSummary | None = None
    summary: str = ""
    notes: str = ""  # reviewer feedback, errors, etc.
    artifacts: list[str] = field(default_factory=list)

    # Tinker integration
    feedback_for_tinker: str = ""  # if set, Grub creates a new Tinker task

    # Metadata
    iterations: int = 1
    duration_seconds: float = 0.0
    completed_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    raw_llm_output: str = ""  # full LLM response (for debugging)

    @property
    def succeeded(self) -> bool:
        return self.status == ResultStatus.SUCCESS

    @property
    def needs_retry(self) -> bool:
        return self.status in (ResultStatus.FAILED, ResultStatus.NEEDS_RETRY)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "minion_name": self.minion_name,
            "status": self.status.value,
            "score": round(self.score, 3),
            "files_written": self.files_written,
            "test_results": self.test_results.to_dict() if self.test_results else None,
            "summary": self.summary,
            "notes": self.notes[:4000],
            "artifacts": self.artifacts,
            "feedback_for_tinker": self.feedback_for_tinker,
            "iterations": self.iterations,
            "duration_seconds": round(self.duration_seconds, 2),
            "completed_at": self.completed_at,
        }
