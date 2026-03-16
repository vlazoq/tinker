# Chapter 09 — Resilience

## The Problem

Tinker runs 24/7.  Things will go wrong:
- The AI model is temporarily overloaded
- Redis times out
- A web search returns an error
- The same failing task gets retried forever

Without resilience patterns, one bad component can bring down the whole
system.  With them, failures are isolated and the orchestrator keeps running.

---

## The Architecture Decision

We add four resilience patterns.  Each solves a specific failure mode:

| Pattern | Failure it prevents |
|---------|---------------------|
| **Circuit Breaker** | Hammering a service that is clearly down |
| **Rate Limiter** | Sending too many requests too fast |
| **Dead Letter Queue (DLQ)** | Losing track of operations that failed permanently |
| **Distributed Lock** | Two processes doing the same work simultaneously |

Each is optional (controlled by feature flags) and degrades gracefully
if not needed.

---

## Step 1 — Directory Structure

```
tinker/
  resilience/
    __init__.py
    circuit_breaker.py
    rate_limiter.py
    dlq.py
    distributed_lock.py
```

---

## Step 2 — Circuit Breaker

A circuit breaker works like an electrical circuit breaker in your home.
When failures reach a threshold, the circuit "opens" — all requests fail
immediately instead of waiting for timeouts.  After a cooldown period, the
circuit "half-opens" and lets one test request through.  If it succeeds,
the circuit "closes" (normal operation resumes).

```
CLOSED  → normal operation, count failures
  │ too many failures
  ▼
OPEN    → fail fast, don't even try (return error immediately)
  │ cooldown period elapsed
  ▼
HALF_OPEN → let one test through
  │ success              │ failure
  ▼                      ▼
CLOSED (reset)         OPEN (reset cooldown)
```

```python
# tinker/resilience/circuit_breaker.py

from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    CLOSED    = "closed"      # normal, counting failures
    OPEN      = "open"        # failing fast
    HALF_OPEN = "half_open"   # testing recovery


class CircuitBreakerOpen(Exception):
    """Raised when a call is rejected because the circuit is open."""


class CircuitBreaker:
    """
    Tracks success/failure rates for a named service and opens the
    circuit when too many failures occur.
    """

    def __init__(
        self,
        name:              str,
        failure_threshold: int   = 5,
        recovery_timeout:  float = 60.0,
        half_open_calls:   int   = 1,
    ) -> None:
        self.name               = name
        self._threshold         = failure_threshold
        self._recovery_timeout  = recovery_timeout
        self._half_open_max     = half_open_calls

        self._state            = CircuitState.CLOSED
        self._failure_count    = 0
        self._last_failure_at  = 0.0
        self._half_open_count  = 0

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            # Check if cooldown has elapsed → move to HALF_OPEN
            if time.monotonic() - self._last_failure_at >= self._recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                self._half_open_count = 0
        return self._state

    def record_success(self) -> None:
        """Call this when the protected operation succeeds."""
        if self._state == CircuitState.HALF_OPEN:
            logger.info("Circuit %s: test call succeeded — closing", self.name)
            self._state         = CircuitState.CLOSED
            self._failure_count = 0
        elif self._state == CircuitState.CLOSED:
            # Reset failure count on success (we use a simple window)
            self._failure_count = max(0, self._failure_count - 1)

    def record_failure(self) -> None:
        """Call this when the protected operation fails."""
        self._failure_count  += 1
        self._last_failure_at = time.monotonic()

        if self._state == CircuitState.HALF_OPEN:
            logger.warning("Circuit %s: test call failed — reopening", self.name)
            self._state = CircuitState.OPEN
        elif self._failure_count >= self._threshold:
            logger.warning(
                "Circuit %s: %d failures — opening (will retry in %.0fs)",
                self.name, self._failure_count, self._recovery_timeout,
            )
            self._state = CircuitState.OPEN

    def allow_request(self) -> bool:
        """Return True if a request should be allowed through."""
        s = self.state
        if s == CircuitState.CLOSED:
            return True
        if s == CircuitState.OPEN:
            return False
        # HALF_OPEN: allow only up to half_open_max calls
        if self._half_open_count < self._half_open_max:
            self._half_open_count += 1
            return True
        return False

    async def call(self, func: Callable, *args, **kwargs) -> Any:
        """
        Run func(*args, **kwargs) with circuit breaker protection.

        Usage:
            result = await cb.call(my_async_function, arg1, arg2)
        """
        if not self.allow_request():
            raise CircuitBreakerOpen(
                f"Circuit {self.name!r} is {self._state.value} — "
                f"refusing request to protect the service"
            )
        try:
            result = await func(*args, **kwargs)
            self.record_success()
            return result
        except Exception:
            self.record_failure()
            raise
```

