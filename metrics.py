"""
metrics.py — Optional Prometheus metrics for Tinker
=====================================================

Exposes key counters, gauges, and histograms so you can monitor Tinker's
progress in Grafana or any Prometheus-compatible dashboard.

Quick start
-----------
1. Install the optional dependency::

       pip install prometheus-client

2. Tinker detects it at startup and begins serving metrics automatically on
   port 9090 (configurable via ``TINKER_METRICS_PORT``).

3. Add a Prometheus scrape job::

       scrape_configs:
         - job_name: tinker
           static_configs:
             - targets: ['localhost:9090']

4. Import a pre-built Grafana dashboard or query metrics directly.

If ``prometheus-client`` is *not* installed, every call in this module is a
silent no-op.  No warnings, no import errors — Tinker runs normally.

Available metrics
-----------------
``tinker_micro_loops_total``
    Counter.  Total micro loops completed (labelled ``status``=success/failed).

``tinker_meso_loops_total``
    Counter.  Total meso syntheses completed (labelled ``status``).

``tinker_macro_loops_total``
    Counter.  Total macro snapshots committed (labelled ``status``).

``tinker_micro_loop_duration_seconds``
    Histogram.  Wall-clock time for each completed micro loop.

``tinker_meso_loop_duration_seconds``
    Histogram.  Wall-clock time for each completed meso synthesis.

``tinker_critic_score``
    Gauge.  The most recent Critic quality score (0.0–1.0).
    A persistent value near 1.0 can signal Critique Collapse.

``tinker_task_queue_depth``
    Gauge.  Current number of pending tasks in the task engine.

``tinker_consecutive_failures``
    Gauge.  Current streak of consecutive failed micro loops.
    High values indicate connectivity or model issues.

``tinker_stagnation_events_total``
    Counter.  Stagnation detections by type (labelled ``stagnation_type``
    and ``intervention``).  Useful for understanding how often each
    detector fires and which interventions are most common.

Environment variables
---------------------
``TINKER_METRICS_PORT``     HTTP port for Prometheus scraping (default: 9090).
``TINKER_METRICS_ENABLED``  Set to ``"false"`` to disable entirely.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prometheus import — optional
# ---------------------------------------------------------------------------

try:
    import prometheus_client as _prom
    from prometheus_client import Counter, Gauge, Histogram, start_http_server

    _PROM_AVAILABLE = True
except ImportError:
    _prom = None  # type: ignore[assignment]
    _PROM_AVAILABLE = False


# ---------------------------------------------------------------------------
# TinkerMetrics
# ---------------------------------------------------------------------------


class TinkerMetrics:
    """
    Thin wrapper around prometheus_client that the Orchestrator calls after
    each loop iteration.

    If ``prometheus-client`` is not installed, every method is a no-op.
    The Orchestrator never needs to check — it just calls the methods and
    the class handles the "absent library" case internally.

    Usage
    -----
    ::

        from metrics import TinkerMetrics
        m = TinkerMetrics()            # starts HTTP server if prom available
        orchestrator = Orchestrator(..., metrics=m)

    Parameters
    ----------
    port    : TCP port for the Prometheus HTTP scrape endpoint.
    enabled : Explicit on/off override (reads ``TINKER_METRICS_ENABLED`` env
              var if not supplied).
    """

    def __init__(
        self,
        port: int | None = None,
        enabled: bool | None = None,
    ) -> None:
        # Honour explicit argument, then env var, then default to True.
        if enabled is None:
            enabled = os.getenv("TINKER_METRICS_ENABLED", "true").lower() != "false"

        self._enabled = enabled and _PROM_AVAILABLE

        if not self._enabled:
            if enabled and not _PROM_AVAILABLE:
                logger.info(
                    "Metrics disabled: prometheus-client not installed.  "
                    "Install it with: pip install prometheus-client"
                )
            return

        self._port = port or int(os.getenv("TINKER_METRICS_PORT", "9090"))

        # ── Counters ───────────────────────────────────────────────────────────
        # Each label vector is pre-declared so Prometheus always shows 0 counts
        # even before the first event fires.

        self._micro_total = Counter(
            "tinker_micro_loops_total",
            "Total number of micro loop iterations completed.",
            ["status"],  # "success" or "failed"
        )
        self._meso_total = Counter(
            "tinker_meso_loops_total",
            "Total number of meso synthesis runs completed.",
            ["status"],
        )
        self._macro_total = Counter(
            "tinker_macro_loops_total",
            "Total number of macro snapshot commits completed.",
            ["status"],
        )
        self._stagnation_total = Counter(
            "tinker_stagnation_events_total",
            "Total stagnation events detected by the StagnationMonitor.",
            ["stagnation_type", "intervention"],
        )

        # ── Gauges ────────────────────────────────────────────────────────────
        # Gauges track the current value of a measurement that can go up or down.

        self._critic_score = Gauge(
            "tinker_critic_score",
            "Most recent Critic quality score (0.0 = worst, 1.0 = best).  "
            "A persistent value near 1.0 can indicate Critique Collapse.",
        )
        self._queue_depth = Gauge(
            "tinker_task_queue_depth",
            "Current number of tasks waiting in the pending queue.",
        )
        self._consecutive_failures = Gauge(
            "tinker_consecutive_failures",
            "Current streak of consecutive failed micro loops.  "
            "High values indicate connectivity or model issues.",
        )

        # ── Histograms ────────────────────────────────────────────────────────
        # Histograms record the distribution of values (here: latencies).
        # The default buckets cover 0.5 s → 300 s, which covers both quick
        # stub runs and real Ollama calls.

        self._micro_duration = Histogram(
            "tinker_micro_loop_duration_seconds",
            "Wall-clock time in seconds for each completed micro loop.",
            buckets=[0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300],
        )
        self._meso_duration = Histogram(
            "tinker_meso_loop_duration_seconds",
            "Wall-clock time in seconds for each completed meso synthesis.",
            buckets=[1, 5, 10, 30, 60, 120, 300, 600],
        )

        # Start the HTTP server that Prometheus scrapes.
        # start_http_server() spawns a daemon thread; it doesn't block.
        try:
            start_http_server(self._port)
            logger.info(
                "Prometheus metrics server started on port %d — "
                "scrape at http://localhost:%d/metrics",
                self._port,
                self._port,
            )
        except OSError as exc:
            # Port already in use or permission denied.  Log and continue —
            # a failed metrics server should never crash Tinker.
            logger.warning("Could not start metrics server on port %d: %s", self._port, exc)
            self._enabled = False

    # ── Hooks called by the Orchestrator ────────────────────────────────────

    def on_micro_loop(self, record: Any) -> None:
        """
        Update metrics after a micro loop completes.

        Called from ``orchestrator.py:_run_micro()`` on every successful loop.
        Also updates the queue-depth gauge and consecutive-failures gauge so
        they stay current even if no stagnation events occur.

        Parameters
        ----------
        record : MicroLoopRecord from the completed micro loop.
        """
        if not self._enabled:
            return

        status = getattr(record, "status", None)
        status_label = status.value if status is not None else "unknown"

        self._micro_total.labels(status=status_label).inc()

        # Duration (monotonic seconds → histogram bucket)
        duration = record.duration() if hasattr(record, "duration") else 0.0
        self._micro_duration.observe(duration)

        # Critic score gauge — only update when a score is available.
        score = getattr(record, "critic_score", None)
        if score is not None:
            self._critic_score.set(float(score))

    def on_meso_loop(self, record: Any) -> None:
        """
        Update metrics after a meso synthesis completes.

        Parameters
        ----------
        record : MesoLoopRecord from the completed meso synthesis.
        """
        if not self._enabled:
            return

        status = getattr(record, "status", None)
        self._meso_total.labels(status=status.value if status else "unknown").inc()

        duration = record.duration() if hasattr(record, "duration") else 0.0
        self._meso_duration.observe(duration)

    def on_macro_loop(self, record: Any) -> None:
        """
        Update metrics after a macro snapshot commit.

        Parameters
        ----------
        record : MacroLoopRecord from the completed macro run.
        """
        if not self._enabled:
            return

        status = getattr(record, "status", None)
        self._macro_total.labels(status=status.value if status else "unknown").inc()

    def on_stagnation(self, directive: Any) -> None:
        """
        Increment the stagnation counter when the StagnationMonitor fires.

        Parameters
        ----------
        directive : InterventionDirective from the StagnationMonitor.
        """
        if not self._enabled:
            return

        stagnation_type = getattr(directive, "stagnation_type", None)
        intervention = getattr(directive, "intervention_type", None)
        self._stagnation_total.labels(
            stagnation_type=stagnation_type.value if stagnation_type else "unknown",
            intervention=intervention.value if intervention else "unknown",
        ).inc()

    def update_gauges(self, queue_depth: int, consecutive_failures: int) -> None:
        """
        Refresh the queue-depth and failure-streak gauges.

        Call this from the main loop (e.g. after each snapshot write) so the
        gauges reflect the current system state between micro loops.

        Parameters
        ----------
        queue_depth           : Current number of pending tasks.
        consecutive_failures  : Current micro-loop failure streak.
        """
        if not self._enabled:
            return

        self._queue_depth.set(queue_depth)
        self._consecutive_failures.set(consecutive_failures)
