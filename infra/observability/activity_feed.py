"""
infra/observability/activity_feed.py
=====================================

Human-readable activity feed for Tinker.

Produces real-time, human-friendly status messages describing what the
system is doing — similar to Claude Code's spinner verbs or a CI/CD
build log.  Messages are designed to be displayed in:

  - The web UI via SSE (Server-Sent Events)
  - The TUI dashboard
  - CLI stdout
  - Log files

The feed is intentionally separate from the event bus.  The event bus
carries structured machine-readable events; the activity feed translates
those into messages a human can glance at and understand.

Architecture
------------
::

    EventBus ──publish──▶ ActivityFeed ──emit──▶ listeners[]
                          (translates events       │
                           to human text)           ├─▶ SSE endpoint
                                                    ├─▶ TUI widget
                                                    ├─▶ CLI printer
                                                    └─▶ log file

Usage
-----
::

    from infra.observability.activity_feed import ActivityFeed, ActivityEntry

    feed = ActivityFeed()
    feed.add_listener(my_sse_callback)

    # Manual entry:
    await feed.post("Selecting next task from queue", category="micro")

    # Auto-wiring to event bus:
    feed.wire_event_bus(event_bus)
    # Now every event bus event automatically produces a feed entry.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Callable

logger = logging.getLogger(__name__)


class ActivityCategory(StrEnum):
    """Broad categories for activity entries.

    Used by UIs to colour-code or filter the feed.
    """

    SYSTEM = "system"          # Startup, shutdown, config changes
    MICRO = "micro"            # Micro loop steps
    MESO = "meso"              # Meso synthesis
    MACRO = "macro"            # Macro snapshot
    RESEARCH = "research"      # Web search, scraping
    QUALITY = "quality"        # Critic scoring, refinement
    STAGNATION = "stagnation"  # Stagnation detection / intervention
    TASK = "task"              # Task selection, generation
    ERROR = "error"            # Failures, timeouts


@dataclass
class ActivityEntry:
    """A single human-readable activity feed entry.

    Parameters
    ----------
    message   : The human-readable status message.
    category  : Broad category for filtering/colouring.
    detail    : Optional longer description or metadata.
    timestamp : When the activity occurred (auto-set to now).
    elapsed   : Optional duration in seconds (for completed activities).
    """

    message: str
    category: ActivityCategory = ActivityCategory.SYSTEM
    detail: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    elapsed: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise for JSON/SSE transport."""
        d: dict[str, Any] = {
            "message": self.message,
            "category": self.category.value,
            "timestamp": self.timestamp.isoformat(),
        }
        if self.detail:
            d["detail"] = self.detail
        if self.elapsed is not None:
            d["elapsed"] = round(self.elapsed, 2)
        return d


# Type alias for listener callbacks.  They receive an ActivityEntry and
# must be async (use asyncio.to_thread for sync callbacks).
ActivityListener = Callable[[ActivityEntry], Any]


