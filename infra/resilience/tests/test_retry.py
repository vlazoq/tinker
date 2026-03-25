"""
resilience/tests/test_retry.py
================================
Tests for ``resilience/retry.py``.

Coverage
--------
RetryConfig
  - defaults are sensible
  - validation rejects max_attempts < 1
  - validation rejects base_delay < 0
  - validation rejects max_delay < base_delay
  - frozen (immutable)

_compute_delay
  - grows exponentially with attempt number
  - capped at max_delay
  - jitter stays within [0, raw]
  - no-jitter is deterministic

retry_async / with_retry
  - succeeds on first attempt (no retry)
  - retries up to max_attempts on retryable errors
  - propagates immediately on non-retryable errors
  - does NOT retry plain (non-TinkerError) exceptions
  - re-raises last exception after exhaustion
  - total call count equals max_attempts on repeated failure
  - returns correct value when success occurs on attempt N
  - sleep durations are between 0 and expected cap (with jitter)
  - with_retry preserves function name and docstring
  - AGGRESSIVE / CONSERVATIVE / ONCE pre-built configs exist and are valid
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from exceptions import (
    ModelConnectionError,
    ResponseParseError,
    ConfigurationError,
)
from infra.resilience.retry import (
    RetryConfig,
    AGGRESSIVE,
    CONSERVATIVE,
    ONCE,
    _compute_delay,
    retry_async,
    with_retry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _retryable_error():
    return ModelConnectionError("connection refused", context={"url": "http://x"})


def _non_retryable_error():
    return ResponseParseError("bad json")


async def _always_fail_retryable():
    raise _retryable_error()


async def _always_fail_non_retryable():
    raise _non_retryable_error()


async def _succeed():
    return "ok"


# ---------------------------------------------------------------------------
# RetryConfig
# ---------------------------------------------------------------------------


class TestRetryConfig:
    def test_defaults(self):
        cfg = RetryConfig()
        assert cfg.max_attempts == 3
        assert cfg.base_delay == 1.0
        assert cfg.max_delay == 60.0
        assert cfg.jitter is True
        assert cfg.only_if_retryable is True
        assert cfg.reraise_after_exhaustion is True

    def test_max_attempts_less_than_1_raises(self):
        with pytest.raises(ValueError, match="max_attempts"):
            RetryConfig(max_attempts=0)

    def test_base_delay_negative_raises(self):
        with pytest.raises(ValueError, match="base_delay"):
            RetryConfig(base_delay=-0.1)

    def test_max_delay_less_than_base_delay_raises(self):
        with pytest.raises(ValueError, match="max_delay"):
            RetryConfig(base_delay=10.0, max_delay=5.0)

    def test_is_immutable(self):
        cfg = RetryConfig()
        with pytest.raises((TypeError, AttributeError)):
            cfg.max_attempts = 10  # type: ignore

    def test_max_attempts_one_is_valid(self):
        cfg = RetryConfig(max_attempts=1)
        assert cfg.max_attempts == 1

    def test_base_delay_zero_is_valid(self):
        cfg = RetryConfig(base_delay=0.0, max_delay=0.0)
        assert cfg.base_delay == 0.0


# ---------------------------------------------------------------------------
# Pre-built configs
# ---------------------------------------------------------------------------


class TestPrebuiltConfigs:
    def test_aggressive_exists_and_valid(self):
        assert isinstance(AGGRESSIVE, RetryConfig)
        assert AGGRESSIVE.max_attempts == 5
        assert AGGRESSIVE.base_delay < 1.0

    def test_conservative_exists_and_valid(self):
        assert isinstance(CONSERVATIVE, RetryConfig)
        assert CONSERVATIVE.max_attempts == 3
        assert CONSERVATIVE.base_delay >= 1.0

    def test_once_means_one_attempt(self):
        assert ONCE.max_attempts == 1


# ---------------------------------------------------------------------------
# _compute_delay
# ---------------------------------------------------------------------------


class TestComputeDelay:
    def test_grows_exponentially_without_jitter(self):
        cfg = RetryConfig(base_delay=1.0, max_delay=1000.0, jitter=False)
        d1 = _compute_delay(1, cfg)  # 1 * 2^0 = 1
        d2 = _compute_delay(2, cfg)  # 1 * 2^1 = 2
        d3 = _compute_delay(3, cfg)  # 1 * 2^2 = 4
        assert d1 == pytest.approx(1.0)
        assert d2 == pytest.approx(2.0)
        assert d3 == pytest.approx(4.0)

    def test_capped_at_max_delay(self):
        cfg = RetryConfig(base_delay=1.0, max_delay=5.0, jitter=False)
        # attempt 10: 1 * 2^9 = 512, should be capped at 5
        assert _compute_delay(10, cfg) == pytest.approx(5.0)

    def test_jitter_within_bounds(self):
        cfg = RetryConfig(base_delay=1.0, max_delay=100.0, jitter=True)
        for _ in range(100):
            d = _compute_delay(3, cfg)  # raw = 4.0
            assert 0.0 <= d <= 4.0, f"jitter out of range: {d}"

    def test_no_jitter_deterministic(self):
        cfg = RetryConfig(base_delay=2.0, max_delay=100.0, jitter=False)
        results = [_compute_delay(2, cfg) for _ in range(10)]
        assert len(set(results)) == 1  # all identical

    def test_base_delay_zero_gives_zero(self):
        cfg = RetryConfig(base_delay=0.0, max_delay=0.0, jitter=False)
        assert _compute_delay(1, cfg) == 0.0


# ---------------------------------------------------------------------------
# retry_async — success path
# ---------------------------------------------------------------------------


class TestRetryAsyncSuccess:
    @pytest.mark.asyncio
    async def test_returns_value_on_first_attempt(self):
        result = await retry_async(lambda: _succeed(), RetryConfig())
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_call_count_is_one_on_success(self):
        calls = 0

        async def fn():
            nonlocal calls
            calls += 1
            return "ok"

        await retry_async(fn, RetryConfig())
        assert calls == 1

    @pytest.mark.asyncio
    async def test_succeeds_on_second_attempt(self):
        calls = 0

        async def fn():
            nonlocal calls
            calls += 1
            if calls < 2:
                raise ModelConnectionError("transient")
            return "success"

        cfg = RetryConfig(max_attempts=3, base_delay=0.0, jitter=False)
        result = await retry_async(fn, cfg)
        assert result == "success"
        assert calls == 2

    @pytest.mark.asyncio
    async def test_succeeds_on_last_attempt(self):
        calls = 0

        async def fn():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise ModelConnectionError("transient")
            return "final"

        cfg = RetryConfig(max_attempts=3, base_delay=0.0, jitter=False)
        result = await retry_async(fn, cfg)
        assert result == "final"
        assert calls == 3


# ---------------------------------------------------------------------------
# retry_async — failure path
# ---------------------------------------------------------------------------


class TestRetryAsyncFailure:
    @pytest.mark.asyncio
    async def test_retries_max_attempts_times(self):
        calls = 0

        async def fn():
            nonlocal calls
            calls += 1
            raise ModelConnectionError("always fails")

        cfg = RetryConfig(max_attempts=4, base_delay=0.0, jitter=False)
        with pytest.raises(ModelConnectionError):
            await retry_async(fn, cfg)
        assert calls == 4

    @pytest.mark.asyncio
    async def test_raises_last_exception_after_exhaustion(self):
        async def fn():
            raise ModelConnectionError("specific message")

        cfg = RetryConfig(max_attempts=2, base_delay=0.0, jitter=False)
        with pytest.raises(ModelConnectionError, match="specific message"):
            await retry_async(fn, cfg)

    @pytest.mark.asyncio
    async def test_non_retryable_propagates_immediately(self):
        calls = 0

        async def fn():
            nonlocal calls
            calls += 1
            raise ResponseParseError("bad json")

        cfg = RetryConfig(max_attempts=5, base_delay=0.0, jitter=False)
        with pytest.raises(ResponseParseError):
            await retry_async(fn, cfg)
        assert calls == 1, "Non-retryable error should not be retried"

    @pytest.mark.asyncio
    async def test_non_tinker_exception_propagates_immediately(self):
        """Plain Python exceptions (not TinkerError) are never retried."""
        calls = 0

        async def fn():
            nonlocal calls
            calls += 1
            raise RuntimeError("not a TinkerError")

        cfg = RetryConfig(max_attempts=5, base_delay=0.0, jitter=False)
        with pytest.raises(RuntimeError):
            await retry_async(fn, cfg)
        assert calls == 1

    @pytest.mark.asyncio
    async def test_only_if_retryable_false_retries_all_tinker_errors(self):
        """When only_if_retryable=False, even ConfigurationError is retried."""
        calls = 0

        async def fn():
            nonlocal calls
            calls += 1
            raise ConfigurationError("bad value")

        cfg = RetryConfig(
            max_attempts=3,
            base_delay=0.0,
            jitter=False,
            only_if_retryable=False,
        )
        with pytest.raises(ConfigurationError):
            await retry_async(fn, cfg)
        assert calls == 3

    @pytest.mark.asyncio
    async def test_max_attempts_one_no_retry(self):
        calls = 0

        async def fn():
            nonlocal calls
            calls += 1
            raise ModelConnectionError("fail")

        cfg = RetryConfig(max_attempts=1, base_delay=0.0, jitter=False)
        with pytest.raises(ModelConnectionError):
            await retry_async(fn, cfg)
        assert calls == 1


# ---------------------------------------------------------------------------
# retry_async — sleep timing (mocked)
# ---------------------------------------------------------------------------


class TestRetryAsyncSleep:
    @pytest.mark.asyncio
    async def test_sleep_called_between_attempts(self):
        calls = 0

        async def fn():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise ModelConnectionError("transient")
            return "ok"

        cfg = RetryConfig(max_attempts=3, base_delay=1.0, jitter=False)
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await retry_async(fn, cfg)

        assert result == "ok"
        assert mock_sleep.call_count == 2  # sleep before attempt 2 and 3

    @pytest.mark.asyncio
    async def test_sleep_durations_are_non_negative(self):
        """Sleep duration must never be negative (even with jitter)."""
        calls = 0

        async def fn():
            nonlocal calls
            calls += 1
            if calls < 4:
                raise ModelConnectionError("x")
            return "ok"

        sleep_durations = []

        async def record_sleep(d):
            sleep_durations.append(d)

        cfg = RetryConfig(max_attempts=4, base_delay=1.0, jitter=True)
        with patch("asyncio.sleep", side_effect=record_sleep):
            await retry_async(fn, cfg)

        assert all(d >= 0 for d in sleep_durations), (
            f"Negative sleep duration found: {sleep_durations}"
        )

    @pytest.mark.asyncio
    async def test_no_sleep_on_first_attempt_success(self):
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await retry_async(lambda: _succeed(), RetryConfig())
        mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_sleep_on_non_retryable_failure(self):
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with pytest.raises(ResponseParseError):
                await retry_async(lambda: _always_fail_non_retryable(), RetryConfig())
        mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# with_retry decorator
# ---------------------------------------------------------------------------


class TestWithRetryDecorator:
    @pytest.mark.asyncio
    async def test_preserves_return_value(self):
        @with_retry()
        async def fn():
            return 42

        assert await fn() == 42

    @pytest.mark.asyncio
    async def test_preserves_function_name(self):
        @with_retry()
        async def my_function():
            pass

        assert my_function.__name__ == "my_function"

    @pytest.mark.asyncio
    async def test_preserves_docstring(self):
        @with_retry()
        async def my_function():
            """My docstring."""

        assert my_function.__doc__ == "My docstring."

    @pytest.mark.asyncio
    async def test_exposes_retry_config(self):
        cfg = RetryConfig(max_attempts=7)

        @with_retry(cfg)
        async def fn():
            pass

        assert fn._retry_config is cfg

    @pytest.mark.asyncio
    async def test_passes_args_and_kwargs(self):
        @with_retry()
        async def fn(a, b, *, c=0):
            return a + b + c

        assert await fn(1, 2, c=3) == 6

    @pytest.mark.asyncio
    async def test_retries_on_retryable_error(self):
        calls = 0
        cfg = RetryConfig(max_attempts=3, base_delay=0.0, jitter=False)

        @with_retry(cfg)
        async def fn():
            nonlocal calls
            calls += 1
            if calls < 2:
                raise ModelConnectionError("x")
            return "done"

        result = await fn()
        assert result == "done"
        assert calls == 2

    @pytest.mark.asyncio
    async def test_custom_config_respected(self):
        calls = 0
        cfg = RetryConfig(max_attempts=2, base_delay=0.0, jitter=False)

        @with_retry(cfg)
        async def fn():
            nonlocal calls
            calls += 1
            raise ModelConnectionError("always")

        with pytest.raises(ModelConnectionError):
            await fn()
        assert calls == 2  # exactly 2, not 3 (the default)
