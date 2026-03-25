"""
tinker_platform/experiments/tests/test_offline_eval_integration.py
=====================================================
Integration tests for OfflineEvaluator with a complete EvalSet.

Verifies end-to-end behaviour without real LLM calls.
"""

from __future__ import annotations

import pytest

from tinker_platform.experiments.offline_eval import EvalCase, EvalSet, OfflineEvaluator


class TestOfflineEvalIntegration:
    """Integration: EvalSet of 5 cases run through OfflineEvaluator."""

    def _make_eval_set(self) -> EvalSet:
        """Create an EvalSet with 5 EvalCases that have overlapping tokens."""
        cases = [
            EvalCase(
                task={"id": f"task-{i}", "description": f"task {i}"},
                expected_output="result A is the correct answer",
                tags=["integration"],
            )
            for i in range(5)
        ]
        return EvalSet(name="integration_test", cases=cases)

    @pytest.mark.asyncio
    async def test_report_has_five_results(self):
        """Report should contain exactly 5 results."""

        async def model_fn(task, context):
            return "result A"

        evaluator = OfflineEvaluator(model_fn=model_fn)
        eval_set = self._make_eval_set()
        report = await evaluator.run(eval_set)

        assert len(report.results) == 5

    @pytest.mark.asyncio
    async def test_mean_score_above_zero(self):
        """With some token overlap between expected and actual, mean_score > 0."""

        async def model_fn(task, context):
            return "result A"

        # expected_output = "result A is the correct answer"
        # actual_output = "result A"
        # intersection = {"result", "A"}, union = {"result", "A", "is", "the", "correct", "answer"}
        # jaccard = 2/6 ≈ 0.333

        evaluator = OfflineEvaluator(model_fn=model_fn)
        eval_set = self._make_eval_set()
        report = await evaluator.run(eval_set)

        assert report.mean_score > 0.0

    @pytest.mark.asyncio
    async def test_pass_rate_at_zero_threshold_is_one(self):
        """pass_rate(threshold=0.0) should be 1.0 since all cases succeed."""

        async def model_fn(task, context):
            return "result A"

        evaluator = OfflineEvaluator(model_fn=model_fn)
        eval_set = self._make_eval_set()
        report = await evaluator.run(eval_set)

        assert report.pass_rate(threshold=0.0) == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_eval_set_name_preserved_in_report(self):
        """Report's eval_set_name must match the EvalSet's name."""

        async def model_fn(task, context):
            return "result A"

        evaluator = OfflineEvaluator(model_fn=model_fn)
        eval_set = self._make_eval_set()
        report = await evaluator.run(eval_set)

        assert report.eval_set_name == "integration_test"

    @pytest.mark.asyncio
    async def test_all_results_have_no_error(self):
        """When model_fn succeeds, no results should have an error."""

        async def model_fn(task, context):
            return "result A"

        evaluator = OfflineEvaluator(model_fn=model_fn)
        eval_set = self._make_eval_set()
        report = await evaluator.run(eval_set)

        for result in report.results:
            assert result.error == ""

    @pytest.mark.asyncio
    async def test_case_ids_match_eval_set(self):
        """Each result's case_id should correspond to a case in the EvalSet."""

        async def model_fn(task, context):
            return "result A"

        evaluator = OfflineEvaluator(model_fn=model_fn)
        eval_set = self._make_eval_set()
        expected_ids = {c.id for c in eval_set.cases}

        report = await evaluator.run(eval_set)

        result_ids = {r.case_id for r in report.results}
        assert result_ids == expected_ids
