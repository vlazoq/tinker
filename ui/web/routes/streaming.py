"""SSE streaming endpoint and StatePublisher."""

import asyncio
import json
import logging
from typing import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ui.core import load_state, now_iso

logger = logging.getLogger(__name__)

router = APIRouter()


class StatePublisher:
    """
    Broadcast SSE events to all connected clients.

    Usage:
      1. Create a single instance at module level.
      2. The SSE endpoint calls subscribe() to get a per-client queue.
      3. Any code path calls publish(event_type, data) to push to all clients.
      4. On disconnect, the SSE endpoint calls unsubscribe(queue).
    """

    def __init__(self) -> None:
        self._clients: set[asyncio.Queue] = set()
        self._lock: asyncio.Lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue:
        """Register a new client and return its personal event queue."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        async with self._lock:
            self._clients.add(queue)
        logger.debug("SSE client subscribed (total=%d)", len(self._clients))
        return queue

    async def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Remove a client's queue after disconnect."""
        async with self._lock:
            self._clients.discard(queue)
        logger.debug("SSE client unsubscribed (total=%d)", len(self._clients))

    async def publish(self, event_type: str, data: dict) -> None:
        """Push an event to every connected client."""
        payload = json.dumps({"type": event_type, **data})
        message = f"event: {event_type}\ndata: {payload}\n\n"

        async with self._lock:
            for queue in self._clients:
                try:
                    queue.put_nowait(message)
                except asyncio.QueueFull:
                    logger.warning("SSE queue full for a client; dropping event")

    @property
    def client_count(self) -> int:
        """Number of currently connected SSE clients."""
        return len(self._clients)


# Global publisher instance — importable by main.py and other modules.
_publisher = StatePublisher()


async def notify_state_change(publisher: StatePublisher, state_dict: dict) -> None:
    """
    Convenience helper the orchestrator can call after mutating state.

    Extracts dashboard-relevant fields and pushes a ``state_update`` event.
    """
    totals = state_dict.get("totals", {})
    micro_hist = state_dict.get("micro_history", [])
    critic = micro_hist[-1].get("critic_score") if micro_hist else None
    await publisher.publish(
        "state_update",
        {
            "time": now_iso(),
            "micro_loops": totals.get("micro", 0),
            "meso_loops": totals.get("meso", 0),
            "macro_loops": totals.get("macro", 0),
            "current_task": state_dict.get("current_task_id"),
            "critic_score": critic,
            "current_level": state_dict.get("current_level"),
            "current_subsystem": state_dict.get("current_subsystem"),
            "consecutive_failures": totals.get("consecutive_failures", 0),
            "status": state_dict.get("status"),
        },
    )


@router.get("/api/logs/stream")
async def api_logs_stream(request: Request, level: str = "INFO"):
    """SSE endpoint: true push via StatePublisher with file-poll fallback."""

    async def gen() -> AsyncIterator[str]:
        queue = await _publisher.subscribe()
        last_micro = -1
        try:
            while True:
                if await request.is_disconnected():
                    break

                try:
                    message = await asyncio.wait_for(queue.get(), timeout=2.0)
                    yield message
                    while not queue.empty():
                        yield queue.get_nowait()
                    continue
                except asyncio.TimeoutError:
                    pass

                # Fallback: poll tinker_state.json for external changes
                state = load_state()
                totals = state.get("totals", {})
                micro = totals.get("micro", -1)
                if micro != last_micro:
                    last_micro = micro
                    micro_hist = state.get("micro_history", [])
                    critic = (
                        micro_hist[-1].get("critic_score") if micro_hist else None
                    )
                    evt = json.dumps(
                        {
                            "type": "state_update",
                            "time": now_iso(),
                            "level": "INFO",
                            "micro_loops": micro,
                            "meso_loops": totals.get("meso", 0),
                            "macro_loops": totals.get("macro", 0),
                            "current_task": state.get("current_task_id"),
                            "critic_score": critic,
                            "current_level": state.get("current_level"),
                            "current_subsystem": state.get("current_subsystem"),
                            "consecutive_failures": totals.get(
                                "consecutive_failures", 0
                            ),
                            "status": state.get("status"),
                        }
                    )
                    yield f"event: state_update\ndata: {evt}\n\n"
        finally:
            await _publisher.unsubscribe(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