### How to use it

```python
# Protect the Ollama call with a circuit breaker
cb = CircuitBreaker("ollama_primary", failure_threshold=3, recovery_timeout=30.0)

try:
    text, pt, ct = await cb.call(llm.complete, prompt, role="architect")
except CircuitBreakerOpen:
    # Ollama is down — skip this task and try again later
    raise MicroLoopError("Ollama primary circuit is open")
```

---

## Step 3 — Rate Limiter

A rate limiter prevents sending too many requests per second.  We use a
**token bucket** algorithm:

- The bucket holds up to `capacity` tokens
- Tokens are added at `rate` per second
- Each request consumes 1 token
- If the bucket is empty, the request waits until a token arrives

```python
# tinker/resilience/rate_limiter.py

from __future__ import annotations

import asyncio
import time
import logging

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Token bucket rate limiter.

    Usage:
        limiter = RateLimiter(rate=2.0, capacity=5)  # 2 req/s, burst of 5
        async with limiter:
            result = await ai_call()
    """

    def __init__(self, rate: float, capacity: float) -> None:
        self._rate     = rate      # tokens per second
        self._capacity = capacity  # maximum token bucket size
        self._tokens   = capacity  # start full
        self._last_refill = time.monotonic()

    def _refill(self) -> None:
        """Add tokens based on elapsed time."""
        now     = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            self._capacity,
            self._tokens + elapsed * self._rate,
        )
        self._last_refill = now

    async def acquire(self) -> None:
        """Wait until a token is available, then consume it."""
        while True:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            # Calculate how long until the next token arrives
            wait = (1.0 - self._tokens) / self._rate
            logger.debug("Rate limiter waiting %.2fs", wait)
            await asyncio.sleep(wait)

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *_):
        pass
```

---

## Step 4 — Dead Letter Queue

When an operation fails permanently (too many retries), we don't just
drop it — we write it to the Dead Letter Queue (DLQ) so a human can
review it later.

```python
# tinker/resilience/dlq.py

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_CREATE_DLQ_SQL = """
CREATE TABLE IF NOT EXISTS dlq_items (
    id          TEXT PRIMARY KEY,
    operation   TEXT NOT NULL,     -- what failed
    error       TEXT NOT NULL,     -- the error message
    status      TEXT NOT NULL DEFAULT 'pending',
    retry_count INTEGER NOT NULL DEFAULT 0,
    notes       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    resolved_at TEXT
);
"""


class DeadLetterQueue:
    """
    SQLite-backed queue for permanently failed operations.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=5, check_same_thread=False)
        con.row_factory = sqlite3.Row
        return con

    def initialise(self) -> None:
        con = self._connect()
        con.executescript(_CREATE_DLQ_SQL)
        con.commit()
        con.close()

    async def push(self, operation: str, error: str) -> str:
        """Add a failed operation to the queue.  Returns the item ID."""
        item_id = str(uuid.uuid4())
        ts      = datetime.now(timezone.utc).isoformat()

        def _run():
            con = self._connect()
            con.execute(
                "INSERT INTO dlq_items (id, operation, error, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (item_id, operation[:2000], error[:2000], ts, ts)
            )
            con.commit()
            con.close()

        await asyncio.to_thread(_run)
        logger.warning("DLQ: pushed item %s — %s", item_id[:8], operation[:80])
        return item_id

    async def stats(self) -> dict[str, int]:
        """Return {status: count}."""
        def _run():
            con = self._connect()
            rows = con.execute(
                "SELECT status, COUNT(*) n FROM dlq_items GROUP BY status"
            ).fetchall()
            con.close()
            return {r["status"]: r["n"] for r in rows}
        return await asyncio.to_thread(_run)
```

---

## Step 5 — Distributed Lock

