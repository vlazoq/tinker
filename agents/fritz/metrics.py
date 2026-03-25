"""
fritz/metrics.py
─────────────────
Optional Prometheus metrics for Fritz git/GitHub/Gitea operations.

Follows the exact same pattern as Tinker's top-level metrics.py:
  - Silent no-op if prometheus-client is not installed
  - FritzMetrics is instantiated once in FritzAgent.setup()
  - Every method is safe to call unconditionally

Quick start
───────────
    pip install prometheus-client
    # Then metrics appear automatically at http://localhost:9091/metrics

Available metrics
─────────────────
fritz_commits_total{status}
    Counter. Git commits created (status = success | failed).

fritz_pushes_total{status, remote}
    Counter. Git push operations (remote = origin | upstream | …).

fritz_prs_total{status, platform}
    Counter. Pull requests created (platform = github | gitea).

fritz_merges_total{status, method}
    Counter. PRs merged (method = squash | merge | rebase).

fritz_api_calls_total{platform, operation, status}
    Counter. Raw API calls to GitHub/Gitea.
    status = 2xx | 4xx | 5xx | error | rate_limited | retried

fritz_api_call_duration_seconds{platform, operation}
    Histogram. Wall-clock time for each API call (post-retry).

fritz_rate_limit_remaining{platform}
    Gauge. Most recently seen X-RateLimit-Remaining value.
    -1 = header not yet received.

fritz_retry_total{platform, operation}
    Counter. Number of API call retries (does not count first attempts).

Environment variables
─────────────────────
FRITZ_METRICS_PORT     HTTP port (default: 9091, avoids conflict with Tinker's 9090).
FRITZ_METRICS_ENABLED  Set to "false" to disable entirely.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

try:
    import prometheus_client as _prom
    from prometheus_client import Counter, Gauge, Histogram, start_http_server

    _PROM_AVAILABLE = True
except ImportError:
    _prom = None  # type: ignore[assignment]
    _PROM_AVAILABLE = False


class FritzMetrics:
    """
    Thin Prometheus wrapper for Fritz.  Every method is a no-op when
    prometheus-client is absent; callers never need to check.
    """

    def __init__(
        self,
        port: int | None = None,
        enabled: bool | None = None,
    ) -> None:
        if enabled is None:
            enabled = os.getenv("FRITZ_METRICS_ENABLED", "true").lower() != "false"

        self._enabled = enabled and _PROM_AVAILABLE

        if not self._enabled:
            if enabled and not _PROM_AVAILABLE:
                logger.info(
                    "Fritz metrics disabled: prometheus-client not installed. "
                    "Install with: pip install prometheus-client"
                )
            return

        self._port = port or int(os.getenv("FRITZ_METRICS_PORT", "9091"))

        # ── Counters ──────────────────────────────────────────────────────────
        self._commits = Counter(
            "fritz_commits_total",
            "Git commits created by Fritz.",
            ["status"],
        )
        self._pushes = Counter(
            "fritz_pushes_total",
            "Git push operations performed by Fritz.",
            ["status", "remote"],
        )
        self._prs = Counter(
            "fritz_prs_total",
            "Pull requests created by Fritz.",
            ["status", "platform"],
        )
        self._merges = Counter(
            "fritz_merges_total",
            "Pull requests merged by Fritz.",
            ["status", "method"],
        )
        self._api_calls = Counter(
            "fritz_api_calls_total",
            "Raw API calls made by Fritz to GitHub or Gitea.",
            ["platform", "operation", "status"],
        )
        self._retries = Counter(
            "fritz_retry_total",
            "Number of API call retries (excludes first attempts).",
            ["platform", "operation"],
        )

        # ── Gauges ────────────────────────────────────────────────────────────
        self._rate_limit_remaining = Gauge(
            "fritz_rate_limit_remaining",
            "Most recently observed X-RateLimit-Remaining for each platform. "
            "-1 = header not yet received.",
            ["platform"],
        )
        # Initialise to -1 (unknown) for all known platforms.
        for p in ("github", "gitea"):
            self._rate_limit_remaining.labels(platform=p).set(-1)

        # ── Histograms ────────────────────────────────────────────────────────
        self._api_duration = Histogram(
            "fritz_api_call_duration_seconds",
            "Wall-clock time in seconds for Fritz API calls (after all retries).",
            ["platform", "operation"],
            buckets=[0.1, 0.25, 0.5, 1, 2, 5, 10, 30],
        )

        # Start HTTP server.
        try:
            start_http_server(self._port)
            logger.info(
                "Fritz metrics server started on port %d — "
                "scrape at http://localhost:%d/metrics",
                self._port,
                self._port,
            )
        except OSError as exc:
            logger.warning(
                "Could not start Fritz metrics server on port %d: %s",
                self._port, exc,
            )
            self._enabled = False

    # ── Hooks called by FritzAgent / ops drivers ──────────────────────────────

    def on_commit(self, success: bool) -> None:
        """Record a git commit attempt."""
        if not self._enabled:
            return
        self._commits.labels(status="success" if success else "failed").inc()

    def on_push(self, success: bool, remote: str = "origin") -> None:
        """Record a git push attempt."""
        if not self._enabled:
            return
        self._pushes.labels(
            status="success" if success else "failed",
            remote=remote,
        ).inc()

    def on_pr_created(self, success: bool, platform: str = "github") -> None:
        """Record a PR creation."""
        if not self._enabled:
            return
        self._prs.labels(
            status="success" if success else "failed",
            platform=platform,
        ).inc()

    def on_merge(self, success: bool, method: str = "squash") -> None:
        """Record a PR merge."""
        if not self._enabled:
            return
        self._merges.labels(
            status="success" if success else "failed",
            method=method,
        ).inc()

    def on_api_call(
        self,
        platform: str,
        operation: str,
        http_status: int | None,
        duration_seconds: float,
        retries: int = 0,
    ) -> None:
        """
        Record a completed API call (after all retries).

        Args:
            platform:         "github" or "gitea"
            operation:        e.g. "create_pr", "merge_pr", "get_ci_status"
            http_status:      Final HTTP status code, or None if a network error.
            duration_seconds: Total wall time including all retry sleeps.
            retries:          Number of retries that occurred (0 = first attempt succeeded).
        """
        if not self._enabled:
            return

        if http_status is None:
            status_label = "error"
        elif 200 <= http_status < 300:
            status_label = "2xx"
        elif http_status in (429, 403):
            status_label = "rate_limited"
        elif 400 <= http_status < 500:
            status_label = "4xx"
        else:
            status_label = "5xx"

        self._api_calls.labels(
            platform=platform,
            operation=operation,
            status=status_label,
        ).inc()

        self._api_duration.labels(
            platform=platform,
            operation=operation,
        ).observe(duration_seconds)

        if retries > 0:
            self._retries.labels(platform=platform, operation=operation).inc(retries)

    def update_rate_limit(self, platform: str, remaining: int) -> None:
        """Update the rate-limit-remaining gauge for a platform."""
        if not self._enabled:
            return
        self._rate_limit_remaining.labels(platform=platform).set(remaining)


# ── Module-level singleton (lazy, shared across all FritzAgent instances) ─────
_default_metrics: FritzMetrics | None = None


def get_metrics() -> FritzMetrics:
    """Return the shared FritzMetrics singleton (created on first call)."""
    global _default_metrics
    if _default_metrics is None:
        _default_metrics = FritzMetrics()
    return _default_metrics
