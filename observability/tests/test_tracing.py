"""
Tests for observability/tracing.py
=====================================

Covers span recording, trace context propagation, and performance summaries.
"""
from __future__ import annotations

import asyncio
import time
import pytest

from observability.tracing import Tracer, Span


class TestSpan:
    def test_span_has_name_and_start_time(self):
        span = Span(name="test_op")
        assert span.name == "test_op"
        assert span.started_at > 0

    def test_span_duration_increases_after_end(self):
        span = Span(name="op")
        time.sleep(0.01)
        span.ended_at = time.monotonic()
        assert span.duration() >= 0.01

    def test_span_duration_none_if_not_ended(self):
        span = Span(name="op")
        assert span.duration() is None


class TestTracer:
    def test_start_trace_records_spans(self):
        tracer = Tracer()
        with tracer.start_trace("my_trace") as trace:
            with trace.span("step_1"):
                time.sleep(0.005)
        recent = tracer.recent_traces()
        assert len(recent) >= 1
        assert recent[-1].name == "my_trace"

    def test_span_records_error(self):
        tracer = Tracer()
        with tracer.start_trace("error_trace") as trace:
            try:
                with trace.span("failing_op"):
                    raise ValueError("test error")
            except ValueError:
                pass
        recent = tracer.recent_traces()
        last_trace = recent[-1]
        failed_spans = [s for s in last_trace.spans if s.error]
        assert len(failed_spans) >= 1

    def test_performance_summary_with_completed_traces(self):
        tracer = Tracer()
        for _ in range(5):
            with tracer.start_trace("loop_iteration") as trace:
                with trace.span("work"):
                    time.sleep(0.001)
        summary = tracer.performance_summary("loop_iteration")
        assert "p50" in summary
        assert "p95" in summary
        assert "count" in summary
        assert summary["count"] == 5

    def test_recent_traces_bounded(self):
        tracer = Tracer(max_traces=3)
        for i in range(6):
            with tracer.start_trace(f"trace_{i}"):
                pass
        assert len(tracer.recent_traces()) <= 3
