"""
agents/fritz/retry.py
───────────────
Shared retry + rate-limit logic for GitHub and Gitea HTTP calls.

Retry policy
────────────
  Transient failures (network, 5xx, 429) are retried with exponential backoff.
  Client errors (4xx except 429) are NOT retried — they indicate a programming
  error or missing resource and retrying would just waste rate-limit quota.

  Default: up to 3 attempts, backoff 1 s / 2 s (jittered ±20 %).

GitHub rate limiting
────────────────────
  Every response from GitHub includes:
    X-RateLimit-Remaining   — requests left in the current window
    X-RateLimit-Reset       — Unix timestamp when the window resets
    X-RateLimit-Used        — requests used so far

  When Remaining < RATE_LIMIT_WARN_THRESHOLD we log a warning.
  When the API returns 403/429 with Remaining == 0 we sleep until Reset,
  then retry the request (counts as one of the max_attempts).

  Gitea also supports the same headers (added in Gitea 1.18), so the same
  logic works for both platforms.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Warn when fewer than this many API calls remain in the rate-limit window.
RATE_LIMIT_WARN_THRESHOLD = 100

# HTTP status codes that are safe to retry.
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

# HTTP status codes that represent client errors — never retry these.
_CLIENT_ERROR_STATUSES = {400, 401, 403, 404, 405, 409, 410, 422}


class RateLimitState:
    """
    Tracks the most recent rate-limit headers seen for one platform.
    Thread-safe reads (all attributes are plain Python int/float).
    """

    __slots__ = ("limit", "remaining", "reset_at", "used")

    def __init__(self) -> None:
        self.limit: int = 0
        self.remaining: int = -1  # -1 = unknown
        self.reset_at: float = 0.0  # Unix timestamp
        self.used: int = 0

    def update(self, response: httpx.Response) -> None:
        """Parse rate-limit headers from an httpx Response."""
        h = response.headers
        try:
            self.limit = int(h.get("x-ratelimit-limit", self.limit))
            self.remaining = int(h.get("x-ratelimit-remaining", self.remaining))
            self.reset_at = float(h.get("x-ratelimit-reset", self.reset_at))
            self.used = int(h.get("x-ratelimit-used", self.used))
        except (ValueError, TypeError):
            pass

        if self.remaining != -1 and self.remaining < RATE_LIMIT_WARN_THRESHOLD:
            logger.warning(
                "API rate limit low: %d/%d remaining, resets at %s",
                self.remaining,
                self.limit,
                _fmt_reset(self.reset_at),
            )

    def seconds_until_reset(self) -> float:
        """Seconds until the rate-limit window resets (0 if already past)."""
        return max(0.0, self.reset_at - time.time())

    def is_exhausted(self) -> bool:
        return self.remaining == 0

    def __str__(self) -> str:
        if self.remaining == -1:
            return "rate_limit=unknown"
        return (
            f"rate_limit={self.remaining}/{self.limit} resets_in={self.seconds_until_reset():.0f}s"
        )


async def with_retry(
    fn: Callable[[], Coroutine[Any, Any, httpx.Response]],
    *,
    rate_state: RateLimitState | None = None,
    max_attempts: int = 3,
    backoff_base: float = 1.0,
    operation: str = "request",
) -> httpx.Response:
    """
    Execute an async HTTP call with retry + rate-limit handling.

    Args:
        fn:           Zero-argument async callable that returns an httpx.Response.
        rate_state:   Optional RateLimitState to update from response headers.
        max_attempts: Maximum number of total attempts (default 3).
        backoff_base: Base sleep in seconds for exponential backoff (default 1 s).
        operation:    Name used in log messages.

    Returns:
        The final httpx.Response (may be an error response if all retries failed).

    Raises:
        httpx.HTTPStatusError: Only if the status is a non-retryable client error
                               AND raise_for_status() would raise.
        Exception:             Any non-HTTP exception on the last attempt.
    """
    last_exc: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            response = await fn()
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            last_exc = exc
            if attempt == max_attempts:
                logger.error("%s: network error after %d attempts: %s", operation, attempt, exc)
                raise
            wait = _jitter(backoff_base * (2 ** (attempt - 1)))
            logger.warning(
                "%s: network error (attempt %d/%d), retrying in %.1fs: %s",
                operation,
                attempt,
                max_attempts,
                wait,
                exc,
            )
            await asyncio.sleep(wait)
            continue

        # Update rate-limit state from headers (even on error responses).
        if rate_state is not None:
            rate_state.update(response)

        status = response.status_code

        # ── Rate limit exhausted: sleep until reset then retry ─────────────
        if status in (403, 429) and rate_state is not None and rate_state.is_exhausted():
            sleep_secs = rate_state.seconds_until_reset() + 1.0  # +1s buffer
            if attempt < max_attempts:
                logger.warning(
                    "%s: rate limit exhausted, sleeping %.0fs until reset (attempt %d/%d)",
                    operation,
                    sleep_secs,
                    attempt,
                    max_attempts,
                )
                await asyncio.sleep(sleep_secs)
                continue
            logger.error("%s: rate limit exhausted and no retries left.", operation)
            return response

        # ── 429 with Retry-After (not rate-limit related) ──────────────────
        if status == 429 and attempt < max_attempts:
            retry_after = _parse_retry_after(response)
            wait = retry_after if retry_after else _jitter(backoff_base * (2 ** (attempt - 1)))
            logger.warning(
                "%s: 429 Too Many Requests (attempt %d/%d), retrying in %.1fs",
                operation,
                attempt,
                max_attempts,
                wait,
            )
            await asyncio.sleep(wait)
            continue

        # ── Transient server errors ────────────────────────────────────────
        if status in _RETRYABLE_STATUSES and attempt < max_attempts:
            wait = _jitter(backoff_base * (2 ** (attempt - 1)))
            logger.warning(
                "%s: HTTP %d (attempt %d/%d), retrying in %.1fs",
                operation,
                status,
                attempt,
                max_attempts,
                wait,
            )
            await asyncio.sleep(wait)
            continue

        # ── Non-retryable client error ─────────────────────────────────────
        if status in _CLIENT_ERROR_STATUSES:
            logger.debug("%s: HTTP %d — not retrying client error", operation, status)
            return response

        # ── Success or unrecognised status ─────────────────────────────────
        return response

    # Exhausted all retries — return the last response if we have one.
    # If we only got exceptions, re-raise the last one.
    if last_exc is not None:
        raise last_exc
    # Should not reach here, but satisfy the type checker.
    raise RuntimeError(f"{operation}: exhausted {max_attempts} attempts")


# ── Private helpers ───────────────────────────────────────────────────────────


def _jitter(seconds: float, jitter: float = 0.2) -> float:
    """Add ±jitter% random noise to a sleep duration to avoid thundering herd."""
    return seconds * (1 + random.uniform(-jitter, jitter))


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Parse the Retry-After header (seconds or HTTP-date)."""
    header = response.headers.get("retry-after")
    if not header:
        return None
    try:
        return float(header)
    except ValueError:
        pass
    # HTTP-date format — try to parse it
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(header)
        return max(0.0, dt.timestamp() - time.time())
    except Exception:
        return None


def _fmt_reset(ts: float) -> str:
    if ts == 0.0:
        return "unknown"
    import datetime

    return datetime.datetime.fromtimestamp(ts).isoformat(timespec="seconds")
