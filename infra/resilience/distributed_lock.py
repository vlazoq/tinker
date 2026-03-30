"""
infra/resilience/distributed_lock.py
================================

Redis-backed distributed locking for Tinker.

Why distributed locking?
------------------------
If multiple Tinker instances share the same Redis, DuckDB, or SQLite databases,
two instances might:
  - Select the same task simultaneously (duplicate work)
  - Run meso synthesis concurrently for the same subsystem (corrupted synthesis)
  - Both try to commit the macro snapshot at the same time (partial writes)

A distributed lock serialises these operations across all instances using Redis
as the arbiter.  The lock is acquired before the critical section, held for at
most ``ttl`` seconds (auto-expiry prevents deadlocks if a process crashes), and
released when done.

This uses the standard Redis SET NX (set-if-not-exists) pattern rather than
Redlock, which is simpler and sufficient for Tinker's use case.  The trade-off:
if Redis itself goes down while a lock is held, the lock is lost.  For Tinker
this is acceptable — a missed lock means duplicate work, not data corruption.

Usage
------
::

    lock = DistributedLock(redis_url="redis://localhost:6379")

    # Acquire a lock with a context manager (recommended):
    async with lock.acquire("task:select") as acquired:
        if not acquired:
            return  # Another instance is doing this — skip
        task = await task_engine.select_task()

    # Or explicitly:
    token = await lock.try_lock("meso:api_gateway", ttl=30)
    if token:
        try:
            await run_meso_synthesis()
        finally:
            await lock.unlock("meso:api_gateway", token)

    # Check if a lock is held (non-destructive):
    is_held = await lock.is_locked("task:select")
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

logger = logging.getLogger(__name__)

# Prefix for all Tinker lock keys in Redis — avoids collisions with other apps.
_KEY_PREFIX = "tinker:lock:"


class DistributedLock:
    """
    Redis-backed distributed lock manager for coordinating multiple Tinker instances.

    Uses Redis SET NX EX (set-if-not-exists with expiry) for atomicity.  Each
    lock stores a random token so only the holder can release it (prevents
    accidental release by another instance).

    Parameters
    ----------
    redis_url : Redis connection URL (e.g. "redis://localhost:6379").
    key_prefix: Optional prefix for all lock keys (default: "tinker:lock:").

    Note
    ----
    The Redis client is created lazily on first use to avoid import errors when
    Redis is not installed.  If Redis is unavailable, all lock operations
    degrade gracefully: ``try_lock`` returns a dummy token (non-None) so the
    caller proceeds as if it holds the lock.  This is safe for single-instance
    deployments where locking is advisory.
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        key_prefix: str = _KEY_PREFIX,
    ) -> None:
        self._redis_url = redis_url
        self._key_prefix = key_prefix
        self._client = None  # Lazy Redis client
        self._available: bool | None = None  # None = unknown, True/False = tested

    # ------------------------------------------------------------------
    # Context manager interface (preferred)
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def acquire(
        self,
        resource: str,
        ttl: int = 30,
        wait: bool = False,
        wait_timeout: float = 10.0,
    ) -> AsyncIterator[bool]:
        """
        Async context manager that acquires ``resource`` and yields True/False.

        Parameters
        ----------
        resource     : Logical name for the resource (e.g. "task:select").
        ttl          : Lock expiry in seconds.  Auto-expires if process dies.
        wait         : If True, poll until the lock is available (up to wait_timeout).
        wait_timeout : Max seconds to wait when ``wait=True``.

        Yields
        ------
        True if the lock was acquired; False if it could not be acquired.

        The lock is always released on exit, even if the body raises.

        Example
        -------
        ::

            async with lock.acquire("task:select") as acquired:
                if not acquired:
                    logger.info("Another instance is selecting — skipping")
                    return
                task = await engine.select_task()
        """
        token: str | None = None
        acquired = False

        if wait:
            deadline = time.monotonic() + wait_timeout
            while time.monotonic() < deadline:
                token = await self.try_lock(resource, ttl)
                if token:
                    acquired = True
                    break
                await asyncio.sleep(0.5)
        else:
            token = await self.try_lock(resource, ttl)
            acquired = token is not None

        try:
            yield acquired
        finally:
            if token and acquired:
                await self.unlock(resource, token)

    # ------------------------------------------------------------------
    # Low-level interface
    # ------------------------------------------------------------------

    async def try_lock(self, resource: str, ttl: int = 30) -> str | None:
        """
        Try to acquire a lock on ``resource`` atomically.

        Returns a random token string if the lock was acquired, or None if
        another instance already holds the lock.

        The token must be passed to ``unlock()`` to release the lock —
        this prevents accidental release of a lock held by another instance.

        Parameters
        ----------
        resource : Logical name for the resource.
        ttl      : Lock expiry in seconds (safety net against crashed processes).

        Returns
        -------
        str  : Random token — keep this to release the lock.
        None : Lock is already held by another instance.
        """
        client = await self._get_client()
        if client is None:
            # Redis unavailable — return a dummy token (advisory-only mode)
            logger.debug("DistributedLock: Redis unavailable — using advisory-only mode")
            return f"advisory:{secrets.token_hex(8)}"

        key = self._key_prefix + resource
        token = secrets.token_hex(16)  # 32-char random hex — hard to guess

        try:
            # SET key token NX EX ttl
            # NX = only set if key doesn't exist (atomic)
            # EX = expire automatically after ttl seconds
            result = await client.set(key, token, nx=True, ex=ttl)
            if result:
                logger.debug("Acquired lock '%s' (ttl=%ds)", resource, ttl)
                return token
            else:
                logger.debug("Lock '%s' already held — skipping", resource)
                return None
        except Exception as exc:
            logger.warning("DistributedLock.try_lock failed for '%s': %s", resource, exc)
            # On Redis error, fall through (advisory mode)
            return f"advisory:{secrets.token_hex(8)}"

    async def unlock(self, resource: str, token: str) -> bool:
        """
        Release a lock that was acquired with ``try_lock``.

        Uses a Lua script to atomically check that the token matches before
        deleting — prevents releasing a lock held by another instance.

        Parameters
        ----------
        resource : Same resource name used in ``try_lock``.
        token    : The token returned by ``try_lock``.

        Returns
        -------
        True  : Lock was successfully released.
        False : Lock had already expired or token didn't match (no-op).
        """
        if token.startswith("advisory:"):
            logger.debug(
                "release_lock: advisory token for '%s' — no Redis release needed", resource
            )
            return True  # Advisory mode — nothing to release

        client = await self._get_client()
        if client is None:
            return True  # Nothing to release

        key = self._key_prefix + resource
        # Lua script: only delete if the stored value matches our token.
        # This is atomic in Redis — prevents race conditions.
        lua_script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        try:
            result = await client.eval(lua_script, 1, key, token)
            if result == 1:
                logger.debug("Released lock '%s'", resource)
                return True
            else:
                logger.debug("Lock '%s' had already expired or token mismatch", resource)
                return False
        except Exception as exc:
            logger.warning("DistributedLock.unlock failed for '%s': %s", resource, exc)
            return False

    async def is_locked(self, resource: str) -> bool:
        """
        Check if a resource is currently locked (non-destructive).

        Useful for health checks or debugging.  Does not acquire the lock.
        """
        client = await self._get_client()
        if client is None:
            return False
        key = self._key_prefix + resource
        try:
            val = await client.get(key)
            return val is not None
        except Exception:
            return False

    async def force_unlock(self, resource: str) -> bool:
        """
        Forcibly delete a lock without checking the token.

        Use only for emergency cleanup.  Under normal circumstances, always
        use ``unlock(resource, token)`` to avoid releasing another instance's lock.
        """
        client = await self._get_client()
        if client is None:
            return False
        key = self._key_prefix + resource
        try:
            result = await client.delete(key)
            logger.warning("Force-unlocked '%s'", resource)
            return bool(result)
        except Exception as exc:
            logger.warning("force_unlock failed for '%s': %s", resource, exc)
            return False

    async def extend(self, resource: str, token: str, ttl: int) -> bool:
        """
        Extend the TTL of an existing lock (lock renewal).

        Use this for long-running critical sections to prevent the lock from
        expiring while still in progress.  Only extends if the token matches
        (the same instance holds the lock).

        Parameters
        ----------
        resource : Resource name.
        token    : Token from the original ``try_lock`` call.
        ttl      : New TTL in seconds from now.

        Returns
        -------
        True if the lock was extended, False if it had already expired.
        """
        if token.startswith("advisory:"):
            return True

        client = await self._get_client()
        if client is None:
            return True

        key = self._key_prefix + resource
        lua_script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("expire", KEYS[1], ARGV[2])
        else
            return 0
        end
        """
        try:
            result = await client.eval(lua_script, 1, key, token, ttl)
            return bool(result)
        except Exception as exc:
            logger.warning("DistributedLock.extend failed for '%s': %s", resource, exc)
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_client(self):
        """
        Return a live Redis async client, or None if Redis is unavailable.

        Caches the client on first successful connection.  If Redis was found
        unavailable on a previous call, retries periodically.
        """
        if self._client is not None:
            return self._client

        # Try to connect
        try:
            import redis.asyncio as aioredis  # type: ignore

            client = aioredis.from_url(
                self._redis_url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=5,
            )
            await client.ping()
            self._client = client
            self._available = True
            logger.info("DistributedLock connected to Redis at %s", self._redis_url)
            return self._client
        except ImportError:
            if self._available is None:
                logger.info("DistributedLock: redis package not installed — advisory-only mode")
            self._available = False
            return None
        except Exception as exc:
            if self._available is not False:
                logger.warning("DistributedLock: Redis unavailable (%s) — advisory-only mode", exc)
            self._available = False
            return None

    async def close(self) -> None:
        """Close the Redis connection. Call during shutdown."""
        if self._client is not None:
            with suppress(Exception):
                await self._client.aclose()
            self._client = None


class NullDistributedLock:
    """
    No-op distributed lock for single-instance or testing deployments.

    Implements the same interface as DistributedLock but always reports
    successful acquisition.  Use this when Redis is not available and you
    are certain only one instance is running.

    Usage
    -----
    ::

        lock = NullDistributedLock()  # Always acquires
        async with lock.acquire("task:select") as acquired:
            assert acquired is True
    """

    @asynccontextmanager
    async def acquire(self, resource: str, ttl: int = 30, **_) -> AsyncIterator[bool]:
        yield True

    async def try_lock(self, resource: str, ttl: int = 30) -> str:
        return f"null:{secrets.token_hex(4)}"

    async def unlock(self, resource: str, token: str) -> bool:
        return True

    async def is_locked(self, resource: str) -> bool:
        return False

    async def force_unlock(self, resource: str) -> bool:
        return True

    async def extend(self, resource: str, token: str, ttl: int) -> bool:
        return True

    async def close(self) -> None:
        pass
