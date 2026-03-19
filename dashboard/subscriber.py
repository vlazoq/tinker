"""
tinker/dashboard/subscriber.py
──────────────────────────────
Async subscriber that bridges Orchestrator state updates → StateStore.

Two backends are supported:
  • QueueSubscriber  – in-process asyncio.Queue (same Python runtime)
  • RedisSubscriber  – Redis pub/sub channel  (separate processes)

The Orchestrator calls `publish_state(patch: dict)` (or publishes to a
Redis channel).  The subscriber receives the patch, validates it, and
writes into the shared StateStore.

If the Orchestrator goes away, the subscriber switches StateStore to
"disconnected" mode and keeps retrying in the background.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from .state import (
    ArchitectOutput,
    ArchitectureState,
    CriticOutput,
    LoopLevel,
    MemoryStats,
    ModelMetrics,
    QueueStats,
    StagnationEvent,
    StagnationStatus,
    TaskInfo,
    TaskStatus,
    TaskType,
    get_store,
)

log = logging.getLogger("tinker.dashboard.subscriber")

# ──────────────────────────────────────────
# Patch deserialisation helpers
# ──────────────────────────────────────────


def _dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _deserialise_patch(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert the raw JSON dict coming from the Orchestrator into typed
    dataclass instances that StateStore can store directly.
    """
    patch: Dict[str, Any] = {}

    if "connected" in raw:
        patch["connected"] = bool(raw["connected"])

    if "loop_level" in raw:
        try:
            patch["loop_level"] = LoopLevel(raw["loop_level"])
        except ValueError:
            pass

    for counter in ("micro_count", "meso_count", "macro_count"):
        if counter in raw:
            patch[counter] = int(raw[counter])

    if "active_task" in raw:
        t = raw["active_task"]
        if t is None:
            patch["active_task"] = None
        else:
            patch["active_task"] = TaskInfo(
                id=t.get("id", ""),
                type=TaskType(t.get("type", "design")),
                subsystem=t.get("subsystem", ""),
                description=t.get("description", ""),
                status=TaskStatus(t.get("status", "active")),
                created_at=_dt(t.get("created_at")) or datetime.utcnow(),
                started_at=_dt(t.get("started_at")),
                completed_at=_dt(t.get("completed_at")),
                result_summary=t.get("result_summary"),
                full_content=t.get("full_content"),
            )

    if "last_architect" in raw:
        a = raw["last_architect"]
        if a is None:
            patch["last_architect"] = None
        else:
            patch["last_architect"] = ArchitectOutput(
                summary=a.get("summary", ""),
                full_content=a.get("full_content", ""),
                timestamp=_dt(a.get("timestamp")) or datetime.utcnow(),
                task_id=a.get("task_id"),
            )

    if "last_critic" in raw:
        c = raw["last_critic"]
        if c is None:
            patch["last_critic"] = None
        else:
            patch["last_critic"] = CriticOutput(
                score=float(c.get("score", 0)),
                top_objection=c.get("top_objection", ""),
                full_content=c.get("full_content", ""),
                timestamp=_dt(c.get("timestamp")) or datetime.utcnow(),
                task_id=c.get("task_id"),
            )

    if "queue_stats" in raw:
        q = raw["queue_stats"]
        patch["queue_stats"] = QueueStats(
            total_depth=int(q.get("total_depth", 0)),
            by_status=q.get("by_status", {}),
            by_type=q.get("by_type", {}),
        )

    if "recent_tasks" in raw:
        tasks = []
        for t in raw["recent_tasks"]:
            tasks.append(
                TaskInfo(
                    id=t.get("id", ""),
                    type=TaskType(t.get("type", "design")),
                    subsystem=t.get("subsystem", ""),
                    description=t.get("description", ""),
                    status=TaskStatus(t.get("status", "pending")),
                    created_at=_dt(t.get("created_at")) or datetime.utcnow(),
                    started_at=_dt(t.get("started_at")),
                    completed_at=_dt(t.get("completed_at")),
                    result_summary=t.get("result_summary"),
                    full_content=t.get("full_content"),
                )
            )
        patch["recent_tasks"] = tasks

    if "arch_state" in raw:
        a = raw["arch_state"]
        if a is None:
            patch["arch_state"] = None
        else:
            patch["arch_state"] = ArchitectureState(
                version=a.get("version", "0.0.0"),
                last_commit_time=_dt(a.get("last_commit_time")),
                summary=a.get("summary", ""),
                full_content=a.get("full_content", ""),
            )

    if "stagnation" in raw:
        s = raw["stagnation"]
        events = []
        for e in s.get("recent_events", []):
            events.append(
                StagnationEvent(
                    timestamp=_dt(e.get("timestamp")) or datetime.utcnow(),
                    description=e.get("description", ""),
                    action_taken=e.get("action_taken", ""),
                )
            )
        patch["stagnation"] = StagnationStatus(
            is_stagnant=bool(s.get("is_stagnant", False)),
            stagnation_score=float(s.get("stagnation_score", 0.0)),
            monitor_status=s.get("monitor_status", "nominal"),
            recent_events=events,
        )

    if "model_metrics" in raw:
        m = raw["model_metrics"]
        patch["model_metrics"] = ModelMetrics(
            avg_latency_ms=float(m.get("avg_latency_ms", 0)),
            p99_latency_ms=float(m.get("p99_latency_ms", 0)),
            error_rate=float(m.get("error_rate", 0)),
            total_calls=int(m.get("total_calls", 0)),
            recent_errors=m.get("recent_errors", []),
        )

    if "memory_stats" in raw:
        m = raw["memory_stats"]
        patch["memory_stats"] = MemoryStats(
            session_artifact_count=int(m.get("session_artifact_count", 0)),
            research_archive_size=int(m.get("research_archive_size", 0)),
            working_memory_tokens=int(m.get("working_memory_tokens", 0)),
        )

    return patch


