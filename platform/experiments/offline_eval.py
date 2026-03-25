"""
experiments/offline_eval.py
============================
Offline evaluation harness for Tinker prompt variants.

What is offline evaluation?
----------------------------
A/B testing measures live production metrics. Offline evaluation instead
runs a fixed set of (input, expected_output) pairs through a new prompt
and scores the results — before the prompt reaches production.

This prevents regressions: if a new prompt scores 15% lower on the golden
set, you know before deploying it.

Components
----------
EvalCase     : A single (task, context, expected_output) test case.
EvalSet      : A named collection of EvalCases (the "golden set").
EvalResult   : The output + score for one EvalCase.
EvalReport   : Aggregate results for one run over an EvalSet.
OfflineEvaluator : Runs an EvalSet through a model callable and scores results.

Scoring
-------
The default scorer uses string overlap (Jaccard similarity on tokens).
A ``judge_fn`` can be injected for LLM-as-judge scoring:
  score = await judge_fn(task, expected, actual)  # returns float 0-1

Usage
-----
::

    evaluator = OfflineEvaluator(model_fn=my_model_call)
    eval_set = EvalSet.from_file("golden_set.json")
    report = await evaluator.run(eval_set)
    print(report.summary())
    assert report.mean_score >= 0.6, "Regression detected"
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# EvalCase
# ---------------------------------------------------------------------------


@dataclass
class EvalCase:
    """
    A single evaluation test case.

    Fields
    ------
    id              : Unique identifier for this case.  Auto-generated as a
                      UUID if not supplied.
    task            : The task dict passed to the model callable.
    context         : Optional additional context dict (e.g. retrieved docs).
    expected_output : The gold-standard output to compare against.
    tags            : Arbitrary labels for filtering (e.g. ["regression", "hard"]).
    metadata        : Free-form dict for extra case-level information.
    """

    task: dict
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    context: dict = field(default_factory=dict)
    expected_output: str = ""
    tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# EvalSet
# ---------------------------------------------------------------------------


@dataclass
class EvalSet:
    """
    A named collection of EvalCases — the "golden set".

    Fields
    ------
    name  : Human-readable name for this eval set (e.g. "architect_v1").
    cases : The list of EvalCase objects in this set.
    """

    name: str
    cases: list[EvalCase] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    @classmethod
    def from_file(cls, path: str) -> "EvalSet":
        """
        Load an EvalSet from a JSON file.

        The file must contain a JSON object with the shape::

            {
                "name": "my_eval_set",
                "cases": [
                    {
                        "id": "...",
                        "task": {...},
                        "context": {...},
                        "expected_output": "...",
                        "tags": [...],
                        "metadata": {...}
                    },
                    ...
                ]
            }

        Alternatively the file may contain a bare JSON array of case dicts,
        in which case the eval set name is derived from the file path.

        Parameters
        ----------
        path : Absolute or relative path to the JSON file.

        Returns
        -------
        EvalSet : The loaded eval set.
        """
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)

        if isinstance(raw, list):
            # Bare array of case dicts — derive name from filename
            import os
            name = os.path.splitext(os.path.basename(path))[0]
            case_dicts = raw
        else:
            name = raw.get("name", path)
            case_dicts = raw.get("cases", [])

        cases = [
            EvalCase(
                id=c.get("id", str(uuid.uuid4())),
                task=c.get("task", {}),
                context=c.get("context", {}),
                expected_output=c.get("expected_output", ""),
                tags=c.get("tags", []),
                metadata=c.get("metadata", {}),
            )
            for c in case_dicts
        ]
        return cls(name=name, cases=cases)

    def to_file(self, path: str) -> None:
        """
        Persist this EvalSet to a JSON file.

        The output format is compatible with ``from_file``.

        Parameters
        ----------
        path : Destination file path.  The file is created or overwritten.
        """
        data = {
            "name": self.name,
            "cases": [
                {
                    "id": c.id,
                    "task": c.task,
                    "context": c.context,
                    "expected_output": c.expected_output,
                    "tags": c.tags,
                    "metadata": c.metadata,
                }
                for c in self.cases
            ],
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def filter_by_tag(self, tag: str) -> "EvalSet":
        """
        Return a new EvalSet containing only cases that carry the given tag.

        Parameters
        ----------
        tag : The tag string to filter on (case-sensitive).

        Returns
        -------
        EvalSet : A new EvalSet with the same name but filtered cases.
        """
        filtered = [c for c in self.cases if tag in c.tags]
        return EvalSet(name=self.name, cases=filtered)


# ---------------------------------------------------------------------------
# EvalResult
# ---------------------------------------------------------------------------


@dataclass
class EvalResult:
    """
    The output and score for a single EvalCase after one evaluation run.

    Fields
    ------
    case_id       : The ``id`` of the EvalCase this result belongs to.
    actual_output : The raw string the model produced.
    score         : Numeric score in [0.0, 1.0].
    judge         : Name of the scoring function used (e.g. "token_overlap").
    latency_ms    : Wall-clock time taken for the model call, in milliseconds.
    error         : Non-empty if the model call failed; contains the error message.
    """

    case_id: str
    actual_output: str
    score: float
    judge: str = "token_overlap"
    latency_ms: float = 0.0
    error: str = ""


# ---------------------------------------------------------------------------
# EvalReport
# ---------------------------------------------------------------------------


@dataclass
class EvalReport:
    """
    Aggregate results for one evaluation run over an EvalSet.

    Fields
    ------
    eval_set_name : Name of the EvalSet that was evaluated.
    results       : Per-case EvalResult objects.
    run_at        : UTC timestamp when the run completed.
    """

    eval_set_name: str
    results: list[EvalResult] = field(default_factory=list)
    run_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------
    # Aggregate metrics
    # ------------------------------------------------------------------

    @property
    def mean_score(self) -> float:
        """
        Mean score across all evaluated cases.

        Cases with errors are included with a score of 0.0, which penalises
        runs that partially fail.  Returns 0.0 if there are no results.
        """
        if not self.results:
            return 0.0
        return sum(r.score for r in self.results) / len(self.results)

    def pass_rate(self, threshold: float = 0.5) -> float:
        """
        Fraction of cases that scored at or above ``threshold``.

        Parameters
        ----------
        threshold : Minimum score to count as a pass (default: 0.5).

        Returns
        -------
        float : Value in [0.0, 1.0].  Returns 0.0 if there are no results.
        """
        if not self.results:
            return 0.0
        passed = sum(1 for r in self.results if r.score >= threshold)
        return passed / len(self.results)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def summary(self) -> str:
        """
        Return a one-paragraph human-readable summary of the evaluation run.

        Includes the eval set name, number of cases, mean score, pass rate
        (at 0.5 threshold), error count, and the UTC timestamp of the run.
        """
        n = len(self.results)
        errors = sum(1 for r in self.results if r.error)
        avg_latency = (
            sum(r.latency_ms for r in self.results) / n if n else 0.0
        )
        return (
            f"EvalReport for '{self.eval_set_name}': {n} case(s) evaluated "
            f"at {self.run_at.isoformat()}. "
            f"Mean score: {self.mean_score:.3f}. "
            f"Pass rate (>=0.5): {self.pass_rate():.1%}. "
            f"Errors: {errors}. "
            f"Avg latency: {avg_latency:.0f} ms."
        )

    def to_dict(self) -> dict:
        """
        Serialise the report to a plain dictionary suitable for JSON output.

        Returns
        -------
        dict : Report data including aggregate metrics and per-case results.
        """
        return {
            "eval_set_name": self.eval_set_name,
            "run_at": self.run_at.isoformat(),
            "mean_score": self.mean_score,
            "pass_rate": self.pass_rate(),
            "total_cases": len(self.results),
            "error_count": sum(1 for r in self.results if r.error),
            "results": [
                {
                    "case_id": r.case_id,
                    "actual_output": r.actual_output,
                    "score": r.score,
                    "judge": r.judge,
                    "latency_ms": r.latency_ms,
                    "error": r.error,
                }
                for r in self.results
            ],
        }


# ---------------------------------------------------------------------------
# OfflineEvaluator
# ---------------------------------------------------------------------------


class OfflineEvaluator:
    """
    Runs an EvalSet through a model callable and scores the results.

    Parameters
    ----------
    model_fn    : Async callable with signature
                  ``async (task: dict, context: dict) -> str``.
                  Called once per EvalCase to obtain the model's output.
    judge_fn    : Optional async callable with signature
                  ``async (task: dict, expected: str, actual: str) -> float``.
                  When provided it is used instead of the default Jaccard scorer.
    concurrency : Maximum number of EvalCases to evaluate in parallel.
                  Controlled via an ``asyncio.Semaphore``.  Default: 4.
    """

    def __init__(
        self,
        model_fn: Callable,
        judge_fn: Optional[Callable] = None,
        concurrency: int = 4,
    ) -> None:
        self._model_fn = model_fn
        self._judge_fn = judge_fn
        self._concurrency = concurrency

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def run(
        self,
        eval_set: EvalSet,
        timeout_per_case: float = 30.0,
    ) -> EvalReport:
        """
        Evaluate every case in ``eval_set`` and return an EvalReport.

        Cases are executed in parallel up to ``self._concurrency`` at a time.
        Each case is wrapped in ``asyncio.wait_for`` with ``timeout_per_case``
        seconds.  Timed-out or errored cases receive a score of 0.0 and have
        their ``error`` field populated.

        Parameters
        ----------
        eval_set          : The EvalSet to run.
        timeout_per_case  : Per-case timeout in seconds.  Default: 30.0.

        Returns
        -------
        EvalReport : Aggregate results for the run.
        """
        semaphore = asyncio.Semaphore(self._concurrency)
        tasks = [
            self._run_case(case, semaphore, timeout_per_case)
            for case in eval_set.cases
        ]
        results: list[EvalResult] = await asyncio.gather(*tasks)
        return EvalReport(eval_set_name=eval_set.name, results=list(results))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run_case(
        self,
        case: EvalCase,
        semaphore: asyncio.Semaphore,
        timeout_per_case: float,
    ) -> EvalResult:
        """
        Evaluate a single EvalCase under the semaphore, returning an EvalResult.

        Catches all exceptions so that one failing case does not abort the run.
        """
        async with semaphore:
            t0 = time.monotonic()
            try:
                actual: str = await asyncio.wait_for(
                    self._model_fn(case.task, case.context),
                    timeout=timeout_per_case,
                )
                latency_ms = (time.monotonic() - t0) * 1000.0

                # Score the result
                if self._judge_fn is not None:
                    score = float(
                        await self._judge_fn(case.task, case.expected_output, actual)
                    )
                    judge_name = getattr(self._judge_fn, "__name__", "judge_fn")
                else:
                    score = self._score_result(case.expected_output, actual)
                    judge_name = "token_overlap"

                return EvalResult(
                    case_id=case.id,
                    actual_output=actual,
                    score=score,
                    judge=judge_name,
                    latency_ms=latency_ms,
                )

            except asyncio.TimeoutError:
                latency_ms = (time.monotonic() - t0) * 1000.0
                logger.warning("EvalCase %s timed out after %.1fs", case.id, timeout_per_case)
                return EvalResult(
                    case_id=case.id,
                    actual_output="",
                    score=0.0,
                    latency_ms=latency_ms,
                    error=f"Timed out after {timeout_per_case}s",
                )
            except Exception as exc:  # noqa: BLE001
                latency_ms = (time.monotonic() - t0) * 1000.0
                logger.warning("EvalCase %s failed: %s", case.id, exc)
                return EvalResult(
                    case_id=case.id,
                    actual_output="",
                    score=0.0,
                    latency_ms=latency_ms,
                    error=str(exc),
                )

    @staticmethod
    def _score_result(expected: str, actual: str) -> float:
        """
        Compute Jaccard similarity between ``expected`` and ``actual`` on word tokens.

        Jaccard similarity = |intersection| / |union| where the sets are the
        unique words in each string (case-sensitive).

        Returns
        -------
        float : Score in [0.0, 1.0].  Returns 0.0 if both strings are empty.
        """
        a_tokens = set(expected.split())
        b_tokens = set(actual.split())
        intersection = a_tokens & b_tokens
        union = a_tokens | b_tokens
        if not union:
            return 0.0
        return len(intersection) / len(union)
