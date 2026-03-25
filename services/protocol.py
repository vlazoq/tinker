"""
services/protocol.py
====================

Core service-boundary contracts.

``ServiceInterface`` is the protocol every deployable service must satisfy.
It mirrors what a real microservice exposes:
  - ``name``       — stable identifier (used in logs, metrics, registry)
  - ``start()``    — async lifecycle hook (open connections, start background tasks)
  - ``stop()``     — async lifecycle hook (graceful shutdown)
  - ``health()``   — async liveness/readiness probe

``ServiceRequest`` and ``ServiceResponse`` are typed envelopes for
cross-service communication.  They carry enough metadata for distributed
tracing and correlation even when the transport layer changes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ServiceInterface(Protocol):
    """Protocol every Tinker service must satisfy.

    Use ``isinstance(obj, ServiceInterface)`` to verify a class meets the
    contract at runtime.

    Implementing classes
    --------------------
    * In-process: implement directly on the component class.
    * Remote: thin HTTP/gRPC client wrapper that proxies to a remote process.
    """

    @property
    def name(self) -> str:
        """Stable, unique name for this service (e.g. ``"orchestrator"``)."""
        ...

    async def start(self) -> None:
        """Open connections, start background tasks.

        Called once before the service begins handling requests.
        Must be idempotent — calling twice should not raise.
        """
        ...

    async def stop(self) -> None:
        """Gracefully shut down the service.

        Called once when the application is stopping.  Should flush queues,
        close connections, and cancel background tasks.
        """
        ...

    async def health(self) -> dict[str, Any]:
        """Return a health status dict.

        The dict must contain at least:
          ``{"status": "ok" | "degraded" | "down", "name": self.name}``

        Additional keys (latency, queue depth, version, etc.) are allowed.

        Used by the health server's ``/health`` endpoint and by the
        ServiceRegistry to report aggregate system health.
        """
        ...


@dataclass
class ServiceRequest:
    """Typed input envelope for cross-service calls.

    Carries the action, payload, and tracing metadata.  When calls move to
    HTTP or gRPC, this becomes the request body / protobuf message.

    Parameters
    ----------
    action   : The operation to perform (e.g. ``"submit_task"``).
    payload  : Action-specific data.
    trace_id : Distributed trace ID for correlation (auto-generated if None).
    caller   : Name of the calling service for audit purposes.
    """

    action: str
    payload: dict[str, Any] = field(default_factory=dict)
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    caller: str = "unknown"
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


@dataclass
class ServiceResponse:
    """Typed output envelope for cross-service calls.

    Parameters
    ----------
    ok      : True if the operation succeeded.
    data    : The result payload (action-specific).
    error   : Human-readable error message if ``ok=False``.
    trace_id: Echo of the request trace_id for correlation.
    """

    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    trace_id: str = ""
    elapsed_ms: float = 0.0

    @classmethod
    def success(
        cls,
        data: dict[str, Any] | None = None,
        trace_id: str = "",
        elapsed_ms: float = 0.0,
    ) -> "ServiceResponse":
        """Convenience constructor for a successful response."""
        return cls(ok=True, data=data or {}, trace_id=trace_id, elapsed_ms=elapsed_ms)

    @classmethod
    def failure(
        cls,
        error: str,
        trace_id: str = "",
        elapsed_ms: float = 0.0,
    ) -> "ServiceResponse":
        """Convenience constructor for a failure response."""
        return cls(ok=False, error=error, trace_id=trace_id, elapsed_ms=elapsed_ms)