# ──────────────────────────────────────────
# Abstract base
# ──────────────────────────────────────────


class BaseSubscriber(ABC):
    """Subscribes to Orchestrator events and feeds StateStore."""

    def __init__(self, on_update: Optional[Callable] = None) -> None:
        self._store = get_store()
        self._running = False
        self._on_update = on_update  # optional callback after each patch

    @abstractmethod
    async def run(self) -> None:
        """Main loop — keep running until cancelled."""
        ...

    def stop(self) -> None:
        self._running = False

    def _apply(self, raw: Dict[str, Any]) -> None:
        patch = _deserialise_patch(raw)
        self._store.apply_patch(patch)
        if self._on_update:
            self._on_update()


# ──────────────────────────────────────────
# In-process queue subscriber
# ──────────────────────────────────────────

# Module-level queue — the Orchestrator (same process) puts dicts here.
_shared_queue: asyncio.Queue = asyncio.Queue(maxsize=256)


def get_shared_queue() -> asyncio.Queue:
    """Called by the Orchestrator to obtain the publish queue."""
    return _shared_queue


def publish_state(patch: Dict[str, Any]) -> None:
    """
    Convenience helper for the Orchestrator.
    Call this synchronously from any thread; it is safe.
    """
    try:
        _shared_queue.put_nowait(patch)
    except asyncio.QueueFull:
        log.warning("Dashboard queue full – dropping state update")


class QueueSubscriber(BaseSubscriber):
    """
    Reads patches from the in-process asyncio.Queue and applies them
    to StateStore.  Used when Orchestrator and Dashboard share a Python
    runtime.
    """

    def __init__(
        self, on_update: Optional[Callable] = None, timeout: float = 5.0
    ) -> None:
        super().__init__(on_update)
        self._timeout = timeout

    async def run(self) -> None:
        self._running = True
        self._store.mark_connected()
        log.info("QueueSubscriber started")

        while self._running:
            try:
                raw = await asyncio.wait_for(_shared_queue.get(), timeout=self._timeout)
                self._apply(raw)
                self._store.mark_connected()
            except asyncio.TimeoutError:
                # No update for a while — still "connected" unless we
                # explicitly hear otherwise; do nothing.
                pass
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("QueueSubscriber error: %s", exc)
                self._store.mark_disconnected()
                await asyncio.sleep(1.0)

        self._store.mark_disconnected()
        log.info("QueueSubscriber stopped")


# ──────────────────────────────────────────
# Redis pub/sub subscriber (optional)
# ──────────────────────────────────────────

REDIS_CHANNEL = "tinker:state"
REDIS_RECONNECT_DELAY = 3.0  # seconds


class RedisSubscriber(BaseSubscriber):
    """
    Subscribes to a Redis pub/sub channel.  The Orchestrator publishes
    JSON-encoded state patches; this subscriber decodes and applies them.

    Requires:  pip install redis[asyncio]
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        channel: str = REDIS_CHANNEL,
        on_update: Optional[Callable] = None,
    ) -> None:
        super().__init__(on_update)
        self._redis_url = redis_url
        self._channel = channel

    async def run(self) -> None:
        self._running = True
        log.info("RedisSubscriber starting on channel '%s'", self._channel)

        while self._running:
            try:
                import redis.asyncio as aioredis  # type: ignore

                client = aioredis.from_url(self._redis_url)
                pubsub = client.pubsub()
                await pubsub.subscribe(self._channel)
                self._store.mark_connected()
                log.info("RedisSubscriber connected to %s", self._redis_url)

                async for message in pubsub.listen():
                    if not self._running:
                        break
                    if message["type"] != "message":
                        continue
                    try:
                        raw = json.loads(message["data"])
                        self._apply(raw)
                        self._store.mark_connected()
                    except json.JSONDecodeError as e:
                        log.warning("Bad JSON from Redis: %s", e)

                await pubsub.unsubscribe(self._channel)
                await client.aclose()

            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error(
                    "RedisSubscriber error: %s – reconnecting in %.1fs",
                    exc,
                    REDIS_RECONNECT_DELAY,
                )
                self._store.mark_disconnected()
                await asyncio.sleep(REDIS_RECONNECT_DELAY)

        self._store.mark_disconnected()
        log.info("RedisSubscriber stopped")
