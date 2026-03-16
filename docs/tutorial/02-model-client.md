# Chapter 02 — The Model Client

## The Problem

Tinker needs to talk to AI models.  Everything else — memory, tools,
orchestration — only makes sense if we can send a prompt to an AI and
get a response back.

We are using **Ollama**, a local AI model server.  It exposes a simple
HTTP API: send it a prompt, get back a completion.  We need to build a
Python client that wraps those HTTP calls.

---

## The Architecture Decision

We will build two classes:

1. **`ModelClient`** — talks to *one* Ollama instance.  Just HTTP calls.
2. **`ModelRouter`** — holds two clients (primary + secondary) and routes
   calls to the right one.  The Architect and Synthesizer use the
   primary (big, capable model).  The Critic uses the secondary (smaller,
   faster model).

**Why separate them?**  `ModelClient` has one job and knows nothing about
routing.  `ModelRouter` has one job and knows nothing about HTTP.  Each
class is testable in isolation.  This is the *single responsibility
principle*.

```
Orchestrator
    │
    └── ModelRouter.complete(prompt, role="architect")
              │
              ├── role == "architect"  →  primary client (qwen3:7b)
              ├── role == "critic"     →  secondary client (phi3:mini)
              └── role == "synthesizer"→  primary client (qwen3:7b)
```

---

## Step 1 — Project Structure

Create the directory layout:

```
tinker/
  llm/
    __init__.py
    client.py
    router.py
```

The `__init__.py` file can be empty — its only purpose is to tell Python
that `llm/` is a package (importable module).

---

## Step 2 — The Model Client

```python
# tinker/llm/client.py

"""
ModelClient — async HTTP client for a single Ollama instance.

Ollama's API reference: https://github.com/ollama/ollama/blob/main/docs/api.md
The endpoint we use: POST /api/generate
"""
from __future__ import annotations

import logging
from typing import Any

import httpx   # pip install httpx — async-capable HTTP library

logger = logging.getLogger(__name__)


class ModelClientError(Exception):
    """Raised when the AI model returns an error or is unreachable."""


class ModelClient:
    """
    Async wrapper around a single Ollama /api/generate endpoint.

    Usage:
        client = ModelClient(base_url="http://localhost:11434", model="qwen3:7b")
        await client.start()                      # opens connection pool
        text = await client.complete("Hello!")    # sends one prompt
        await client.close()                      # closes connections
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        context_length: int = 8192,
        max_tokens: int = 2048,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")   # remove trailing slash if present
        self.model = model
        self.context_length = context_length
        self.max_tokens = max_tokens
        self.timeout = timeout
        self._http: httpx.AsyncClient | None = None   # created in start()

    async def start(self) -> None:
        """Open the connection pool.  Call this before the first complete()."""
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(
                connect=10.0,       # fail fast if Ollama isn't running
                read=self.timeout,  # but wait a long time for the completion
            ),
        )
        logger.info("ModelClient connected to %s (model=%s)", self.base_url, self.model)

    async def close(self) -> None:
        """Close the connection pool.  Call this on shutdown."""
        if self._http:
            await self._http.aclose()
            self._http = None

    async def complete(
        self,
        prompt: str,
        system_prompt: str = "",
        temperature: float = 0.7,
    ) -> tuple[str, int, int]:
        """
        Send a prompt to Ollama and return the completion.

        Returns a tuple of (response_text, prompt_tokens, completion_tokens).
        Raises ModelClientError if anything goes wrong.
        """
        if self._http is None:
            raise ModelClientError("Client not started — call await client.start() first")

        # Build the request body Ollama expects
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,    # we want the full response, not streamed tokens
            "options": {
                "num_ctx":    self.context_length,
                "num_predict": self.max_tokens,
                "temperature": temperature,
            },
        }
        if system_prompt:
            body["system"] = system_prompt

        try:
            response = await self._http.post("/api/generate", json=body)
            response.raise_for_status()   # raises if HTTP status is 4xx or 5xx
        except httpx.ConnectError as exc:
            raise ModelClientError(
                f"Cannot reach Ollama at {self.base_url} — is it running?"
            ) from exc
        except httpx.TimeoutException as exc:
            raise ModelClientError(
                f"Ollama timed out after {self.timeout}s — model too slow or prompt too long"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise ModelClientError(
                f"Ollama returned HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc

        data = response.json()
        text = data.get("response", "")
        prompt_tokens     = data.get("prompt_eval_count", 0)
        completion_tokens = data.get("eval_count", 0)

        logger.debug(
            "ModelClient: %d prompt tokens, %d completion tokens",
            prompt_tokens, completion_tokens,
        )
        return text, prompt_tokens, completion_tokens

    async def ping(self) -> bool:
        """Return True if Ollama is reachable, False otherwise."""
        if self._http is None:
            return False
        try:
            r = await self._http.get("/api/tags", timeout=3.0)
            return r.status_code == 200
        except Exception:
            return False
```

