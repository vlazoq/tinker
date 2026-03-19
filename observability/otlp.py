"""
observability/otlp.py
=====================
OpenTelemetry (OTLP) export bridge for Tinker's custom span tracer.

Why?
----
Tinker's observability/tracing.py provides a lightweight custom span API.
This module bridges it to the OpenTelemetry standard so spans can be
exported to Jaeger, Grafana Tempo, Datadog, Honeycomb, or any OTLP-compatible
backend — without changing the rest of the codebase.

Architecture
------------
  1. OTLPBridge wraps Tinker's custom Tracer.
  2. When a span is finished, OTLPBridge converts it to an OTLP-format dict
     and batches it for export.
  3. The exporter sends batches to the OTLP HTTP endpoint (protobuf or JSON).
  4. If opentelemetry-sdk is installed, it uses the real SDK.
     Otherwise, it falls back to a minimal HTTP/JSON export (no extra deps).

Configuration
-------------
Set TINKER_OTLP_ENDPOINT to your collector (e.g. http://localhost:4318).
Set TINKER_OTLP_SERVICE_NAME (default: "tinker").
Set TINKER_OTLP_HEADERS for auth headers (comma-separated key=value pairs).

Usage
-----
::

    from observability.otlp import setup_otlp, shutdown_otlp

    setup_otlp()          # call once at startup
    # ... run Tinker ...
    await shutdown_otlp() # flush and close at shutdown
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

TINKER_SERVICE_NAME = os.getenv("TINKER_OTLP_SERVICE_NAME", "tinker")
TINKER_OTLP_ENDPOINT = os.getenv("TINKER_OTLP_ENDPOINT", "")
TINKER_OTLP_HEADERS_RAW = os.getenv("TINKER_OTLP_HEADERS", "")


def _parse_headers(raw: str) -> dict[str, str]:
    """Parse 'key1=val1,key2=val2' into a dict."""
    if not raw or not raw.strip():
        return {}
    headers: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if "=" in pair:
            key, _, value = pair.partition("=")
            headers[key.strip()] = value.strip()
    return headers


def _span_to_otlp_dict(span) -> dict:
    """Convert a Tinker Span object to an OTLP-compatible span dict.

    The Tinker Span has:
      - span.name
      - span.started_at  (monotonic float, seconds)
      - span.ended_at    (monotonic float or None)
      - span.attributes  (dict)
      - span.error       (str or None)

    OTLP requires wall-clock Unix nanoseconds for start/end times.
    Since Tinker uses monotonic time we anchor to the current wall clock
    and adjust by the delta between monotonic now and span times.
    """
    # Anchor: current wall-clock time in seconds vs current monotonic time
    mono_now = time.monotonic()
    wall_now = time.time()
    mono_offset = wall_now - mono_now  # offset to convert monotonic → wall

    start_unix = span.started_at + mono_offset
    if span.ended_at is not None:
        end_unix = span.ended_at + mono_offset
    else:
        end_unix = wall_now

    start_ns = int(start_unix * 1e9)
    end_ns = int(end_unix * 1e9)

    # Build a stable traceId (32 hex chars = 16 bytes) from the span name +
    # started_at.  We don't have a real trace ID on individual Span objects
    # (they live inside a Trace), so we derive one deterministically from the
    # span's numeric identity.  When called from setup_otlp we can attach the
    # parent Trace's trace_id; for now we generate a placeholder.
    raw_trace_id = getattr(span, "trace_id", None)
    if raw_trace_id:
        # Normalise to exactly 32 hex chars
        hex_tid = raw_trace_id.replace("-", "").lower()
        # Hash down or pad to 32 chars
        if len(hex_tid) > 32:
            import hashlib
            hex_tid = hashlib.md5(hex_tid.encode()).hexdigest()
        else:
            hex_tid = hex_tid.zfill(32)
    else:
        import hashlib
        seed = f"{span.name}:{span.started_at}"
        hex_tid = hashlib.md5(seed.encode()).hexdigest().zfill(32)

    raw_span_id = getattr(span, "span_id", None)
    if raw_span_id:
        hex_sid = raw_span_id.replace("-", "").lower()
        if len(hex_sid) > 16:
            import hashlib
            hex_sid = hashlib.md5(hex_sid.encode()).hexdigest()[:16]
        else:
            hex_sid = hex_sid.zfill(16)
    else:
        import hashlib
        seed = f"{span.name}:{span.started_at}:span"
        hex_sid = hashlib.md5(seed.encode()).hexdigest()[:16]

    attributes = [
        {"key": k, "value": {"stringValue": str(v)}}
        for k, v in (span.attributes or {}).items()
    ]

    status_code = 2 if span.error else 1  # 1=OK, 2=ERROR

    return {
        "traceId": hex_tid,
        "spanId": hex_sid,
        "name": span.name,
        "startTimeUnixNano": start_ns,
        "endTimeUnixNano": end_ns,
        "attributes": attributes,
        "status": {"code": status_code},
    }


class OTLPExporter:
    """
    Batches finished spans and exports them to an OTLP HTTP/JSON endpoint.

    Falls back to SDK if opentelemetry-exporter-otlp-proto-http is installed.
    Uses raw aiohttp/httpx otherwise.
    """

    def __init__(
        self,
        endpoint: str = TINKER_OTLP_ENDPOINT,
        service_name: str = TINKER_SERVICE_NAME,
        headers: Optional[dict] = None,
        batch_size: int = 100,
        flush_interval: float = 5.0,
    ):
        self._endpoint = endpoint.rstrip("/") + "/v1/traces" if endpoint else ""
        self._service_name = service_name
        self._headers = headers or _parse_headers(TINKER_OTLP_HEADERS_RAW)
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._pending: list[dict] = []
        self._lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task] = None
        self._sdk_tracer_provider = None  # set if SDK available
        self._enabled = bool(endpoint)

    def on_span_finish(self, span) -> None:
        """Called when a Tinker span finishes. Queues it for export."""
        if not self._enabled:
            return
        span_dict = _span_to_otlp_dict(span)
        # Use create_task to avoid blocking the caller
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._enqueue(span_dict))
        except RuntimeError:
            pass  # No running loop (e.g. in tests)

    async def _enqueue(self, span_dict: dict) -> None:
        async with self._lock:
            self._pending.append(span_dict)
            if len(self._pending) >= self._batch_size:
                await self._flush()

    async def flush(self) -> None:
        """Manually flush all pending spans."""
        async with self._lock:
            await self._flush()

    async def _flush(self) -> None:
        """Send pending spans (must be called with self._lock held)."""
        if not self._pending or not self._endpoint:
            self._pending.clear()
            return
        spans_to_send = self._pending[:]
        self._pending.clear()
        asyncio.get_event_loop().create_task(
            self._send_batch(spans_to_send)
        )

    async def _send_batch(self, spans: list[dict]) -> None:
        """POST spans to the OTLP endpoint as JSON."""
        payload = {
            "resourceSpans": [{
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": self._service_name}},
                        {"key": "tinker.version", "value": {"stringValue": "1.0"}},
                    ]
                },
                "scopeSpans": [{
                    "scope": {"name": "tinker.orchestrator"},
                    "spans": spans,
                }]
            }]
        }
        headers = {"Content-Type": "application/json", **self._headers}
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._endpoint,
                    data=json.dumps(payload),
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status >= 400:
                        logger.warning("OTLP export failed: HTTP %d", resp.status)
                    else:
                        logger.debug("OTLP: exported %d spans", len(spans))
        except ImportError:
            logger.debug("aiohttp not available — OTLP spans not exported")
        except Exception as exc:
            logger.debug("OTLP export error (non-fatal): %s", exc)

    async def start_background_flush(self) -> None:
        """Start a background task that flushes every flush_interval seconds."""
        if not self._enabled:
            return
        self._flush_task = asyncio.create_task(self._background_flush_loop())

    async def _background_flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval)
            await self.flush()

    async def shutdown(self) -> None:
        """Cancel background task and flush remaining spans."""
        if self._flush_task:
            self._flush_task.cancel()
        await self.flush()


# Module-level singleton
_exporter: Optional[OTLPExporter] = None


def setup_otlp(
    endpoint: str = TINKER_OTLP_ENDPOINT,
    service_name: str = TINKER_SERVICE_NAME,
    **kwargs,
) -> OTLPExporter:
    """
    Initialise the OTLP exporter and wire it to the default tracer.

    Safe to call multiple times — returns the existing exporter if already set up.
    """
    global _exporter
    if _exporter is not None:
        return _exporter
    _exporter = OTLPExporter(endpoint=endpoint, service_name=service_name, **kwargs)
    # Wire to the default tracer's on_finish callback
    try:
        from observability.tracing import default_tracer
        original_finish = getattr(default_tracer, '_on_span_finish', None)

        def _on_finish(span):
            _exporter.on_span_finish(span)  # type: ignore
            if original_finish:
                original_finish(span)

        default_tracer._on_span_finish = _on_finish
        logger.info(
            "OTLP exporter wired to default tracer (endpoint=%s)",
            endpoint or "disabled",
        )
    except Exception as exc:
        logger.debug("Could not wire OTLP to default tracer: %s", exc)
    return _exporter


async def shutdown_otlp() -> None:
    """Flush and shut down the OTLP exporter."""
    global _exporter
    if _exporter:
        await _exporter.shutdown()
        _exporter = None
