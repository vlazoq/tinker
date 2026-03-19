"""
observability/tracing.py
=========================

Lightweight span-based tracing for Tinker loop performance analysis.

What is tracing?
-----------------
Tracing records the time taken by each step of a workflow as a "span".
A trace is a collection of spans that together describe one complete workflow
(e.g. one micro loop iteration).

For Tinker, a micro loop trace looks like:
  micro_loop [0.0s → 45.3s]
    task_selection  [0.0s → 0.1s]
    context_assembly[0.1s → 1.2s]
    architect_call  [1.2s → 32.1s]
    critic_call     [32.1s → 44.8s]
    store_artifact  [44.8s → 45.1s]
    complete_task   [45.1s → 45.3s]

This immediately shows that the Architect took 30 seconds and everything
else was fast — which is the expected behavior.

Why not OpenTelemetry?
-----------------------
Full OpenTelemetry (OTEL) adds significant complexity and dependencies.
This module provides the same data (span timings, attributes) in a simpler
format that can be:
  1. Exported to any OTEL-compatible backend (Jaeger, Zipkin) via JSON
  2. Written to structured logs for ad-hoc analysis
  3. Stored in the DLQ for forensic investigation

OTEL export can be added later as a thin wrapper over this module.

Usage
------
::

    tracer = Tracer()

    # Create a trace (one per micro loop):
    with tracer.start_trace("micro_loop", attributes={"iteration": 42}) as trace:
        with trace.span("task_selection"):
            task = await select_task()

        with trace.span("architect_call", {"task_id": task["id"]}):
            result = await architect.call(task, context)

    # When the with-block exits, durations are recorded automatically.

    # Inspect the trace:
    print(trace.to_dict())

    # Or retrieve recent traces:
    recent = tracer.recent_traces(limit=10)
"""

from __future__ import annotations

import time
import logging
import threading
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Deque, Iterator, Optional

logger = logging.getLogger(__name__)


@dataclass
class Span:
    """
    A single timed step within a trace.

    Attributes
    ----------
    name        : Human-readable step name (e.g. "architect_call").
    started_at  : Monotonic start time (seconds).
    ended_at    : Monotonic end time, or None if still running.
    attributes  : Arbitrary key-value metadata about this step.
    error       : Error message if this span failed, else None.
    """

    name: str
    started_at: float = field(default_factory=time.monotonic)
    ended_at: Optional[float] = None
    attributes: dict = field(default_factory=dict)
    error: Optional[str] = None

    def finish(self, error: Optional[str] = None) -> None:
        """Mark the span as finished."""
        self.ended_at = time.monotonic()
        if error:
            self.error = error

    @property
    def duration_ms(self) -> Optional[float]:
        """Duration in milliseconds, or None if still running."""
        if self.ended_at is None:
            return None
        return (self.ended_at - self.started_at) * 1000

    def to_dict(self) -> dict:
        """Serialise the span to a JSON-compatible dict."""
        return {
            "name": self.name,
            "started_at": round(self.started_at, 4),
            "ended_at": round(self.ended_at, 4) if self.ended_at else None,
            "duration_ms": round(self.duration_ms, 2)
            if self.duration_ms is not None
            else None,
            "attributes": self.attributes,
            "error": self.error,
        }


@dataclass
class Trace:
    """
    A complete trace for one workflow (e.g. one micro loop iteration).

    Contains a root span and zero or more child spans.

    Attributes
    ----------
    trace_id   : Unique identifier for this trace.
    name       : Workflow name (e.g. "micro_loop").
    spans      : List of child spans in order of creation.
    started_at : When the trace started.
    ended_at   : When the trace finished, or None if still running.
    attributes : Trace-level metadata.
    """

    trace_id: str
    name: str
    started_at: float = field(default_factory=time.monotonic)
    ended_at: Optional[float] = None
    attributes: dict = field(default_factory=dict)
    spans: list = field(default_factory=list)
    _current_span: Optional[Span] = field(default=None, repr=False)

    def finish(self) -> None:
        """Mark the trace as complete."""
        self.ended_at = time.monotonic()

    @property
    def duration_ms(self) -> Optional[float]:
        """Total trace duration in milliseconds."""
        if self.ended_at is None:
            return None
        return (self.ended_at - self.started_at) * 1000

    @contextmanager
    def span(self, name: str, attributes: Optional[dict] = None) -> Iterator[Span]:
        """
        Context manager to create a child span.

        Usage::

            with trace.span("architect_call", {"task_id": task["id"]}):
                result = architect.call(task, context)

        Automatically records start/end time and any exceptions.
        """
        s = Span(name=name, attributes=attributes or {})
        self.spans.append(s)
        try:
            yield s
            s.finish()
        except Exception as exc:
            s.finish(error=str(exc))
            raise

    def to_dict(self) -> dict:
        """Serialise the trace to a JSON-compatible dict."""
        return {
            "trace_id": self.trace_id,
            "name": self.name,
            "started_at": round(self.started_at, 4),
            "ended_at": round(self.ended_at, 4) if self.ended_at else None,
            "duration_ms": round(self.duration_ms, 2)
            if self.duration_ms is not None
            else None,
            "attributes": self.attributes,
            "spans": [s.to_dict() for s in self.spans],
        }

    def slowest_span(self) -> Optional[Span]:
        """Return the span with the longest duration."""
        finished = [s for s in self.spans if s.duration_ms is not None]
        if not finished:
            return None
        return max(finished, key=lambda s: s.duration_ms)

    def log_summary(self) -> None:
        """Log a one-line summary of the trace to the logger."""
        if self.duration_ms is None:
            return
        slowest = self.slowest_span()
        slowest_info = (
            f" (slowest: {slowest.name}={slowest.duration_ms:.0f}ms)" if slowest else ""
        )
        logger.debug(
            "Trace '%s' [%s] completed in %.0fms%s",
            self.name,
            self.trace_id,
            self.duration_ms,
            slowest_info,
        )


