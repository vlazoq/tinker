"""
infra/resilience/idempotency.py
==========================

Idempotency key cache for Tinker loop operations.

What is idempotency?
---------------------
An idempotent operation produces the same result whether it runs once or many
times.  For Tinker, this matters when:
  - A micro loop crashes after storing the artifact but before marking the
    task complete — the next attempt would create a duplicate artifact.
  - A meso synthesis starts, crashes, and the orchestrator retries it.
  - A task completion is retried due to a transient Redis failure.

By assigning each operation a unique idempotency key and caching the result,
we can detect retries and return the cached result instead of re-executing.

How it works
------------
Before executing an operation, the caller:
  1. Generates a key: ``key = idempotency_key("complete_task", task_id=t)``
  2. Checks the cache: ``cached = await cache.get(key)``
  3. If cached, returns the cached result immediately.
  4. If not cached, runs the operation and stores the result:
     ``await cache.set(key, result, ttl=3600)``

Storage
-------
Keys are stored in Redis (if available) with a configurable TTL.  If Redis is
not available, an in-process dict is used as a fallback (non-distributed, but
prevents duplicate work within a single process run).

Usage
------
::

    cache = IdempotencyCache(redis_url="redis://localhost:6379", ttl=3600)

    # Generate a deterministic key for an operation:
    key = idempotency_key("store_artifact", task_id=task["id"], micro_iter=iteration)

    # Check for existing result:
    cached = await cache.get(key)
    if cached is not None:
        logger.info("Returning cached result for %s", key[:16])
        return cached

    # Execute and cache:
    result = await store_artifact(...)
    await cache.set(key, result, ttl=3600)
    return result
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def idempotency_key(operation: str, **params: Any) -> str:
    """
    Generate a deterministic idempotency key from an operation name and parameters.

    The key is a SHA-256 hash of the operation name and all parameters,
    ensuring it's unique per (operation, inputs) combination and short enough
    to use as a Redis key.

    Parameters
    ----------
    operation : Name of the operation (e.g. "store_artifact", "complete_task").
    **params  : Key-value pairs that uniquely identify this specific invocation.

    Returns
    -------
    str : A 16-character hex prefix of the SHA-256 hash, prefixed with the
          operation name for human readability.
          Format: "store_artifact:a3f9b2c1d5e7f8a0"

    Example
    -------
    ::

        key = idempotency_key("complete_task", task_id="abc123", artifact_id="xyz789")
        # Returns: "complete_task:8f3a..."
    """
    # Sort params for deterministic ordering
    serialised = json.dumps({**params}, sort_keys=True, default=str)
    digest = hashlib.sha256(f"{operation}:{serialised}".encode()).hexdigest()
    # Use first 16 hex chars (64-bit entropy) — collision risk is negligible
    return f"{operation}:{digest[:16]}"


class IdempotencyCache:
    """
    Redis-backed cache for deduplicating retried operations.

    Falls back to an in-memory dict if Redis is unavailable.  In-memory
    mode only prevents duplicates within the current process session, not
    across multiple Tinker instances.

    Parameters
    ----------
    redis_url       : Redis connection URL.
    default_ttl     : How long to keep results cached (seconds).  Default: 1 hour.
                      Shorter TTLs free up memory faster; longer TTLs protect
                      against very slow retries.
    key_prefix      : Redis key prefix (default: "tinker:idem:").
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        default_ttl: int = 3600,
        key_prefix: str = "tinker:idem:",
    ) -> None:
        self._redis_url = redis_url
        self._default_ttl = default_ttl
        self._key_prefix = key_prefix
        self._client = None
        self._memory: dict[str, str] = {}  # fallback in-memory cache
        self._redis_available: bool | None = None

    async def get(self, key: str) -> Any | None:
        """
        Retrieve a previously cached result.

        Returns the cached value (deserialized from JSON) or None if not found.

        Parameters
        ----------
        key : Idempotency key (from ``idempotency_key()``).

        Returns
        -------
        The cached value, or None if there's no cached entry for this key.
        """
        client = await self._get_client()
        full_key = self._key_prefix + key

        if client is not None:
            try:
                raw = await client.get(full_key)
                if raw is None:
                    return None
                return json.loads(raw)
            except Exception as exc:
                logger.debug("IdempotencyCache.get Redis error: %s", exc)

        # Fallback to in-memory
        raw = self._memory.get(full_key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    async def set(self, key: str, value: Any, ttl: int | None = None) -> bool:
        """
        Cache the result of a completed operation.

        Parameters
        ----------
        key   : Idempotency key.
        value : The result to cache (must be JSON-serialisable).
        ttl   : Override the default TTL (in seconds).

        Returns
        -------
        True if the value was cached, False on error.
        """
        effective_ttl = ttl or self._default_ttl
        full_key = self._key_prefix + key

        try:
            serialised = json.dumps(value, default=str)
        except (TypeError, ValueError) as exc:
            logger.warning("IdempotencyCache.set: value not JSON-serialisable: %s", exc)
            serialised = json.dumps(str(value))

        client = await self._get_client()
        if client is not None:
            try:
                await client.set(full_key, serialised, ex=effective_ttl)
                logger.debug("Cached idempotency key '%s' (ttl=%ds)", key[:20], effective_ttl)
                return True
            except Exception as exc:
                logger.debug("IdempotencyCache.set Redis error (falling back): %s", exc)

        # Fallback: in-memory (no TTL enforcement, but prevents same-session duplicates)
        self._memory[full_key] = serialised
        # Bound memory usage: keep at most 10000 keys in memory
        if len(self._memory) > 10000:
            oldest = next(iter(self._memory))
            del self._memory[oldest]
        return True

    async def exists(self, key: str) -> bool:
        """
        Check if a key exists in the cache (faster than ``get`` for existence checks).

        Returns True if the key exists, False otherwise.
        """
        client = await self._get_client()
        full_key = self._key_prefix + key

        if client is not None:
            try:
                return bool(await client.exists(full_key))
            except Exception:
                pass

        return full_key in self._memory

    async def invalidate(self, key: str) -> bool:
        """
        Remove a cached key (e.g. if you want to force re-execution).

        Returns True if the key was found and deleted.
        """
        full_key = self._key_prefix + key
        client = await self._get_client()
        deleted_redis = False

        if client is not None:
            with contextlib.suppress(Exception):
                deleted_redis = bool(await client.delete(full_key))

        deleted_memory = self._memory.pop(full_key, None) is not None
        return deleted_redis or deleted_memory

    async def close(self) -> None:
        """Close the Redis connection."""
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()
            self._client = None

    async def _get_client(self):
        """Return a live Redis client, or None if unavailable."""
        if self._client is not None:
            return self._client
        if self._redis_available is False:
            return None

        try:
            import redis.asyncio as aioredis  # type: ignore

            client = aioredis.from_url(
                self._redis_url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=3,
            )
            await client.ping()
            self._client = client
            self._redis_available = True
            return client
        except ImportError:
            self._redis_available = False
            return None
        except Exception as exc:
            if self._redis_available is None:
                logger.info("IdempotencyCache: Redis unavailable (%s) — in-memory fallback", exc)
            self._redis_available = False
            return None
