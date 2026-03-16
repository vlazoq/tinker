"""
health/http_server.py
======================

Lightweight HTTP health check server for Tinker.

Why health endpoints?
----------------------
Without HTTP health endpoints:
  - Kubernetes doesn't know if a Tinker pod is ready to receive traffic
  - Load balancers can't detect unhealthy instances
  - Monitoring tools can't verify Tinker is responsive
  - Engineers have to SSH into the machine to check if the process is alive

With health endpoints:
  - Kubernetes restarts pods that fail liveness probes
  - Load balancers route traffic only to ready instances
  - Monitoring alerts when Tinker becomes unhealthy
  - Engineers can check health with a simple ``curl localhost:8080/health``

Endpoints
----------
  GET /live  — Liveness probe: is the process alive and not deadlocked?
               Returns 200 OK immediately.  If this fails, the process is dead.

  GET /ready — Readiness probe: is the system ready to accept work?
               Checks all component connections (Redis, DuckDB, Ollama, etc.)
               Returns 200 if all critical services are up, 503 otherwise.

  GET /health — Detailed health report as JSON.
               Returns all component statuses and key metrics.

  GET /status — Live orchestrator state (micro/meso/macro counters, etc.)

Usage
------
::

    server = HealthServer(
        orchestrator   = orchestrator,
        memory_manager = memory_manager,
        circuit_registry = circuit_registry,
        rate_registry  = rate_registry,
        sla_tracker    = sla_tracker,
    )
    await server.start(port=8080)

    # In main cleanup:
    await server.stop()
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


class HealthServer:
    """
    Tiny asyncio HTTP server exposing liveness, readiness, and status endpoints.

    Does not require any external HTTP framework (no fastapi, no aiohttp server
    dependency).  Uses raw asyncio sockets for minimal overhead.

    Parameters
    ----------
    orchestrator     : The Tinker Orchestrator (for state and counters).
    memory_manager   : MemoryManager (for Redis/DuckDB/ChromaDB health).
    circuit_registry : CircuitBreakerRegistry (for circuit breaker statuses).
    rate_registry    : RateLimiterRegistry (for rate limiter stats).
    sla_tracker      : SLATracker (for SLA compliance).
    dlq              : DeadLetterQueue (for DLQ stats).
    """

    def __init__(
        self,
        orchestrator: Any = None,
        memory_manager: Any = None,
        circuit_registry: Any = None,
        rate_registry: Any = None,
        sla_tracker: Any = None,
        dlq: Any = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._memory_manager = memory_manager
        self._circuit_registry = circuit_registry
        self._rate_registry = rate_registry
        self._sla_tracker = sla_tracker
        self._dlq = dlq
        self._server = None
        self._start_time = time.monotonic()
        self._request_count = 0

    async def start(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        """
        Start the HTTP health server.

        Parameters
        ----------
        host : Bind address (default: all interfaces).
        port : Port number (default: 8080).
        """
        self._server = await asyncio.start_server(
            self._handle_request, host=host, port=port
        )
        logger.info("Health server started on %s:%d", host, port)

    async def stop(self) -> None:
        """Stop the HTTP health server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("Health server stopped")

    # ------------------------------------------------------------------
    # Request handler
    # ------------------------------------------------------------------

    async def _handle_request(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Parse incoming HTTP request and route to the appropriate handler."""
        self._request_count += 1
        try:
            # Read the request line (e.g. "GET /health HTTP/1.1")
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            parts = line.decode().strip().split()
            if len(parts) < 2:
                await self._send_response(writer, 400, {"error": "bad request"})
                return

            method, path = parts[0], parts[1].split("?")[0]   # strip query string

            # Consume headers (required to drain the socket properly)
            while True:
                header_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if header_line in (b"\r\n", b"\n", b""):
                    break

            # Route to handler
            if path == "/live" or path == "/healthz":
                await self._handle_live(writer)
            elif path == "/ready" or path == "/readyz":
                await self._handle_ready(writer)
            elif path == "/health":
                await self._handle_health(writer)
            elif path == "/status":
                await self._handle_status(writer)
            else:
                await self._send_response(writer, 404, {"error": "not found"})

        except (asyncio.TimeoutError, ConnectionResetError):
            pass
        except Exception as exc:
            logger.debug("Health server request error: %s", exc)
            try:
                await self._send_response(writer, 500, {"error": str(exc)})
            except Exception:
                pass
        finally:
            writer.close()

    async def _handle_live(self, writer) -> None:
        """
        Liveness probe: always returns 200 if the process is alive.

        If this endpoint doesn't respond, the process is likely deadlocked
        or out of memory.  Kubernetes should restart the pod.
        """
        await self._send_response(writer, 200, {
            "status": "alive",
            "uptime_seconds": round(time.monotonic() - self._start_time, 1),
        })

    async def _handle_ready(self, writer) -> None:
        """
        Readiness probe: returns 200 only if all critical services are up.

        Checks: Ollama reachability, Redis connectivity.
        Returns 503 if any critical service is down (so load balancers stop
        routing traffic to this instance).
        """
        issues = []

        # Check circuit breakers
        if self._circuit_registry:
            for name, stats in self._circuit_registry.all_stats().items():
                if stats.get("state") == "open":
                    issues.append(f"circuit:{name} is OPEN")

        # Check memory manager
        if self._memory_manager and hasattr(self._memory_manager, "health_check"):
            try:
                health = await asyncio.wait_for(
                    self._memory_manager.health_check(), timeout=5.0
                )
                for component, ok in health.items():
                    if not ok:
                        issues.append(f"memory:{component} is DOWN")
            except Exception as exc:
                issues.append(f"memory health check failed: {exc}")

        if issues:
            await self._send_response(writer, 503, {
                "status": "not_ready",
                "issues": issues,
            })
        else:
            await self._send_response(writer, 200, {"status": "ready"})

    async def _handle_health(self, writer) -> None:
        """
        Detailed health report as JSON.

        Returns a comprehensive status object with all component states,
        loop counters, SLA compliance, and DLQ stats.
        """
        report = {
            "status": "ok",
            "uptime_seconds": round(time.monotonic() - self._start_time, 1),
            "request_count": self._request_count,
        }

        # Orchestrator state
        if self._orchestrator and hasattr(self._orchestrator, "state"):
            state = self._orchestrator.state
            report["loops"] = {
                "micro": getattr(state, "total_micro_loops", 0),
                "meso":  getattr(state, "total_meso_loops", 0),
                "macro": getattr(state, "total_macro_loops", 0),
                "current_level": getattr(getattr(state, "current_level", None), "value", ""),
                "consecutive_failures": getattr(state, "consecutive_failures", 0),
                "stagnation_events": getattr(state, "stagnation_events_total", 0),
            }

        # Circuit breakers
        if self._circuit_registry:
            report["circuit_breakers"] = self._circuit_registry.all_stats()

        # Rate limiters
        if self._rate_registry:
            report["rate_limiters"] = self._rate_registry.all_stats()
            report["total_llm_tokens"] = self._rate_registry.total_llm_tokens()

        # SLA compliance
        if self._sla_tracker:
            report["sla"] = self._sla_tracker.all_reports()

        # Memory health
        if self._memory_manager and hasattr(self._memory_manager, "health_check"):
            try:
                report["memory"] = await asyncio.wait_for(
                    self._memory_manager.health_check(), timeout=3.0
                )
            except Exception as exc:
                report["memory"] = {"error": str(exc)}

        # DLQ stats
        if self._dlq and hasattr(self._dlq, "stats"):
            try:
                report["dlq"] = await asyncio.wait_for(self._dlq.stats(), timeout=3.0)
            except Exception:
                report["dlq"] = {"error": "unavailable"}

        # Determine overall status
        has_open_circuits = (
            self._circuit_registry is not None
            and self._circuit_registry.any_open()
        )
        if has_open_circuits:
            report["status"] = "degraded"

        await self._send_response(writer, 200, report)

    async def _handle_status(self, writer) -> None:
        """Return the live orchestrator state dict."""
        if self._orchestrator and hasattr(self._orchestrator, "get_state_snapshot"):
            await self._send_response(writer, 200, self._orchestrator.get_state_snapshot())
        else:
            await self._send_response(writer, 503, {"error": "orchestrator not available"})

    # ------------------------------------------------------------------
    # Low-level HTTP response writer
    # ------------------------------------------------------------------

    async def _send_response(
        self, writer: asyncio.StreamWriter, status: int, body: dict
    ) -> None:
        """Write a minimal HTTP response with a JSON body."""
        body_bytes = json.dumps(body, default=str).encode("utf-8")
        status_text = {200: "OK", 400: "Bad Request", 404: "Not Found",
                       500: "Internal Server Error", 503: "Service Unavailable"}.get(status, "")
        response = (
            f"HTTP/1.1 {status} {status_text}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body_bytes)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode() + body_bytes
        try:
            writer.write(response)
            await writer.drain()
        except Exception:
            pass