class Tracer:
    """
    Factory and repository for Tinker traces.

    Creates new traces, manages their lifecycle, and stores a rolling window
    of recent completed traces for post-hoc analysis.

    Parameters
    ----------
    max_traces : How many recent traces to keep in memory (default: 100).
    auto_log   : If True, log a summary line when each trace completes.
    """

    def __init__(self, max_traces: int = 100, auto_log: bool = True) -> None:
        self._max_traces = max_traces
        self._auto_log = auto_log
        self._traces: Deque[Trace] = deque(maxlen=max_traces)
        self._lock = threading.Lock()
        self._trace_counter: int = 0

    @contextmanager
    def start_trace(
        self, name: str, attributes: Optional[dict] = None
    ) -> Iterator[Trace]:
        """
        Context manager that starts a trace and yields it.

        The trace is finished automatically when the block exits (even on error).
        The completed trace is added to the rolling history.

        Parameters
        ----------
        name       : Name of the workflow (e.g. "micro_loop", "meso_loop").
        attributes : Optional metadata for the trace.

        Yields
        ------
        Trace : The trace object.  Use ``trace.span(...)`` inside the block.

        Usage::

            with tracer.start_trace("micro_loop", {"iteration": 42}) as trace:
                with trace.span("task_selection"):
                    task = await select_task()
                with trace.span("architect_call"):
                    result = await architect.call(task, context)
        """
        with self._lock:
            self._trace_counter += 1
            trace_id = f"{name[:6]}-{self._trace_counter:04d}"

        trace = Trace(
            trace_id=trace_id,
            name=name,
            attributes=attributes or {},
        )
        try:
            yield trace
        finally:
            trace.finish()
            with self._lock:
                self._traces.append(trace)
            if self._auto_log:
                trace.log_summary()

    def recent_traces(self, limit: int = 10) -> list[dict]:
        """
        Return recent completed traces as dicts, newest first.

        Parameters
        ----------
        limit : Maximum number of traces to return.

        Returns
        -------
        list[dict] : Serialised trace dicts.
        """
        with self._lock:
            traces = list(self._traces)
        traces.reverse()  # Newest first
        return [t.to_dict() for t in traces[:limit]]

    def performance_summary(self) -> dict:
        """
        Return aggregate performance statistics across all stored traces.

        Useful for identifying consistently slow operations.

        Returns
        -------
        dict : Per-span-name statistics (avg, p50, p95, p99, max).
        """
        from collections import defaultdict

        span_durations: dict[str, list[float]] = defaultdict(list)

        with self._lock:
            for trace in self._traces:
                for span in trace.spans:
                    if span.duration_ms is not None:
                        span_durations[span.name].append(span.duration_ms)

        summary = {}
        for span_name, durations in span_durations.items():
            durations.sort()
            n = len(durations)
            if n == 0:
                continue
            summary[span_name] = {
                "count": n,
                "avg_ms": round(sum(durations) / n, 2),
                "p50_ms": round(durations[n // 2], 2),
                "p95_ms": round(durations[int(n * 0.95)], 2),
                "p99_ms": round(durations[int(n * 0.99)], 2),
                "max_ms": round(durations[-1], 2),
            }
        return summary


# Module-level default tracer — import and use directly
default_tracer = Tracer()


# ---------------------------------------------------------------------------
# TinkerError ↔ Span integration
# ---------------------------------------------------------------------------


def record_tinker_exception(exc: Exception, span: "Span") -> None:
    """
    Record a ``TinkerError`` on a tracing ``Span``.

    Attaches the exception's ``context`` dict as individual span attributes
    (prefixed with ``"exc."``) and sets the span's ``error`` field to the
    exception string representation.

    This is the **canonical** way to record an exception inside a span.
    It ensures structured context from ``exc.context`` is surfaced in the
    trace — an on-call engineer can see the full diagnostic dict (task IDs,
    URLs, attempt counts) in the span attributes without parsing log strings.

    Works with any exception, not just ``TinkerError`` — non-TinkerError
    exceptions get no attribute enrichment, only the error string.

    Usage inside a trace span::

        from observability.tracing import record_tinker_exception

        with trace.span("architect_call") as span:
            try:
                result = await architect.call(task, context)
            except TinkerError as exc:
                record_tinker_exception(exc, span)
                raise

    The span's ``error`` field and ``attributes`` are then available in
    ``Trace.to_dict()`` and in any downstream log aggregator that consumes
    Tinker traces.

    Parameters
    ----------
    exc : Exception
        The exception to record.  ``TinkerError`` subclasses get their
        ``context`` dict and ``retryable`` flag attached as attributes.
    span : Span
        The active span to annotate.
    """
    # Always set the error string — this is visible in the trace timeline
    span.error = str(exc)

    # Enrich span attributes from TinkerError.context
    try:
        from exceptions import TinkerError  # local import to avoid circular dep

        if isinstance(exc, TinkerError):
            span.attributes["exc.type"] = type(exc).__name__
            span.attributes["exc.retryable"] = exc.retryable
            for key, value in exc.context.items():
                # Prefix with "exc." to namespace exception attrs from normal attrs
                span.attributes[f"exc.{key}"] = value
        else:
            span.attributes["exc.type"] = type(exc).__name__
    except Exception:
        # Never let observability code break the calling code
        pass