When two Tinker processes are running (unusual but possible), we need to
ensure only one of them processes a given task at a time.  We use Redis
as the locking backend — `SETNX` (Set if Not eXists) is atomic.

```python
# tinker/resilience/distributed_lock.py  (simplified)

from __future__ import annotations

import asyncio
import logging
import uuid

logger = logging.getLogger(__name__)


class NullDistributedLock:
    """
    No-op lock used when Redis is unavailable.
    Safe for single-instance deployments (no actual locking needed).
    """
    async def acquire(self, key: str, ttl: int = 30) -> bool:
        return True   # always succeed

    async def release(self, key: str) -> None:
        pass


class DistributedLock:
    """
    Redis-backed distributed lock using SETNX with TTL.

    Usage:
        lock = DistributedLock(redis_url="redis://localhost:6379")
        await lock.connect()

        acquired = await lock.acquire("micro_loop:task_123", ttl=60)
        if acquired:
            try:
                # do the work
                ...
            finally:
                await lock.release("micro_loop:task_123")
    """

    def __init__(self, redis_url: str) -> None:
        self._url    = redis_url
        self._client = None
        self._token  = str(uuid.uuid4())   # unique per lock instance

    async def connect(self) -> None:
        try:
            import redis.asyncio as aioredis
            client = await aioredis.from_url(self._url, decode_responses=True)
            await client.ping()
            self._client = client
        except Exception as exc:
            logger.warning("DistributedLock: Redis unavailable (%s) — using no-op", exc)
            # Fall through — self._client stays None

    async def acquire(self, key: str, ttl: int = 30) -> bool:
        """Try to acquire the lock.  Returns True if acquired."""
        if not self._client:
            return True   # no Redis = no distributed locking needed
        try:
            result = await self._client.set(
                f"lock:{key}", self._token,
                nx=True,    # SET if Not eXists — atomic
                ex=ttl,     # auto-expire after ttl seconds
            )
            return result is not None
        except Exception:
            return True   # fail open (safer than blocking forever)

    async def release(self, key: str) -> None:
        """Release the lock (only if we own it)."""
        if not self._client:
            return
        try:
            # Only delete if we own this lock (compare-and-delete)
            lua = """
            if redis.call('GET', KEYS[1]) == ARGV[1] then
                return redis.call('DEL', KEYS[1])
            else
                return 0
            end
            """
            await self._client.eval(lua, 1, f"lock:{key}", self._token)
        except Exception as exc:
            logger.debug("Lock release failed (not critical): %s", exc)
```

---

## Putting It Together: Wrapping the Micro Loop

Here is how resilience wraps the micro loop in the real orchestrator:

```python
# In Orchestrator._tick()

from resilience.circuit_breaker import CircuitBreaker, CircuitBreakerOpen
from resilience.dlq             import DeadLetterQueue

cb  = CircuitBreaker("llm_primary", failure_threshold=3)
dlq = DeadLetterQueue("tinker_dlq.sqlite")

try:
    if not cb.allow_request():
        raise MicroLoopError("LLM circuit is open")

    result = await run_micro_loop(task, ...)
    cb.record_success()

except MicroLoopError as exc:
    cb.record_failure()
    if task.attempt_count >= 3:
        # Give up — send to DLQ
        await dlq.push(
            operation = f"micro_loop:{task.id} ({task.title[:80]})",
            error     = str(exc),
        )
        await task_engine.mark_failed(task.id)
    else:
        # Try again later
        await task_engine.requeue(task.id)
```

---

## Key Concepts Introduced

| Concept | What it solves |
|---------|---------------|
| Circuit breaker | Stops hammering a failing service |
| Token bucket | Controls request rate |
| DLQ | Surfaces permanent failures for human review |
| Distributed lock | Prevents duplicate work |
| "Fail open" vs "fail closed" | Locks fail open (allow) when Redis is down; circuit breakers fail closed (reject) |

The difference between fail-open and fail-closed is important:
- **Locks fail open** — if we can't acquire the lock (Redis down), we proceed
  anyway.  The risk is duplicate work.  Acceptable for single-instance.
- **Circuits fail closed** — if the circuit is open, we reject the request.
  This protects a struggling service from more load.

---

→ Next: [Chapter 10 — Anti-Stagnation](./10-stagnation.md)
