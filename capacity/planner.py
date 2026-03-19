"""
capacity/planner.py
====================

Capacity planning and resource usage tracking for Tinker.

Why capacity planning?
-----------------------
Without capacity awareness, Tinker can:
  - Exhaust disk space (unbounded artifact storage)
  - Consume all available GPU memory (unbounded context windows)
  - Generate surprise LLM API bills (unbounded token consumption)
  - Hit Redis memory limits (too many working memory keys)

This module tracks resource usage over time and generates projections
so operators can plan ahead and set appropriate limits.

Usage
------
::

    planner = CapacityPlanner()

    # Record measurements after each loop:
    planner.record_tokens(micro_tokens=1500, meso_tokens=0, macro_tokens=0)
    planner.record_disk_usage()   # auto-detects from configured paths
    planner.record_artifact_count(total=42, archived=8)

    # Get a capacity report:
    report = planner.report()
    print(report)
    # {
    #   "tokens_per_hour": 15000,
    #   "estimated_tokens_per_day": 360000,
    #   "disk_growth_mb_per_hour": 5.2,
    #   "estimated_disk_full_in_hours": 48,
    #   "artifact_growth_per_hour": 12,
    # }

    # Set thresholds and check if they're exceeded:
    planner.set_threshold("disk_mb", 10000)   # 10GB disk limit
    alerts = planner.check_thresholds()
    for alert in alerts:
        print(alert)
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Optional

logger = logging.getLogger(__name__)


@dataclass
class CapacitySnapshot:
    """A point-in-time capacity measurement."""

    timestamp: float = field(default_factory=time.monotonic)
    tokens_used: int = 0
    disk_mb: float = 0.0
    artifact_count: int = 0
    redis_memory_mb: float = 0.0


class CapacityPlanner:
    """
    Tracks and projects Tinker's resource consumption.

    Parameters
    ----------
    workspace_path : Path to the workspace directory for disk usage tracking.
    artifact_path  : Path to artifact output directory.
    window_size    : Number of snapshots to keep for trend analysis (default: 200).
    """

    def __init__(
        self,
        workspace_path: str = "./tinker_workspace",
        artifact_path: str = "./tinker_artifacts",
        window_size: int = 200,
    ) -> None:
        self._workspace_path = Path(workspace_path)
        self._artifact_path = Path(artifact_path)
        self._window_size = window_size
        self._snapshots: Deque[CapacitySnapshot] = deque(maxlen=window_size)
        self._thresholds: dict[str, float] = {}
        self._start_time = time.monotonic()
        self._total_tokens = 0
        self._first_snapshot: Optional[CapacitySnapshot] = None

    def record_tokens(
        self, micro_tokens: int = 0, meso_tokens: int = 0, macro_tokens: int = 0
    ) -> None:
        """
        Record LLM token consumption for one loop iteration.

        Call this after each micro/meso/macro loop with the token counts.
        """
        tokens = micro_tokens + meso_tokens + macro_tokens
        self._total_tokens += tokens
        # Update the latest snapshot if it's recent (< 60s old)
        if self._snapshots and time.monotonic() - self._snapshots[-1].timestamp < 60:
            self._snapshots[-1].tokens_used += tokens
        else:
            snap = CapacitySnapshot(tokens_used=tokens)
            self._snapshots.append(snap)
            if self._first_snapshot is None:
                self._first_snapshot = snap

    def record_disk_usage(self) -> None:
        """
        Measure current disk usage of Tinker's data directories.

        Reads the actual file system rather than estimating.
        """
        total_mb = 0.0
        for path in (self._workspace_path, self._artifact_path):
            if path.exists():
                size_bytes = sum(
                    f.stat().st_size for f in path.rglob("*") if f.is_file()
                )
                total_mb += size_bytes / (1024 * 1024)

        if self._snapshots:
            self._snapshots[-1].disk_mb = total_mb
        else:
            self._snapshots.append(CapacitySnapshot(disk_mb=total_mb))

    def record_artifact_count(self, total: int, archived: int = 0) -> None:
        """Record the current artifact count."""
        if self._snapshots:
            self._snapshots[-1].artifact_count = total
        else:
            self._snapshots.append(CapacitySnapshot(artifact_count=total))

    def set_threshold(self, resource: str, max_value: float) -> None:
        """
        Set a capacity threshold that triggers an alert if exceeded.

        Parameters
        ----------
        resource  : Resource name ("tokens_per_day", "disk_mb", "artifact_count").
        max_value : Alert threshold value.
        """
        self._thresholds[resource] = max_value

    def check_thresholds(self) -> list[str]:
        """
        Check if any thresholds are exceeded or at risk.

        Returns a list of alert strings (empty list = all clear).
        """
        report = self.report()
        alerts = []

        if "disk_mb" in self._thresholds:
            current_mb = report.get("current_disk_mb", 0)
            max_mb = self._thresholds["disk_mb"]
            if current_mb > max_mb:
                alerts.append(
                    f"DISK EXCEEDED: {current_mb:.0f}MB > {max_mb:.0f}MB limit"
                )
            elif current_mb > max_mb * 0.8:
                alerts.append(
                    f"DISK WARNING: {current_mb:.0f}MB = {current_mb / max_mb * 100:.0f}% "
                    f"of {max_mb:.0f}MB limit"
                )

        if "tokens_per_day" in self._thresholds:
            est = report.get("estimated_tokens_per_day", 0)
            max_t = self._thresholds["tokens_per_day"]
            if est > max_t:
                alerts.append(f"TOKEN RATE EXCEEDED: {est:,}/day > {max_t:,}/day limit")

        return alerts

    def report(self) -> dict:
        """
        Generate a capacity report with current usage and projections.

        Returns
        -------
        dict : Current usage, hourly rates, and projections.
        """
        report: dict = {
            "total_tokens_used": self._total_tokens,
            "current_disk_mb": 0.0,
            "current_artifact_count": 0,
        }

        if not self._snapshots:
            return report

        latest = self._snapshots[-1]
        report["current_disk_mb"] = round(latest.disk_mb, 2)
        report["current_artifact_count"] = latest.artifact_count

        # Calculate hourly rates if we have enough data
        elapsed_hours = (time.monotonic() - self._start_time) / 3600
        if elapsed_hours > 0.05:  # At least 3 minutes of data
            report["tokens_per_hour"] = round(self._total_tokens / elapsed_hours)
            report["estimated_tokens_per_day"] = round(
                self._total_tokens / elapsed_hours * 24
            )

        # Disk growth rate (compare first and latest snapshots)
        if self._first_snapshot and len(self._snapshots) >= 5:
            first = self._first_snapshot
            first_elapsed = (latest.timestamp - first.timestamp) / 3600
            if first_elapsed > 0.05:
                disk_growth_per_hour = (latest.disk_mb - first.disk_mb) / first_elapsed
                report["disk_growth_mb_per_hour"] = round(disk_growth_per_hour, 2)

        return report
