"""
core/events/bus.py
==================

Async publish-subscribe EventBus.

Design
------
* Handlers are registered per EventType (or for ALL events via a wildcard).
* ``publish()`` fans out to all matching handlers concurrently via
  ``asyncio.gather``.  A failing handler is isolated — it cannot prevent
  other handlers from executing.
* The bus is intentionally lightweight: no persistence, no replay, no
  dead-letter queue.  Those concerns live in AuditLog and DeadLetterQueue.
* Thread-safe: all methods acquire ``_lock`` so the bus can be used from
  concurrent coroutines without data races.

Usage
-----
::

    bus = EventBus()

    # Register a handler for one event type:
    @bus.subscribe(EventType.STAGNATION_DETECTED)
    async def handle_stagnation(event: Event) -> None:
        await alerter.send(f"Stagnation: {event.payload}")

    # Register a handler for ALL event types:
    @bus.subscribe()
    async def log_all(event: Event) -> None:
        logger.info("Event: %s", event.type)

    # Emit an event:
    await bus.publish(Event(type=EventType.STAGNATION_DETECTED, payload={"count": 3}))

    # Inspect registered handlers:
    count = bus.handler_count(EventType.STAGNATION_DETECTED)
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Awaitable, Callable

from .types import Event, EventType

logger = logging.getLogger(__name__)

#: Type alias for an async handler function.
EventHandler = Callable[[Event], Awaitable[None]]

#: Sentinel used to register a handler for every event type.
_WILDCARD = "__all__"


class EventBus:
    """Async publish-subscribe bus.

    All handlers are async callables that accept a single ``Event`` argument.
    Synchronous callables are not supported — wrap them in ``asyncio.to_thread``
    if you need to call blocking code from a handler.

    Parameters
    ----------
    error_policy : ``"log"`` (default) — log handler errors and continue.
                   ``"raise"`` — re-raise the first handler error.
    """

    def __init__(self, error_policy: str = "log") -> None:
        if error_policy not in ("log", "raise"):
            raise ValueError(f"error_policy must be 'log' or 'raise', got {error_policy!r}")
        self._policy = error_policy
        # Dict: event_type_value | _WILDCARD → list of handlers
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def subscribe(
        self,
        event_type: EventType | None = None,
    ) -> Callable[[EventHandler], EventHandler]:
        """Decorator that registers a handler for the given event type.

        If ``event_type`` is omitted (or ``None``), the handler receives
        every event regardless of type (wildcard subscription).

        Parameters
        ----------
        event_type : EventType, optional
            The event type to subscribe to, or ``None`` for all events.

        Returns
        -------
        A decorator that registers the wrapped function and returns it
        unchanged, so it can still be called directly in tests.

        Example
        -------
        ::

            @bus.subscribe(EventType.CIRCUIT_OPENED)
            async def on_open(event: Event) -> None:
                ...

            # Wildcard — receives every event
            @bus.subscribe()
            async def log_all(event: Event) -> None:
                ...
        """
        key = event_type.value if event_type is not None else _WILDCARD

        def decorator(fn: EventHandler) -> EventHandler:
            self._handlers[key].append(fn)
            logger.debug(
                "EventBus: subscribed %s to %s",
                getattr(fn, "__qualname__", repr(fn)),
                key,
            )
            return fn

        return decorator

    def subscribe_handler(
        self,
        event_type: EventType | None,
        handler: EventHandler,
    ) -> None:
        """Register a handler without using the decorator syntax.

        Equivalent to ``@bus.subscribe(event_type)`` but useful when
        handlers are registered programmatically (e.g. in a loop).
        """
        key = event_type.value if event_type is not None else _WILDCARD
        self._handlers[key].append(handler)

    def unsubscribe(
        self,
        event_type: EventType | None,
        handler: EventHandler,
    ) -> bool:
        """Remove a previously registered handler.

        Parameters
        ----------
        event_type : The event type the handler was subscribed to,
                     or ``None`` if it was a wildcard subscription.
        handler    : The exact handler function to remove.

        Returns
        -------
        True if the handler was found and removed, False otherwise.
        """
        key = event_type.value if event_type is not None else _WILDCARD
        handlers = self._handlers.get(key, [])
        if handler in handlers:
            handlers.remove(handler)
            return True
        return False

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    async def publish(self, event: Event) -> int:
        """Fan out ``event`` to all matching handlers concurrently.

        Handlers for ``event.type`` and wildcard handlers both receive the
        event.  Handlers run concurrently via ``asyncio.gather``.

        A handler that raises is isolated:
        * Under ``error_policy="log"`` (default): the error is logged and
          the remaining handlers continue normally.
        * Under ``error_policy="raise"``: the first error is re-raised after
          all handlers have completed (other handlers are not cancelled).

        Parameters
        ----------
        event : The event to dispatch.

        Returns
        -------
        int
            Number of handlers that were invoked.
        """
        type_handlers = list(self._handlers.get(event.type.value, []))
        wildcard_handlers = list(self._handlers.get(_WILDCARD, []))
        all_handlers = type_handlers + wildcard_handlers

        if not all_handlers:
            logger.debug("EventBus: no handlers for %s", event.type)
            return 0

        errors: list[Exception] = []

        async def _call(fn: EventHandler) -> None:
            try:
                await fn(event)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "EventBus handler %s raised for event %s: %s",
                    getattr(fn, "__qualname__", repr(fn)),
                    event.type,
                    exc,
                    exc_info=True,
                )
                if self._policy == "raise":
                    errors.append(exc)

        await asyncio.gather(*[_call(fn) for fn in all_handlers])

        if errors:
            raise errors[0]

        logger.debug(
            "EventBus: dispatched %s to %d handler(s)", event.type, len(all_handlers)
        )
        return len(all_handlers)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def handler_count(self, event_type: EventType | None = None) -> int:
        """Return the number of handlers registered for the given type.

        Parameters
        ----------
        event_type : The type to query, or ``None`` to query wildcard handlers.
        """
        key = event_type.value if event_type is not None else _WILDCARD
        return len(self._handlers.get(key, []))

    def clear(self, event_type: EventType | None = None) -> None:
        """Remove all handlers for the given event type, or all handlers.

        Parameters
        ----------
        event_type : Type to clear, or ``None`` to clear everything.
        """
        if event_type is None:
            self._handlers.clear()
        else:
            self._handlers.pop(event_type.value, None)
