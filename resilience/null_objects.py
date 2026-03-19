"""
resilience/null_objects.py — Null Object implementations for optional enterprise components.

Why Null Objects?
-----------------
Optional enterprise features (alerter, audit log, lineage tracker, rate
limiter, stagnation monitor) are currently guarded throughout the codebase
with patterns like:

    if alerter is not None:
        await alerter.alert(...)

This violates the Open/Closed Principle: every consumer must know whether
its dependency is optional and include the None guard.  It also makes the
code noisy and error-prone (missing guards silently skip the feature).

The Null Object Pattern replaces None with an object that has the same
interface but does nothing.  Callers then use the dependency unconditionally:

    await alerter.alert(...)   # always safe — NoopAlerter silently discards

Benefits
--------
* Remove all ``if component is not None`` guards from hot-path code.
* Simpler testing — inject Null Objects instead of mocking out None checks.
* Explicit — ``NoopAlerter`` is visible in code review; ``None`` is invisible.

Usage
-----
Replace None defaults with Null Objects at construction time::

    from resilience.null_objects import NoopAlerter, NoopAuditLog, NoopLineageTracker

    enterprise = {
        "alerter":         NoopAlerter(),
        "audit_log":       NoopAuditLog(),
        "lineage_tracker": NoopLineageTracker(),
    }

Then inject the real implementations when available::

    if use_alerter:
        enterprise["alerter"] = AlertManager(...)
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class NoopAlerter:
    """
    Null Object for AlertManager.

    Silently discards all alerts.  Used when no alert channel is configured.
    """

    async def alert(self, alert_type: Any, message: str = "", **kwargs: Any) -> None:
        logger.debug("NoopAlerter: discarding alert '%s': %s", alert_type, message)

    async def close(self) -> None:
        pass


class NoopAuditLog:
    """
    Null Object for AuditLog.

    Silently discards all audit events.  Used in minimal deployments or tests.
    """

    async def log(
        self,
        event_type: Any,
        actor: str,
        resource: Optional[str] = None,
        outcome: Optional[str] = None,
        details: Optional[dict] = None,
        trace_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Optional[str]:
        return None

    async def query(self, **kwargs: Any) -> list:
        return []

    async def stats(self) -> dict:
        return {"total": 0, "disabled": True}

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass


class NoopLineageTracker:
    """
    Null Object for LineageTracker.

    Silently discards all lineage records.  The system functions correctly
    without lineage — it is a diagnostic/provenance feature, not critical path.
    """

    async def record_derivation(
        self,
        parent_id: str,
        child_id: str,
        operation: str = "",
        metadata: Optional[dict] = None,
    ) -> None:
        pass

    async def get_lineage(self, artifact_id: str) -> dict:
        return {"artifact_id": artifact_id, "parents": [], "children": []}


class NoopIdempotencyCache:
    """
    Null Object for IdempotencyCache.

    Always reports "not seen" — effectively disables deduplication.
    Safe: tasks may be re-processed, but no crashes occur.
    """

    async def exists(self, key: str) -> bool:
        return False

    async def set(self, key: str, value: str = "1", ttl: int = 3600) -> None:
        pass


class NoopRateLimiter:
    """
    Null Object for a single-resource TokenBucketRateLimiter.

    Immediately grants every acquire() call — no throttling.
    """

    async def acquire(self, cost: float = 1.0) -> None:
        pass

    def record_tokens(self, count: int) -> None:
        pass

    @property
    def total_tokens_used(self) -> int:
        return 0

    async def __aenter__(self) -> "NoopRateLimiter":
        return self

    async def __aexit__(self, *_: Any) -> None:
        pass


class NoopSLATracker:
    """
    Null Object for SLATracker.

    Accepts latency samples without tracking or alerting.
    """

    def record(self, loop_type: str, duration_seconds: float) -> None:
        pass

    async def check_sla(self) -> list:
        return []

    def stats(self) -> dict:
        return {}


class NoopTracer:
    """
    Null Object for Tracer.

    Accepts spans without storing anything.
    """

    def start_span(self, name: str, **kwargs: Any) -> "NoopSpan":
        return NoopSpan()

    def record(self, *args: Any, **kwargs: Any) -> None:
        pass


class NoopSpan:
    """Null Object for a tracing span — supports context-manager usage."""

    def __enter__(self) -> "NoopSpan":
        return self

    def __exit__(self, *_: Any) -> None:
        pass

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def record_exception(self, exc: Exception) -> None:
        pass
