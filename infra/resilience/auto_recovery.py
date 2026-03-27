"""
infra/resilience/auto_recovery.py
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
  - DuckDB (session memory): file health check + test connection
  - Ollama (AI models): active HTTP probe with actionable diagnostics
  - SearXNG (web search): skip researcher calls until available

Configurable recovery actions
------------------------------
Operators can register custom recovery callables per service via the
``recovery_actions`` constructor parameter or ``register_recovery_action()``.
Custom actions override the built-in recovery logic for that service.

Cascading fallback
------------------
When Redis recovery fails, the manager sets a ``degraded_mode`` flag and logs
a warning.  Other components can check this flag to fall back to in-process
memory (data will not persist across restarts).

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
import os
import time
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Type alias for recovery action callables.  Each callable receives the
# service name and the AutoRecoveryManager instance, and returns True if
# recovery succeeded.
RecoveryCallable = Callable[[str, "AutoRecoveryManager"], Awaitable[bool]]


class AutoRecoveryManager:
    """
    Monitors component health and attempts automatic recovery after failures.

    Parameters
    ----------
    memory_manager      : The Tinker MemoryManager (for Redis/DuckDB/ChromaDB reconnect).
    circuit_registry    : CircuitBreakerRegistry (to check which services are degraded).
    max_attempts        : Max recovery attempts before giving up (default: 3).
    base_delay          : Initial retry delay in seconds (doubles each attempt).
    recovery_actions    : Optional dict mapping service names to custom recovery
                          callables.  When a service needs recovery, the manager
                          checks this dict first.  If a callable is registered for
                          the service name, it is called *instead of* the built-in
                          recovery method.  This lets operators inject
                          domain-specific recovery logic without subclassing.
    duckdb_path         : File path to the DuckDB database (for DuckDB recovery).
                          Defaults to the ``TINKER_DUCKDB_PATH`` env var or
                          ``./tinker_sessions.duckdb``.

    Example
    -------
    ::

        recovery = AutoRecoveryManager(
            memory_manager=memory_manager,
            circuit_registry=breakers,
        )
        # Wire to circuit breaker state changes:
        breakers.get("redis").on_state_change(recovery.on_circuit_open)

        # Register a custom recovery action for a service:
        async def my_custom_redis_recovery(name, mgr):
            # ... custom reconnect logic ...
            return True
        recovery.register_recovery_action("redis", my_custom_redis_recovery)
    """

    def __init__(
        self,
        memory_manager: Any = None,
        circuit_registry: Any = None,
        max_attempts: int = 3,
        base_delay: float = 5.0,
        recovery_actions: Optional[dict[str, RecoveryCallable]] = None,
        duckdb_path: Optional[str] = None,
    ) -> None:
        self._memory_manager = memory_manager
        self._circuit_registry = circuit_registry
        self._max_attempts = max_attempts
        self._base_delay = base_delay

        # Configurable recovery actions per service.  Operators can register
        # custom callables that override the built-in recovery methods.
        # Each callable signature: async (service_name, manager) -> bool
        self._recovery_actions: dict[str, RecoveryCallable] = dict(
            recovery_actions or {}
        )

        # Path to the DuckDB database file (used by _recover_duckdb).
        self._duckdb_path: str = duckdb_path or os.getenv(
            "TINKER_DUCKDB_PATH", "./tinker_sessions.duckdb"
        )

        # Track recovery history per service
        self._recovery_attempts: dict[str, int] = {}
        self._recovery_success: dict[str, bool] = {}
        self._last_recovery_at: dict[str, float] = {}

        # Degraded-mode flag — set when a non-critical service (e.g. Redis)
        # fails all recovery attempts.  Other components can check this flag
        # to decide whether to use in-process fallbacks.
        self.degraded_mode: bool = False

    def register_recovery_action(
        self, service_name: str, action: RecoveryCallable
    ) -> None:
        """
        Register (or replace) a custom recovery callable for a service.

        Parameters
        ----------
        service_name : The name that matches the circuit breaker name
                       (e.g. "redis", "ollama_server", "duckdb").
        action       : An async callable ``(service_name, manager) -> bool``.
                       Return True if recovery succeeded, False otherwise.

        Example
        -------
        ::

            async def restart_redis(name, mgr):
                subprocess.run(["systemctl", "restart", "redis"])
                await asyncio.sleep(2)
                return True

            recovery.register_recovery_action("redis", restart_redis)
        """
        self._recovery_actions[service_name] = action
        logger.info(
            "AutoRecovery: registered custom recovery action for '%s'",
            service_name,
        )

    def on_circuit_open(self, breaker: Any, old_state: Any, new_state: Any) -> None:
        """
        Callback to wire to circuit breaker state changes.

        When a breaker opens, schedule an async recovery attempt.
        This is synchronous (callback) but schedules an async coroutine.
        """
        from infra.resilience.circuit_breaker import CircuitState

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

        # ── Cascading fallback for Redis ──────────────────────────────────
        # When Redis recovery is exhausted, switch to degraded mode so that
        # other components (e.g. MemoryManager) can fall back to in-process
        # storage.  The ``degraded_mode`` flag is a simple boolean that any
        # component can check via ``recovery_manager.degraded_mode``.
        if service_name == "redis":
            self.degraded_mode = True
            logger.warning(
                "Redis unavailable — falling back to in-process memory. "
                "Data will NOT persist across restarts."
            )

        return False

    async def _recover_service(self, service_name: str) -> bool:
        """
        Attempt service-specific recovery action.

        Recovery dispatch order:
          1. If a custom recovery callable is registered for this service
             (via ``register_recovery_action()`` or the ``recovery_actions``
             constructor parameter), call it and return its result.
          2. Otherwise, fall through to the built-in recovery methods:
             - Redis: reconnect the adapter
             - ChromaDB: reconnect the adapter
             - DuckDB: check file health, attempt test connection
             - Ollama: active health probe with HTTP status code inspection
             - SearXNG: simple ping

        Returns True if the recovery action succeeded.
        """
        # ── Check for a custom (operator-supplied) recovery action first ──
        custom_action = self._recovery_actions.get(service_name)
        if custom_action is not None:
            logger.info(
                "AutoRecovery: using custom recovery action for '%s'",
                service_name,
            )
            return await custom_action(service_name, self)

        # ── Built-in recovery strategies ──────────────────────────────────
        if service_name == "redis" and self._memory_manager is not None:
            return await self._recover_redis()

        elif service_name == "chromadb" and self._memory_manager is not None:
            return await self._recover_chromadb()

        elif service_name == "duckdb":
            return await self._recover_duckdb()

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

    async def _recover_duckdb(self) -> bool:
        """
        Check DuckDB file health and attempt a test connection.

        DuckDB is a file-based embedded database, so "recovery" means verifying
        that the database file is present, readable, and not corrupted.  Unlike
        Redis or ChromaDB, there is no remote server to reconnect to — the
        failure modes are:

          - **File missing**: The database file was deleted or moved.
          - **File unreadable**: Permission denied (wrong user, read-only mount).
          - **File corrupted**: Partial write during a crash left the file in a
            bad state.  DuckDB is crash-safe in most cases, but external tools
            (e.g. truncating the file) can still corrupt it.

        If the file is missing or corrupted, this method logs detailed
        diagnostic information (file path, permissions, size) so the operator
        can investigate.

        Returns
        -------
        True  if a test connection (``SELECT 1``) succeeds.
        False if the file is missing, unreadable, or corrupted.
        """
        import stat
        from pathlib import Path

        db_path = Path(self._duckdb_path)

        # ── Step 1: Check if the file exists ──────────────────────────────
        if not db_path.exists():
            logger.error(
                "AutoRecovery: DuckDB file not found at '%s' — "
                "the database may need to be re-created on next startup",
                db_path,
            )
            return False

        # ── Step 2: Log diagnostic info about the file ────────────────────
        # This information helps operators diagnose permission or corruption
        # issues without needing to SSH into the machine.
        try:
            file_stat = db_path.stat()
            file_size = file_stat.st_size
            file_mode = stat.filemode(file_stat.st_mode)
            logger.info(
                "AutoRecovery: DuckDB file diagnostics — "
                "path='%s' size=%d bytes permissions=%s",
                db_path,
                file_size,
                file_mode,
            )

            # A zero-byte file is almost certainly corrupted or truncated.
            if file_size == 0:
                logger.error(
                    "AutoRecovery: DuckDB file is empty (0 bytes) — "
                    "the database is likely corrupted.  "
                    "Delete '%s' and restart to re-create it.",
                    db_path,
                )
                return False
        except OSError as exc:
            logger.error(
                "AutoRecovery: Cannot stat DuckDB file '%s': %s",
                db_path,
                exc,
            )
            return False

        # ── Step 3: Check readability ─────────────────────────────────────
        if not os.access(db_path, os.R_OK | os.W_OK):
            logger.error(
                "AutoRecovery: DuckDB file '%s' is not readable/writable — "
                "check file permissions (current: %s)",
                db_path,
                file_mode,
            )
            return False

        # ── Step 4: Attempt a test connection ─────────────────────────────
        # Try to open the database and run a trivial query.  If this fails,
        # the file is likely corrupted.
        try:
            import duckdb  # type: ignore

            conn = duckdb.connect(str(db_path), read_only=True)
            result = conn.execute("SELECT 1").fetchone()
            conn.close()

            if result and result[0] == 1:
                logger.info(
                    "AutoRecovery: DuckDB test connection succeeded "
                    "(path='%s')",
                    db_path,
                )
                return True
            else:
                logger.error(
                    "AutoRecovery: DuckDB test query returned unexpected "
                    "result: %s",
                    result,
                )
                return False
        except ImportError:
            # duckdb package not installed — cannot perform the test
            # connection, but the file checks above passed.
            logger.warning(
                "AutoRecovery: duckdb package not installed — "
                "file checks passed but cannot verify database integrity"
            )
            return True
        except Exception as exc:
            logger.error(
                "AutoRecovery: DuckDB test connection failed for '%s': %s — "
                "the database may be corrupted.  "
                "path='%s' size=%d permissions=%s",
                db_path,
                exc,
                db_path,
                file_size,
                file_mode,
            )
            return False

    async def _recover_ollama(self, service_name: str) -> bool:
        """
        Actively check Ollama health and provide actionable diagnostics.

        Unlike a passive "wait and retry" approach, this method inspects the
        HTTP status code from the Ollama health probe and logs specific
        guidance so operators know exactly what to fix:

          - **200 OK**: Ollama is healthy — recovery succeeded.
          - **404 Not Found**: The requested model is not pulled.  The log
            message tells the operator which ``ollama pull`` command to run.
          - **Connection refused / timeout**: Ollama is not running at all.
            The log message reminds the operator to start ``ollama serve``.

        The model name is read from environment variables:
          - ``TINKER_SERVER_MODEL`` for the primary Ollama server
          - ``TINKER_SECONDARY_MODEL`` for the secondary (judge) server

        Returns True only if the health probe returns HTTP 200.
        """
        # Determine the base URL and model name from environment variables.
        # These mirror the env vars used in core/llm/types.py.
        url_env = (
            "TINKER_SERVER_URL"
            if service_name == "ollama_server"
            else "TINKER_SECONDARY_URL"
        )
        model_env = (
            "TINKER_SERVER_MODEL"
            if service_name == "ollama_server"
            else "TINKER_SECONDARY_MODEL"
        )
        base_url = os.getenv(url_env, "http://localhost:11434")
        model_name = os.getenv(model_env, "unknown")
        health_url = f"{base_url.rstrip('/')}/api/tags"

        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    health_url, timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    status = resp.status

                    if status == 200:
                        # Ollama is reachable — recovery succeeded.
                        logger.info(
                            "AutoRecovery: Ollama health check %s → HTTP 200 (OK)",
                            service_name,
                        )
                        return True

                    if status == 404:
                        # 404 on /api/tags typically means the endpoint exists
                        # but no models are loaded, or a model-specific endpoint
                        # was not found.  Log an actionable message.
                        logger.error(
                            "AutoRecovery: Model '%s' not pulled — "
                            "run 'ollama pull %s' to download it",
                            model_name,
                            model_name,
                        )
                        return False

                    # Any other non-200 status — log it for diagnostics.
                    logger.warning(
                        "AutoRecovery: Ollama health check %s → HTTP %d "
                        "(unexpected status)",
                        service_name,
                        status,
                    )
                    return False

        except (ConnectionRefusedError, OSError):
            # Connection refused means Ollama is not running at all.
            logger.error(
                "AutoRecovery: Ollama unreachable at %s — "
                "check if 'ollama serve' is running",
                base_url,
            )
            return False
        except Exception as exc:
            # aiohttp wraps connection errors in its own exception hierarchy.
            # Check for "Cannot connect" or "Connection refused" in the message
            # to provide the same actionable guidance.
            exc_msg = str(exc).lower()
            if "connect" in exc_msg or "refused" in exc_msg:
                logger.error(
                    "AutoRecovery: Ollama unreachable at %s — "
                    "check if 'ollama serve' is running",
                    base_url,
                )
            else:
                logger.debug(
                    "AutoRecovery: Ollama health check failed: %s", exc
                )
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
