"""
health/http_server.py
======================

Lightweight HTTP health check server for Tinker.

Why health endpoints?
----------------------
Without HTTP health endpoints:
  - Monitoring tools can't verify Tinker is responsive
  - Engineers have to check logs to know if the process is alive
  - There's no programmatic way to check if Ollama is reachable

With health endpoints:
  - Engineers can check health with a simple ``curl localhost:8080/health``
  - Dashboards and monitoring tools get structured JSON status
  - Prometheus scrapes ``/metrics`` for time-series alerting

Endpoints
----------
  GET /live    — Liveness probe: process alive? Returns 200 immediately.
  GET /healthz — Alias for /live (Kubernetes convention).

  GET /ready   — Readiness probe: all critical services up?
                 Checks circuit breakers, memory backends, Ollama connectivity,
                 and disk space.  Returns 200 or 503.
  GET /readyz  — Alias for /ready.

  GET /health  — Detailed health report as JSON.
                 Includes loop counters, SLA compliance, DLQ stats, disk usage.

  GET /status  — Live orchestrator state dict.

  GET /metrics — Prometheus text format exposition.
                 Counters and gauges for scraping by Prometheus/VictoriaMetrics.
                 No extra dependencies — emits plain text with the standard
                 ``# HELP`` / ``# TYPE`` / metric-value lines.

Usage
------
::

    server = HealthServer(
        orchestrator     = orchestrator,
        memory_manager   = memory_manager,
        circuit_registry = circuit_registry,
        rate_registry    = rate_registry,
        sla_tracker      = sla_tracker,
        ollama_url       = "http://localhost:11434",   # optional liveness check
        data_dir         = "./tinker_workspace",       # for disk usage check
    )
    await server.start(port=8080)

    # Prometheus scrape target: http://localhost:8080/metrics

    # In main cleanup:
    await server.stop()
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from typing import Any

logger = logging.getLogger(__name__)


class HealthServer:
    """
    Tiny asyncio HTTP server exposing liveness, readiness, status, and
    Prometheus metrics endpoints.

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
    ollama_url       : Base URL of the Ollama server to probe on /ready checks
                       (e.g. "http://localhost:11434").  Optional.
    data_dir         : Path to Tinker's data directory for disk usage reporting.
    disk_warn_pct    : Disk usage percentage at which /ready returns 503.
                       Default: 90 (warn when 90 % of disk is used).
    """

    def __init__(
        self,
        orchestrator: Any = None,
        memory_manager: Any = None,
        circuit_registry: Any = None,
        rate_registry: Any = None,
        sla_tracker: Any = None,
        dlq: Any = None,
        ollama_url: str = "",
        data_dir: str = ".",
        disk_warn_pct: float = 90.0,
    ) -> None:
        self._orchestrator = orchestrator
        self._memory_manager = memory_manager
        self._circuit_registry = circuit_registry
        self._rate_registry = rate_registry
        self._sla_tracker = sla_tracker
        self._dlq = dlq
        self._ollama_url = ollama_url.rstrip("/")
        self._data_dir = data_dir
        self._disk_warn_pct = disk_warn_pct
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

            _, path = parts[0], parts[1].split("?")[0]  # strip query string

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
            elif path == "/metrics":
                await self._handle_metrics(writer)
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
        await self._send_response(
            writer,
            200,
            {
                "status": "alive",
                "uptime_seconds": round(time.monotonic() - self._start_time, 1),
            },
        )

    async def _handle_ready(self, writer) -> None:
        """
        Readiness probe: returns 200 only if all critical services are up.

        Checks:
          - Open circuit breakers
          - Memory backend health (Redis, DuckDB, ChromaDB)
          - Ollama connectivity (if ollama_url is configured)
          - Disk space (warns at disk_warn_pct% full)

        Returns 503 if any critical service is down.
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

        # Check Ollama connectivity
        if self._ollama_url:
            ollama_ok = await self._check_ollama()
            if not ollama_ok:
                issues.append(f"ollama:{self._ollama_url} is unreachable")

        # Check disk space
        disk_issue = self._check_disk()
        if disk_issue:
            issues.append(disk_issue)

        if issues:
            await self._send_response(
                writer,
                503,
                {
                    "status": "not_ready",
                    "issues": issues,
                    # Microservices: declare upstream dependencies so
                    # orchestration layers (k8s, service mesh) know which
                    # external services this bounded context requires.
                    "dependencies": {
                        "ollama": {
                            "url": self._ollama_url or "not configured",
                            "required": True,
                        },
                        "redis": {"required": False, "note": "degrades gracefully"},
                        "chromadb": {"required": False, "note": "degrades gracefully"},
                        "duckdb": {"required": True, "note": "local file"},
                    },
                },
            )
        else:
            await self._send_response(
                writer,
                200,
                {
                    "status": "ready",
                    "dependencies": {
                        "ollama": {
                            "url": self._ollama_url or "not configured",
                            "required": True,
                        },
                        "redis": {"required": False, "note": "degrades gracefully"},
                        "chromadb": {"required": False, "note": "degrades gracefully"},
                        "duckdb": {"required": True, "note": "local file"},
                    },
                },
            )

    async def _check_ollama(self) -> bool:
        """Ping Ollama's /api/tags endpoint; return True if reachable."""
        try:
            import aiohttp  # type: ignore

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._ollama_url}/api/tags",
                    timeout=aiohttp.ClientTimeout(total=3),
                ) as resp:
                    return resp.status < 500
        except Exception:
            return False

    def _check_disk(self) -> str:
        """Return a warning string if disk is over threshold, else empty string."""
        try:
            usage = shutil.disk_usage(self._data_dir)
            pct = usage.used / usage.total * 100
            if pct >= self._disk_warn_pct:
                return (
                    f"disk:{self._data_dir} is {pct:.1f}% full "
                    f"({usage.free // (1024**3)}GB free)"
                )
        except Exception:
            pass
        return ""

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
                "meso": getattr(state, "total_meso_loops", 0),
                "macro": getattr(state, "total_macro_loops", 0),
                "current_level": getattr(
                    getattr(state, "current_level", None), "value", ""
                ),
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

        # Disk usage
        try:
            usage = shutil.disk_usage(self._data_dir)
            report["disk"] = {
                "path": self._data_dir,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "total_bytes": usage.total,
                "used_pct": round(usage.used / usage.total * 100, 1),
            }
        except Exception:
            pass

        # Ollama status
        if self._ollama_url:
            report["ollama"] = {
                "url": self._ollama_url,
                "reachable": await self._check_ollama(),
            }

        # Determine overall status
        has_open_circuits = (
            self._circuit_registry is not None and self._circuit_registry.any_open()
        )
        if has_open_circuits:
            report["status"] = "degraded"

        await self._send_response(writer, 200, report)

    async def _handle_status(self, writer) -> None:
        """Return the live orchestrator state dict."""
        if self._orchestrator and hasattr(self._orchestrator, "get_state_snapshot"):
            await self._send_response(
                writer, 200, self._orchestrator.get_state_snapshot()
            )
        else:
            await self._send_response(
                writer, 503, {"error": "orchestrator not available"}
            )

    async def _handle_metrics(self, writer) -> None:
        """
        Prometheus text format exposition endpoint.

        Emits standard ``# HELP`` / ``# TYPE`` / metric lines that Prometheus,
        VictoriaMetrics, or Grafana Alloy can scrape directly.  No extra
        dependencies — uses only built-in Python.

        Exposed metrics:
          tinker_uptime_seconds          gauge
          tinker_http_requests_total     counter
          tinker_micro_loops_total       counter
          tinker_meso_loops_total        counter
          tinker_macro_loops_total       counter
          tinker_consecutive_failures    gauge
          tinker_stagnation_events_total counter
          tinker_llm_tokens_total        counter
          tinker_disk_used_bytes         gauge
          tinker_disk_free_bytes         gauge
          tinker_dlq_pending_total       gauge
        """
        lines: list[str] = []

        def metric(
            name: str, mtype: str, help_text: str, value: Any, labels: str = ""
        ) -> None:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {mtype}")
            label_str = f"{{{labels}}}" if labels else ""
            lines.append(f"{name}{label_str} {value}")

        metric(
            "tinker_uptime_seconds",
            "gauge",
            "Seconds since health server started",
            round(time.monotonic() - self._start_time, 1),
        )
        metric(
            "tinker_http_requests_total",
            "counter",
            "Total HTTP requests handled by health server",
            self._request_count,
        )

        # Orchestrator loop counters
        if self._orchestrator and hasattr(self._orchestrator, "state"):
            state = self._orchestrator.state
            metric(
                "tinker_micro_loops_total",
                "counter",
                "Total micro loops executed",
                getattr(state, "total_micro_loops", 0),
            )
            metric(
                "tinker_meso_loops_total",
                "counter",
                "Total meso loops executed",
                getattr(state, "total_meso_loops", 0),
            )
            metric(
                "tinker_macro_loops_total",
                "counter",
                "Total macro loops executed",
                getattr(state, "total_macro_loops", 0),
            )
            metric(
                "tinker_consecutive_failures",
                "gauge",
                "Consecutive micro loop failures",
                getattr(state, "consecutive_failures", 0),
            )
            metric(
                "tinker_stagnation_events_total",
                "counter",
                "Total stagnation events detected",
                getattr(state, "stagnation_events_total", 0),
            )

        # LLM token usage
        if self._rate_registry and hasattr(self._rate_registry, "total_llm_tokens"):
            metric(
                "tinker_llm_tokens_total",
                "counter",
                "Total LLM tokens consumed",
                self._rate_registry.total_llm_tokens(),
            )

        # Disk usage
        try:
            usage = shutil.disk_usage(self._data_dir)
            metric(
                "tinker_disk_used_bytes",
                "gauge",
                "Bytes used on the data partition",
                usage.used,
            )
            metric(
                "tinker_disk_free_bytes",
                "gauge",
                "Bytes free on the data partition",
                usage.free,
            )
        except Exception:
            pass

        # DLQ pending count
        if self._dlq and hasattr(self._dlq, "stats"):
            try:
                dlq_stats = await asyncio.wait_for(self._dlq.stats(), timeout=2.0)
                pending = dlq_stats.get("pending", 0)
                metric(
                    "tinker_dlq_pending_total",
                    "gauge",
                    "DLQ entries awaiting replay",
                    pending,
                )
            except Exception:
                pass

        body = "\n".join(lines) + "\n"
        body_bytes = body.encode("utf-8")
        response = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/plain; version=0.0.4; charset=utf-8\r\n"
            f"Content-Length: {len(body_bytes)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode() + body_bytes
        try:
            writer.write(response)
            await writer.drain()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Low-level HTTP response writer
    # ------------------------------------------------------------------

    async def _send_response(
        self, writer: asyncio.StreamWriter, status: int, body: dict
    ) -> None:
        """Write a minimal HTTP response with a JSON body."""
        body_bytes = json.dumps(body, default=str).encode("utf-8")
        status_text = {
            200: "OK",
            400: "Bad Request",
            404: "Not Found",
            500: "Internal Server Error",
            503: "Service Unavailable",
        }.get(status, "")
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
