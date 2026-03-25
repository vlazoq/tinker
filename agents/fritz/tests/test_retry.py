"""
fritz/tests/test_retry.py
──────────────────────────
Tests for the retry + rate-limit logic.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agents.fritz.retry import RateLimitState, with_retry


# ── RateLimitState ────────────────────────────────────────────────────────────


class TestRateLimitState:
    def _response(self, headers: dict) -> httpx.Response:
        return httpx.Response(200, headers=headers)

    def test_initial_remaining_unknown(self):
        state = RateLimitState()
        assert state.remaining == -1
        assert not state.is_exhausted()

    def test_update_parses_headers(self):
        state = RateLimitState()
        state.update(self._response({
            "x-ratelimit-limit": "5000",
            "x-ratelimit-remaining": "4999",
            "x-ratelimit-reset": "9999999999",
            "x-ratelimit-used": "1",
        }))
        assert state.limit == 5000
        assert state.remaining == 4999
        assert state.used == 1

    def test_exhausted_when_remaining_zero(self):
        state = RateLimitState()
        state.remaining = 0
        assert state.is_exhausted()

    def test_not_exhausted_when_remaining_positive(self):
        state = RateLimitState()
        state.remaining = 100
        assert not state.is_exhausted()

    def test_seconds_until_reset_past(self):
        state = RateLimitState()
        state.reset_at = time.time() - 10
        assert state.seconds_until_reset() == 0.0

    def test_seconds_until_reset_future(self):
        state = RateLimitState()
        state.reset_at = time.time() + 60
        delta = state.seconds_until_reset()
        assert 55 < delta <= 61

    def test_update_ignores_malformed_headers(self):
        state = RateLimitState()
        state.update(self._response({"x-ratelimit-remaining": "not-a-number"}))
        assert state.remaining == -1  # unchanged

    def test_str_shows_unknown_before_first_call(self):
        assert "unknown" in str(RateLimitState())


# ── with_retry ────────────────────────────────────────────────────────────────


@pytest.fixture()
def make_response():
    def _make(status: int, headers: dict | None = None) -> httpx.Response:
        return httpx.Response(status, headers=headers or {})
    return _make


class TestWithRetry:
    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self, make_response):
        fn = AsyncMock(return_value=make_response(200))
        resp = await with_retry(fn, max_attempts=3)
        assert resp.status_code == 200
        fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_retries_on_500(self, make_response):
        fn = AsyncMock(side_effect=[make_response(500), make_response(200)])
        with patch("asyncio.sleep", new_callable=AsyncMock):
            resp = await with_retry(fn, max_attempts=3, backoff_base=0.001)
        assert resp.status_code == 200
        assert fn.call_count == 2

    @pytest.mark.asyncio
    async def test_retries_on_502(self, make_response):
        fn = AsyncMock(side_effect=[make_response(502), make_response(502), make_response(200)])
        with patch("asyncio.sleep", new_callable=AsyncMock):
            resp = await with_retry(fn, max_attempts=3, backoff_base=0.001)
        assert resp.status_code == 200
        assert fn.call_count == 3

    @pytest.mark.asyncio
    async def test_no_retry_on_404(self, make_response):
        fn = AsyncMock(return_value=make_response(404))
        resp = await with_retry(fn, max_attempts=3)
        assert resp.status_code == 404
        fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_retry_on_401(self, make_response):
        fn = AsyncMock(return_value=make_response(401))
        resp = await with_retry(fn, max_attempts=3)
        fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_retry_on_422(self, make_response):
        fn = AsyncMock(return_value=make_response(422))
        resp = await with_retry(fn, max_attempts=3)
        fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_last_5xx_after_exhausting_retries(self, make_response):
        fn = AsyncMock(return_value=make_response(503))
        with patch("asyncio.sleep", new_callable=AsyncMock):
            resp = await with_retry(fn, max_attempts=2, backoff_base=0.001)
        assert resp.status_code == 503
        assert fn.call_count == 2

    @pytest.mark.asyncio
    async def test_retries_on_429(self, make_response):
        fn = AsyncMock(side_effect=[make_response(429), make_response(200)])
        with patch("asyncio.sleep", new_callable=AsyncMock):
            resp = await with_retry(fn, max_attempts=3, backoff_base=0.001)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_retry_after_header_respected(self, make_response):
        fn = AsyncMock(side_effect=[
            make_response(429, headers={"retry-after": "1"}),
            make_response(200),
        ])
        slept: list[float] = []
        async def mock_sleep(s):
            slept.append(s)
        with patch("asyncio.sleep", side_effect=mock_sleep):
            await with_retry(fn, max_attempts=3, backoff_base=0.001)
        assert slept and slept[0] == pytest.approx(1.0, abs=0.3)

    @pytest.mark.asyncio
    async def test_rate_limit_exhausted_sleeps_until_reset(self, make_response):
        state = RateLimitState()
        state.remaining = 0
        state.reset_at = time.time() + 5

        fn = AsyncMock(side_effect=[
            make_response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(time.time() + 5))}),
            make_response(200),
        ])
        slept: list[float] = []
        async def mock_sleep(s):
            slept.append(s)
        with patch("asyncio.sleep", side_effect=mock_sleep):
            resp = await with_retry(fn, rate_state=state, max_attempts=3, backoff_base=0.001)
        assert resp.status_code == 200
        assert slept  # slept for rate-limit reset

    @pytest.mark.asyncio
    async def test_network_error_retried(self):
        fn = AsyncMock(side_effect=[
            httpx.ConnectError("connection refused"),
            httpx.Response(200),
        ])
        with patch("asyncio.sleep", new_callable=AsyncMock):
            resp = await with_retry(fn, max_attempts=3, backoff_base=0.001)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_network_error_raised_after_exhausting(self):
        fn = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(httpx.ConnectError):
                await with_retry(fn, max_attempts=2, backoff_base=0.001)

    @pytest.mark.asyncio
    async def test_timeout_error_retried(self):
        fn = AsyncMock(side_effect=[
            httpx.TimeoutException("timed out"),
            httpx.Response(200),
        ])
        with patch("asyncio.sleep", new_callable=AsyncMock):
            resp = await with_retry(fn, max_attempts=3, backoff_base=0.001)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_rate_state_updated_on_success(self, make_response):
        state = RateLimitState()
        fn = AsyncMock(return_value=make_response(
            200,
            headers={"x-ratelimit-remaining": "42", "x-ratelimit-limit": "5000"},
        ))
        await with_retry(fn, rate_state=state)
        assert state.remaining == 42
        assert state.limit == 5000
