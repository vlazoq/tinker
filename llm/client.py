"""
Tinker Model Client — Low-level async Ollama/OpenAI-compatible HTTP client.

What this file does
-------------------
This file contains ``OllamaClient``, the class that actually sends HTTP
requests to an Ollama server and reads back the responses.  Think of it as
the "telephone handset" of the system — everything else decides *what* to
say, but this class is the one that picks up the phone and dials.

Why it exists (and why it's separate from the router)
------------------------------------------------------
The ``ModelRouter`` (in router.py) handles high-level concerns: routing,
context trimming, JSON parsing.  This file handles only the low-level
network concerns:
  - Sending an HTTP POST to Ollama's ``/v1/chat/completions`` endpoint.
  - Waiting for the response.
  - Detecting errors (rate limits, server crashes, timeouts).
  - Retrying with exponential back-off so a brief hiccup doesn't fail the run.

Keeping these separate means you can swap the model backend (e.g. replace
Ollama with another service) without touching any routing logic.

How it fits into Tinker
-----------------------
``ModelRouter`` creates one ``OllamaClient`` per machine (SERVER and
SECONDARY).  When an agent needs a completion, the router calls
``client.chat(...)`` here.

Key concepts for beginners
--------------------------
- **async / await**: Python's way of doing things "in the background" so the
  program doesn't freeze while waiting for a slow network response.
- **aiohttp**: An async HTTP library (like the built-in ``requests``, but
  works with async code).
- **Exponential back-off**: Wait 2s, then 4s, then 8s, … between retries so
  we don't hammer an already-struggling server.

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

# aiohttp is an optional dependency — we try to import it and remember
# whether it succeeded.  If it's not installed the error will only be
# raised when someone actually tries to make a request, not at import time.
try:
    import aiohttp
    _AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover
    # ``pragma: no cover`` tells the test-coverage tool to ignore this branch
    # because it only runs when aiohttp is missing, which won't happen in CI.
    aiohttp = None  # type: ignore[assignment]
    _AIOHTTP_AVAILABLE = False

from .types import MachineConfig, Message, RetryConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions — a tidy hierarchy of errors
# ---------------------------------------------------------------------------
# Having separate exception classes for each failure mode is important because
# callers can then catch exactly the error type they care about.  For example,
# a caller that wants to retry on connection errors but not on parse errors
# can write ``except ConnectionError`` instead of checking string messages.

class ModelClientError(Exception):
    """
    Base class for all errors raised by the model client.

    Catch this if you want to handle *any* model-client failure in one place.
    Catch a more specific subclass if you only care about one kind of failure.
    """


class ConnectionError(ModelClientError):
    """
    Raised when the client cannot establish a TCP connection to Ollama.

    Common causes: Ollama is not running, wrong ``base_url``, firewall rules.
    This error is retryable — the server might come back up shortly.
    """


class TimeoutError(ModelClientError):
    """
    Raised when a request or connection attempt takes longer than the
    configured timeout.

    This is retryable because the server might just be under heavy load.
    """


class RateLimitError(ModelClientError):
    """
    Raised when the server responds with HTTP 429 ("Too Many Requests").

    This means we're sending requests faster than the server can handle.
    The retry logic will back off and try again after a delay.
    """


class ServerError(ModelClientError):
    """
    Raised when the server responds with a 5xx status code (500, 502, 503, …).

    5xx means something went wrong on the *server's* side.  These are
    retryable — the server may recover quickly.
    """


class ResponseParseError(ModelClientError):
    """
    Raised when the server's response is not valid JSON.

    This is *not* retryable — if the server sends garbage, retrying will
    probably get the same garbage back.
    """


# ---------------------------------------------------------------------------
# Low-level client
# ---------------------------------------------------------------------------

class OllamaClient:
    """
    Async HTTP client for a single Ollama machine.

    Ollama exposes an OpenAI-compatible API, meaning it uses the same URL
    path and JSON format as OpenAI's API.  This class talks to that API.

    There is one ``OllamaClient`` per machine (SERVER and SECONDARY).  It
    manages a persistent HTTP session (so we don't re-establish a TCP
    connection on every request) and handles retries with exponential back-off.

    Analogy: think of this class as a persistent phone line to one AI server.
    You open the line (``_get_session``), make calls (``chat``), and close
    it when done (``close``).  If the call drops, you redial automatically
    a few times before giving up.

    Usage as a context manager (recommended — ensures clean shutdown)::

        async with OllamaClient(config) as client:
            response = await client.chat(messages=[...])
    """

    def __init__(
        self,
        config: MachineConfig,
        retry: RetryConfig | None = None,
    ) -> None:
        """
        Create an OllamaClient.

        Parameters
        ----------
        config : MachineConfig
            Connection settings (URL, model, timeouts).
        retry  : RetryConfig, optional
            How many times to retry and how long to wait.  Defaults to
            RetryConfig() which gives 3 attempts with exponential back-off.
        """
        self.config  = config
        # Use provided retry config, or fall back to sensible defaults
        self.retry   = retry or RetryConfig()
        # The aiohttp session is created lazily on first use (see _get_session)
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        """
        Return a live aiohttp session, creating one if needed.

        We lazily create the session (only when the first request is made)
        and re-use it for all subsequent requests.  Re-using the session
        means TCP connections are kept alive ("connection pooling"), which
        is faster than opening a new connection for every request.

        If the session was closed (e.g. after ``close()`` was called) a new
        one is created automatically.
        """
        if self._session is None or self._session.closed:
            # limit=10: allow at most 10 simultaneous TCP connections to this host
            connector = aiohttp.TCPConnector(limit=10)
            timeout   = aiohttp.ClientTimeout(
                total=self.config.request_timeout,   # entire request must finish in N seconds
                connect=self.config.connect_timeout, # just the TCP handshake
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                # Tell Ollama we're sending JSON in the request body
                headers={"Content-Type": "application/json"},
            )
        return self._session

    async def close(self) -> None:
        """
        Gracefully close the HTTP session and free its resources.

        Always call this when you're done using the client, or use the
        ``async with OllamaClient(...) as client:`` pattern which calls it
        automatically.
        """
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> "OllamaClient":
        """Support the ``async with OllamaClient(...) as client:`` pattern."""
        return self

    async def __aexit__(self, *_: Any) -> None:
        """Called automatically at the end of an ``async with`` block."""
        await self.close()

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        """
        Return True if the Ollama server is reachable and responding.

        Calls Ollama's ``/api/tags`` endpoint, which lists available models.
        If it returns HTTP 200, the server is up.  Any error (connection
        refused, timeout, etc.) returns False — never raises.

        Used by the router to verify that machines are alive before routing
        requests to them.
        """
        url = f"{self.config.base_url.rstrip('/')}/api/tags"
        try:
            session = await self._get_session()
            async with session.get(url) as resp:
                return resp.status == 200
        except Exception as exc:
            # Log at DEBUG level — this is expected when a machine is down
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
        Send a chat-completion request to Ollama and return the raw JSON response.

        This is the main method callers use.  It handles the retry loop,
        back-off timing, and error classification.  The actual HTTP work
        is delegated to ``_send``.

        Parameters
        ----------
        messages    : The conversation history (list of Message objects).
        model       : Override the model name.  Defaults to ``config.model``.
        temperature : Creativity knob (0.0 = deterministic, 1.0 = creative).
        max_tokens  : Override the reply length limit.  Defaults to config value.
        stream      : If True, request a streaming response.  Currently not
                      supported by the rest of Tinker, so always pass False.

        Returns
        -------
        dict : The raw parsed JSON from Ollama's response body.

        Raises
        ------
        ModelClientError (or a subclass) if all retry attempts fail.
        """
        # Fall back to config defaults if caller didn't specify these
        model      = model or self.config.model
        max_tokens = max_tokens or self.config.max_output_tokens
        url        = f"{self.config.base_url.rstrip('/')}/v1/chat/completions"

        # Build the request body in the OpenAI chat-completions format
        payload: dict[str, Any] = {
            "model":       model,
            "messages":    [m.to_dict() for m in messages],  # list of {"role":…,"content":…}
            "temperature": temperature,
            "max_tokens":  max_tokens,
            "stream":      stream,
        }

        # ``last_exc`` stores the most recent exception so we can re-raise it
        # after all retries are exhausted.
        last_exc: Exception = RuntimeError("No attempts made")
        # Start with the base delay; it will grow after each failed attempt.
        delay = self.retry.base_delay

        for attempt in range(1, self.retry.max_attempts + 1):
            try:
                result = await self._send(url, payload, attempt)
                return result  # success — return immediately, skip retries
            except (RateLimitError, ServerError) as exc:
                # These server-side errors are worth retrying
                last_exc = exc
                if attempt == self.retry.max_attempts:
                    break  # no more retries — fall through to raise
                logger.warning(
                    "Retryable error on attempt %d/%d: %s — waiting %.1fs",
                    attempt, self.retry.max_attempts, exc, delay,
                )
                await asyncio.sleep(delay)   # pause before retrying
                # Exponential back-off: double the delay, but cap at max_delay
                delay = min(delay * self.retry.backoff_factor, self.retry.max_delay)
            except (ConnectionError, TimeoutError) as exc:
                # Network problems are also worth retrying
                last_exc = exc
                if attempt == self.retry.max_attempts:
                    break
                logger.warning(
                    "Connection/timeout error on attempt %d/%d: %s — waiting %.1fs",
                    attempt, self.retry.max_attempts, exc, delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * self.retry.backoff_factor, self.retry.max_delay)
            # Non-retryable errors (e.g. ResponseParseError, bad 4xx) propagate immediately
            except ModelClientError:
                raise

        # All attempts exhausted — re-raise the last error we saw
        raise last_exc

    async def _send(self, url: str, payload: dict, attempt: int) -> dict[str, Any]:
        """
        Execute one HTTP POST and return the parsed JSON body.

        This is the innermost layer — no retry logic here.  It sends the
        request, reads the response, checks the HTTP status code, and either
        returns the parsed JSON or raises the appropriate exception.

        Parameters
        ----------
        url     : The full URL to POST to.
        payload : The request body (a dict that will be JSON-encoded).
        attempt : Which attempt number this is (used only for logging).

        Returns
        -------
        dict : The parsed JSON response body.

        Raises
        ------
        RateLimitError     : HTTP 429
        ServerError        : HTTP 5xx
        ModelClientError   : Other HTTP 4xx errors
        ResponseParseError : Response body is not valid JSON
        ConnectionError    : TCP connection failed
        TimeoutError       : Request timed out
        """
        t0 = time.monotonic()   # record start time for elapsed logging
        session = await self._get_session()
        try:
            # ``data=json.dumps(payload)`` manually serialises to JSON string
            # (instead of using ``json=payload``) because aiohttp's json= kwarg
            # sets Content-Type automatically, but we already set it in headers.
            async with session.post(url, data=json.dumps(payload)) as resp:
                elapsed = time.monotonic() - t0
                body    = await resp.text()   # read the full response body as a string
                logger.debug(
                    "POST %s  status=%d  attempt=%d  elapsed=%.2fs",
                    url, resp.status, attempt, elapsed,
                )

                # --- Map HTTP status codes to our exception hierarchy ---

                if resp.status == 429:
                    # 429 = "Too Many Requests" — server is asking us to slow down
                    raise RateLimitError(f"Rate limited by {url}")

                if resp.status in self.retry.retryable_status_codes:
                    # 5xx server errors — worth retrying
                    raise ServerError(f"HTTP {resp.status} from {url}: {body[:200]}")

                if resp.status >= 400:
                    # Any other 4xx error is a client mistake — don't retry
                    raise ModelClientError(f"HTTP {resp.status} from {url}: {body[:200]}")

                # --- Parse the response body ---
                try:
                    return json.loads(body)
                except json.JSONDecodeError as exc:
                    raise ResponseParseError(
                        f"Could not parse JSON from {url}: {body[:200]}"
                    ) from exc

        # --- Translate aiohttp network errors into our own exception types ---

        except aiohttp.ServerConnectionError as exc:
            # Server closed the connection unexpectedly
            raise ConnectionError(f"Cannot connect to {url}: {exc}") from exc
        except aiohttp.ClientConnectorError as exc:
            # TCP connection refused or DNS lookup failed
            raise ConnectionError(f"Cannot connect to {url}: {exc}") from exc
        except asyncio.TimeoutError as exc:
            # Either the connect timeout or the total request timeout fired
            raise TimeoutError(f"Request to {url} timed out") from exc
        except aiohttp.ClientError as exc:
            # Catch-all for any other aiohttp-level error
            raise ModelClientError(f"HTTP client error: {exc}") from exc