class ActivityFeed:
    """Human-readable activity feed with listener support.

    The feed keeps a bounded history (default 200 entries) so new
    listeners can catch up without unbounded memory growth.

    Parameters
    ----------
    max_history : Maximum entries to retain in the ring buffer.
    """

    def __init__(self, max_history: int = 200) -> None:
        self._history: deque[ActivityEntry] = deque(maxlen=max_history)
        self._listeners: list[ActivityListener] = []
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Posting entries
    # ------------------------------------------------------------------

    async def post(
        self,
        message: str,
        category: str | ActivityCategory = ActivityCategory.SYSTEM,
        detail: str = "",
        elapsed: float | None = None,
    ) -> ActivityEntry:
        """Create and emit an activity entry.

        Parameters
        ----------
        message  : Human-readable status message (e.g. "Calling Architect AI").
        category : Category string or ActivityCategory enum.
        detail   : Optional longer description.
        elapsed  : Optional duration in seconds.

        Returns
        -------
        The created ActivityEntry.
        """
        if isinstance(category, str):
            try:
                category = ActivityCategory(category)
            except ValueError:
                category = ActivityCategory.SYSTEM

        entry = ActivityEntry(
            message=message,
            category=category,
            detail=detail,
            elapsed=elapsed,
        )

        async with self._lock:
            self._history.append(entry)

        # Fan out to listeners (fire-and-forget, errors isolated).
        await self._notify(entry)
        return entry

    async def _notify(self, entry: ActivityEntry) -> None:
        """Notify all registered listeners about a new entry."""
        for listener in self._listeners:
            try:
                result = listener(entry)
                # Support both sync and async listeners
                if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                    await result
            except Exception as exc:
                logger.debug("Activity feed listener error (non-fatal): %s", exc)

    # ------------------------------------------------------------------
    # Listeners
    # ------------------------------------------------------------------

    def add_listener(self, listener: ActivityListener) -> None:
        """Register a callback that receives every new entry."""
        self._listeners.append(listener)

    def remove_listener(self, listener: ActivityListener) -> bool:
        """Remove a previously registered listener.  Returns True if found."""
        try:
            self._listeners.remove(listener)
            return True
        except ValueError:
            return False

    # ------------------------------------------------------------------
    # History access
    # ------------------------------------------------------------------

    def get_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent entries as dicts (for JSON serialisation)."""
        entries = list(self._history)
        return [e.to_dict() for e in entries[-limit:]]

    def get_entries(self, limit: int = 50) -> list[ActivityEntry]:
        """Return the most recent ActivityEntry objects."""
        entries = list(self._history)
        return entries[-limit:]

    @property
    def latest(self) -> ActivityEntry | None:
        """The most recent entry, or None if empty."""
        return self._history[-1] if self._history else None

    # ------------------------------------------------------------------
    # Event bus integration
    # ------------------------------------------------------------------

    def wire_event_bus(self, bus: Any) -> None:
        """Subscribe to all event bus events and translate them to feed entries.

        This is the primary integration point.  Call once at startup after
        both the EventBus and ActivityFeed are created.
        """
        try:
            from core.events import EventType
        except ImportError:
            logger.warning("ActivityFeed: core.events not available — skipping event bus wiring")
            return

        # Map event types to human-readable messages and categories.
        _EVENT_MESSAGES: dict[str, tuple[str, ActivityCategory]] = {
            EventType.SYSTEM_STARTED: ("System started", ActivityCategory.SYSTEM),
            EventType.SYSTEM_STOPPING: ("System shutting down", ActivityCategory.SYSTEM),
            EventType.TASK_SELECTED: ("Selected task: {subsystem} — {title}", ActivityCategory.TASK),
            EventType.TASK_COMPLETED: ("Task completed: {task_id}", ActivityCategory.TASK),
            EventType.TASK_FAILED: ("Task failed: {task_id}", ActivityCategory.ERROR),
            EventType.TASKS_GENERATED: ("Generated {count} follow-up tasks", ActivityCategory.TASK),
            EventType.ARCHITECT_COMPLETED: (
                "Architect produced design ({tokens} tokens)",
                ActivityCategory.MICRO,
            ),
            EventType.CRITIC_SCORED: (
                "Critic scored output: {score:.2f}",
                ActivityCategory.QUALITY,
            ),
            EventType.REFINEMENT_ITERATION: (
                "Refinement iteration {iteration}: score {score:.2f} < {threshold:.2f}, re-running",
                ActivityCategory.QUALITY,
            ),
            EventType.RESEARCH_COMPLETED: (
                "Research completed: resolved {gaps_resolved} knowledge gaps",
                ActivityCategory.RESEARCH,
            ),
            EventType.MICRO_LOOP_COMPLETED: (
                "Micro loop #{iteration} completed ({subsystem}, score={score:.2f}, {duration:.1f}s)",
                ActivityCategory.MICRO,
            ),
            EventType.MICRO_LOOP_FAILED: (
                "Micro loop #{iteration} failed: {error}",
                ActivityCategory.ERROR,
            ),
            EventType.MESO_LOOP_COMPLETED: (
                "Meso synthesis completed for {subsystem}",
                ActivityCategory.MESO,
            ),
            EventType.MESO_LOOP_FAILED: (
                "Meso synthesis failed: {error}",
                ActivityCategory.ERROR,
            ),
            EventType.MACRO_LOOP_COMPLETED: (
                "Macro snapshot committed",
                ActivityCategory.MACRO,
            ),
            EventType.MACRO_LOOP_FAILED: (
                "Macro snapshot failed: {error}",
                ActivityCategory.ERROR,
            ),
            EventType.ARTIFACT_STORED: (
                "Stored artifact {artifact_id}",
                ActivityCategory.MICRO,
            ),
            EventType.SYNTHESIZER_COMPLETED: (
                "Synthesizer produced summary ({tokens} tokens)",
                ActivityCategory.MESO,
            ),
            EventType.STAGNATION_DETECTED: (
                "Stagnation detected: {type}",
                ActivityCategory.STAGNATION,
            ),
            EventType.STAGNATION_INTERVENTION: (
                "Intervention applied: {directive}",
                ActivityCategory.STAGNATION,
            ),
            EventType.STAGNATION_RESOLVED: (
                "Stagnation resolved",
                ActivityCategory.STAGNATION,
            ),
            EventType.CIRCUIT_OPENED: (
                "Circuit breaker opened: {name}",
                ActivityCategory.ERROR,
            ),
            EventType.CIRCUIT_CLOSED: (
                "Circuit breaker closed: {name}",
                ActivityCategory.SYSTEM,
            ),
            EventType.HUMAN_REVIEW_REQUESTED: (
                "Waiting for human review",
                ActivityCategory.QUALITY,
            ),
            EventType.HUMAN_REVIEW_SUBMITTED: (
                "Human review submitted (score={score:.2f})",
                ActivityCategory.QUALITY,
            ),
        }

        async def _on_event(event: Any) -> None:
            """Translate an Event into a human-readable ActivityEntry."""
            event_key = event.type
            template_pair = _EVENT_MESSAGES.get(event_key)
            if template_pair is None:
                # Unknown event — emit a generic message
                await self.post(
                    f"Event: {event.type.value}",
                    category=ActivityCategory.SYSTEM,
                    detail=str(event.payload)[:200],
                )
                return

            template, category = template_pair
            payload = event.payload or {}
            try:
                message = template.format(**payload)
            except (KeyError, ValueError, IndexError):
                # Template format failed — use the raw template
                message = template

            await self.post(
                message,
                category=category,
                detail=str(payload)[:300] if payload else "",
                elapsed=payload.get("duration"),
            )

        # Subscribe as a wildcard handler — receives every event.
        bus.subscribe_handler(None, _on_event)
        logger.info("ActivityFeed wired to EventBus (wildcard subscription)")
