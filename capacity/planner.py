"""
capacity/planner.py
====================

Capacity planning and resource usage tracking for Tinker.

Why capacity planning?
-----------------------
Without capacity awareness, Tinker can:
  - Exhaust disk space (unbounded artifact storage)
  - Consume all available GPU memory (unbounded context windows)
  - Hit Redis memory limits (too many working memory keys)

This module tracks resource usage over time and generates projections
so operators can plan ahead and set appropriate limits.

Key additions over the baseline
---------------------------------
- **Per-loop-type token tracking**: micro/meso/macro tokens counted separately
  so you can see where tokens are actually going.
- **Disk-full projection**: uses ``shutil.disk_usage()`` to measure actual
  free space on the partition, not just Tinker's own data size.
- **Cost estimation**: rough mapping from token counts to cost.  For local
  Ollama, reports estimated GPU-hours consumed (based on configurable
  tokens/second).  For hypothetical cloud usage, reports $/1M-token estimate.
- **check_thresholds** integrates disk-full projection: alerts when the
  partition will be full in fewer than ``disk_hours_warning`` hours.

Usage
------
::

    planner = CapacityPlanner(
        tokens_per_second=50,   # tune to your GPU speed
    )

    # Record measurements after each loop:
    planner.record_tokens(micro_tokens=1500, meso_tokens=0, macro_tokens=0)
    planner.record_disk_usage()   # reads actual partition free space
    planner.record_artifact_count(total=42, archived=8)

    # Get a capacity report:
    report = planner.report()
    # {
    #   "tokens_per_hour": 15000,
    #   "estimated_tokens_per_day": 360000,
    #   "tokens_by_type": {"micro": 14000, "meso": 1000, "macro": 0},
    #   "disk_growth_mb_per_hour": 5.2,
    #   "disk_free_gb": 42.3,
    #   "estimated_disk_full_in_hours": 48.0,
    #   "gpu_hours_used": 0.08,
    #   "cost_estimate_usd": 0.09,   # hypothetical cloud cost
    # }

    # Set thresholds and check if they're exceeded:
    planner.set_threshold("disk_mb", 10000)   # 10GB data-size limit
    alerts = planner.check_thresholds()
    for alert in alerts:
        print(alert)
"""

from __future__ import annotations

import logging
import shutil
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
    micro_tokens: int = 0
    meso_tokens: int = 0
    macro_tokens: int = 0
    disk_mb: float = 0.0
    disk_free_gb: float = 0.0
    artifact_count: int = 0
    redis_memory_mb: float = 0.0


# Cost constants (adjustable)
# Local Ollama: approximate GPU watt draw at full load for a mid-range GPU
_GPU_WATTS = 150.0  # watts during inference
_KWH_COST_USD = 0.12  # $/kWh (US average)
# Cloud reference cost (hypothetical, for comparison only)
_CLOUD_COST_PER_1M_TOKENS = 3.0  # $/1M tokens (roughly Sonnet-class)