### What just happened?

- `start()` creates an `httpx.AsyncClient` — a connection pool to Ollama.
  We create it once and reuse it for all calls (faster than reconnecting each time).
- `complete()` builds the JSON body Ollama expects and POSTs it.
- The `try/except` block converts generic HTTP errors into our own
  `ModelClientError` with helpful messages.
- The function returns three values: the text, and both token counts.
  Token counts let us monitor costs.

---

## Step 3 — The Model Router

```python
# tinker/llm/router.py

"""
ModelRouter — routes AI calls to the appropriate model.

The primary model (large, capable) handles Architect and Synthesizer roles.
The secondary model (small, fast) handles the Critic role.
"""
from __future__ import annotations

import logging
from typing import Literal

from .client import ModelClient, ModelClientError

logger = logging.getLogger(__name__)

# The four roles that can make AI calls
Role = Literal["architect", "critic", "synthesizer", "researcher"]


class ModelRouter:
    """
    Routes completion requests to primary or secondary ModelClient.

    Inject this into the Orchestrator instead of using ModelClient directly.
    This way, the orchestrator doesn't need to know which model handles
    which role.
    """

    def __init__(
        self,
        primary: ModelClient,
        secondary: ModelClient,
    ) -> None:
        self._primary   = primary
        self._secondary = secondary
        # Which roles go to which client
        self._routing: dict[str, ModelClient] = {
            "architect":   primary,
            "synthesizer": primary,
            "researcher":  primary,
            "critic":      secondary,   # Critic uses the smaller, faster model
        }

    async def start(self) -> None:
        """Open both model clients."""
        await self._primary.start()
        await self._secondary.start()

    async def close(self) -> None:
        """Close both model clients."""
        await self._primary.close()
        await self._secondary.close()

    async def complete(
        self,
        prompt: str,
        role: Role = "architect",
        system_prompt: str = "",
        temperature: float = 0.7,
    ) -> tuple[str, int, int]:
        """
        Route a completion request to the right model.

        Returns (response_text, prompt_tokens, completion_tokens).
        """
        client = self._routing.get(role, self._primary)
        logger.debug("Routing role=%s to %s", role, client.base_url)

        text, pt, ct = await client.complete(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=temperature,
        )
        return text, pt, ct

    async def ping_all(self) -> dict[str, bool]:
        """Return reachability status of both models."""
        return {
            "primary":   await self._primary.ping(),
            "secondary": await self._secondary.ping(),
        }
```

---

## Step 4 — The `__init__.py` Exports

```python
# tinker/llm/__init__.py

"""
llm — Ollama model client and router.
"""
from .client import ModelClient, ModelClientError
from .router import ModelRouter

__all__ = ["ModelClient", "ModelClientError", "ModelRouter"]
```

This lets other modules import cleanly: `from llm import ModelRouter`.

---

## Step 5 — Try It

Create a quick test script at the repo root:

```python
# test_llm.py  (run this to verify your Ollama setup)
import asyncio
from llm import ModelClient

async def main():
    client = ModelClient(
        base_url="http://localhost:11434",
        model="qwen3:7b",
        timeout=30.0,
    )
    await client.start()

    reachable = await client.ping()
    if not reachable:
        print("❌ Ollama is not running. Start it with: ollama serve")
        return

    print("✅ Ollama is reachable")
    text, pt, ct = await client.complete("Say 'hello world' and nothing else.")
    print(f"Response: {text.strip()}")
    print(f"Tokens: {pt} prompt, {ct} completion")

    await client.close()

asyncio.run(main())
```

Run it:
```bash
python test_llm.py
```

Expected output:
```
✅ Ollama is reachable
Response: hello world
Tokens: 14 prompt, 5 completion
```

---

## What We Have So Far

```
tinker/
  llm/
    __init__.py    ✅
    client.py      ✅ — talks to one Ollama instance
    router.py      ✅ — routes between primary and secondary
```

The rest of the system doesn't know whether we're using Ollama, OpenAI, or
a stub.  It calls `router.complete(prompt, role="architect")` and gets back
text.  This abstraction means we can swap the AI backend later without
changing anything else.

---

## Key Concepts Introduced

| Concept | Where | What it means |
|---------|-------|---------------|
| `httpx.AsyncClient` | client.py | Async HTTP connection pool |
| `raise_for_status()` | client.py | Auto-raise on HTTP 4xx/5xx |
| Custom exception class | client.py | `ModelClientError` wraps underlying errors |
| Dependency injection | router.py | Router receives clients, doesn't create them |
| `Literal` type | router.py | Restrict a string to specific values |

---

→ Next: [Chapter 03 — The Memory Manager](./03-memory-manager.md)
