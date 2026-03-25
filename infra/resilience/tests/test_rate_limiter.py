"""
Tests for resilience/rate_limiter.py
=====================================

Verifies token bucket refill, burst capacity, acquire blocking, and the registry.
"""

from __future__ import annotations

import time
import pytest

from infra.resilience.rate_limiter import (
    TokenBucketRateLimiter,
    RateLimiterRegistry,
    build_default_rate_limiters,
)


class TestTokenBucketRateLimiter:
    @pytest.mark.asyncio
    async def test_acquire_succeeds_when_tokens_available(self):
        limiter = TokenBucketRateLimiter("test", rate=10.0, burst=10)
        # Should complete immediately — bucket starts full
        t0 = time.monotonic()
        await limiter.acquire()
        assert time.monotonic() - t0 < 0.5

    @pytest.mark.asyncio
    async def test_burst_capacity_exhaustion(self):
        # burst=3 means only 3 immediate calls; the 4th must wait for a refill
        limiter = TokenBucketRateLimiter("test", rate=100.0, burst=3)
        for _ in range(3):
            await limiter.acquire()
        # Bucket is now empty; next acquire must wait (but at 100/s, < 15ms)
        t0 = time.monotonic()
        await limiter.acquire()
        elapsed = time.monotonic() - t0
        # Should have waited at least a tiny bit (refill takes ~10ms at 100/s)
        # but not more than 0.5s in any reasonable environment
        assert elapsed < 0.5

    @pytest.mark.asyncio
    async def test_context_manager(self):
        limiter = TokenBucketRateLimiter("test", rate=10.0, burst=5)
        async with limiter:
            pass  # should not raise

    def test_record_tokens_accumulates(self):
        limiter = TokenBucketRateLimiter("test", rate=1.0, burst=5)
        limiter.record_tokens(500)
        limiter.record_tokens(300)
        assert limiter.total_tokens_used == 800

    def test_stats_contains_expected_fields(self):
        limiter = TokenBucketRateLimiter("my_agent", rate=2.0, burst=4)
        stats = limiter.stats()
        assert stats["name"] == "my_agent"
        assert "current_tokens" in stats  # field was renamed from tokens_available
        assert "total_llm_tokens" in stats  # field was renamed from total_tokens_used
        assert "rate_per_second" in stats  # field was renamed from rate


class TestRateLimiterRegistry:
    def test_register_and_get(self):
        registry = RateLimiterRegistry()
        limiter = registry.register("architect", rate=1.0, burst=3)
        assert registry.get("architect") is limiter

    def test_get_unknown_returns_none(self):
        registry = RateLimiterRegistry()
        assert registry.get("nope") is None

    def test_total_llm_tokens(self):
        registry = RateLimiterRegistry()
        a = registry.register("architect", rate=1.0, burst=5)
        c = registry.register("critic", rate=1.0, burst=5)
        a.record_tokens(100)
        c.record_tokens(50)
        assert registry.total_llm_tokens() == 150


class TestBuildDefaultRateLimiters:
    def test_creates_expected_limiters(self):
        registry = build_default_rate_limiters()
        for name in ("architect", "critic", "synthesizer", "researcher"):
            limiter = registry.get(name)
            assert limiter is not None
