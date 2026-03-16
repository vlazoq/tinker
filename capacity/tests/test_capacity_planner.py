"""
Tests for capacity/planner.py
================================

Verifies token recording, disk usage tracking, threshold alerting,
and report generation.
"""
from __future__ import annotations

import time
import pytest

from capacity.planner import CapacityPlanner, CapacitySnapshot


class TestCapacitySnapshot:
    def test_default_values(self):
        snap = CapacitySnapshot()
        assert snap.tokens_used == 0
        assert snap.disk_mb == 0.0
        assert snap.artifact_count == 0


class TestCapacityPlanner:
    def test_initial_report_empty(self):
        planner = CapacityPlanner()
        report = planner.report()
        assert report["total_tokens_used"] == 0
        assert report["current_disk_mb"] == 0.0

    def test_record_tokens_accumulates_total(self):
        planner = CapacityPlanner()
        planner.record_tokens(micro_tokens=1000, meso_tokens=500)
        assert planner._total_tokens == 1500

    def test_record_tokens_creates_snapshot(self):
        planner = CapacityPlanner()
        planner.record_tokens(micro_tokens=200)
        report = planner.report()
        assert report["total_tokens_used"] == 200

    def test_record_artifact_count(self):
        planner = CapacityPlanner()
        planner.record_tokens(micro_tokens=100)  # create a snapshot first
        planner.record_artifact_count(total=42)
        report = planner.report()
        assert report["current_artifact_count"] == 42

    def test_set_and_check_threshold_below_limit(self):
        planner = CapacityPlanner()
        planner.record_tokens(micro_tokens=100)
        planner.record_artifact_count(total=5)
        planner.set_threshold("disk_mb", 10000)
        alerts = planner.check_thresholds()
        assert len(alerts) == 0   # no disk usage recorded

    def test_threshold_exceeded_generates_alert(self):
        planner = CapacityPlanner()
        planner.record_tokens(micro_tokens=100)
        # Manually inject a high disk usage into the latest snapshot
        planner._snapshots[-1].disk_mb = 5000.0
        planner.set_threshold("disk_mb", 1000)
        alerts = planner.check_thresholds()
        assert any("EXCEEDED" in a for a in alerts)

    def test_threshold_warning_at_80_percent(self):
        planner = CapacityPlanner()
        planner.record_tokens(micro_tokens=100)
        planner._snapshots[-1].disk_mb = 850.0  # 85% of 1000
        planner.set_threshold("disk_mb", 1000)
        alerts = planner.check_thresholds()
        assert any("WARNING" in a for a in alerts)

    def test_record_disk_usage_on_missing_paths(self, tmp_path):
        """record_disk_usage on non-existent paths should not raise."""
        planner = CapacityPlanner(
            workspace_path=str(tmp_path / "no_workspace"),
            artifact_path=str(tmp_path / "no_artifacts"),
        )
        planner.record_tokens(micro_tokens=1)  # create snapshot
        planner.record_disk_usage()
        report = planner.report()
        assert report["current_disk_mb"] == 0.0

    def test_window_size_limits_snapshots(self):
        planner = CapacityPlanner(window_size=5)
        for i in range(10):
            planner._snapshots.append(CapacitySnapshot(tokens_used=i * 100))
        assert len(planner._snapshots) <= 5
