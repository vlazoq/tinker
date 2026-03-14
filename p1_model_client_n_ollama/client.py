"""
Tinker Model Client — Low-level async Ollama/OpenAI-compat HTTP client.

One OllamaClient instance per machine.  Handles:
  - async HTTP via aiohttp
  - exponential-backoff retry
  - per-request + connect timeouts
  - clean error taxonomy
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

try:
    import aiohttp
    _AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore[assignment]
    _AIOHTTP_AVAILABLE = False

from .types import MachineConfig, Message, RetryConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ModelClientError(Exception):
    """Base class for all model-client errors."""


class ConnectionError(ModelClientError):
    """Cannot reach the Ollama instance."""


class TimeoutError(ModelClientError):
    """Request or connection timed out."""


class RateLimitError(ModelClientError):
    """429 from the server."""


class ServerError(ModelClientError):
    """5xx from the server."""


class ResponseParseError(ModelClientError):
    """Could not parse the server's response body."""


# ---------------------------------------------------------------------------
# Low-level client
# ---------------------------------------------------------------------------

class OllamaClient:
    """
    Async client for a single Ollama machine (OpenAI-compatible /v1/chat/completions).
    """

    def __init__(
        self,
        config: MachineConfig,
        retry: RetryConfig | None = None,
    ) -> None:
        self.config  = config
        self.retry   = retry or RetryConfig()
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(limit=10)
            timeout   = aiohttp.ClientTimeout(
                total=self.config.request_timeout,
                connect=self.config.connect_timeout,
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers={"Content-Type": "application/json"},
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> "OllamaClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        """
        Returns True if the Ollama instance is reachable.
        Uses the /api/tags endpoint which lists available models.
        """
        url = f"{self.config.base_url.rstrip('/')}/api/tags"
        try:
            session = await self._get_session()
            async with session.get(url) as resp:
                return resp.status == 200
        except Exception as exc:
            logger.debug("Health check failed for %s: %s", self.config.base_url, exc)
            return False

    # ------------------------------------------------------------------
    # Core request
    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        stream: bool = False,
    ) -> dict[str, Any]:
        """
        Send a chat-completion request.  Returns the raw parsed JSON response.
        Raises a ModelClientError subclass on failure.
        """
        model      = model or self.config.model
        max_tokens = max_tokens or self.config.max_output_tokens
        url        = f"{self.config.base_url.rstrip('/')}/v1/chat/completions"

        payload: dict[str, Any] = {
            "model":       model,
            "messages":    [m.to_dict() for m in messages],
            "temperature": temperature,
            "max_tokens":  max_tokens,
            "stream":      stream,
        }

        last_exc: Exception = RuntimeError("No attempts made")
        delay = self.retry.base_delay

        for attempt in range(1, self.retry.max_attempts + 1):
            try:
                result = await self._send(url, payload, attempt)
                return result
            except (RateLimitError, ServerError) as exc:
                last_exc = exc
                if attempt == self.retry.max_attempts:
                    break
                logger.warning(
                    "Retryable error on attempt %d/%d: %s — waiting %.1fs",
                    attempt, self.retry.max_attempts, exc, delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * self.retry.backoff_factor, self.retry.max_delay)
            except (ConnectionError, TimeoutError) as exc:
                last_exc = exc
                if attempt == self.retry.max_attempts:
                    break
                logger.warning(
                    "Connection/timeout error on attempt %d/%d: %s — waiting %.1fs",
                    attempt, self.retry.max_attempts, exc, delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * self.retry.backoff_factor, self.retry.max_delay)
            # Non-retryable errors propagate immediately
            except ModelClientError:
                raise

        raise last_exc

    async def _send(self, url: str, payload: dict, attempt: int) -> dict[str, Any]:
        t0 = time.monotonic()
        session = await self._get_session()
        try:
            async with session.post(url, data=json.dumps(payload)) as resp:
                elapsed = time.monotonic() - t0
                body    = await resp.text()
                logger.debug(
                    "POST %s  status=%d  attempt=%d  elapsed=%.2fs",
                    url, resp.status, attempt, elapsed,
                )

                if resp.status == 429:
                    raise RateLimitError(f"Rate limited by {url}")

                if resp.status in self.retry.retryable_status_codes:
                    raise ServerError(f"HTTP {resp.status} from {url}: {body[:200]}")

                if resp.status >= 400:
                    raise ModelClientError(f"HTTP {resp.status} from {url}: {body[:200]}")

                try:
                    return json.loads(body)
                except json.JSONDecodeError as exc:
                    raise ResponseParseError(
                        f"Could not parse JSON from {url}: {body[:200]}"
                    ) from exc

        except aiohttp.ServerConnectionError as exc:
            raise ConnectionError(f"Cannot connect to {url}: {exc}") from exc
        except aiohttp.ClientConnectorError as exc:
            raise ConnectionError(f"Cannot connect to {url}: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"Request to {url} timed out") from exc
        except aiohttp.ClientError as exc:
            raise ModelClientError(f"HTTP client error: {exc}") from exc