class CapacityPlanner:
    """
    Tracks and projects Tinker's resource consumption.

    Parameters
    ----------
    workspace_path   : Path to the workspace directory for disk usage tracking.
    artifact_path    : Path to artifact output directory.
    window_size      : Number of snapshots to keep for trend analysis (default: 200).
    tokens_per_second: Approximate inference speed of the local model (default: 50).
                       Used to estimate GPU-hours consumed.
    disk_hours_warning: Alert in check_thresholds when disk will be full within
                        this many hours (default: 24).
    """

    def __init__(
        self,
        workspace_path: str = "./tinker_workspace",
        artifact_path: str = "./tinker_artifacts",
        window_size: int = 200,
        tokens_per_second: float = 50.0,
        disk_hours_warning: float = 24.0,
    ) -> None:
        self._workspace_path = Path(workspace_path)
        self._artifact_path = Path(artifact_path)
        self._window_size = window_size
        self._tokens_per_second = max(tokens_per_second, 1.0)
        self._disk_hours_warning = disk_hours_warning
        self._snapshots: Deque[CapacitySnapshot] = deque(maxlen=window_size)
        self._thresholds: dict[str, float] = {}
        self._start_time = time.monotonic()
        self._total_tokens = 0
        self._total_micro_tokens = 0
        self._total_meso_tokens = 0
        self._total_macro_tokens = 0
        self._first_snapshot: Optional[CapacitySnapshot] = None

    def record_tokens(
        self, micro_tokens: int = 0, meso_tokens: int = 0, macro_tokens: int = 0
    ) -> None:
        """
        Record LLM token consumption for one loop iteration.

        Tracks totals per-type (micro/meso/macro) for the breakdown report.
        Call this after each micro/meso/macro loop with the token counts.
        """
        tokens = micro_tokens + meso_tokens + macro_tokens
        self._total_tokens += tokens
        self._total_micro_tokens += micro_tokens
        self._total_meso_tokens += meso_tokens
        self._total_macro_tokens += macro_tokens

        # Update the latest snapshot if it's recent (< 60s old)
        if self._snapshots and time.monotonic() - self._snapshots[-1].timestamp < 60:
            self._snapshots[-1].tokens_used += tokens
            self._snapshots[-1].micro_tokens += micro_tokens
            self._snapshots[-1].meso_tokens += meso_tokens
            self._snapshots[-1].macro_tokens += macro_tokens
        else:
            snap = CapacitySnapshot(
                tokens_used=tokens,
                micro_tokens=micro_tokens,
                meso_tokens=meso_tokens,
                macro_tokens=macro_tokens,
            )
            self._snapshots.append(snap)
            if self._first_snapshot is None:
                self._first_snapshot = snap

    def record_disk_usage(self) -> None:
        """
        Measure current disk usage of Tinker's data directories and read
        the actual free space on the partition.
        """
        total_mb = 0.0
        for path in (self._workspace_path, self._artifact_path):
            if path.exists():
                size_bytes = sum(
                    f.stat().st_size for f in path.rglob("*") if f.is_file()
                )
                total_mb += size_bytes / (1024 * 1024)

        # Actual partition free space
        disk_free_gb = 0.0
        try:
            usage = shutil.disk_usage(
                str(self._workspace_path) if self._workspace_path.exists() else "."
            )
            disk_free_gb = usage.free / (1024**3)
        except Exception:
            pass

        if self._snapshots:
            self._snapshots[-1].disk_mb = total_mb
            self._snapshots[-1].disk_free_gb = disk_free_gb
        else:
            self._snapshots.append(
                CapacitySnapshot(disk_mb=total_mb, disk_free_gb=disk_free_gb)
            )

    def record_artifact_count(self, total: int, archived: int = 0) -> None:
        """Record the current artifact count."""
        if self._snapshots:
            self._snapshots[-1].artifact_count = total
        else:
            self._snapshots.append(CapacitySnapshot(artifact_count=total))

    def record_redis_memory(self, used_mb: float) -> None:
        """
        Record current Redis working-memory usage in megabytes.

        Call this periodically (e.g. via ``INFO memory`` from the Redis client)
        to populate the ``redis_memory_mb`` field in snapshots.

        Parameters
        ----------
        used_mb : Redis ``used_memory`` in megabytes.
        """
        if self._snapshots:
            self._snapshots[-1].redis_memory_mb = used_mb
        else:
            self._snapshots.append(CapacitySnapshot(redis_memory_mb=used_mb))

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

        Also checks disk-full projection: warns if the partition will be
        exhausted within ``disk_hours_warning`` hours based on the current
        growth rate.

        Returns a list of alert strings (empty list = all clear).
        """
        rpt = self.report()
        alerts = []

        if "disk_mb" in self._thresholds:
            current_mb = rpt.get("current_disk_mb", 0)
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
            est = rpt.get("estimated_tokens_per_day", 0)
            max_t = self._thresholds["tokens_per_day"]
            if est > max_t:
                alerts.append(f"TOKEN RATE EXCEEDED: {est:,}/day > {max_t:,}/day limit")

        if "artifact_count" in self._thresholds:
            current_count = rpt.get("current_artifact_count", 0)
            max_count = int(self._thresholds["artifact_count"])
            if current_count > max_count:
                alerts.append(
                    f"ARTIFACT COUNT EXCEEDED: {current_count} > {max_count} limit"
                )
            elif current_count > max_count * 0.8:
                alerts.append(
                    f"ARTIFACT COUNT WARNING: {current_count} = "
                    f"{current_count / max_count * 100:.0f}% of {max_count} limit"
                )

        # Disk-full projection using actual partition free space
        disk_full_hours = rpt.get("estimated_disk_full_in_hours")
        if disk_full_hours is not None and disk_full_hours < self._disk_hours_warning:
            alerts.append(
                f"DISK FULL SOON: partition will be full in ~{disk_full_hours:.0f}h "
                f"({rpt.get('disk_free_gb', 0):.1f}GB free)"
            )

        return alerts

    def report(self) -> dict:
        """
        Generate a capacity report with current usage and projections.

        Returns
        -------
        dict with keys:
          total_tokens_used, tokens_by_type (micro/meso/macro),
          current_disk_mb, disk_free_gb,
          tokens_per_hour, estimated_tokens_per_day,
          disk_growth_mb_per_hour, estimated_disk_full_in_hours,
          gpu_hours_used, cost_estimate_usd (hypothetical cloud cost).
        """
        rpt: dict = {
            "total_tokens_used": self._total_tokens,
            "tokens_by_type": {
                "micro": self._total_micro_tokens,
                "meso": self._total_meso_tokens,
                "macro": self._total_macro_tokens,
            },
            "current_disk_mb": 0.0,
            "disk_free_gb": 0.0,
            "current_artifact_count": 0,
        }

        if not self._snapshots:
            return rpt

        latest = self._snapshots[-1]
        rpt["current_disk_mb"] = round(latest.disk_mb, 2)
        rpt["disk_free_gb"] = round(latest.disk_free_gb, 2)
        rpt["current_artifact_count"] = latest.artifact_count
        rpt["redis_memory_mb"] = round(latest.redis_memory_mb, 2)

        # Calculate hourly rates if we have enough data
        elapsed_hours = (time.monotonic() - self._start_time) / 3600
        if elapsed_hours > 0.05:  # At least 3 minutes of data
            rpt["tokens_per_hour"] = round(self._total_tokens / elapsed_hours)
            rpt["estimated_tokens_per_day"] = round(
                self._total_tokens / elapsed_hours * 24
            )

            # GPU time: total_tokens / tokens_per_second → seconds → hours
            gpu_seconds = self._total_tokens / self._tokens_per_second
            rpt["gpu_hours_used"] = round(gpu_seconds / 3600, 4)

            # Electricity cost: GPU watts × hours × $/kWh
            rpt["electricity_cost_usd"] = round(
                (_GPU_WATTS / 1000) * (gpu_seconds / 3600) * _KWH_COST_USD, 4
            )

            # Hypothetical cloud cost (for comparison)
            rpt["cloud_cost_estimate_usd"] = round(
                self._total_tokens / 1_000_000 * _CLOUD_COST_PER_1M_TOKENS, 4
            )

        # Disk growth rate (compare first and latest snapshots)
        if self._first_snapshot and len(self._snapshots) >= 5:
            first = self._first_snapshot
            first_elapsed = (latest.timestamp - first.timestamp) / 3600
            if first_elapsed > 0.05:
                disk_growth_per_hour = (latest.disk_mb - first.disk_mb) / first_elapsed
                rpt["disk_growth_mb_per_hour"] = round(disk_growth_per_hour, 2)

                # Disk-full projection using actual free space on partition
                if disk_growth_per_hour > 0 and latest.disk_free_gb > 0:
                    free_mb = latest.disk_free_gb * 1024
                    hours_to_full = free_mb / disk_growth_per_hour
                    rpt["estimated_disk_full_in_hours"] = round(hours_to_full, 1)

        return rpt
