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

# Import the typed exception hierarchy from the central exceptions module.
# All model-client exceptions live in exceptions.py so the full error surface
# is visible from one place.  We re-export the names that callers of THIS
# module typically import so existing ``from llm.client import ...`` code
# continues to work without changes.
from exceptions import (
    ModelClientError,
    ModelConnectionError,
    ModelTimeoutError,
    ModelRateLimitError,
    ModelServerError,
    ResponseParseError,
)

# Backwards-compatibility aliases: older code that did
#   from llm.client import ConnectionError, TimeoutError, RateLimitError, ServerError
# still works — these names are intentional re-exports, NOT new definitions.
ConnectionError = ModelConnectionError  # noqa: A001  (intentional shadowing alias)
TimeoutError = ModelTimeoutError  # noqa: A001
RateLimitError = ModelRateLimitError
ServerError = ModelServerError

logger = logging.getLogger(__name__)

# CircuitBreaker is an optional enterprise dependency — import it lazily so
# the LLM client works even when the resilience package is not installed.
try:
    from resilience.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError

    _CIRCUIT_BREAKER_AVAILABLE = True
except ImportError:
    CircuitBreaker = None  # type: ignore[assignment,misc]
    CircuitBreakerOpenError = None  # type: ignore[assignment,misc]
    _CIRCUIT_BREAKER_AVAILABLE = False


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
        circuit_breaker: "CircuitBreaker | None" = None,
    ) -> None:
        """
        Create an OllamaClient.

        Parameters
        ----------
        config          : MachineConfig
            Connection settings (URL, model, timeouts).
        retry           : RetryConfig, optional
            How many times to retry and how long to wait.  Defaults to
            RetryConfig() which gives 3 attempts with exponential back-off.
        circuit_breaker : CircuitBreaker, optional
            An optional circuit breaker to protect all calls to this Ollama
            machine.  When the breaker is OPEN, ``chat()`` raises
            ``CircuitBreakerOpenError`` immediately (fast fail) instead of
            hammering an unavailable server.  Pass ``None`` to disable circuit
            breaking (the default, for backward compatibility).

            Example::

                registry = build_default_registry()
                client = OllamaClient(
                    config,
                    circuit_breaker=registry.get("ollama_server"),
                )
        """
        self.config = config
        # Use provided retry config, or fall back to sensible defaults
        self.retry = retry or RetryConfig()
        # Optional circuit breaker — None means "no protection" (legacy mode)
        self._circuit_breaker = circuit_breaker
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
            timeout = aiohttp.ClientTimeout(
                total=self.config.request_timeout,  # entire request must finish in N seconds
                connect=self.config.connect_timeout,  # just the TCP handshake
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

    async def warmup(self) -> bool:
        """
        Pre-load this client's model into Ollama's VRAM.

        Sends a single-token completion with an empty prompt so Ollama loads
        the model weights before the first real request arrives.  Call this
        after creating a new client or after ``hot_reload()`` so agents never
        wait for a cold model load during their actual work.

        Returns True if the warmup succeeded, False if the server was
        unreachable (non-fatal — the model will load lazily on the first
        real request instead).
        """
        try:
            from .types import Message as _Msg
            dummy = [_Msg(role="user", content=" ")]
            await self.chat(messages=dummy, max_tokens=1, temperature=0.0)
            logger.info("Warmup complete for model=%s @ %s", self.config.model, self.config.base_url)
            return True
        except Exception as exc:
            logger.warning(
                "Warmup skipped for model=%s @ %s: %s",
                self.config.model, self.config.base_url, exc,
            )
            return False

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
        stream      : If True, request a streaming NDJSON response.  Chunks
                      are aggregated into the same dict shape as a non-streaming
                      call, so callers need not branch on this flag.

        Returns
        -------
        dict : The raw parsed JSON from Ollama's response body.

        Raises
        ------
        ModelClientError (or a subclass) if all retry attempts fail.
        """
        # Fall back to config defaults if caller didn't specify these
        model = model or self.config.model
        max_tokens = max_tokens or self.config.max_output_tokens
        url = f"{self.config.base_url.rstrip('/')}/v1/chat/completions"

        # Build the request body in the OpenAI chat-completions format.
        # ``keep_alive`` tells Ollama how long to hold model weights in VRAM
        # after this request completes.  Sending it on every request resets
        # the idle timer, so active sessions never trigger an unload.
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                m.to_dict() for m in messages
            ],  # list of {"role":…,"content":…}
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
            "keep_alive": self.config.keep_alive,
        }

        # ── Circuit breaker fast-fail ────────────────────────────────────────
        # If a circuit breaker is wired in, check it before doing any work.
        # When the breaker is OPEN, this raises CircuitBreakerOpenError
        # immediately and we never touch the network.  Callers should catch
        # this and apply graceful degradation (e.g., skip this call, use a
        # cached response, or report a transient error to the orchestrator).
        if self._circuit_breaker is not None:
            # ``circuit_breaker.call`` wraps the entire retry loop so the
            # breaker counts the whole logical operation as one failure, not
            # each individual retry attempt.  This avoids nuking the breaker
            # on expected transient errors that the retry logic handles fine.
            return await self._circuit_breaker.call(self._chat_inner, url, payload)

        return await self._chat_inner(url, payload)

    async def _chat_inner(self, url: str, payload: dict) -> dict[str, Any]:
        """
        Internal retry loop, separated from ``chat()`` so the circuit breaker
        can wrap the entire operation as a single logical call.

        Parameters
        ----------
        url     : The full endpoint URL.
        payload : The pre-built request body dict.

        Returns
        -------
        dict : Parsed JSON response from Ollama.

        Raises
        ------
        ModelClientError (or subclass) : If all retries fail.
        """
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
                    attempt,
                    self.retry.max_attempts,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)  # pause before retrying
                # Exponential back-off: double the delay, but cap at max_delay
                delay = min(delay * self.retry.backoff_factor, self.retry.max_delay)
            except (ModelConnectionError, ModelTimeoutError) as exc:
                # Network problems are also worth retrying
                last_exc = exc
                if attempt == self.retry.max_attempts:
                    break
                logger.warning(
                    "Connection/timeout error on attempt %d/%d: %s — waiting %.1fs",
                    attempt,
                    self.retry.max_attempts,
                    exc,
                    delay,
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
        ModelRateLimitError  : HTTP 429
        ModelServerError     : HTTP 5xx
        ModelClientError     : Other HTTP 4xx errors
        ResponseParseError   : Response body is not valid JSON
        ModelConnectionError : TCP connection failed
        ModelTimeoutError    : Request timed out
        """
        t0 = time.monotonic()  # record start time for elapsed logging
        session = await self._get_session()
        is_streaming = payload.get("stream", False)

        # Propagate distributed trace context across service boundaries.
        # These headers allow log aggregators (Loki, Datadog, Jaeger) to
        # correlate Tinker log lines with Ollama server logs for the same
        # request.  We attach the current trace_id (from contextvars) and a
        # per-request UUID so each attempt can be uniquely identified.
        import uuid as _uuid
        from contextvars import copy_context as _copy_ctx
        try:
            from agents import _current_trace_id as _tid_var
            _trace_id = _tid_var.get("") or str(_uuid.uuid4())
        except Exception:
            _trace_id = str(_uuid.uuid4())
        _request_id = str(_uuid.uuid4())
        trace_headers = {
            "X-Trace-ID": _trace_id,
            "X-Request-ID": _request_id,
            "X-Attempt": str(attempt),
        }

        try:
            # ``data=json.dumps(payload)`` manually serialises to JSON string
            # (instead of using ``json=payload``) because aiohttp's json= kwarg
            # sets Content-Type automatically, but we already set it in headers.
            async with session.post(url, data=json.dumps(payload), headers=trace_headers) as resp:
                elapsed = time.monotonic() - t0
                logger.debug(
                    "POST %s  status=%d  attempt=%d  elapsed=%.2fs  stream=%s",
                    url,
                    resp.status,
                    attempt,
                    elapsed,
                    is_streaming,
                )

                # --- Map HTTP status codes to our exception hierarchy ---
                # For streaming responses we must check the status before
                # starting to read chunks, so we do it up front regardless.

                if resp.status == 429:
                    # 429 = "Too Many Requests" — server is asking us to slow down
                    raise ModelRateLimitError(
                        f"Rate limited by {url}",
                        context={"url": url, "status": 429},
                    )

                if resp.status in self.retry.retryable_status_codes:
                    # 5xx server errors — worth retrying
                    body = await resp.text()
                    raise ModelServerError(
                        f"HTTP {resp.status} from {url}: {body[:200]}",
                        context={"url": url, "status": resp.status},
                    )

                if resp.status >= 400:
                    # Any other 4xx error is a client mistake — don't retry
                    body = await resp.text()
                    raise ModelClientError(
                        f"HTTP {resp.status} from {url}: {body[:200]}",
                        context={"url": url, "status": resp.status},
                    )

                # --- Parse the response body ---

                if is_streaming:
                    # Streaming path: read NDJSON lines and aggregate them
                    # into a single response dict that matches the shape of a
                    # non-streaming response so callers don't need to branch.
                    return await self._read_stream(resp, url)
                else:
                    body = await resp.text()
                    try:
                        return json.loads(body)
                    except json.JSONDecodeError as exc:
                        raise ResponseParseError(
                            f"Could not parse JSON from {url}: {body[:200]}",
                            context={"url": url},
                        ) from exc

        # --- Translate aiohttp network errors into our own exception types ---

        except aiohttp.ServerConnectionError as exc:
            # Server closed the connection unexpectedly
            raise ModelConnectionError(
                f"Cannot connect to {url}: {exc}",
                context={"url": url},
            ) from exc
        except aiohttp.ClientConnectorError as exc:
            # TCP connection refused or DNS lookup failed
            raise ModelConnectionError(
                f"Cannot connect to {url}: {exc}",
                context={"url": url},
            ) from exc
        except asyncio.TimeoutError as exc:
            # Either the connect timeout or the total request timeout fired
            raise ModelTimeoutError(
                f"Request to {url} timed out",
                context={"url": url},
            ) from exc
        except aiohttp.ClientError as exc:
            # Catch-all for any other aiohttp-level error
            raise ModelClientError(
                f"HTTP client error: {exc}",
                context={"url": url},
            ) from exc

    @staticmethod
    async def _read_stream(resp: Any, url: str) -> dict[str, Any]:
        """
        Consume an NDJSON streaming response (``stream=True``) and aggregate
        the chunks into a single dict that mirrors the non-streaming shape::

            {
                "choices": [{"message": {"role": "assistant", "content": "<full text>"}}],
                "usage": {"prompt_tokens": N, "completion_tokens": M, "total_tokens": T},
            }

        Each chunk from Ollama's OpenAI-compat endpoint looks like::

            {"choices": [{"delta": {"content": "…"}, "finish_reason": null}]}

        The final chunk carries usage::

            {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {…}}

        Lines starting with ``data: `` (SSE format) are stripped before JSON
        parsing.  Empty lines and ``data: [DONE]`` sentinels are skipped.
        """
        content_parts: list[str] = []
        usage: dict[str, int] = {}

        async for raw_line in resp.content:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or line == "data: [DONE]":
                continue
            # Strip the "data: " prefix used by SSE-style streaming
            if line.startswith("data: "):
                line = line[6:]
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("Skipping non-JSON stream line: %r", line[:80])
                continue

            # Accumulate content from delta chunks
            for choice in chunk.get("choices", []):
                delta = choice.get("delta", {})
                piece = delta.get("content")
                if piece:
                    content_parts.append(piece)

            # Capture usage from whichever chunk carries it (usually the last)
            if chunk.get("usage"):
                usage = chunk["usage"]

        full_content = "".join(content_parts)
        if not usage:
            # Estimate token counts when the server didn't report them
            estimated = max(1, len(full_content) // 4)
            usage = {
                "prompt_tokens": 0,
                "completion_tokens": estimated,
                "total_tokens": estimated,
            }

        return {
            "choices": [
                {"message": {"role": "assistant", "content": full_content}}
            ],
            "usage": usage,
        }
