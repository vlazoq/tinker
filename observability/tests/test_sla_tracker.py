"""
Tests for observability/sla_tracker.py
========================================

Verifies SLA definition, recording, percentile computation, and breach detection.
"""

from __future__ import annotations


from observability.sla_tracker import (
    SLATracker,
    build_default_sla_tracker,
)


class TestSLATracker:
    def test_define_and_report_empty(self):
        tracker = SLATracker()
        tracker.define("micro_loop", p95_seconds=30.0, p99_seconds=60.0)
        report = tracker.report("micro_loop")
        assert report is not None
        assert report.count == 0

    def test_record_and_report_percentiles(self):
        tracker = SLATracker()
        tracker.define("micro_loop", p95_seconds=10.0, p99_seconds=20.0)
        durations = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        for d in durations:
            tracker.record("micro_loop", d)
        report = tracker.report("micro_loop")
        assert report.count == 10
        assert report.p50_s > 0
        assert report.p95_s > 0
        assert report.p50_s <= report.p95_s

    def test_breach_detected_when_over_p95(self):
        tracker = SLATracker()
        tracker.define("micro_loop", p95_seconds=1.0, p99_seconds=2.0)
        # Record all values way over the threshold
        for _ in range(20):
            tracker.record("micro_loop", 10.0)
        report = tracker.report("micro_loop")
        assert report.p95_breach

    def test_no_breach_when_under_threshold(self):
        tracker = SLATracker()
        tracker.define("micro_loop", p95_seconds=100.0, p99_seconds=200.0)
        for _ in range(10):
            tracker.record("micro_loop", 0.5)
        report = tracker.report("micro_loop")
        assert not report.p95_breach

    def test_all_reports_returns_all_defined(self):
        tracker = SLATracker()
        tracker.define("micro_loop", p95_seconds=10.0, p99_seconds=20.0)
        tracker.define("meso_loop", p95_seconds=60.0, p99_seconds=120.0)
        reports = tracker.all_reports()
        assert "micro_loop" in reports
        assert "meso_loop" in reports

    def test_unknown_operation_returns_empty_report(self):
        # report() now returns an empty SLAReport (count=0) for unknown operations
        tracker = SLATracker()
        report = tracker.report("unknown_op")
        assert report is not None
        assert report.count == 0


class TestBuildDefaultSLATracker:
    def test_creates_expected_definitions(self):
        tracker = build_default_sla_tracker()
        # Should have micro/meso/macro loops pre-defined
        for op in ("micro_loop", "meso_loop", "macro_loop"):
            assert tracker.report(op) is not None
