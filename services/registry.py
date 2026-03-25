"""
services/registry.py
====================

In-process service registry for discovering and managing services.

Current implementation: an in-memory dict backed by a simple lock.

Future implementation: swap the backing store for a real service-discovery
system (consul, etcd, k8s endpoints) by subclassing and overriding
``_store_*`` / ``_lookup_*`` methods without changing any call sites.

Usage
-----
::

    registry = ServiceRegistry()

    # Register services at startup:
    registry.register("orchestrator", orchestrator_service)
    registry.register("grub",         grub_service)
    registry.register("fritz",        fritz_service)

    # Look up a service:
    orch = registry.get("orchestrator")

    # Aggregate health check:
    report = await registry.health_report()
    # → {"orchestrator": {"status": "ok", ...}, "grub": {...}, ...}
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .protocol import ServiceInterface

logger = logging.getLogger(__name__)


class ServiceRegistry:
    """In-process registry for ``ServiceInterface`` implementations.

    Thread-safe: all mutations are protected by ``_lock``.
    """

    def __init__(self) -> None:
        self._services: dict[str, ServiceInterface] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, name: str, service: ServiceInterface) -> None:
        """Register a service.

        Parameters
        ----------
        name    : Stable identifier (must be unique within this registry).
        service : Any object implementing ``ServiceInterface``.

        Raises
        ------
        TypeError  : If ``service`` does not implement ``ServiceInterface``.
        ValueError : If ``name`` is already registered.
        """
        if not isinstance(service, ServiceInterface):
            raise TypeError(
                f"{service!r} does not implement ServiceInterface.  "
                "It must have .name, .start(), .stop(), and .health() methods."
            )
        if name in self._services:
            raise ValueError(
                f"Service {name!r} is already registered.  "
                "Call unregister() first if you want to replace it."
            )
        self._services[name] = service
        logger.debug("ServiceRegistry: registered %r", name)

    def unregister(self, name: str) -> bool:
        """Remove a service from the registry.

        Returns True if the service was found and removed, False otherwise.
        """
        removed = self._services.pop(name, None)
        if removed is not None:
            logger.debug("ServiceRegistry: unregistered %r", name)
            return True
        return False

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, name: str) -> ServiceInterface:
        """Return the registered service for ``name``.

        Raises
        ------
        KeyError : If no service is registered under ``name``.
        """
        try:
            return self._services[name]
        except KeyError:
            raise KeyError(
                f"No service registered as {name!r}.  "
                f"Known services: {list(self._services)}"
            )

    def get_or_none(self, name: str) -> ServiceInterface | None:
        """Return the service, or None if not registered."""
        return self._services.get(name)

    def all_names(self) -> list[str]:
        """Return the names of all registered services."""
        return list(self._services)

    def __contains__(self, name: str) -> bool:
        return name in self._services

    def __len__(self) -> int:
        return len(self._services)

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    async def start_all(self) -> None:
        """Call ``start()`` on every registered service concurrently."""
        await asyncio.gather(
            *[svc.start() for svc in self._services.values()],
            return_exceptions=True,
        )
        logger.info("ServiceRegistry: all services started")

    async def stop_all(self) -> None:
        """Call ``stop()`` on every registered service concurrently."""
        await asyncio.gather(
            *[svc.stop() for svc in self._services.values()],
            return_exceptions=True,
        )
        logger.info("ServiceRegistry: all services stopped")

    # ------------------------------------------------------------------
    # Health aggregation
    # ------------------------------------------------------------------

    async def health_report(self) -> dict[str, Any]:
        """Query every service's health endpoint and aggregate results.

        Returns
        -------
        dict[str, dict]
            ``{service_name: health_dict, ...}``

        A failing health check is captured rather than propagated so the
        report always covers all services.
        """
        results: dict[str, Any] = {}
        for name, svc in self._services.items():
            try:
                results[name] = await svc.health()
            except Exception as exc:  # noqa: BLE001
                results[name] = {"status": "down", "error": str(exc)}
                logger.warning("Health check failed for service %r: %s", name, exc)

        overall = (
            "ok"
            if all(r.get("status") == "ok" for r in results.values())
            else "degraded"
        )
        return {"overall": overall, "services": results}
