"""
Tests for resilience/circuit_breaker.py
========================================

Covers the full state machine (CLOSED → OPEN → HALF_OPEN → CLOSED),
fast-fail behaviour, recovery probes, and the registry helpers.
"""
from __future__ import annotations

import asyncio
import pytest

from resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitBreakerRegistry,
    CircuitState,
    build_default_registry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _ok() -> str:
    """A call that always succeeds."""
    return "ok"


async def _fail() -> None:
    """A call that always raises."""
    raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# CircuitBreaker state machine
# ---------------------------------------------------------------------------

class TestCircuitBreakerStateMachine:
    """Unit tests for CircuitBreaker transitions."""

    @pytest.mark.asyncio
    async def test_starts_closed(self):
        breaker = CircuitBreaker(name="test", failure_threshold=3)
        assert breaker.state == CircuitState.CLOSED
        assert breaker.is_closed
        assert not breaker.is_open

    @pytest.mark.asyncio
    async def test_success_stays_closed(self):
        breaker = CircuitBreaker(name="test", failure_threshold=3)
        result = await breaker.call(_ok)
        assert result == "ok"
        assert breaker.is_closed

    @pytest.mark.asyncio
    async def test_opens_after_threshold_failures(self):
        breaker = CircuitBreaker(name="test", failure_threshold=3, recovery_timeout=99)
        for _ in range(3):
            with pytest.raises(RuntimeError):
                await breaker.call(_fail)
        assert breaker.is_open
        assert breaker.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_fast_fail_when_open(self):
        breaker = CircuitBreaker(name="test", failure_threshold=1, recovery_timeout=99)
        with pytest.raises(RuntimeError):
            await breaker.call(_fail)
        assert breaker.is_open

        # Now the next call should fast-fail (CircuitBreakerOpenError), NOT call _fail
        with pytest.raises(CircuitBreakerOpenError) as exc_info:
            await breaker.call(_fail)
        assert "OPEN" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_half_open_after_recovery_timeout(self):
        breaker = CircuitBreaker(name="test", failure_threshold=1, recovery_timeout=0.01)
        with pytest.raises(RuntimeError):
            await breaker.call(_fail)
        assert breaker.is_open

        await asyncio.sleep(0.05)   # wait for recovery timeout
        # Trigger state check — attempt a call to drive the transition
        result = await breaker.call(_ok)
        assert result == "ok"
        assert breaker.is_closed   # closed again after successful probe

    @pytest.mark.asyncio
    async def test_reopens_if_probe_fails(self):
        breaker = CircuitBreaker(name="test", failure_threshold=1, recovery_timeout=0.01)
        with pytest.raises(RuntimeError):
            await breaker.call(_fail)
        await asyncio.sleep(0.05)
        # Probe fails → breaker should reopen
        with pytest.raises(RuntimeError):
            await breaker.call(_fail)
        assert breaker.is_open

    @pytest.mark.asyncio
    async def test_success_resets_failure_count(self):
        breaker = CircuitBreaker(name="test", failure_threshold=3)
        with pytest.raises(RuntimeError):
            await breaker.call(_fail)
        assert breaker.failure_count == 1
        await breaker.call(_ok)
        assert breaker.failure_count == 0

    @pytest.mark.asyncio
    async def test_state_change_callback_called(self):
        transitions = []
        def on_change(b, old, new):
            transitions.append((old, new))

        breaker = CircuitBreaker(name="test", failure_threshold=2, recovery_timeout=0.01,
                                 on_state_change=on_change)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await breaker.call(_fail)
        # Should have fired CLOSED → OPEN
        assert (CircuitState.CLOSED, CircuitState.OPEN) in transitions

    @pytest.mark.asyncio
    async def test_protect_decorator(self):
        breaker = CircuitBreaker(name="test", failure_threshold=2)

        @breaker.protect
        async def my_service():
            return "protected"

        result = await my_service()
        assert result == "protected"

    @pytest.mark.asyncio
    async def test_stats_contains_expected_fields(self):
        breaker = CircuitBreaker(name="my_service", failure_threshold=3)
        await breaker.call(_ok)
        stats = breaker.stats()
        assert stats["name"] == "my_service"
        assert stats["state"] == "closed"
        assert stats["total_calls"] == 1
        assert stats["total_failures"] == 0


# ---------------------------------------------------------------------------
# CircuitBreakerRegistry
# ---------------------------------------------------------------------------

class TestCircuitBreakerRegistry:
    def test_register_and_get(self):
        registry = CircuitBreakerRegistry()
        b = registry.register("svc_a", failure_threshold=3)
        assert registry.get("svc_a") is b

    def test_duplicate_register_raises(self):
        registry = CircuitBreakerRegistry()
        registry.register("svc_a")
        with pytest.raises(ValueError, match="already registered"):
            registry.register("svc_a")

    def test_get_unknown_raises(self):
        registry = CircuitBreakerRegistry()
        with pytest.raises(KeyError):
            registry.get("unknown")

    def test_get_or_default_returns_none_for_unknown(self):
        registry = CircuitBreakerRegistry()
        assert registry.get_or_default("nope") is None

    @pytest.mark.asyncio
    async def test_any_open(self):
        registry = CircuitBreakerRegistry()
        b = registry.register("svc", failure_threshold=1, recovery_timeout=99)
        assert not registry.any_open()
        with pytest.raises(RuntimeError):
            await b.call(_fail)
        assert registry.any_open()

    def test_all_stats(self):
        registry = CircuitBreakerRegistry()
        registry.register("a")
        registry.register("b")
        stats = registry.all_stats()
        assert set(stats.keys()) == {"a", "b"}


# ---------------------------------------------------------------------------
# build_default_registry
# ---------------------------------------------------------------------------

class TestBuildDefaultRegistry:
    def test_creates_expected_breakers(self):
        registry = build_default_registry()
        for name in ("ollama_server", "ollama_secondary", "redis", "searxng", "chromadb"):
            b = registry.get(name)
            assert b is not None
            assert b.is_closed
