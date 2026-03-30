"""
tinker_platform/features/flags.py
==================

Feature flag system for Tinker.

What are feature flags?
------------------------
Feature flags (also called feature toggles) let you enable or disable
functionality without deploying new code.  For Tinker, this means:
  - Safely rolling out new loop logic with a kill-switch
  - Disabling researcher calls if SearXNG is down
  - Disabling meso synthesis during load testing
  - A/B testing different stagnation strategies
  - Emergency disabling of expensive operations

Flag sources (in priority order)
---------------------------------
  1. Environment variables (highest priority — for manual overrides)
  2. File-based config (tinker_flags.json — for dynamic reloading)
  3. In-memory defaults (lowest priority — hardcoded safe defaults)

Usage
------
::

    flags = FeatureFlags()

    # Check a flag:
    if flags.is_enabled("researcher_calls"):
        enriched, n = await _route_researcher(...)

    if flags.is_enabled("meso_synthesis"):
        await _run_meso(subsystem)

    # Override programmatically (for tests or emergency disable):
    flags.set("researcher_calls", False)
    flags.set("meso_synthesis",   False)

    # Register a callback for dynamic reloads:
    flags.on_change("researcher_calls", lambda k, v: logger.info("Flag %s = %s", k, v))

    # Get all flags and their values:
    print(flags.all())
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

# Default flag values — safe conservative defaults
_DEFAULTS: dict[str, bool] = {
    # Core loop components
    "researcher_calls": True,  # Enable Architect knowledge gap research
    "researcher_calls_enabled": True,
    "meso_synthesis": True,  # Enable subsystem-level synthesis
    "meso_synthesis_enabled": True,
    "macro_synthesis": True,  # Enable architectural snapshot commits
    "stagnation_detection": True,  # Enable anti-stagnation monitor
    "context_assembly": True,  # Enable prior context fetching
    # Resilience features
    "circuit_breakers": True,  # Enable circuit breakers for external services
    "circuit_breakers_enabled": True,
    "distributed_locking": True,  # Enable Redis distributed locks
    "idempotency_cache": True,  # Enable idempotency key caching
    "rate_limiting": True,  # Enable AI call rate limiting
    "rate_limiting_enabled": True,
    "backpressure": True,  # Enable queue backpressure
    # Observability features
    "structured_logging": True,  # Enable JSON structured logging
    "tracing": True,  # Enable span tracing
    "audit_log": True,  # Enable immutable audit log
    "sla_tracking": True,  # Enable SLA measurement
    "health_endpoints": True,  # Enable HTTP health server
    # Alerting
    "slack_alerts": True,  # Enable Slack alerting
    "webhook_alerts": True,  # Enable webhook alerting
    # Storage operations
    "auto_backup": False,  # Auto-backup (disabled by default — manual trigger)
    "memory_compression": True,  # Enable automatic memory compression
    # Experimental (off by default)
    "ab_testing": False,  # A/B prompt variant testing
    "lineage_tracking": False,  # Data lineage graph tracking
}


class FeatureFlags:
    """
    Feature flag registry that reads from env vars, a JSON file, and defaults.

    Environment variable convention: ``TINKER_FLAG_{KEY}`` where KEY is the
    flag name in uppercase.  Example: ``TINKER_FLAG_RESEARCHER_CALLS=false``
    disables researcher calls.

    Parameters
    ----------
    config_file  : Optional path to a JSON file with flag overrides.
                   Reloaded every ``reload_interval`` seconds.
    reload_interval: How often to check the config file for changes (seconds).
                     Default: 30. Set to 0 to disable file watching.
    """

    def __init__(
        self,
        config_file: str | None = None,
        reload_interval: float = 30.0,
    ) -> None:
        self._defaults = dict(_DEFAULTS)
        self._overrides: dict[str, bool] = {}
        self._file_flags: dict[str, bool] = {}
        self._config_file = Path(config_file) if config_file else None
        self._reload_interval = reload_interval
        self._last_reload: float = 0.0
        self._callbacks: dict[str, list[Callable]] = {}

        # Load file flags at startup
        self._load_file_flags()

    def is_enabled(self, flag: str) -> bool:
        """
        Check whether a feature flag is enabled.

        Checks (in order):
          1. Environment variable ``TINKER_FLAG_{FLAG_UPPER}``
          2. In-memory overrides (set via ``set()``)
          3. File-based flags (from config_file)
          4. Built-in defaults

        Parameters
        ----------
        flag : Flag name (case-insensitive).

        Returns
        -------
        bool : True if the flag is enabled.
        """
        flag_lower = flag.lower()

        # Maybe reload file
        self._maybe_reload()

        # 1. Environment variable override
        env_key = f"TINKER_FLAG_{flag.upper()}"
        env_val = os.getenv(env_key)
        if env_val is not None:
            return env_val.lower() not in ("false", "0", "no", "off", "disabled")

        # 2. In-memory override
        if flag_lower in self._overrides:
            return self._overrides[flag_lower]

        # 3. File-based flags
        if flag_lower in self._file_flags:
            return self._file_flags[flag_lower]

        # 4. Default
        return self._defaults.get(flag_lower, False)

    def set(self, flag: str, enabled: bool) -> None:
        """
        Override a flag in memory.

        This override takes precedence over file-based flags but NOT over
        environment variables.

        Parameters
        ----------
        flag    : Flag name.
        enabled : New value.
        """
        flag_lower = flag.lower()
        old_val = self.is_enabled(flag_lower)
        self._overrides[flag_lower] = enabled

        if old_val != enabled:
            logger.info("Feature flag '%s' changed: %s → %s", flag_lower, old_val, enabled)
            self._notify_callbacks(flag_lower, enabled)

    def on_change(self, flag: str, callback: Callable[[str, bool], None]) -> None:
        """
        Register a callback that fires whenever a flag's value changes.

        Parameters
        ----------
        flag     : Flag name to watch.
        callback : Callable(flag_name: str, new_value: bool).
        """
        flag_lower = flag.lower()
        if flag_lower not in self._callbacks:
            self._callbacks[flag_lower] = []
        self._callbacks[flag_lower].append(callback)

    def all(self) -> dict[str, bool]:
        """Return all flags and their current values."""
        all_flags = dict(self._defaults)
        all_flags.update(self._file_flags)
        all_flags.update(self._overrides)

        # Apply env var overrides
        for key in list(all_flags.keys()):
            env_key = f"TINKER_FLAG_{key.upper()}"
            env_val = os.getenv(env_key)
            if env_val is not None:
                all_flags[key] = env_val.lower() not in (
                    "false",
                    "0",
                    "no",
                    "off",
                    "disabled",
                )

        return all_flags

    def reset_overrides(self) -> None:
        """Clear all in-memory overrides, reverting to file/env/defaults."""
        self._overrides.clear()
        logger.info("Feature flag overrides cleared")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_file_flags(self) -> None:
        """Load flags from the JSON config file."""
        if not self._config_file or not self._config_file.exists():
            return
        try:
            raw = json.loads(self._config_file.read_text())
            if isinstance(raw, dict):
                self._file_flags = {k.lower(): bool(v) for k, v in raw.items()}
                logger.debug(
                    "Loaded %d feature flags from %s",
                    len(self._file_flags),
                    self._config_file,
                )
        except Exception as exc:
            logger.warning("Could not load feature flags from '%s': %s", self._config_file, exc)

    def _maybe_reload(self) -> None:
        """Reload the config file if enough time has elapsed."""
        if not self._config_file or self._reload_interval <= 0:
            return
        now = time.monotonic()
        if now - self._last_reload >= self._reload_interval:
            self._last_reload = now
            old_flags = dict(self._file_flags)
            self._load_file_flags()
            # Notify callbacks for changed flags
            for key, new_val in self._file_flags.items():
                if old_flags.get(key) != new_val:
                    self._notify_callbacks(key, new_val)

    def _notify_callbacks(self, flag: str, new_value: bool) -> None:
        """Call all registered callbacks for a flag change."""
        for cb in self._callbacks.get(flag, []):
            try:
                cb(flag, new_value)
            except Exception as exc:
                logger.warning("Feature flag callback raised: %s", exc)


# Module-level default instance
default_flags = FeatureFlags(
    config_file=os.getenv("TINKER_FLAGS_FILE"),
    reload_interval=float(os.getenv("TINKER_FLAGS_RELOAD_INTERVAL", "30")),
)
