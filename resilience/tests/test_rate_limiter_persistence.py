"""
resilience/tests/test_rate_limiter_persistence.py
==================================================
Tests for the Redis save_state / load_state methods on
TokenBucketRateLimiter and RateLimiterRegistry.

All Redis interactions are handled by FakeRedis — no real Redis required.
"""

from __future__ import annotations

import pytest

from resilience.rate_limiter import TokenBucketRateLimiter, RateLimiterRegistry


# ---------------------------------------------------------------------------
# Fake Redis
# ---------------------------------------------------------------------------


class FakeRedis:
    """In-memory Redis stub that stores hashes as dicts of byte strings."""

    def __init__(self):
        self._data = {}

    async def hset(self, key, mapping):
        self._data[key] = {k: str(v) for k, v in mapping.items()}

    async def hgetall(self, key):
        raw = self._data.get(key, {})
        # Return bytes keys/values to match real Redis behaviour
        return {k.encode(): v.encode() for k, v in raw.items()}

    async def expire(self, key, ttl):
        pass  # no-op


class ErrorRedis:
    """Redis stub that raises on every call."""

    async def hset(self, key, mapping):
        raise RuntimeError("Redis unavailable")

    async def hgetall(self, key):
        raise RuntimeError("Redis unavailable")

    async def expire(self, key, ttl):
        raise RuntimeError("Redis unavailable")


# ---------------------------------------------------------------------------
# TokenBucketRateLimiter — save_state
# ---------------------------------------------------------------------------


class TestSaveState:
    @pytest.mark.asyncio
    async def test_save_stores_tokens_and_calls(self):
        limiter = TokenBucketRateLimiter(name="arch", rate=2.0, burst=10.0)
        limiter._tokens = 7.5
        limiter._total_calls = 42
        limiter._total_tokens_used = 1000
        limiter._calls_throttled = 3

        redis = FakeRedis()
        await limiter.save_state(redis)

        stored = redis._data["tinker:rl:arch"]
        assert float(stored["tokens"]) == pytest.approx(7.5)
        assert int(stored["total_calls"]) == 42
        assert int(stored["total_tokens_used"]) == 1000
        assert int(stored["calls_throttled"]) == 3

    @pytest.mark.asyncio
    async def test_save_sets_ttl(self):
        """save_state must call expire() — verified indirectly by no error."""
        limiter = TokenBucketRateLimiter(name="test", rate=1.0, burst=5.0)
        redis = FakeRedis()
        await limiter.save_state(redis)  # should not raise

    @pytest.mark.asyncio
    async def test_save_with_redis_error_is_nonfatal(self):
        """Redis errors during save_state must not propagate."""
        limiter = TokenBucketRateLimiter(name="bad", rate=1.0, burst=5.0)
        await limiter.save_state(ErrorRedis())  # must not raise


# ---------------------------------------------------------------------------
# TokenBucketRateLimiter — load_state
# ---------------------------------------------------------------------------


class TestLoadState:
    @pytest.mark.asyncio
    async def test_load_restores_tokens_and_calls(self):
        limiter = TokenBucketRateLimiter(name="arch", rate=2.0, burst=10.0)
        redis = FakeRedis()
        # Pre-populate the Redis hash
        await redis.hset(
            "tinker:rl:arch",
            {
                "tokens": "3.25",
                "last_refill": "123456.78",
                "total_calls": "99",
                "total_tokens_used": "5000",
                "calls_throttled": "7",
            },
        )

        await limiter.load_state(redis)

        assert limiter._tokens == pytest.approx(3.25)
        assert limiter._last_refill == pytest.approx(123456.78)
        assert limiter._total_calls == 99
        assert limiter._total_tokens_used == 5000
        assert limiter._calls_throttled == 7

    @pytest.mark.asyncio
    async def test_load_empty_redis_does_nothing(self):
        """load_state with an empty Redis key must not crash or mutate state."""
        limiter = TokenBucketRateLimiter(name="new", rate=1.0, burst=5.0)
        original_tokens = limiter._tokens  # should be burst (5.0)

        redis = FakeRedis()  # empty
        await limiter.load_state(redis)

        assert limiter._tokens == pytest.approx(original_tokens)
        assert limiter._total_calls == 0

    @pytest.mark.asyncio
    async def test_load_with_redis_error_is_nonfatal(self):
        """Redis errors during load_state must not propagate."""
        limiter = TokenBucketRateLimiter(name="bad", rate=1.0, burst=5.0)
        await limiter.load_state(ErrorRedis())  # must not raise

    @pytest.mark.asyncio
    async def test_save_then_load_roundtrip(self):
        """save_state followed by load_state restores all fields."""
        limiter = TokenBucketRateLimiter(name="roundtrip", rate=1.0, burst=20.0)
        limiter._tokens = 12.5
        limiter._total_calls = 55
        limiter._total_tokens_used = 800
        limiter._calls_throttled = 2

        redis = FakeRedis()
        await limiter.save_state(redis)

        # New limiter instance — starts fresh
        limiter2 = TokenBucketRateLimiter(name="roundtrip", rate=1.0, burst=20.0)
        await limiter2.load_state(redis)

        assert limiter2._tokens == pytest.approx(12.5)
        assert limiter2._total_calls == 55
        assert limiter2._total_tokens_used == 800
        assert limiter2._calls_throttled == 2


# ---------------------------------------------------------------------------
# RateLimiterRegistry — save_all / load_all
# ---------------------------------------------------------------------------


class TestRegistrySaveLoadAll:
    @pytest.mark.asyncio
    async def test_save_all_calls_each_limiter(self):
        registry = RateLimiterRegistry()
        registry.register("a", rate=1.0, burst=5.0)
        registry.register("b", rate=2.0, burst=10.0)

        redis = FakeRedis()
        await registry.save_all(redis)

        assert "tinker:rl:a" in redis._data
        assert "tinker:rl:b" in redis._data

    @pytest.mark.asyncio
    async def test_load_all_calls_each_limiter(self):
        registry = RateLimiterRegistry()
        la = registry.register("x", rate=1.0, burst=5.0)
        lb = registry.register("y", rate=1.0, burst=5.0)

        redis = FakeRedis()
        # Pre-load values
        await redis.hset("tinker:rl:x", {"tokens": "2.0", "total_calls": "10",
                                          "total_tokens_used": "0", "calls_throttled": "0",
                                          "last_refill": "0"})
        await redis.hset("tinker:rl:y", {"tokens": "4.0", "total_calls": "20",
                                          "total_tokens_used": "0", "calls_throttled": "0",
                                          "last_refill": "0"})

        await registry.load_all(redis)

        assert la._tokens == pytest.approx(2.0)
        assert la._total_calls == 10
        assert lb._tokens == pytest.approx(4.0)
        assert lb._total_calls == 20

    @pytest.mark.asyncio
    async def test_save_all_empty_registry(self):
        """Empty registry — save_all must not raise."""
        registry = RateLimiterRegistry()
        await registry.save_all(FakeRedis())

    @pytest.mark.asyncio
    async def test_load_all_empty_registry(self):
        """Empty registry — load_all must not raise."""
        registry = RateLimiterRegistry()
        await registry.load_all(FakeRedis())
