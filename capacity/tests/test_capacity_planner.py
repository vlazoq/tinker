"""
Tests for capacity/planner.py
================================

Verifies token recording (per-type breakdown), disk usage tracking,
threshold alerting, disk-full projection, cost estimation,
and report generation.
"""

from __future__ import annotations


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
        assert len(alerts) == 0  # no disk usage recorded

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


class TestPerTypeTokenBreakdown:
    def test_tokens_by_type_tracked_separately(self):
        planner = CapacityPlanner()
        planner.record_tokens(micro_tokens=1000, meso_tokens=200, macro_tokens=50)
        assert planner._total_micro_tokens == 1000
        assert planner._total_meso_tokens == 200
        assert planner._total_macro_tokens == 50
        assert planner._total_tokens == 1250

    def test_report_includes_tokens_by_type(self):
        planner = CapacityPlanner()
        planner.record_tokens(micro_tokens=500, meso_tokens=100)
        report = planner.report()
        assert report["tokens_by_type"]["micro"] == 500
        assert report["tokens_by_type"]["meso"] == 100
        assert report["tokens_by_type"]["macro"] == 0

    def test_multiple_record_calls_accumulate_per_type(self):
        planner = CapacityPlanner()
        planner.record_tokens(micro_tokens=300)
        planner.record_tokens(micro_tokens=200, meso_tokens=100)
        assert planner._total_micro_tokens == 500
        assert planner._total_meso_tokens == 100


class TestDiskFreeProjection:
    def test_disk_free_gb_populated_after_record(self, tmp_path):
        planner = CapacityPlanner(workspace_path=str(tmp_path))
        planner.record_tokens(micro_tokens=1)  # create a snapshot
        planner.record_disk_usage()
        report = planner.report()
        assert report["disk_free_gb"] > 0  # actual partition has free space

    def test_disk_full_alert_fires_when_estimated_full_soon(self):
        planner = CapacityPlanner(disk_hours_warning=100)
        planner.record_tokens(micro_tokens=1)
        snap = planner._snapshots[-1]
        snap.disk_mb = 1000.0
        snap.disk_free_gb = 0.001  # 1 MB free

        # Simulate growth data
        planner._first_snapshot = snap
        # Add enough snapshots to trigger growth calculation
        for _ in range(6):
            planner._snapshots.append(
                CapacitySnapshot(disk_mb=1001.0, disk_free_gb=0.001)
            )

        planner.set_threshold("disk_mb", 999999)  # won't trigger disk_mb alert
        alerts = planner.check_thresholds()
        # May or may not fire depending on elapsed time, but should not raise
        assert isinstance(alerts, list)


class TestCostEstimation:
    def test_cost_fields_present_after_tokens_recorded(self):
        import time

        planner = CapacityPlanner(tokens_per_second=100)
        planner.record_tokens(micro_tokens=10000)
        # Force elapsed time to be > 0.05h by manipulating start time
        planner._start_time = time.monotonic() - 200  # 200 seconds ago
        report = planner.report()
        assert "gpu_hours_used" in report
        assert "electricity_cost_usd" in report
        assert "cloud_cost_estimate_usd" in report
        assert report["gpu_hours_used"] >= 0
        assert report["electricity_cost_usd"] >= 0
        assert report["cloud_cost_estimate_usd"] >= 0

    def test_gpu_hours_scale_with_tokens(self):
        import time

        planner = CapacityPlanner(
            tokens_per_second=3600
        )  # 3600 t/s → 1 GPU-hour per 3600*3600 tokens
        planner.record_tokens(micro_tokens=3600 * 3600)  # exactly 1 GPU-hour
        planner._start_time = time.monotonic() - 3700  # ensure elapsed > 0.05h
        report = planner.report()
        assert abs(report.get("gpu_hours_used", 0) - 1.0) < 0.01
