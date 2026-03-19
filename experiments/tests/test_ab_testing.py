"""
Tests for experiments/ab_testing.py
======================================

Verifies experiment creation, deterministic assignment, outcome recording,
and statistical analysis.
"""

from __future__ import annotations

import pytest

from experiments.ab_testing import ABTestingFramework
from exceptions import ExperimentError


@pytest.fixture
def ab():
    return ABTestingFramework(seed="test-seed")


class TestExperimentCreation:
    def test_create_experiment(self, ab):
        exp = ab.create_experiment(
            name="temp_test",
            variants={"control": 0.7, "treatment": 0.5},
            metric="critic_score",
        )
        assert exp.name == "temp_test"
        assert exp.active is True

    def test_duplicate_name_raises(self, ab):
        ab.create_experiment("dup", {"a": 1, "b": 2})
        with pytest.raises(ExperimentError):
            ab.create_experiment("dup", {"a": 1, "b": 2})

    def test_single_variant_raises(self, ab):
        with pytest.raises(ExperimentError, match="at least 2"):
            ab.create_experiment("single", {"only_one": 1})


class TestVariantAssignment:
    def test_deterministic_assignment(self, ab):
        ab.create_experiment("det", {"A": 1, "B": 2})
        v1, _ = ab.get_variant("det", "task-123")
        v2, _ = ab.get_variant("det", "task-123")
        assert v1 == v2  # same unit always gets same variant

    def test_different_units_may_differ(self, ab):
        ab.create_experiment("diff", {"A": 1, "B": 2})
        variants = set()
        for i in range(20):
            v, _ = ab.get_variant("diff", f"unit-{i}")
            variants.add(v)
        # With 20 units, we should see both variants
        assert len(variants) == 2

    def test_inactive_experiment_returns_control(self, ab):
        ab.create_experiment("inactive", {"control": 0.7, "treatment": 0.5})
        ab.deactivate("inactive")
        variant, val = ab.get_variant("inactive", "any-unit")
        assert variant == "control"
        assert val == 0.7

    def test_unknown_experiment_raises(self, ab):
        with pytest.raises(ExperimentError):
            ab.get_variant("nope", "unit-1")


class TestOutcomeRecording:
    def test_record_outcome(self, ab):
        ab.create_experiment("scores", {"A": 1, "B": 2})
        ab.record_outcome("scores", "A", 0.8)
        ab.record_outcome("scores", "B", 0.9)
        report = ab.analyse("scores")
        assert "A" in report["variants"]
        assert "B" in report["variants"]

    def test_unknown_experiment_log_warning(self, ab):
        # Should not raise — just logs
        ab.record_outcome("nonexistent", "control", 0.5)


class TestAnalysis:
    def test_insufficient_data_no_winner(self, ab):
        ab.create_experiment("few_data", {"control": 1, "treatment": 2})
        # Only 5 outcomes — need >= 10 for winner determination
        for i in range(5):
            ab.record_outcome("few_data", "control", 0.7)
        report = ab.analyse("few_data")
        assert report["winner"] is None

    def test_clear_winner_detected(self, ab):
        import random
        rng = random.Random(42)
        ab.create_experiment("clear_win", {"control": 1, "treatment": 2})
        # control gets low scores, treatment gets high scores (with variance so t-test fires)
        for _ in range(20):
            ab.record_outcome("clear_win", "control", 0.3 + rng.uniform(-0.05, 0.05))
        for _ in range(20):
            ab.record_outcome("clear_win", "treatment", 0.9 + rng.uniform(-0.05, 0.05))
        report = ab.analyse("clear_win")
        assert report["winner"] == "treatment"
        assert report["significant"] is True

    def test_analyse_unknown_raises(self, ab):
        with pytest.raises(ExperimentError):
            ab.analyse("nope")


class TestListAndDeactivate:
    def test_list_experiments(self, ab):
        ab.create_experiment("e1", {"a": 1, "b": 2})
        ab.create_experiment("e2", {"a": 1, "b": 2})
        names = ab.list_experiments()
        assert "e1" in names
        assert "e2" in names

    def test_deactivate_stops_experiment(self, ab):
        ab.create_experiment("to_stop", {"a": 1, "b": 2})
        ab.deactivate("to_stop")
        exp = ab._experiments["to_stop"]
        assert exp.active is False

    def test_all_reports(self, ab):
        ab.create_experiment("r1", {"a": 1, "b": 2})
        reports = ab.all_reports()
        assert "r1" in reports


class TestTrafficGateAndRampUp:
    def test_get_variant_returns_control_outside_traffic(self, ab):
        """With traffic_percentage=0.0, all units get control."""
        ab.create_experiment(
            "low_traffic",
            {"control": "ctl", "treatment": "trt"},
            traffic_percentage=0.0,
        )
        # With 0% traffic, every unit should get the control
        for i in range(20):
            variant, _ = ab.get_variant("low_traffic", f"unit-{i}")
            assert variant == "control"

    def test_ramp_up_changes_traffic_percentage(self, ab):
        ab.create_experiment("ramp_exp", {"control": 1, "treatment": 2})
        assert ab._experiments["ramp_exp"].traffic_percentage == 1.0
        ab.ramp_up("ramp_exp", 0.5)
        assert ab._experiments["ramp_exp"].traffic_percentage == 0.5

    def test_ramp_up_clamps_to_bounds(self, ab):
        ab.create_experiment("clamp_exp", {"control": 1, "treatment": 2})
        ab.ramp_up("clamp_exp", 1.5)
        assert ab._experiments["clamp_exp"].traffic_percentage == 1.0
        ab.ramp_up("clamp_exp", -0.1)
        assert ab._experiments["clamp_exp"].traffic_percentage == 0.0

    def test_ramp_up_unknown_experiment_raises(self, ab):
        with pytest.raises(ExperimentError):
            ab.ramp_up("does_not_exist", 0.5)


class TestResetAndMeanStd:
    def test_reset_experiment_clears_outcomes(self, ab):
        ab.create_experiment("reset_me", {"control": 1, "treatment": 2})
        ab.record_outcome("reset_me", "control", 0.8)
        ab.record_outcome("reset_me", "control", 0.9)
        ab.reset_experiment("reset_me")
        report = ab.analyse("reset_me")
        assert report["variants"]["control"].get("n", 0) == 0

    def test_analyse_mean_and_std(self, ab):
        ab.create_experiment("stats_exp", {"control": 1, "treatment": 2})
        values = [0.6, 0.7, 0.8, 0.9, 1.0]
        for v in values:
            ab.record_outcome("stats_exp", "control", v)
        report = ab.analyse("stats_exp")
        stats = report["variants"]["control"]
        assert stats["n"] == 5
        assert abs(stats["mean"] - 0.8) < 0.001
        import statistics
        expected_std = round(statistics.stdev(values), 4)
        assert abs(stats["std"] - expected_std) < 0.001

    def test_insufficient_data_all_variants_no_winner(self, ab):
        """Fewer than 10 observations per variant → no winner declared."""
        ab.create_experiment("tiny", {"control": 1, "treatment": 2})
        for _ in range(9):
            ab.record_outcome("tiny", "control", 0.2)
            ab.record_outcome("tiny", "treatment", 0.9)
        report = ab.analyse("tiny")
        assert report["winner"] is None
