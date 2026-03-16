"""
resilience/circuit_breaker.py
==============================

Circuit breaker implementation for Tinker's external service calls.

What is a circuit breaker?
---------------------------
A circuit breaker wraps calls to external services (Ollama, Redis, SearXNG,
etc.) and prevents cascading failures.  It works like an electrical circuit
breaker: when too many failures happen in a row, it "opens" (trips) and
subsequent calls fail immediately without ever hitting the downstream service.
After a configurable cooldown period, it goes "half-open" and allows one
probe request through.  If that succeeds, the breaker "closes" again and
normal traffic resumes.

States
------
  CLOSED    — Normal operation.  Calls pass through to the real service.
  OPEN      — Breaker has tripped.  Calls fail immediately (fast fail).
  HALF_OPEN — Cooldown elapsed.  One probe call allowed through.

Why this matters for Tinker
----------------------------
Without circuit breakers, a Redis outage causes thousands of failed calls per
minute as every micro loop hammers the unavailable server.  With a circuit
breaker, after 5 failures the breaker opens, and the orchestrator falls back to
graceful degradation (e.g., running without caching) until Redis recovers.

Usage
------
    breaker = CircuitBreaker(name="redis", failure_threshold=5, recovery_timeout=30)

    # Wrap any async call:
    result = await breaker.call(redis_client.ping)

    # Or use as a decorator:
    @breaker.protect
    async def fetch_artifact(id: str) -> dict:
        return await redis.get(id)

    # Check current state:
    print(breaker.state)       # CircuitState.CLOSED
    print(breaker.is_open)     # False

    # Register a callback for state changes (e.g. to trigger alerts):
    breaker.on_state_change(lambda b, old, new: alert(f"{b.name}: {old}→{new}"))
"""

from __future__ import annotations

import asyncio
import enum
import functools
import logging
import time
from typing import Any, Callable, Awaitable, Optional

logger = logging.getLogger(__name__)


class CircuitState(enum.Enum):
    """Possible states of a circuit breaker."""
    CLOSED    = "closed"     # Normal operation — calls pass through
    OPEN      = "open"       # Tripped — calls fail immediately
    HALF_OPEN = "half_open"  # Probe state — one call allowed through


class CircuitBreakerOpenError(Exception):
    """
    Raised when a call is attempted on an OPEN circuit breaker.

    Callers should catch this and apply graceful degradation logic rather
    than propagating it as a fatal error.
    """
    def __init__(self, name: str, recovery_at: float) -> None:
        self.name = name
        self.recovery_at = recovery_at
        remaining = max(0.0, recovery_at - time.monotonic())
        super().__init__(
            f"Circuit '{name}' is OPEN — service unavailable. "
            f"Recovery probe in {remaining:.1f}s."
        )


