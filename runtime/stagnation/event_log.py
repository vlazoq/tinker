"""
tinker/anti_stagnation/event_log.py
────────────────────────────────────
Thread-safe, in-memory stagnation event log with bounded size.
Provides query helpers used by the Observability Dashboard.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Iterator
from datetime import datetime

from .models import StagnationEvent, StagnationType


class StagnationEventLog:
    """
    Bounded, thread-safe ring-buffer of StagnationEvent records.

    Usage:
        log = StagnationEventLog(max_size=500)
        log.append(event)
        recent = log.recent(n=10)
        by_type = log.filter(stagnation_type=StagnationType.SEMANTIC_LOOP)
    """

    def __init__(self, max_size: int = 500):
        self._max_size = max_size
        self._events: deque[StagnationEvent] = deque(maxlen=max_size)
        self._lock = threading.RLock()

        # Frequency counters — updated on every append for O(1) stats
        self._type_counts: dict[StagnationType, int] = {t: 0 for t in StagnationType}

    # ── write ────────────────────────────────────────────────

    def append(self, event: StagnationEvent) -> None:
        with self._lock:
            # If the deque is full, the leftmost (oldest) event will be
            # evicted — decrement its type count to keep stats accurate.
            if len(self._events) == self._events.maxlen:
                evicted = self._events[0]
                self._type_counts[evicted.stagnation_type] -= 1
            self._events.append(event)
            self._type_counts[event.stagnation_type] += 1

    # ── read ─────────────────────────────────────────────────

    def recent(self, n: int = 20) -> list[StagnationEvent]:
        with self._lock:
            items = list(self._events)
        return items[-n:]

    def filter(
        self,
        stagnation_type: StagnationType | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        predicate: Callable[[StagnationEvent], bool] | None = None,
    ) -> list[StagnationEvent]:
        with self._lock:
            items = list(self._events)

        result: list[StagnationEvent] = []
        for evt in items:
            if stagnation_type and evt.stagnation_type != stagnation_type:
                continue
            if since and evt.detected_at < since:
                continue
            if until and evt.detected_at > until:
                continue
            if predicate and not predicate(evt):
                continue
            result.append(evt)
        return result

    # ── stats ────────────────────────────────────────────────

    def counts_by_type(self) -> dict[str, int]:
        with self._lock:
            return {t.value: c for t, c in self._type_counts.items()}

    def total(self) -> int:
        with self._lock:
            return len(self._events)

    def last_event_of_type(self, stagnation_type: StagnationType) -> StagnationEvent | None:
        with self._lock:
            for evt in reversed(self._events):
                if evt.stagnation_type == stagnation_type:
                    return evt
        return None

    # ── export ───────────────────────────────────────────────

    def to_dicts(self, n: int | None = None) -> list[dict]:
        events = self.recent(n or self.total()) if n else list(self._events)
        return [e.to_dict() for e in events]

    # ── iteration ────────────────────────────────────────────

    def __iter__(self) -> Iterator[StagnationEvent]:
        with self._lock:
            snapshot = list(self._events)
        return iter(snapshot)

    def __len__(self) -> int:
        return self.total()

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            for t in StagnationType:
                self._type_counts[t] = 0
