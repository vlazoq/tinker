"""
Tests for experiments/offline_eval.py
======================================
Unit tests for EvalCase, EvalSet, EvalReport, OfflineEvaluator, and the
Jaccard scorer.  No real LLM or network calls.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from platform.experiments.offline_eval import (
    EvalCase,
    EvalSet,
    EvalReport,
    EvalResult,
    OfflineEvaluator,
)


# ---------------------------------------------------------------------------
# EvalCase
# ---------------------------------------------------------------------------


class TestEvalCase:
    def test_auto_generated_id(self):
        case = EvalCase(task={"description": "test"})
        assert case.id is not None
        assert len(case.id) > 0

    def test_id_is_unique(self):
        c1 = EvalCase(task={})
        c2 = EvalCase(task={})
        assert c1.id != c2.id

    def test_explicit_id_preserved(self):
        case = EvalCase(task={}, id="my-id-123")
        assert case.id == "my-id-123"

    def test_defaults(self):
        case = EvalCase(task={"x": 1})
        assert case.expected_output == ""
        assert case.tags == []
        assert case.context == {}
        assert case.metadata == {}


# ---------------------------------------------------------------------------
# EvalSet
# ---------------------------------------------------------------------------


class TestEvalSet:
    def _make_set(self, n: int = 3, tags=None) -> EvalSet:
        cases = [
            EvalCase(task={"i": i}, tags=tags or [], expected_output=f"out{i}")
            for i in range(n)
        ]
        return EvalSet(name="test_set", cases=cases)

    def test_filter_by_tag_returns_subset(self):
        cases = [
            EvalCase(task={"i": 0}, tags=["regression"]),
            EvalCase(task={"i": 1}, tags=["smoke"]),
            EvalCase(task={"i": 2}, tags=["regression", "hard"]),
        ]
        es = EvalSet(name="mixed", cases=cases)
        filtered = es.filter_by_tag("regression")
        assert len(filtered.cases) == 2
        assert filtered.name == "mixed"

    def test_filter_by_tag_empty_match(self):
        es = self._make_set()
        filtered = es.filter_by_tag("nonexistent_tag")
        assert len(filtered.cases) == 0

    def test_from_file_to_file_roundtrip(self, tmp_path):
        cases = [
            EvalCase(task={"x": i}, expected_output=f"res{i}", tags=["t"])
            for i in range(4)
        ]
        original = EvalSet(name="my_set", cases=cases)
        path = str(tmp_path / "eval.json")
        original.to_file(path)

        loaded = EvalSet.from_file(path)
        assert loaded.name == "my_set"
        assert len(loaded.cases) == 4
        for orig, reloaded in zip(cases, loaded.cases):
            assert reloaded.id == orig.id
            assert reloaded.expected_output == orig.expected_output
            assert reloaded.tags == orig.tags

    def test_from_file_bare_array(self, tmp_path):
        """from_file handles a bare JSON array (no 'name' key)."""
        cases_data = [
            {"id": "a", "task": {"x": 1}, "expected_output": "y", "tags": []}
        ]
        path = str(tmp_path / "bare.json")
        with open(path, "w") as fh:
            json.dump(cases_data, fh)
        es = EvalSet.from_file(path)
        assert es.name == "bare"  # derived from filename
        assert len(es.cases) == 1
        assert es.cases[0].id == "a"


# ---------------------------------------------------------------------------
# EvalReport
# ---------------------------------------------------------------------------


class TestEvalReport:
    def _report(self, scores: list[float]) -> EvalReport:
        results = [
            EvalResult(case_id=str(i), actual_output="x", score=s)
            for i, s in enumerate(scores)
        ]
        return EvalReport(eval_set_name="test", results=results)

    def test_mean_score_averages_correctly(self):
        report = self._report([0.6, 0.8, 1.0])
        assert abs(report.mean_score - 0.8) < 1e-6

    def test_mean_score_empty_returns_zero(self):
        report = EvalReport(eval_set_name="empty")
        assert report.mean_score == 0.0

    def test_pass_rate_counts_above_threshold(self):
        report = self._report([0.0, 0.5, 0.5, 1.0])
        assert report.pass_rate(threshold=0.5) == pytest.approx(0.75)

    def test_pass_rate_empty_returns_zero(self):
        assert EvalReport(eval_set_name="x").pass_rate() == 0.0

    def test_pass_rate_all_pass(self):
        report = self._report([1.0, 0.9, 0.8])
        assert report.pass_rate(threshold=0.5) == 1.0

    def test_pass_rate_none_pass(self):
        report = self._report([0.1, 0.2])
        assert report.pass_rate(threshold=0.5) == 0.0


# ---------------------------------------------------------------------------
# Jaccard scorer
# ---------------------------------------------------------------------------


class TestJaccardScorer:
    def test_identical_strings_score_one(self):
        score = OfflineEvaluator._score_result("hello world", "hello world")
        assert score == pytest.approx(1.0)

    def test_disjoint_strings_score_zero(self):
        score = OfflineEvaluator._score_result("foo bar", "baz qux")
        assert score == pytest.approx(0.0)

    def test_partial_overlap(self):
        score = OfflineEvaluator._score_result("a b c", "b c d")
        # intersection={b,c}, union={a,b,c,d} → 2/4 = 0.5
        assert score == pytest.approx(0.5)

    def test_both_empty_strings(self):
        score = OfflineEvaluator._score_result("", "")
        assert score == 0.0


# ---------------------------------------------------------------------------
# OfflineEvaluator
# ---------------------------------------------------------------------------


class TestOfflineEvaluator:
    def _make_eval_set(self, n: int = 3) -> EvalSet:
        cases = [
            EvalCase(task={"id": i}, expected_output="hello world")
            for i in range(n)
        ]
        return EvalSet(name="test", cases=cases)

    @pytest.mark.asyncio
    async def test_run_returns_report_with_correct_count(self):
        async def model_fn(task, context):
            return "hello world"

        evaluator = OfflineEvaluator(model_fn=model_fn)
        es = self._make_eval_set(5)
        report = await evaluator.run(es)
        assert len(report.results) == 5
        assert report.eval_set_name == "test"

    @pytest.mark.asyncio
    async def test_judge_fn_is_called_when_provided(self):
        call_count = 0

        async def model_fn(task, context):
            return "output"

        async def judge_fn(task, expected, actual):
            nonlocal call_count
            call_count += 1
            return 0.9

        evaluator = OfflineEvaluator(model_fn=model_fn, judge_fn=judge_fn)
        es = self._make_eval_set(3)
        report = await evaluator.run(es)
        assert call_count == 3
        for r in report.results:
            assert r.score == pytest.approx(0.9)
            assert r.judge == "judge_fn"

    @pytest.mark.asyncio
    async def test_timeout_per_case_enforced(self):
        """A model that sleeps too long should produce an error result."""

        async def slow_model(task, context):
            await asyncio.sleep(10)
            return "never"

        evaluator = OfflineEvaluator(model_fn=slow_model, concurrency=1)
        es = self._make_eval_set(1)
        report = await evaluator.run(es, timeout_per_case=0.05)
        assert len(report.results) == 1
        result = report.results[0]
        assert result.score == 0.0
        assert "Timed out" in result.error or "timed out" in result.error.lower()

    @pytest.mark.asyncio
    async def test_concurrency_semaphore_respected(self):
        """At most `concurrency` cases should run simultaneously."""
        max_concurrent = 0
        current_concurrent = 0
        lock = asyncio.Lock()

        async def model_fn(task, context):
            nonlocal max_concurrent, current_concurrent
            async with lock:
                current_concurrent += 1
                if current_concurrent > max_concurrent:
                    max_concurrent = current_concurrent
            await asyncio.sleep(0.02)
            async with lock:
                current_concurrent -= 1
            return "done"

        concurrency_limit = 2
        evaluator = OfflineEvaluator(model_fn=model_fn, concurrency=concurrency_limit)
        es = self._make_eval_set(6)
        await evaluator.run(es)
        assert max_concurrent <= concurrency_limit
