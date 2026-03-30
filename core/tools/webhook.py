"""
core/tools/webhook.py
=====================

Outbound webhook tool for integrating Tinker with local automation
platforms (n8n, Node-RED, Home Assistant, etc.).

This tool sends HTTP POST requests to locally-deployed webhook endpoints
whenever the orchestrator needs to notify an external system. It also
provides an EventBus subscriber that auto-fires webhooks on configurable
event types — perfect for n8n workflow triggers.

All URLs are expected to be local/private network addresses (e.g.
http://localhost:5678, http://192.168.1.x:5678). No cloud services.

Usage as a tool::

    result = await registry.execute("webhook",
        url="http://localhost:5678/webhook/tinker-events",
        payload={"event": "task_completed", "task_id": "abc123"},
    )

Usage as an EventBus subscriber::

    from core.tools.webhook import WebhookDispatcher

    dispatcher = WebhookDispatcher(
        endpoints=[
            {"url": "http://localhost:5678/webhook/tinker",
             "events": ["micro_loop_completed", "stagnation_detected"]},
        ],
    )
    dispatcher.attach(event_bus)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict
from typing import Any

from .base import BaseTool, ToolSchema

logger = logging.getLogger("tinker.tools.webhook")


class WebhookTool(BaseTool):
    """Send HTTP POST requests to local webhook endpoints.

    Parameters
    ----------
    timeout : float
        HTTP request timeout in seconds (default 10).
    """

    def __init__(self, timeout: float = 10.0) -> None:
        self._timeout = timeout

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="webhook",
            description=(
                "Send an HTTP POST request with a JSON payload to a local "
                "webhook endpoint. Use this to trigger workflows in n8n, "
                "Node-RED, or other local automation platforms."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": (
                            "The webhook URL to POST to. Should be a local "
                            "address (e.g. http://localhost:5678/webhook/tinker)."
                        ),
                    },
                    "payload": {
                        "type": "object",
                        "description": "JSON-serializable dict to send as the POST body.",
                    },
                    "headers": {
                        "type": "object",
                        "description": "Optional extra HTTP headers.",
                    },
                },
                "required": ["url", "payload"],
            },
            returns="Dict with status_code, success, and response_text.",
        )

    async def _execute(
        self,
        url: str,
        payload: dict | None = None,
        headers: dict | None = None,
        **_,
    ) -> dict:
        import httpx

        req_headers = {"Content-Type": "application/json"}
        if headers:
            req_headers.update(headers)

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, json=payload or {}, headers=req_headers)

        return {
            "status_code": resp.status_code,
            "success": 200 <= resp.status_code < 300,
            "response_text": resp.text[:500],
            "url": url,
        }


# ---------------------------------------------------------------------------
# EventBus → Webhook dispatcher
# ---------------------------------------------------------------------------


class WebhookDispatcher:
    """Subscribe to EventBus events and auto-fire webhooks to local endpoints.

    This is the bridge between Tinker's internal event system and external
    automation platforms like n8n running on your local network.

    Parameters
    ----------
    endpoints : list of dicts, each with:
        - url: str — the webhook URL
        - events: list[str] — event type values to subscribe to
                               (empty list or ["*"] = all events)
        - headers: dict (optional) — extra HTTP headers
    timeout : float — HTTP request timeout per webhook call (default 10s)
    max_concurrent : int — max parallel outbound webhooks (default 5)

    Configuration via environment::

        TINKER_WEBHOOK_ENDPOINTS='[
            {"url": "http://localhost:5678/webhook/tinker",
             "events": ["micro_loop_completed", "stagnation_detected"]}
        ]'
    """

    def __init__(
        self,
        endpoints: list[dict[str, Any]] | None = None,
        timeout: float = 10.0,
        max_concurrent: int = 5,
    ) -> None:
        self._endpoints = endpoints or self._load_from_env()
        self._timeout = timeout
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._stats = {"sent": 0, "failed": 0, "last_error": None}

        if self._endpoints:
            logger.info(
                "WebhookDispatcher: %d endpoint(s) configured",
                len(self._endpoints),
            )
        else:
            logger.debug("WebhookDispatcher: no endpoints configured")

    @staticmethod
    def _load_from_env() -> list[dict[str, Any]]:
        """Load webhook endpoints from TINKER_WEBHOOK_ENDPOINTS env var."""
        raw = os.getenv("TINKER_WEBHOOK_ENDPOINTS", "")
        if not raw.strip():
            return []
        try:
            endpoints = json.loads(raw)
            if isinstance(endpoints, list):
                return endpoints
        except json.JSONDecodeError as exc:
            logger.warning("WebhookDispatcher: invalid JSON in TINKER_WEBHOOK_ENDPOINTS: %s", exc)
        return []

    def attach(self, bus: Any) -> None:
        """Subscribe to EventBus events based on configured endpoints."""
        from core.events import EventType

        # Collect all event types we need to listen to
        listen_all = False
        specific_events: set[str] = set()
        for ep in self._endpoints:
            events = ep.get("events", [])
            if not events or "*" in events:
                listen_all = True
                break
            specific_events.update(events)

        if listen_all:
            bus.subscribe_handler(None, self._on_event)  # wildcard
            logger.info("WebhookDispatcher: subscribed to ALL events")
        else:
            for event_val in specific_events:
                try:
                    etype = EventType(event_val)
                    bus.subscribe_handler(etype, self._on_event)
                except ValueError:
                    logger.warning("WebhookDispatcher: unknown event type '%s'", event_val)
            logger.info(
                "WebhookDispatcher: subscribed to %d event type(s)", len(specific_events)
            )

    async def _on_event(self, event: Any) -> None:
        """Handle an event by dispatching to matching endpoints."""
        event_value = event.type.value

        for ep in self._endpoints:
            events_filter = ep.get("events", [])
            # Fire if: no filter, wildcard, or event matches
            if events_filter and "*" not in events_filter and event_value not in events_filter:
                continue

            # Fire webhook asynchronously
            asyncio.create_task(self._fire(ep, event))

    async def _fire(self, endpoint: dict, event: Any) -> None:
        """Send a webhook POST to one endpoint."""
        import httpx

        url = endpoint.get("url", "")
        if not url:
            return

        payload = {
            "event_type": event.type.value,
            "event_id": event.id,
            "timestamp": event.timestamp.isoformat(),
            "source": event.source,
            "payload": event.payload,
        }

        headers = {"Content-Type": "application/json"}
        extra_headers = endpoint.get("headers", {})
        if extra_headers:
            headers.update(extra_headers)

        async with self._semaphore:
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(url, json=payload, headers=headers)

                if 200 <= resp.status_code < 300:
                    self._stats["sent"] += 1
                    logger.debug(
                        "webhook: %s → %s (status=%d)", event.type.value, url, resp.status_code
                    )
                else:
                    self._stats["failed"] += 1
                    self._stats["last_error"] = f"HTTP {resp.status_code} from {url}"
                    logger.warning(
                        "webhook: %s → %s failed (status=%d)", event.type.value, url, resp.status_code
                    )
            except Exception as exc:
                self._stats["failed"] += 1
                self._stats["last_error"] = str(exc)
                logger.debug("webhook: %s → %s error: %s", event.type.value, url, exc)

    def get_stats(self) -> dict:
        """Return dispatch statistics."""
        return {
            **self._stats,
            "endpoints": len(self._endpoints),
        }
