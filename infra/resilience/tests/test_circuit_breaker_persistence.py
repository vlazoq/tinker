"""
infra/resilience/tests/test_circuit_breaker_persistence.py
=====================================================
Tests for the Redis save_state / load_state methods on
CircuitBreaker and CircuitBreakerRegistry.

All Redis interactions are handled by FakeRedis — no real Redis required.
"""

from __future__ import annotations

import pytest

from infra.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerRegistry,
    CircuitState,
)

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
# CircuitBreaker — save_state
# ---------------------------------------------------------------------------


class TestCircuitBreakerSaveState:
    @pytest.mark.asyncio
    async def test_save_stores_state_and_failure_count(self):
        breaker = CircuitBreaker(name="redis", failure_threshold=5)
        breaker._failure_count = 3
        breaker._state = CircuitState.CLOSED

        redis = FakeRedis()
        await breaker.save_state(redis)

        stored = redis._data["tinker:cb:redis"]
        assert stored["state"] == "closed"
        assert int(stored["failure_count"]) == 3

    @pytest.mark.asyncio
    async def test_save_stores_open_state(self):
        breaker = CircuitBreaker(name="ollama", failure_threshold=5)
        breaker._state = CircuitState.OPEN
        breaker._failure_count = 5

        redis = FakeRedis()
        await breaker.save_state(redis)

        stored = redis._data["tinker:cb:ollama"]
        assert stored["state"] == "open"
        assert int(stored["failure_count"]) == 5

    @pytest.mark.asyncio
    async def test_save_with_redis_error_is_nonfatal(self):
        """Redis errors during save_state must not propagate."""
        breaker = CircuitBreaker(name="bad", failure_threshold=3)
        await breaker.save_state(ErrorRedis())  # must not raise


# ---------------------------------------------------------------------------
# CircuitBreaker — load_state
# ---------------------------------------------------------------------------


class TestCircuitBreakerLoadState:
    @pytest.mark.asyncio
    async def test_load_restores_open_state_and_failure_count(self):
        breaker = CircuitBreaker(name="redis", failure_threshold=5)
        redis = FakeRedis()
        await redis.hset(
            "tinker:cb:redis",
            {
                "state": "open",
                "failure_count": "5",
                "success_count": "0",
                "open_at": "9999.0",
                "total_calls": "100",
                "total_failures": "5",
                "total_short_circuits": "10",
            },
        )

        await breaker.load_state(redis)

        assert breaker._state == CircuitState.OPEN
        assert breaker._failure_count == 5
        assert breaker._open_at == pytest.approx(9999.0)
        assert breaker._total_calls == 100
        assert breaker._total_failures == 5
        assert breaker._total_short_circuits == 10

    @pytest.mark.asyncio
    async def test_load_restores_closed_state(self):
        breaker = CircuitBreaker(name="searxng", failure_threshold=3)
        redis = FakeRedis()
        await redis.hset(
            "tinker:cb:searxng",
            {
                "state": "closed",
                "failure_count": "0",
                "success_count": "0",
                "open_at": "0.0",
                "total_calls": "50",
                "total_failures": "2",
                "total_short_circuits": "0",
            },
        )

        await breaker.load_state(redis)

        assert breaker._state == CircuitState.CLOSED
        assert breaker._failure_count == 0

    @pytest.mark.asyncio
    async def test_load_restores_half_open_state(self):
        breaker = CircuitBreaker(name="chroma", failure_threshold=3)
        redis = FakeRedis()
        await redis.hset(
            "tinker:cb:chroma",
            {
                "state": "half_open",
                "failure_count": "3",
                "success_count": "0",
                "open_at": "100.0",
                "total_calls": "10",
                "total_failures": "3",
                "total_short_circuits": "2",
            },
        )

        await breaker.load_state(redis)

        assert breaker._state == CircuitState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_load_empty_redis_does_nothing(self):
        """load_state with an empty Redis key must not crash or mutate state."""
        breaker = CircuitBreaker(name="new", failure_threshold=5)
        original_state = breaker._state
        original_failure_count = breaker._failure_count

        redis = FakeRedis()  # empty
        await breaker.load_state(redis)

        assert breaker._state == original_state
        assert breaker._failure_count == original_failure_count

    @pytest.mark.asyncio
    async def test_load_with_redis_error_is_nonfatal(self):
        """Redis errors during load_state must not propagate."""
        breaker = CircuitBreaker(name="bad", failure_threshold=3)
        await breaker.load_state(ErrorRedis())  # must not raise

    @pytest.mark.asyncio
    async def test_save_then_load_roundtrip(self):
        """save_state followed by load_state restores all fields."""
        breaker = CircuitBreaker(name="roundtrip", failure_threshold=5)
        breaker._state = CircuitState.OPEN
        breaker._failure_count = 5
        breaker._open_at = 54321.0
        breaker._total_calls = 200
        breaker._total_failures = 5
        breaker._total_short_circuits = 15

        redis = FakeRedis()
        await breaker.save_state(redis)

        breaker2 = CircuitBreaker(name="roundtrip", failure_threshold=5)
        await breaker2.load_state(redis)

        assert breaker2._state == CircuitState.OPEN
        assert breaker2._failure_count == 5
        assert breaker2._open_at == pytest.approx(54321.0)
        assert breaker2._total_calls == 200
        assert breaker2._total_failures == 5
        assert breaker2._total_short_circuits == 15


# ---------------------------------------------------------------------------
# CircuitBreakerRegistry — save_all / load_all
# ---------------------------------------------------------------------------


class TestCircuitBreakerRegistrySaveLoadAll:
    @pytest.mark.asyncio
    async def test_save_all_persists_all_breakers(self):
        registry = CircuitBreakerRegistry()
        registry.register("a", failure_threshold=3)
        registry.register("b", failure_threshold=5)

        redis = FakeRedis()
        await registry.save_all(redis)

        assert "tinker:cb:a" in redis._data
        assert "tinker:cb:b" in redis._data

    @pytest.mark.asyncio
    async def test_load_all_restores_all_breakers(self):
        registry = CircuitBreakerRegistry()
        ba = registry.register("x", failure_threshold=3)
        bb = registry.register("y", failure_threshold=5)

        redis = FakeRedis()
        await redis.hset(
            "tinker:cb:x",
            {
                "state": "open",
                "failure_count": "3",
                "success_count": "0",
                "open_at": "0.0",
                "total_calls": "10",
                "total_failures": "3",
                "total_short_circuits": "0",
            },
        )
        await redis.hset(
            "tinker:cb:y",
            {
                "state": "closed",
                "failure_count": "1",
                "success_count": "0",
                "open_at": "0.0",
                "total_calls": "20",
                "total_failures": "1",
                "total_short_circuits": "0",
            },
        )

        await registry.load_all(redis)

        assert ba._state == CircuitState.OPEN
        assert ba._failure_count == 3
        assert bb._state == CircuitState.CLOSED
        assert bb._failure_count == 1

    @pytest.mark.asyncio
    async def test_save_all_empty_registry(self):
        """Empty registry — save_all must not raise."""
        registry = CircuitBreakerRegistry()
        await registry.save_all(FakeRedis())

    @pytest.mark.asyncio
    async def test_load_all_empty_registry(self):
        """Empty registry — load_all must not raise."""
        registry = CircuitBreakerRegistry()
        await registry.load_all(FakeRedis())
