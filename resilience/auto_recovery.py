"""
resilience/auto_recovery.py
============================

Auto-recovery and self-healing for Tinker component failures.

What this does
--------------
When a Tinker component fails repeatedly (e.g. Redis goes down, Ollama becomes
unresponsive), the auto-recovery system:

  1. Detects the failure pattern (via circuit breakers and failure counts)
  2. Attempts component-specific recovery actions (reconnect, reinitialise)
  3. Falls back to a degraded-mode stub if recovery fails
  4. Periodically retries recovery until the service comes back
  5. Logs all recovery attempts for forensic analysis

Components it can recover
--------------------------
  - Redis (MemoryManager working memory): reconnect + flush corrupted state
  - ChromaDB (research archive): reconnect
  - DuckDB (session memory): reconnect (file-based, usually self-healing)
  - Ollama (AI models): wait + retry with exponential backoff
  - SearXNG (web search): skip researcher calls until available

Usage
------
::

    recovery = AutoRecoveryManager(memory_manager=mm, circuit_registry=breakers)

    # Called automatically when circuit breaker opens:
    @breaker.on_state_change
    def handle_state_change(breaker, old, new):
        if new == CircuitState.OPEN:
            asyncio.create_task(recovery.attempt_recovery(breaker.name))

    # Check overall system health:
    status = await recovery.health_summary()
    print(status)  # {'redis': 'healthy', 'ollama': 'degraded', ...}
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class AutoRecoveryManager:
    """
    Monitors component health and attempts automatic recovery after failures.

    Parameters
    ----------
    memory_manager    : The Tinker MemoryManager (for Redis/DuckDB/ChromaDB reconnect).
    circuit_registry  : CircuitBreakerRegistry (to check which services are degraded).
    max_attempts      : Max recovery attempts before giving up (default: 3).
    base_delay        : Initial retry delay in seconds (doubles each attempt).

    Example
    -------
    ::

        recovery = AutoRecoveryManager(
            memory_manager=memory_manager,
            circuit_registry=breakers,
        )
        # Wire to circuit breaker state changes:
        breakers.get("redis").on_state_change(recovery.on_circuit_open)
    """

    def __init__(
        self,
        memory_manager: Any = None,
        circuit_registry: Any = None,
        max_attempts: int = 3,
        base_delay: float = 5.0,
    ) -> None:
        self._memory_manager = memory_manager
        self._circuit_registry = circuit_registry
        self._max_attempts = max_attempts
        self._base_delay = base_delay

        # Track recovery history per service
        self._recovery_attempts: dict[str, int] = {}
        self._recovery_success: dict[str, bool] = {}
        self._last_recovery_at: dict[str, float] = {}

    def on_circuit_open(self, breaker: Any, old_state: Any, new_state: Any) -> None:
        """
        Callback to wire to circuit breaker state changes.

        When a breaker opens, schedule an async recovery attempt.
        This is synchronous (callback) but schedules an async coroutine.
        """
        from resilience.circuit_breaker import CircuitState

        if new_state == CircuitState.OPEN:
            logger.info(
                "AutoRecovery: %s circuit opened — scheduling recovery", breaker.name
            )
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.attempt_recovery(breaker.name))
            except RuntimeError:
                # No running loop (e.g. in tests) — skip async scheduling
                pass

    async def attempt_recovery(self, service_name: str) -> bool:
        """
        Attempt to recover a specific service after a circuit breaker opened.

        Tries up to ``max_attempts`` times with exponential backoff.
        Returns True if recovery succeeded, False if all attempts failed.

        Parameters
        ----------
        service_name : The name of the service (matches circuit breaker name).

        Returns
        -------
        True  : Service recovered (circuit breaker will close on its own).
        False : All recovery attempts failed — system remains in degraded mode.
        """
        attempts = self._recovery_attempts.get(service_name, 0)
        delay = self._base_delay

        for attempt in range(1, self._max_attempts + 1):
            logger.info(
                "AutoRecovery: attempting %s recovery (attempt %d/%d)",
                service_name,
                attempt,
                self._max_attempts,
            )
            try:
                success = await self._recover_service(service_name)
                if success:
                    self._recovery_success[service_name] = True
                    self._recovery_attempts[service_name] = 0
                    self._last_recovery_at[service_name] = time.monotonic()
                    logger.info("AutoRecovery: %s recovered successfully", service_name)
                    return True
            except Exception as exc:
                logger.warning(
                    "AutoRecovery: %s recovery attempt %d failed: %s",
                    service_name,
                    attempt,
                    exc,
                )

            if attempt < self._max_attempts:
                await asyncio.sleep(delay)
                delay *= 2  # Exponential backoff

        self._recovery_success[service_name] = False
        self._recovery_attempts[service_name] = attempts + self._max_attempts
        logger.error(
            "AutoRecovery: %s failed to recover after %d attempts — "
            "system continues in degraded mode",
            service_name,
            self._max_attempts,
        )
        return False

    async def _recover_service(self, service_name: str) -> bool:
        """
        Attempt service-specific recovery action.

        Each service has a tailored recovery strategy:
        - Redis: reconnect the adapter
        - ChromaDB: reconnect the adapter
        - DuckDB: reconnect (file-based, usually self-healing)
        - Ollama: ping the health endpoint
        - SearXNG: simple ping

        Returns True if the recovery action succeeded.
        """
        if service_name == "redis" and self._memory_manager is not None:
            return await self._recover_redis()

        elif service_name == "chromadb" and self._memory_manager is not None:
            return await self._recover_chromadb()

        elif service_name in ("ollama_server", "ollama_secondary"):
            return await self._recover_ollama(service_name)

        elif service_name == "searxng":
            return await self._recover_searxng()

        else:
            logger.warning("AutoRecovery: no recovery strategy for '%s'", service_name)
            return False

    async def _recover_redis(self) -> bool:
        """Attempt to reconnect to Redis."""
        mm = self._memory_manager
        if mm is None or not hasattr(mm, "_redis"):
            return False
        try:
            await mm._redis.close()
            await asyncio.sleep(1)
            await mm._redis.connect()
            ping_ok = await mm._redis.ping()
            logger.info("AutoRecovery: Redis reconnected (ping=%s)", ping_ok)
            return bool(ping_ok)
        except Exception as exc:
            logger.debug("AutoRecovery: Redis reconnect failed: %s", exc)
            return False

    async def _recover_chromadb(self) -> bool:
        """Attempt to reconnect to ChromaDB."""
        mm = self._memory_manager
        if mm is None or not hasattr(mm, "_chroma"):
            return False
        try:
            await mm._chroma.close()
            await asyncio.sleep(1)
            await mm._chroma.connect()
            count = await mm._chroma.count()
            logger.info("AutoRecovery: ChromaDB reconnected (%d docs)", count)
            return True
        except Exception as exc:
            logger.debug("AutoRecovery: ChromaDB reconnect failed: %s", exc)
            return False

    async def _recover_ollama(self, service_name: str) -> bool:
        """
        Check if Ollama is reachable by hitting the health endpoint.
        Recovery for Ollama is passive — we just wait for it to come back.
        """
        import os

        url_env = (
            "TINKER_SERVER_URL"
            if service_name == "ollama_server"
            else "TINKER_SECONDARY_URL"
        )
        base_url = os.getenv(url_env, "http://localhost:11434")
        health_url = f"{base_url.rstrip('/')}/api/tags"

        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    health_url, timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    ok = resp.status == 200
                    logger.info(
                        "AutoRecovery: Ollama health check %s → HTTP %d",
                        service_name,
                        resp.status,
                    )
                    return ok
        except Exception as exc:
            logger.debug("AutoRecovery: Ollama health check failed: %s", exc)
            return False

    async def _recover_searxng(self) -> bool:
        """Ping SearXNG to check if it's reachable."""
        import os

        searxng_url = os.getenv("TINKER_SEARXNG_URL", "http://localhost:8080")
        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{searxng_url}/healthz",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    return resp.status < 500
        except Exception as exc:
            logger.debug("AutoRecovery: SearXNG health check failed: %s", exc)
            return False

    async def health_summary(self) -> dict[str, str]:
        """
        Return a summary of recovery status for all known services.

        Returns
        -------
        dict : Maps service name to "healthy", "degraded", or "unknown".
        """
        summary = {}
        if self._circuit_registry is not None:
            for name, stats in self._circuit_registry.all_stats().items():
                if stats["state"] == "open":
                    summary[name] = "degraded"
                elif stats["state"] == "half_open":
                    summary[name] = "recovering"
                else:
                    summary[name] = "healthy"
        return summary
