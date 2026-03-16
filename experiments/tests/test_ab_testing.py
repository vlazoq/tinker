"""
Tests for experiments/ab_testing.py
======================================

Verifies experiment creation, deterministic assignment, outcome recording,
and statistical analysis.
"""
from __future__ import annotations

import pytest

from experiments.ab_testing import ABTestingFramework, Experiment


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
        with pytest.raises(ValueError, match="already exists"):
            ab.create_experiment("dup", {"a": 1, "b": 2})

    def test_single_variant_raises(self, ab):
        with pytest.raises(ValueError, match="at least 2"):
            ab.create_experiment("single", {"only_one": 1})


class TestVariantAssignment:
    def test_deterministic_assignment(self, ab):
        ab.create_experiment("det", {"A": 1, "B": 2})
        v1, _ = ab.get_variant("det", "task-123")
        v2, _ = ab.get_variant("det", "task-123")
        assert v1 == v2   # same unit always gets same variant

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
        with pytest.raises(KeyError):
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
        ab.create_experiment("clear_win", {"control": 1, "treatment": 2})
        # control gets low scores, treatment gets high scores
        for _ in range(15):
            ab.record_outcome("clear_win", "control", 0.3)
        for _ in range(15):
            ab.record_outcome("clear_win", "treatment", 0.9)
        report = ab.analyse("clear_win")
        assert report["winner"] == "treatment"
        assert report["significant"] is True

    def test_analyse_unknown_raises(self, ab):
        with pytest.raises(KeyError):
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
