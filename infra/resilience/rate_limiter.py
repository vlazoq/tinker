"""
infra/resilience/rate_limiter.py
===========================

Token-bucket rate limiter for Tinker's AI and tool calls.

Why rate limiting?
------------------
Without rate limits, a runaway Tinker loop could:
  - Make thousands of requests to Ollama per hour (monopolising the GPU)
  - Exhaust SearXNG's search quota with unbounded tool calls
  - Generate surprising bills if connected to a paid LLM API ($1000+/day)
  - Trigger Ollama's own rate limiting, causing cascading failures

The token bucket algorithm
--------------------------
Each limiter has a "bucket" of tokens.  Tokens refill at a constant rate
(``rate`` tokens per second).  Each API call consumes one or more tokens.
If there are not enough tokens, the call waits until refilled.

Example: ``rate=2, burst=10`` means you can fire 10 calls instantly (burst),
then at most 2 calls/second after that.

This is the same algorithm used by AWS, GCP, and most production rate limiters.

Usage
------
::

    # Create a limiter: max 2 Architect calls/second, burst of 5
    architect_limiter = TokenBucketRateLimiter(name="architect", rate=2.0, burst=5)

    # Wrap an AI call:
    await architect_limiter.acquire()   # blocks if rate exceeded
    result = await architect_agent.call(task, context)

    # Track costs:
    architect_limiter.record_tokens(result.total_tokens)

    # Check spend:
    print(architect_limiter.total_tokens_used)

    # Or as a context manager:
    async with architect_limiter:
        result = await architect_agent.call(...)
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class TokenBucketRateLimiter:
    """
    Async token bucket rate limiter for a single resource (e.g. one AI model).

    Parameters
    ----------
    name  : Human-readable identifier for logging.
    rate  : Tokens added to the bucket per second (steady-state throughput).
    burst : Maximum bucket capacity (allows short bursts above the steady rate).
    cost  : Default token cost per ``acquire()`` call.  Override per-call with
            the ``cost`` parameter of ``acquire()``.

    Example
    -------
    ::

        # Allow 1 architect call every 3 seconds, burst up to 3:
        limiter = TokenBucketRateLimiter("architect", rate=0.33, burst=3)

        await limiter.acquire()
        result = await architect.call(task, ctx)
        limiter.record_tokens(result.total_tokens)
    """

    def __init__(
        self,
        name: str,
        rate: float = 1.0,
        burst: float = 10.0,
        cost: float = 1.0,
    ) -> None:
        self.name = name
        self._rate = rate  # tokens per second
        self._burst = burst  # max bucket size
        self._cost = cost  # default cost per call
        self._tokens: float = burst  # start full
        self._last_refill: float = time.monotonic()
        self._lock = asyncio.Lock()

        # Statistics
        self._total_calls: int = 0
        self._total_wait_seconds: float = 0.0
        self._total_tokens_used: int = 0
        self._calls_throttled: int = 0

    @property
    def total_tokens_used(self) -> int:
        """Total LLM tokens recorded via ``record_tokens()``."""
        return self._total_tokens_used

    @property
    def total_calls(self) -> int:
        """Total number of calls that passed through this limiter."""
        return self._total_calls

    @property
    def calls_throttled(self) -> int:
        """Number of calls that had to wait due to rate limiting."""
        return self._calls_throttled

    async def acquire(self, cost: float | None = None) -> None:
        """
        Acquire permission to make one call.

        If the bucket has enough tokens, returns immediately (no wait).
        Otherwise, sleeps until enough tokens have refilled.

        Parameters
        ----------
        cost : How many tokens this call consumes (default: self._cost = 1.0).

        This method is safe to call concurrently from multiple coroutines.
        """
        effective_cost = cost if cost is not None else self._cost
        total_slept = 0.0

        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= effective_cost:
                    # Enough tokens available — consume and return.
                    self._tokens -= effective_cost
                    self._total_calls += 1
                    self._total_wait_seconds += total_slept
                    return
                # Not enough tokens yet: compute how long to wait for the deficit
                # and release the lock while sleeping so other coroutines can run.
                deficit = effective_cost - self._tokens
                wait_time = deficit / self._rate
                if total_slept == 0.0:
                    # Only count as throttled on the first wait, not on re-checks.
                    self._calls_throttled += 1
                    logger.debug(
                        "RateLimiter '%s': throttling %.2fs (tokens=%.2f, need=%.2f)",
                        self.name,
                        wait_time,
                        self._tokens,
                        effective_cost,
                    )

            # Sleep outside the lock so the bucket can refill and other callers
            # can make progress.  After waking we re-enter the lock to re-check
            # (another coroutine may have consumed tokens during our sleep).
            await asyncio.sleep(wait_time)
            total_slept += wait_time

    def record_tokens(self, token_count: int) -> None:
        """
        Record LLM tokens consumed by a call (for cost tracking).

        This is separate from ``acquire()`` because we don't know the token
        count until after the AI responds.

        Parameters
        ----------
        token_count : Number of LLM tokens used (prompt + completion).
        """
        self._total_tokens_used += token_count

    def stats(self) -> dict:
        """Return current rate limiter statistics for monitoring/alerting."""
        throttle_pct = (
            round(self._calls_throttled / self._total_calls * 100, 1) if self._total_calls else 0.0
        )
        return {
            "name": self.name,
            "rate_per_second": self._rate,
            "burst_capacity": self._burst,
            "current_tokens": round(self._tokens, 2),
            "total_calls": self._total_calls,
            "calls_throttled": self._calls_throttled,
            "throttle_pct": throttle_pct,
            "total_wait_seconds": round(self._total_wait_seconds, 2),
            "total_llm_tokens": self._total_tokens_used,
        }

    def reset_stats(self) -> None:
        """Reset all counters (useful for per-interval reporting or tests)."""
        self._total_calls = 0
        self._total_wait_seconds = 0.0
        self._total_tokens_used = 0
        self._calls_throttled = 0

    async def try_acquire(self, cost: float | None = None) -> tuple[bool, float]:
        """
        Non-blocking attempt to acquire a token.

        Unlike ``acquire()``, this method **never sleeps**.  If the bucket
        does not have enough tokens it returns immediately with
        ``(False, wait_seconds)`` so the caller can return an HTTP 429 (or
        any other rejection) without stalling.

        Use this in HTTP middleware or any context where you want to shed
        load rather than queue it.

        Parameters
        ----------
        cost : Tokens to consume (default: self._cost).

        Returns
        -------
        (acquired, retry_after_seconds)
            acquired          : True  → tokens were consumed, call may proceed.
            retry_after_seconds : 0.0 if acquired; otherwise the number of
                                  seconds the caller should wait before retrying.

        Example
        -------
        ::

            acquired, retry_after = await limiter.try_acquire()
            if not acquired:
                raise TooManyRequestsError(retry_after=retry_after)
        """
        effective_cost = cost if cost is not None else self._cost
        async with self._lock:
            self._refill()
            if self._tokens >= effective_cost:
                self._tokens -= effective_cost
                self._total_calls += 1
                return True, 0.0
            # Bucket empty — compute how long until it would have enough tokens.
            deficit = effective_cost - self._tokens
            wait_time = deficit / self._rate
            self._calls_throttled += 1
            logger.debug(
                "RateLimiter '%s': try_acquire denied (tokens=%.2f, need=%.2f, retry_after=%.2fs)",
                self.name,
                self._tokens,
                effective_cost,
                wait_time,
            )
            return False, wait_time

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *_):
        pass

    # ------------------------------------------------------------------
    # Redis state persistence
    # ------------------------------------------------------------------

    async def save_state(self, redis_client) -> None:
        """Serialise limiter state to Redis hash ``tinker:rl:{name}`` with TTL 3600s."""
        key = f"tinker:rl:{self.name}"
        try:
            mapping = {
                "tokens": str(self._tokens),
                "last_refill": str(self._last_refill),
                "total_calls": str(self._total_calls),
                "total_tokens_used": str(self._total_tokens_used),
                "calls_throttled": str(self._calls_throttled),
            }
            await redis_client.hset(key, mapping=mapping)
            await redis_client.expire(key, 3600)
        except Exception as exc:
            logger.warning("TokenBucketRateLimiter.save_state failed for '%s': %s", self.name, exc)

    async def load_state(self, redis_client) -> None:
        """Restore limiter state from Redis hash ``tinker:rl:{name}``. No-op if key absent."""
        key = f"tinker:rl:{self.name}"
        try:
            data = await redis_client.hgetall(key)
            if not data:
                return
            if b"tokens" in data:
                self._tokens = float(data[b"tokens"])
            if b"last_refill" in data:
                self._last_refill = float(data[b"last_refill"])
            if b"total_calls" in data:
                self._total_calls = int(data[b"total_calls"])
            if b"total_tokens_used" in data:
                self._total_tokens_used = int(data[b"total_tokens_used"])
            if b"calls_throttled" in data:
                self._calls_throttled = int(data[b"calls_throttled"])
        except Exception as exc:
            logger.warning("TokenBucketRateLimiter.load_state failed for '%s': %s", self.name, exc)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _refill(self) -> None:
        """Add tokens based on elapsed time since last refill."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._last_refill = now


