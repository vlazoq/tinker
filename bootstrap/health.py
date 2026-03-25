"""
bootstrap/health.py
===================

Single responsibility: startup health verification and asyncio error handling.

Extracted from main.py so that health-check logic can be read, tested,
and modified without touching logging or component construction code.

Usage
-----
::

    from bootstrap.health import run_health_check, asyncio_exception_handler
    import asyncio

    await run_health_check()
    asyncio.get_event_loop().set_exception_handler(asyncio_exception_handler)
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger("tinker.bootstrap.health")


def asyncio_exception_handler(
    loop: asyncio.AbstractEventLoop,  # noqa: ARG001
    context: dict,
) -> None:
    """Log exceptions that escape asyncio background Tasks.

    asyncio silently discards exceptions raised inside Tasks that are never
    awaited. This handler surfaces them at ERROR level so they appear in logs.
    """
    exc = context.get("exception")
    msg = context.get("message", "Unknown asyncio error")
    task = context.get("task") or context.get("future")
    task_name = getattr(task, "get_name", lambda: repr(task))()

    if exc is not None:
        logger.exception(
            "Unhandled exception in asyncio task %r: %s",
            task_name,
            msg,
            exc_info=exc,
        )
    else:
        logger.error("asyncio error in task %r: %s", task_name, msg)


async def run_health_check() -> None:
    """Verify that required external services are reachable at startup.

    Logs a clear WARNING for each service that is down so the user gets a
    useful message rather than a cryptic timeout error during the first loop.

    Services checked
    ----------------
    * Ollama (primary model server)
    * Redis  (working memory)
    """
    server_url = os.getenv("TINKER_SERVER_URL", "http://localhost:11434")
    redis_url = os.getenv("TINKER_REDIS_URL", "redis://localhost:6379")

    def _redact_url(url: str) -> str:
        from urllib.parse import urlparse, urlunparse

        try:
            p = urlparse(url)
            if p.password:
                netloc = f"{p.hostname}:{p.port}" if p.port else (p.hostname or "")
                return urlunparse(p._replace(netloc=netloc))
        except Exception:
            pass
        return url

    # ── Ollama ────────────────────────────────────────────────────────────────
    try:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{server_url.rstrip('/')}/api/tags",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    logger.info("Health check OK: Ollama reachable at %s", server_url)
                else:
                    logger.warning(
                        "Health check WARN: Ollama at %s returned HTTP %d",
                        server_url,
                        resp.status,
                    )
    except Exception as exc:
        logger.warning(
            "Health check WARN: Ollama NOT reachable at %s (%s)",
            server_url,
            exc,
        )

    # ── Redis ─────────────────────────────────────────────────────────────────
    try:
        import aioredis  # type: ignore

        client = aioredis.from_url(redis_url, socket_connect_timeout=3)
        await client.ping()
        await client.aclose()
        logger.info("Health check OK: Redis reachable at %s", _redact_url(redis_url))
    except ImportError:
        pass
    except Exception as exc:
        logger.warning(
            "Health check WARN: Redis NOT reachable at %s (%s)",
            _redact_url(redis_url),
            exc,
        )
