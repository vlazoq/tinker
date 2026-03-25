"""
resilience/retry.py
====================
Production-grade async retry decorator with exponential back-off and jitter.

This module is the **only place** in Tinker that decides whether to retry
a failing operation.  The decision is based on ``exc.retryable`` from the
typed exception hierarchy (``exceptions.py``), **not** on exception class
names or message strings.

Key design choices
------------------
* **``retryable`` flag is authoritative.**  If ``exc.retryable is False``,
  the exception propagates immediately — no retry is attempted regardless
  of ``max_attempts``.

* **Full jitter** (AWS recommendation).  Without jitter, all callers that
  started at the same moment and hit the same error will retry at the same
  time — creating a thundering-herd spike.  Jitter spreads them out::

      delay = uniform(0, base_delay * 2^(attempt - 1))

* **``max_delay`` cap.**  Exponential growth is unbounded; the cap ensures
  retries converge to a fixed maximum wait rather than waiting minutes.

* **Structured logging.**  Every retry attempt emits a WARNING with the
  attempt number, max attempts, delay, exception class, and ``exc.context``
  so that on-call engineers can correlate retry storms with root causes in
  their log aggregator.

* **Async-only.**  Tinker's I/O is async throughout; a synchronous wrapper
  is not provided.  Use ``asyncio.to_thread`` for sync callables.

Usage
-----
Apply the decorator with default settings::

    from infra.resilience.retry import with_retry

    @with_retry()
    async def call_model(prompt: str) -> str:
        ...

Override settings per call-site::

    from infra.resilience.retry import with_retry, RetryConfig

    @with_retry(RetryConfig(max_attempts=5, base_delay=2.0))
    async def fetch_research(query: str) -> list[str]:
        ...

Use as a context manager for dynamic callables::

    from infra.resilience.retry import retry_async

    result = await retry_async(
        lambda: fetch(url),
        config=RetryConfig(max_attempts=3),
    )

Non-retryable errors pass through immediately::

    @with_retry()
    async def validate(data):
        raise ValidationError("email", data["email"], "invalid format")
        # ValidationError.retryable is False → no retry, propagates immediately

``CircuitBreakerOpenError`` has ``retryable=True`` but callers should combine
the retry decorator with ``CircuitBreaker.call()`` — do not retry circuit-
breaker errors in isolation since the breaker manages its own back-off window.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, TypeVar

from exceptions import TinkerError

log = logging.getLogger(__name__)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryConfig:
    """
    Configuration for the retry decorator.

    Attributes
    ----------
    max_attempts : int
        Total number of attempts (including the first).  Must be >= 1.
        ``max_attempts=1`` means "try once, never retry".
    base_delay : float
        Initial sleep duration in seconds.  Doubles on each subsequent
        attempt before jitter is applied.
    max_delay : float
        Hard ceiling on the sleep duration (seconds).  Prevents exponential
        growth from producing unreasonably long waits.
    jitter : bool
        If ``True`` (default), apply full jitter: ``sleep = uniform(0, delay)``
        instead of the deterministic ``delay``.  Strongly recommended for
        distributed systems to prevent thundering-herd retries.
    only_if_retryable : bool
        If ``True`` (default), only retry when ``exc.retryable is True``.
        Set to ``False`` to retry on every ``TinkerError`` regardless.
    reraise_after_exhaustion : bool
        If ``True`` (default), re-raise the last exception after all attempts
        are exhausted.  If ``False``, return ``None`` (use with care).
    idempotent : bool
        If ``False``, the wrapped operation is *not* idempotent (e.g. a
        ``save_artifact`` call that may have partially written data before
        failing).  Non-idempotent operations are only retried on the first
        attempt failure and never after that — i.e. ``max_attempts`` is
        effectively capped at 2 when ``idempotent=False``.

        When ``True`` (default), retries are unrestricted up to
        ``max_attempts`` because re-running the same operation is safe.

        Rule of thumb:
          * Pure reads  (SELECT, GET)           → idempotent=True
          * Conditional writes (INSERT OR IGNORE, upsert) → idempotent=True
          * Append / create-only operations     → idempotent=False
          * Anything that sends an external side-effect (email, webhook)
                                                → idempotent=False
    max_total_seconds : float or None
        Hard wall-clock cap on total retry time (seconds).  When set, retries
        stop as soon as the elapsed time since the *first* attempt exceeds
        this value, regardless of ``max_attempts``.  ``None`` (default) means
        "no wall-clock cap — rely on max_attempts only".

        Use this to bound the worst-case latency of a call site::

            RetryConfig(max_attempts=10, max_total_seconds=30)
            # Guarantees a result or error within 30 s even if delays are long.
    """

    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    jitter: bool = True
    only_if_retryable: bool = True
    reraise_after_exhaustion: bool = True
    idempotent: bool = True
    max_total_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {self.max_attempts}")
        if self.base_delay < 0:
            raise ValueError(f"base_delay must be >= 0, got {self.base_delay}")
        if self.max_delay < self.base_delay:
            raise ValueError(
                f"max_delay ({self.max_delay}) must be >= base_delay ({self.base_delay})"
            )
        if self.max_total_seconds is not None and self.max_total_seconds <= 0:
            raise ValueError(
                f"max_total_seconds must be > 0, got {self.max_total_seconds}"
            )


# Common pre-built configs ─────────────────────────────────────────────────

#: Aggressive: 5 attempts, starting at 0.5 s, up to 30 s.
#: Suitable for highly transient failures (e.g. Redis blip).
AGGRESSIVE = RetryConfig(max_attempts=5, base_delay=0.5, max_delay=30.0)

#: Conservative: 3 attempts, starting at 2 s, up to 60 s.
#: Suitable for external service calls (Ollama, ChromaDB).
CONSERVATIVE = RetryConfig(max_attempts=3, base_delay=2.0, max_delay=60.0)

#: No retry: try exactly once.
ONCE = RetryConfig(max_attempts=1)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _compute_delay(attempt: int, config: RetryConfig) -> float:
    """
    Return the sleep duration (seconds) before attempt number *attempt*.

    Uses exponential back-off with optional full jitter::

        raw   = min(base_delay * 2^(attempt-1), max_delay)
        sleep = uniform(0, raw)  if jitter else raw

    *attempt* is 1-based: the delay before the *second* attempt is
    ``base_delay * 2^0 = base_delay``.
    """
    raw = min(config.base_delay * (2 ** (attempt - 1)), config.max_delay)
    return random.uniform(0, raw) if config.jitter else raw


async def retry_async(
    fn: Callable[[], Coroutine[Any, Any, T]],
    config: RetryConfig = RetryConfig(),
) -> T:
    """
    Call the zero-argument async callable *fn*, retrying on ``TinkerError``.

    This is the core retry loop used by both ``with_retry`` and direct callers.

    Parameters
    ----------
    fn :
        A zero-argument coroutine factory: ``lambda: some_async_call(args...)``.
    config :
        ``RetryConfig`` instance controlling the retry policy.

    Returns
    -------
    The return value of *fn* on the first successful call.

    Raises
    ------
    TinkerError
        The last exception raised after all attempts are exhausted.
    Exception
        Any non-``TinkerError`` exception propagates immediately (no retry).
    """
    import time as _time

    last_exc: TinkerError | None = None
    started_at = _time.monotonic()

    for attempt in range(1, config.max_attempts + 1):
        # Wall-clock cap: stop retrying if total elapsed time exceeded
        if config.max_total_seconds is not None:
            elapsed = _time.monotonic() - started_at
            if elapsed >= config.max_total_seconds:
                log.warning(
                    "retry: wall-clock cap of %.1fs exceeded after attempt %d "
                    "(elapsed=%.1fs) — aborting retries for %s",
                    config.max_total_seconds,
                    attempt - 1,
                    elapsed,
                    type(last_exc).__name__ if last_exc else "unknown",
                )
                break

        try:
            return await fn()

        except TinkerError as exc:
            last_exc = exc

            # Non-retryable errors propagate immediately
            if config.only_if_retryable and not exc.retryable:
                log.debug(
                    "retry: %s is not retryable — propagating immediately",
                    type(exc).__name__,
                )
                raise

            # Non-idempotent operations are unsafe to retry after the first
            # failure: the operation may have partially committed side effects
            # (e.g. a row was written before the connection dropped).  Allow
            # exactly one retry attempt (attempt == 1 means the first call
            # just failed) so transient blips at the TCP level are tolerated,
            # but cap further retries to avoid double-processing.
            if not config.idempotent and attempt > 1:
                log.warning(
                    "retry: aborting further retries for non-idempotent operation "
                    "after attempt %d — partial side effects may exist: %s",
                    attempt,
                    type(exc).__name__,
                )
                break

            if attempt >= config.max_attempts:
                # All attempts exhausted
                log.warning(
                    "retry: all %d attempts exhausted for %s: %s",
                    config.max_attempts,
                    type(exc).__name__,
                    exc,
                )
                break

            delay = _compute_delay(attempt, config)
            # Clamp sleep to remaining wall-clock budget if cap is active
            if config.max_total_seconds is not None:
                remaining = config.max_total_seconds - (_time.monotonic() - started_at)
                delay = min(delay, max(0.0, remaining))

            log.warning(
                "retry: attempt %d/%d failed (%s: %s) — sleeping %.2fs before retry",
                attempt,
                config.max_attempts,
                type(exc).__name__,
                exc,
                delay,
            )
            await asyncio.sleep(delay)

    if config.reraise_after_exhaustion and last_exc is not None:
        raise last_exc
    return None  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------


def with_retry(config: RetryConfig = RetryConfig()):
    """
    Decorator: wrap an async function with the retry policy described by
    *config*.

    The decorated function has the same signature as the original and is
    fully transparent to callers — they do not need to know retries happen.

    Example::

        @with_retry(RetryConfig(max_attempts=4, base_delay=1.0))
        async def call_ollama(prompt: str) -> str:
            ...

    The decorator preserves ``__name__``, ``__doc__``, and all other
    ``functools.wraps`` attributes.

    Parameters
    ----------
    config : RetryConfig
        Retry policy.  Defaults to ``RetryConfig()`` (3 attempts, 1 s base,
        60 s max, with jitter, only retrying retryable errors).
    """

    def decorator(
        fn: Callable[..., Coroutine[Any, Any, T]],
    ) -> Callable[..., Coroutine[Any, Any, T]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            return await retry_async(lambda: fn(*args, **kwargs), config)

        # Attach config to the wrapper so callers can inspect it in tests
        wrapper._retry_config = config  # type: ignore[attr-defined]
        return wrapper

    return decorator