class RateLimiterRegistry:
    """
    Registry of rate limiters for all Tinker AI and tool calls.

    Provides a single place to define, retrieve, and inspect all rate limits.

    Usage
    -----
    ::

        registry = RateLimiterRegistry()
        registry.register("architect",  rate=0.5, burst=3)
        registry.register("critic",     rate=1.0, burst=5)
        registry.register("searxng",    rate=0.2, burst=2)

        # In the micro loop:
        await registry.get("architect").acquire()
        result = await architect.call(...)
        registry.get("architect").record_tokens(result.total_tokens)
    """

    def __init__(self) -> None:
        self._limiters: dict[str, TokenBucketRateLimiter] = {}

    def register(
        self,
        name: str,
        rate: float = 1.0,
        burst: float = 10.0,
        cost: float = 1.0,
    ) -> TokenBucketRateLimiter:
        """Create and register a rate limiter."""
        limiter = TokenBucketRateLimiter(name=name, rate=rate, burst=burst, cost=cost)
        self._limiters[name] = limiter
        logger.info(
            "Registered rate limiter '%s' (rate=%.2f/s, burst=%.0f)",
            name,
            rate,
            burst,
        )
        return limiter

    def get(self, name: str) -> TokenBucketRateLimiter | None:
        """Return a registered limiter, or None if not found."""
        return self._limiters.get(name)

    def all_stats(self) -> dict[str, dict]:
        """Snapshot of all rate limiter stats — for metrics/dashboards."""
        return {name: lim.stats() for name, lim in self._limiters.items()}

    def total_llm_tokens(self) -> int:
        """Total LLM tokens consumed across all limiters (cost tracking)."""
        return sum(lim.total_tokens_used for lim in self._limiters.values())

    async def save_all(self, redis_client) -> None:
        """Persist state of every registered limiter to Redis."""
        for limiter in self._limiters.values():
            await limiter.save_state(redis_client)

    async def load_all(self, redis_client) -> None:
        """Restore state of every registered limiter from Redis."""
        for limiter in self._limiters.values():
            await limiter.load_state(redis_client)


def build_default_rate_limiters() -> RateLimiterRegistry:
    """
    Create the default Tinker rate limiter registry with sensible limits.

    Limits are conservative defaults that prevent runaway costs while
    allowing normal throughput.  Override via environment variables or
    by adjusting the OrchestratorConfig.

    Rate summary:
      - architect  : 1 call/3s steady, burst 3  (slow, expensive reasoning)
      - critic     : 1 call/2s steady, burst 5  (lighter than architect)
      - synthesizer: 1 call/5s steady, burst 2  (rare but expensive)
      - researcher : 1 call/2s steady, burst 3  (web searches)

    Returns
    -------
    RateLimiterRegistry configured for all standard Tinker components.
    """
    registry = RateLimiterRegistry()

    # Architect: slowest, most expensive — limit to 1 call every 3 seconds
    registry.register("architect", rate=0.33, burst=3)

    # Critic: lighter than architect, higher throughput
    registry.register("critic", rate=0.5, burst=5)

    # Synthesizer: called rarely (meso/macro), but expensive
    registry.register("synthesizer", rate=0.2, burst=2)

    # Researcher (web search): limit to avoid hammering SearXNG
    registry.register("researcher", rate=0.5, burst=3)

    return registry
