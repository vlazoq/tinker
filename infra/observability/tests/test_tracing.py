"""
Tests for observability/tracing.py
=====================================

Covers span recording, trace context propagation, and performance summaries.
"""

from __future__ import annotations

import time

from infra.observability.tracing import Tracer, Span


class TestSpan:
    def test_span_has_name_and_start_time(self):
        span = Span(name="test_op")
        assert span.name == "test_op"
        assert span.started_at > 0

    def test_span_duration_increases_after_end(self):
        span = Span(name="op")
        time.sleep(0.01)
        span.ended_at = time.monotonic()
        # duration_ms is a property (not a method); returns milliseconds
        assert span.duration_ms is not None
        assert span.duration_ms >= 10.0  # at least 10 ms

    def test_span_duration_none_if_not_ended(self):
        span = Span(name="op")
        # duration_ms returns None when ended_at is not yet set
        assert span.duration_ms is None


class TestTracer:
    def test_start_trace_records_spans(self):
        tracer = Tracer()
        with tracer.start_trace("my_trace") as trace:
            with trace.span("step_1"):
                time.sleep(0.005)
        recent = tracer.recent_traces()
        assert len(recent) >= 1
        # recent_traces() returns serialised dicts, not Trace objects
        assert recent[-1]["name"] == "my_trace"

    def test_span_records_error(self):
        tracer = Tracer()
        with tracer.start_trace("error_trace") as trace:
            try:
                with trace.span("failing_op"):
                    raise ValueError("test error")
            except ValueError:
                pass
        recent = tracer.recent_traces()
        last_trace = recent[-1]  # dict from to_dict()
        failed_spans = [s for s in last_trace["spans"] if s.get("error")]
        assert len(failed_spans) >= 1

    def test_performance_summary_with_completed_traces(self):
        tracer = Tracer()
        for _ in range(5):
            with tracer.start_trace("loop_iteration") as trace:
                with trace.span("work"):
                    time.sleep(0.001)
        # performance_summary() takes no arguments; returns {span_name: stats_dict}
        summary = tracer.performance_summary()
        assert "work" in summary
        work_stats = summary["work"]
        assert "p50_ms" in work_stats
        assert "p95_ms" in work_stats
        assert work_stats["count"] == 5

    def test_recent_traces_bounded(self):
        tracer = Tracer(max_traces=3)
        for i in range(6):
            with tracer.start_trace(f"trace_{i}"):
                pass
        assert len(tracer.recent_traces()) <= 3


# ---------------------------------------------------------------------------
# record_tinker_exception
# ---------------------------------------------------------------------------


class TestRecordTinkerException:
    """record_tinker_exception attaches TinkerError context to a Span."""

    def _span(self, name: str = "op") -> "Span":
        from infra.observability.tracing import Span

        return Span(name=name)

    def test_sets_error_field(self):
        from infra.observability.tracing import record_tinker_exception
        from exceptions import ModelConnectionError

        span = self._span()
        exc = ModelConnectionError("connect failed")
        record_tinker_exception(exc, span)
        assert span.error is not None
        assert "connect failed" in span.error

    def test_attaches_exc_type_attribute(self):
        from infra.observability.tracing import record_tinker_exception
        from exceptions import ModelTimeoutError

        span = self._span()
        record_tinker_exception(ModelTimeoutError("timeout"), span)
        assert span.attributes.get("exc.type") == "ModelTimeoutError"

    def test_attaches_retryable_attribute(self):
        from infra.observability.tracing import record_tinker_exception
        from exceptions import ModelConnectionError, ResponseParseError

        span_r = self._span()
        span_nr = self._span()
        record_tinker_exception(ModelConnectionError("x"), span_r)
        record_tinker_exception(ResponseParseError("y"), span_nr)
        assert span_r.attributes.get("exc.retryable") is True
        assert span_nr.attributes.get("exc.retryable") is False

    def test_context_dict_attached_as_prefixed_attrs(self):
        from infra.observability.tracing import record_tinker_exception
        from exceptions import ModelConnectionError

        span = self._span()
        exc = ModelConnectionError("fail", context={"url": "http://x", "attempt": 2})
        record_tinker_exception(exc, span)
        assert span.attributes.get("exc.url") == "http://x"
        assert span.attributes.get("exc.attempt") == 2

    def test_empty_context_adds_no_exc_prefixed_attrs(self):
        from infra.observability.tracing import record_tinker_exception
        from exceptions import TinkerError

        span = self._span()
        record_tinker_exception(TinkerError("plain"), span)
        # TinkerError always has a trace_id in its context, so exc.trace_id is expected
        exc_ctx_keys = [
            k
            for k in span.attributes
            if k.startswith("exc.")
            and k not in ("exc.type", "exc.retryable", "exc.trace_id")
        ]
        assert exc_ctx_keys == []

    def test_plain_exception_gets_type_only(self):
        from infra.observability.tracing import record_tinker_exception

        span = self._span()
        record_tinker_exception(ValueError("bad input"), span)
        assert span.attributes.get("exc.type") == "ValueError"
        assert "exc.retryable" not in span.attributes

    def test_does_not_raise_on_malformed_exc(self):
        """record_tinker_exception must never crash the caller."""
        from infra.observability.tracing import record_tinker_exception

        span = self._span()
        # Pass a completely unexpected type — should not raise
        record_tinker_exception(None, span)  # type: ignore

    def test_importable_from_observability_package(self):
        from infra.observability import record_tinker_exception

        assert callable(record_tinker_exception)
