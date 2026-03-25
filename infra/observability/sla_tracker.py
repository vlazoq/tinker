"""
infra/observability/sla_tracker.py
==============================

SLA (Service Level Agreement) tracking and enforcement for Tinker loops.

What is an SLA in Tinker's context?
-------------------------------------
An SLA defines a performance target for each loop type:
  - "95% of micro loops should complete in under 60 seconds"
  - "99% of micro loops should complete in under 120 seconds"
  - "Meso loops should complete in under 300 seconds"

Without SLA tracking:
  - You don't know if the system is getting slower over time
  - You can't trigger alerts when performance degrades
  - You have no baseline for autoscaling decisions
  - SLA violations go unnoticed until users complain

Usage
------
::

    sla = SLATracker()
    sla.define("micro_loop", p95_seconds=60.0, p99_seconds=120.0)
    sla.define("meso_loop",  p95_seconds=180.0, p99_seconds=300.0)
    sla.define("macro_loop", p95_seconds=600.0, p99_seconds=900.0)

    # After each loop, record the duration:
    sla.record("micro_loop", duration_seconds=42.3)
    sla.record("meso_loop",  duration_seconds=210.5)

    # Check for SLA breaches:
    report = sla.report("micro_loop")
    print(report)
    # {
    #   "name": "micro_loop",
    #   "count": 100,
    #   "p50_s": 35.2,
    #   "p95_s": 58.1,     ← below 60s target: PASS
    #   "p99_s": 125.0,    ← above 120s target: BREACH
    #   "sla_p95_breach": False,
    #   "sla_p99_breach": True,
    # }
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

logger = logging.getLogger(__name__)


@dataclass
class SLADefinition:
    """
    SLA targets for a single loop type.

    Attributes
    ----------
    name         : Loop type name (e.g. "micro_loop").
    p95_seconds  : 95th percentile target (most calls must be faster than this).
    p99_seconds  : 99th percentile target (99% of calls must be faster than this).
    max_seconds  : Hard maximum (any call slower than this is a critical breach).
    window_size  : Rolling window of measurements to use for percentile calculation.
    """

    name: str
    p95_seconds: float = 60.0
    p99_seconds: float = 120.0
    max_seconds: Optional[float] = None
    window_size: int = 200  # Keep last 200 measurements


@dataclass
class SLAReport:
    """
    SLA compliance report for one loop type at a point in time.

    Attributes
    ----------
    name          : Loop type name.
    count         : Total measurements recorded.
    p50_s         : 50th percentile (median) duration.
    p95_s         : 95th percentile duration.
    p99_s         : 99th percentile duration.
    max_s         : Maximum observed duration.
    avg_s         : Average duration.
    sla_p95       : The p95 SLA target.
    sla_p99       : The p99 SLA target.
    p95_breach    : True if p95 exceeds the SLA target.
    p99_breach    : True if p99 exceeds the SLA target.
    max_breach    : True if any measurement exceeded the hard max.
    breach_count  : Number of measurements that exceeded the p99 target.
    """

    name: str
    count: int = 0
    p50_s: float = 0.0
    p95_s: float = 0.0
    p99_s: float = 0.0
    max_s: float = 0.0
    avg_s: float = 0.0
    sla_p95: float = 0.0
    sla_p99: float = 0.0
    p95_breach: bool = False
    p99_breach: bool = False
    max_breach: bool = False
    breach_count: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "count": self.count,
            "p50_s": round(self.p50_s, 2),
            "p95_s": round(self.p95_s, 2),
            "p99_s": round(self.p99_s, 2),
            "max_s": round(self.max_s, 2),
            "avg_s": round(self.avg_s, 2),
            "sla_p95": self.sla_p95,
            "sla_p99": self.sla_p99,
            "p95_breach": self.p95_breach,
            "p99_breach": self.p99_breach,
            "max_breach": self.max_breach,
            "breach_count": self.breach_count,
        }


def _percentile(sorted_data: list[float], pct: float) -> float:
    """Return the p-th percentile of a sorted list (0 < pct < 100)."""
    if not sorted_data:
        return 0.0
    k = (len(sorted_data) - 1) * pct / 100
    floor_k = int(k)
    ceil_k = min(floor_k + 1, len(sorted_data) - 1)
    frac = k - floor_k
    return sorted_data[floor_k] * (1 - frac) + sorted_data[ceil_k] * frac


class SLATracker:
    """
    Tracks SLA compliance across all Tinker loop types.

    Parameters
    ----------
    alert_on_breach : Optional callable(report) invoked when an SLA is breached.
                      Signature: ``callback(report: SLAReport) -> None``
    """

    def __init__(self, alert_on_breach=None) -> None:
        self._definitions: dict[str, SLADefinition] = {}
        self._measurements: dict[str, Deque[float]] = {}
        self._breach_count: dict[str, int] = {}
        self._alert_on_breach = alert_on_breach

    def define(
        self,
        name: str,
        p95_seconds: float = 60.0,
        p99_seconds: float = 120.0,
        max_seconds: Optional[float] = None,
        window_size: int = 200,
    ) -> None:
        """
        Define an SLA for a loop type.

        Call this once at startup for each loop type.  If the SLA already
        exists it will be overwritten.

        Parameters
        ----------
        name         : Loop type name (e.g. "micro_loop").
        p95_seconds  : 95th-percentile SLA target in seconds.
        p99_seconds  : 99th-percentile SLA target in seconds.
        max_seconds  : Optional hard maximum (None = no hard limit).
        window_size  : Rolling window for percentile calculations.
        """
        self._definitions[name] = SLADefinition(
            name=name,
            p95_seconds=p95_seconds,
            p99_seconds=p99_seconds,
            max_seconds=max_seconds,
            window_size=window_size,
        )
        self._measurements[name] = deque(maxlen=window_size)
        self._breach_count[name] = 0
        logger.debug(
            "SLA defined: %s (p95=%.1fs, p99=%.1fs)", name, p95_seconds, p99_seconds
        )

    def record(self, name: str, duration_seconds: float) -> Optional[SLAReport]:
        """
        Record a loop duration measurement.

        If the measurement breaches the SLA, generates a report and calls
        the alert callback if configured.

        Parameters
        ----------
        name             : Loop type name.
        duration_seconds : The measured duration.

        Returns
        -------
        SLAReport if a breach was detected, None otherwise.
        """
        if name not in self._definitions:
            # Auto-create a lenient SLA if not explicitly defined
            self.define(name, p95_seconds=300.0, p99_seconds=600.0)

        sla_def = self._definitions[name]
        self._measurements[name].append(duration_seconds)

        # Check hard maximum breach
        max_breach = (
            sla_def.max_seconds is not None and duration_seconds > sla_def.max_seconds
        )

        if max_breach:
            self._breach_count[name] += 1
            logger.warning(
                "SLA BREACH: %s duration=%.1fs exceeds max=%.1fs",
                name,
                duration_seconds,
                sla_def.max_seconds,
            )

        # Check p99 breach (only relevant once we have enough data)
        measurements = list(self._measurements[name])
        if len(measurements) >= 10:
            report = self.report(name)
            if report.p99_breach or report.p95_breach or max_breach:
                if self._alert_on_breach:
                    try:
                        self._alert_on_breach(report)
                    except Exception as exc:
                        logger.warning("SLA breach alert callback raised: %s", exc)
                return report

        return None

    def report(self, name: str) -> SLAReport:
        """
        Generate a compliance report for a loop type.

        Parameters
        ----------
        name : Loop type name.

        Returns
        -------
        SLAReport with current percentile values and breach status.
        """
        if name not in self._definitions:
            return SLAReport(name=name)

        sla_def = self._definitions[name]
        data = sorted(self._measurements.get(name, []))
        n = len(data)

        if n == 0:
            return SLAReport(
                name=name, sla_p95=sla_def.p95_seconds, sla_p99=sla_def.p99_seconds
            )

        p50 = _percentile(data, 50)
        p95 = _percentile(data, 95)
        p99 = _percentile(data, 99)
        max_val = data[-1]
        avg = sum(data) / n

        breach_count = sum(1 for d in data if d > sla_def.p99_seconds)

        return SLAReport(
            name=name,
            count=n,
            p50_s=p50,
            p95_s=p95,
            p99_s=p99,
            max_s=max_val,
            avg_s=avg,
            sla_p95=sla_def.p95_seconds,
            sla_p99=sla_def.p99_seconds,
            p95_breach=p95 > sla_def.p95_seconds,
            p99_breach=p99 > sla_def.p99_seconds,
            max_breach=(
                sla_def.max_seconds is not None and max_val > sla_def.max_seconds
            ),
            breach_count=breach_count,
        )

    def all_reports(self) -> dict[str, dict]:
        """Return compliance reports for all defined SLAs."""
        return {name: self.report(name).to_dict() for name in self._definitions}


def build_default_sla_tracker(alert_on_breach=None) -> SLATracker:
    """
    Create an SLATracker with Tinker's standard SLA definitions.

    Returns
    -------
    SLATracker pre-configured for all loop types.
    """
    tracker = SLATracker(alert_on_breach=alert_on_breach)

    # Micro loop: should be fast — each task → architect → critic → store
    tracker.define("micro_loop", p95_seconds=60.0, p99_seconds=120.0, max_seconds=300.0)

    # Meso loop: synthesises multiple artifacts — allowed to be slower
    tracker.define("meso_loop", p95_seconds=180.0, p99_seconds=300.0, max_seconds=600.0)

    # Macro loop: reads all memory — can be very slow
    tracker.define(
        "macro_loop", p95_seconds=300.0, p99_seconds=600.0, max_seconds=1800.0
    )

    # Context assembly: memory retrieval — should be fast
    tracker.define("context_assembly", p95_seconds=15.0, p99_seconds=30.0)

    # Architect call: model inference
    tracker.define("architect_call", p95_seconds=60.0, p99_seconds=120.0)

    # Critic call: lighter model
    tracker.define("critic_call", p95_seconds=30.0, p99_seconds=60.0)

    return tracker