class CircuitBreaker:
    """
    Thread-safe (asyncio-safe) circuit breaker for a single external service.

    Parameters
    ----------
    name              : Human-readable name used in logs and alerts.
    failure_threshold : How many consecutive failures before the breaker opens.
                        Default: 5.  Lower means more sensitive.
    recovery_timeout  : Seconds to wait after opening before allowing a probe.
                        Default: 30s.  Increase for slow-recovering services.
    success_threshold : How many consecutive probe successes needed to close
                        again from HALF_OPEN state.  Default: 1.
    on_state_change   : Optional callback(breaker, old_state, new_state) called
                        whenever the state transitions.

    Example
    -------
    ::

        redis_breaker = CircuitBreaker(
            name="redis",
            failure_threshold=5,
            recovery_timeout=30,
        )

        try:
            result = await redis_breaker.call(redis.get, "key")
        except CircuitBreakerOpenError:
            result = None  # degrade gracefully
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        success_threshold: int = 1,
        on_state_change: Optional[Callable] = None,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.success_threshold = success_threshold
        self._on_state_change = on_state_change

        self._state: CircuitState = CircuitState.CLOSED
        self._failure_count: int = 0
        self._success_count: int = 0
        self._last_failure_time: float = 0.0
        self._open_at: float = 0.0       # monotonic time when breaker opened
        self._total_calls: int = 0
        self._total_failures: int = 0
        self._total_short_circuits: int = 0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # State properties
    # ------------------------------------------------------------------

    @property
    def state(self) -> CircuitState:
        """Current state of the circuit breaker."""
        return self._state

    @property
    def is_open(self) -> bool:
        """True if the breaker is OPEN (fast-fail mode)."""
        return self._state == CircuitState.OPEN

    @property
    def is_closed(self) -> bool:
        """True if the breaker is CLOSED (normal operation)."""
        return self._state == CircuitState.CLOSED

    @property
    def failure_count(self) -> int:
        """Current consecutive failure streak."""
        return self._failure_count

    def stats(self) -> dict:
        """Return a snapshot of circuit breaker statistics for monitoring."""
        return {
            "name": self.name,
            "state": self._state.value,
            "failure_count": self._failure_count,
            "total_calls": self._total_calls,
            "total_failures": self._total_failures,
            "total_short_circuits": self._total_short_circuits,
            "last_failure_ago": (
                time.monotonic() - self._last_failure_time
                if self._last_failure_time else None
            ),
        }

    # ------------------------------------------------------------------
    # Core call interface
    # ------------------------------------------------------------------

    async def call(self, fn: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
        """
        Execute ``fn(*args, **kwargs)`` through this circuit breaker.

        If the breaker is OPEN, raises CircuitBreakerOpenError immediately
        without calling fn (fast fail).

        If the breaker is HALF_OPEN, allows exactly one call through.  If
        it succeeds, transitions back to CLOSED; if it fails, reopens.

        If the breaker is CLOSED, calls fn normally.  On failure, increments
        the failure counter and may trip the breaker.

        Parameters
        ----------
        fn     : Async callable to protect.
        *args  : Positional arguments forwarded to fn.
        **kwargs : Keyword arguments forwarded to fn.

        Returns
        -------
        The return value of fn on success.

        Raises
        ------
        CircuitBreakerOpenError : If breaker is OPEN.
        Any exception raised by fn : On actual failure (and increments counter).
        """
        async with self._lock:
            await self._check_recovery()
            current = self._state

        if current == CircuitState.OPEN:
            self._total_short_circuits += 1
            logger.debug("Circuit '%s' OPEN — short-circuiting call to %s", self.name, fn)
            raise CircuitBreakerOpenError(self.name, self._open_at + self.recovery_timeout)

        # Attempt the actual call
        self._total_calls += 1
        try:
            result = await fn(*args, **kwargs)
            await self._on_success()
            return result
        except Exception as exc:
            await self._on_failure(exc)
            raise

    def protect(self, fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        """
        Decorator that wraps an async function with this circuit breaker.

        Usage::

            @breaker.protect
            async def fetch(key: str) -> str:
                return await redis.get(key)
        """
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            return await self.call(fn, *args, **kwargs)
        return wrapper

    def on_state_change(self, callback: Callable) -> None:
        """
        Register a callback invoked on every state transition.

        Signature: ``callback(breaker: CircuitBreaker, old: CircuitState, new: CircuitState)``

        Use this to send alerts when a breaker opens or closes.
        """
        self._on_state_change = callback

    # ------------------------------------------------------------------
    # Internal state machine
    # ------------------------------------------------------------------

    async def _check_recovery(self) -> None:
        """
        If OPEN and recovery timeout has elapsed, transition to HALF_OPEN.

        Called under the lock at the start of every ``call()``.
        """
        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._open_at
            if elapsed >= self.recovery_timeout:
                self._transition(CircuitState.HALF_OPEN)
                logger.info(
                    "Circuit '%s' HALF_OPEN — probe call allowed after %.1fs",
                    self.name, elapsed,
                )

    async def _on_success(self) -> None:
        """Handle a successful call — may close the breaker."""
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.success_threshold:
                    self._success_count = 0
                    self._failure_count = 0
                    self._transition(CircuitState.CLOSED)
                    logger.info("Circuit '%s' CLOSED — service recovered.", self.name)
            elif self._state == CircuitState.CLOSED:
                # Reset failure streak on any success
                self._failure_count = 0

    async def _on_failure(self, exc: Exception) -> None:
        """Handle a failed call — may open the breaker."""
        async with self._lock:
            self._failure_count += 1
            self._total_failures += 1
            self._last_failure_time = time.monotonic()

            if self._state == CircuitState.HALF_OPEN:
                # Probe failed — reopen the breaker
                self._success_count = 0
                self._transition(CircuitState.OPEN)
                self._open_at = time.monotonic()
                logger.warning(
                    "Circuit '%s' OPEN (probe failed): %s — will retry in %.1fs",
                    self.name, exc, self.recovery_timeout,
                )

            elif self._state == CircuitState.CLOSED:
                if self._failure_count >= self.failure_threshold:
                    self._transition(CircuitState.OPEN)
                    self._open_at = time.monotonic()
                    logger.warning(
                        "Circuit '%s' OPEN after %d consecutive failures. "
                        "Last error: %s — will retry in %.1fs",
                        self.name, self._failure_count, exc, self.recovery_timeout,
                    )
                else:
                    logger.debug(
                        "Circuit '%s' failure %d/%d: %s",
                        self.name, self._failure_count, self.failure_threshold, exc,
                    )

    def _transition(self, new_state: CircuitState) -> None:
        """Perform a state transition and notify the callback if registered."""
        old_state = self._state
        self._state = new_state
        if self._on_state_change and old_state != new_state:
            try:
                self._on_state_change(self, old_state, new_state)
            except Exception as e:
                logger.warning("Circuit breaker state-change callback raised: %s", e)


class CircuitBreakerRegistry:
    """
    A named registry of circuit breakers for all Tinker external services.

    Provides a single place to create, retrieve, and inspect all breakers.
    Intended to be a singleton created at startup and passed to components
    that need protection.

    Usage
    -----
    ::

        registry = CircuitBreakerRegistry()
        registry.register("ollama_server",   failure_threshold=5, recovery_timeout=30)
        registry.register("ollama_secondary", failure_threshold=5, recovery_timeout=30)
        registry.register("redis",           failure_threshold=3, recovery_timeout=15)
        registry.register("searxng",         failure_threshold=3, recovery_timeout=20)

        # Later, in the LLM client:
        breaker = registry.get("ollama_server")
        result  = await breaker.call(client.chat, messages)
    """

    def __init__(self) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}

    def register(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        success_threshold: int = 1,
        on_state_change: Optional[Callable] = None,
    ) -> CircuitBreaker:
        """
        Create and register a new circuit breaker.

        Raises ValueError if a breaker with the same name already exists.
        """
        if name in self._breakers:
            raise ValueError(f"Circuit breaker '{name}' already registered")
        breaker = CircuitBreaker(
            name=name,
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
            success_threshold=success_threshold,
            on_state_change=on_state_change,
        )
        self._breakers[name] = breaker
        logger.info(
            "Registered circuit breaker '%s' (threshold=%d, timeout=%.1fs)",
            name, failure_threshold, recovery_timeout,
        )
        return breaker

    def get(self, name: str) -> CircuitBreaker:
        """
        Retrieve a registered circuit breaker by name.

        Raises KeyError if the breaker hasn't been registered.
        """
        if name not in self._breakers:
            raise KeyError(f"Circuit breaker '{name}' not found. Call register() first.")
        return self._breakers[name]

    def get_or_default(self, name: str) -> Optional[CircuitBreaker]:
        """Return breaker if registered, or None (no-op path)."""
        return self._breakers.get(name)

    def all_stats(self) -> dict[str, dict]:
        """Return stats for every registered breaker — useful for health endpoints."""
        return {name: b.stats() for name, b in self._breakers.items()}

    def any_open(self) -> bool:
        """True if at least one breaker is currently OPEN (degraded mode)."""
        return any(b.is_open for b in self._breakers.values())


def build_default_registry(on_state_change: Optional[Callable] = None) -> CircuitBreakerRegistry:
    """
    Build the standard Tinker circuit breaker registry with sensible defaults.

    Creates breakers for all known external services:
      - ollama_server    : Primary model server (higher threshold — models are slow)
      - ollama_secondary : Secondary model server
      - redis            : Working memory cache (lower threshold — fast I/O)
      - searxng          : Web search tool
      - chromadb         : Vector database (research archive)

    Parameters
    ----------
    on_state_change : Optional callback for all breaker state transitions.
                      Signature: (breaker, old_state, new_state)

    Returns
    -------
    CircuitBreakerRegistry pre-loaded with all standard breakers.
    """
    registry = CircuitBreakerRegistry()

    # Ollama calls are slow (120s timeout), so we use a higher threshold
    # before tripping — transient timeouts are expected under load.
    registry.register(
        "ollama_server",
        failure_threshold=5,
        recovery_timeout=60.0,
        on_state_change=on_state_change,
    )
    registry.register(
        "ollama_secondary",
        failure_threshold=5,
        recovery_timeout=60.0,
        on_state_change=on_state_change,
    )

    # Redis is fast — 3 failures in a row means it's really down.
    # Short recovery time because Redis comes back quickly.
    registry.register(
        "redis",
        failure_threshold=3,
        recovery_timeout=15.0,
        on_state_change=on_state_change,
    )

    # SearXNG — web searches can be slow but errors shouldn't cascade.
    registry.register(
        "searxng",
        failure_threshold=3,
        recovery_timeout=30.0,
        on_state_change=on_state_change,
    )

    # ChromaDB — vector DB reads/writes
    registry.register(
        "chromadb",
        failure_threshold=3,
        recovery_timeout=30.0,
        on_state_change=on_state_change,
    )

    return registry
