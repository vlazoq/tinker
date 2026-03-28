"""Startup validator for TinkerSettings.

Performs fast, offline sanity checks on configuration values and returns
human-readable warnings for anything that looks wrong.  No network calls
are made — only value parsing and path existence checks.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

from config.settings import TinkerSettings

logger = logging.getLogger(__name__)

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_VALID_DB_BACKENDS = {"sqlite", "postgres", "duckdb"}
_VALID_SESSION_BACKENDS = {"", "redis", "duckdb", "sqlite"}
_VALID_LLM_BACKENDS = {"ollama", "openai", "litellm"}


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _check_url(url: str, label: str, warnings: list[str]) -> None:
    """Warn if *url* cannot be parsed into a scheme + host."""
    if not url:
        return
    parsed = urlparse(url)
    if not parsed.scheme:
        warnings.append(f"{label}: missing URL scheme (got {url!r})")
    elif parsed.scheme not in ("http", "https", "redis", "rediss"):
        warnings.append(
            f"{label}: unexpected URL scheme {parsed.scheme!r} in {url!r}"
        )
    if not parsed.hostname:
        warnings.append(f"{label}: missing hostname in {url!r}")


def _check_port(port: int, label: str, warnings: list[str]) -> None:
    """Warn if *port* is outside the valid TCP range."""
    if not 1 <= port <= 65535:
        warnings.append(f"{label}: port {port} is outside valid range 1-65535")


def _check_positive(value: int | float, label: str, warnings: list[str]) -> None:
    """Warn if *value* is not positive."""
    if value <= 0:
        warnings.append(f"{label}: expected a positive value, got {value}")


def _check_non_negative(value: int | float, label: str, warnings: list[str]) -> None:
    """Warn if *value* is negative."""
    if value < 0:
        warnings.append(f"{label}: expected a non-negative value, got {value}")


def _check_dir_exists(path: str, label: str, warnings: list[str]) -> None:
    """Warn if *path* is set and the directory does not exist."""
    if path and not os.path.isdir(path):
        warnings.append(f"{label}: directory does not exist: {path!r}")


# ------------------------------------------------------------------
# Per-section validators
# ------------------------------------------------------------------

def _validate_llm(llm, warnings: list[str]) -> None:
    _check_url(llm.server_url, "llm.server_url", warnings)
    _check_url(llm.secondary_url, "llm.secondary_url", warnings)
    _check_positive(llm.server_ctx, "llm.server_ctx", warnings)
    _check_positive(llm.server_max_out, "llm.server_max_out", warnings)
    _check_positive(llm.server_timeout, "llm.server_timeout", warnings)
    _check_positive(llm.secondary_ctx, "llm.secondary_ctx", warnings)
    _check_positive(llm.secondary_max_out, "llm.secondary_max_out", warnings)
    _check_positive(llm.secondary_timeout, "llm.secondary_timeout", warnings)
    if not llm.server_model:
        warnings.append("llm.server_model: no primary model specified")
    if llm.llm_backend and llm.llm_backend not in _VALID_LLM_BACKENDS:
        warnings.append(
            f"llm.llm_backend: unknown backend {llm.llm_backend!r} "
            f"(expected one of {sorted(_VALID_LLM_BACKENDS)})"
        )


def _validate_storage(storage, warnings: list[str]) -> None:
    _check_url(storage.redis_url, "storage.redis_url", warnings)
    _check_positive(storage.redis_ttl, "storage.redis_ttl", warnings)
    _check_port(storage.trino_port, "storage.trino_port", warnings)

    if storage.db_backend and storage.db_backend not in _VALID_DB_BACKENDS:
        warnings.append(
            f"storage.db_backend: unknown backend {storage.db_backend!r} "
            f"(expected one of {sorted(_VALID_DB_BACKENDS)})"
        )

    if storage.db_backend == "postgres" and not storage.postgres_dsn:
        warnings.append(
            "storage: db_backend is 'postgres' but no postgres_dsn provided"
        )

    if storage.session_backend and storage.session_backend not in _VALID_SESSION_BACKENDS:
        warnings.append(
            f"storage.session_backend: unknown backend {storage.session_backend!r} "
            f"(expected one of {sorted(_VALID_SESSION_BACKENDS)})"
        )

    if storage.session_backend == "redis" and not storage.redis_url:
        warnings.append(
            "storage: session_backend is 'redis' but no redis_url provided"
        )


def _validate_paths(paths, warnings: list[str]) -> None:
    _check_dir_exists(paths.workspace, "paths.workspace", warnings)
    _check_dir_exists(paths.artifact_dir, "paths.artifact_dir", warnings)
    _check_dir_exists(paths.diagram_dir, "paths.diagram_dir", warnings)
    _check_dir_exists(paths.backup_dir, "paths.backup_dir", warnings)


def _validate_webui(webui, warnings: list[str]) -> None:
    _check_port(webui.port, "webui.port", warnings)
    _check_port(webui.streamlit_port, "webui.streamlit_port", warnings)
    _check_port(webui.gradio_port, "webui.gradio_port", warnings)
    _check_positive(webui.rate_per_sec, "webui.rate_per_sec", warnings)
    _check_positive(webui.rate_burst, "webui.rate_burst", warnings)


def _validate_orchestrator(orch, warnings: list[str]) -> None:
    _check_positive(orch.macro_interval, "orchestrator.macro_interval", warnings)
    _check_positive(orch.meso_trigger, "orchestrator.meso_trigger", warnings)
    _check_positive(orch.architect_timeout, "orchestrator.architect_timeout", warnings)
    _check_positive(orch.critic_timeout, "orchestrator.critic_timeout", warnings)
    _check_positive(orch.confirm_timeout, "orchestrator.confirm_timeout", warnings)
    if not 0.0 <= orch.temperature <= 2.0:
        warnings.append(
            f"orchestrator.temperature: {orch.temperature} is outside "
            "typical range 0.0-2.0"
        )


def _validate_observability(obs, warnings: list[str]) -> None:
    if obs.log_level.upper() not in _VALID_LOG_LEVELS:
        warnings.append(
            f"observability.log_level: unknown level {obs.log_level!r} "
            f"(expected one of {sorted(_VALID_LOG_LEVELS)})"
        )
    _check_port(obs.metrics_port, "observability.metrics_port", warnings)
    _check_port(obs.health_port, "observability.health_port", warnings)
    _check_positive(obs.tracer_window, "observability.tracer_window", warnings)
    if obs.otlp_endpoint:
        _check_url(obs.otlp_endpoint, "observability.otlp_endpoint", warnings)


def _validate_mcp(mcp, warnings: list[str]) -> None:
    _check_positive(mcp.connect_timeout, "mcp.connect_timeout", warnings)
    _check_positive(mcp.rate_per_sec, "mcp.rate_per_sec", warnings)
    _check_positive(mcp.rate_burst, "mcp.rate_burst", warnings)


def _validate_grub(grub, warnings: list[str]) -> None:
    _check_url(grub.ollama_url, "grub.ollama_url", warnings)
    _check_positive(grub.max_iterations, "grub.max_iterations", warnings)
    _check_positive(grub.request_timeout, "grub.request_timeout", warnings)
    _check_positive(grub.context_max_chars, "grub.context_max_chars", warnings)
    _check_positive(grub.context_target_chars, "grub.context_target_chars", warnings)
    if 0.0 < grub.quality_threshold > 1.0:
        warnings.append(
            f"grub.quality_threshold: {grub.quality_threshold} is outside "
            "expected range 0.0-1.0"
        )
    if grub.context_target_chars > grub.context_max_chars:
        warnings.append(
            f"grub: context_target_chars ({grub.context_target_chars}) exceeds "
            f"context_max_chars ({grub.context_max_chars})"
        )


def _validate_fritz(fritz, warnings: list[str]) -> None:
    _check_port(fritz.metrics_port, "fritz.metrics_port", warnings)


def _validate_backpressure(bp, warnings: list[str]) -> None:
    _check_positive(bp.bp_warn_depth, "backpressure.bp_warn_depth", warnings)
    _check_positive(bp.bp_pause_depth, "backpressure.bp_pause_depth", warnings)
    _check_positive(bp.idempotency_ttl, "backpressure.idempotency_ttl", warnings)
    _check_positive(bp.backup_retention_days, "backpressure.backup_retention_days", warnings)
    if bp.bp_warn_depth >= bp.bp_pause_depth:
        warnings.append(
            f"backpressure: bp_warn_depth ({bp.bp_warn_depth}) should be "
            f"less than bp_pause_depth ({bp.bp_pause_depth})"
        )


def _validate_search(search, warnings: list[str]) -> None:
    _check_url(search.searxng_url, "search.searxng_url", warnings)
    _check_positive(search.scraper_timeout_ms, "search.scraper_timeout_ms", warnings)


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def validate_settings(settings: TinkerSettings) -> list[str]:
    """Return a list of warning strings for questionable config values.

    An empty list means all checks passed.  No network calls are made.
    """
    warnings: list[str] = []

    _validate_llm(settings.llm, warnings)
    _validate_storage(settings.storage, warnings)
    _validate_paths(settings.paths, warnings)
    _validate_webui(settings.webui, warnings)
    _validate_orchestrator(settings.orchestrator, warnings)
    _validate_observability(settings.observability, warnings)
    _validate_mcp(settings.mcp, warnings)
    _validate_grub(settings.grub, warnings)
    _validate_fritz(settings.fritz, warnings)
    _validate_backpressure(settings.backpressure, warnings)
    _validate_search(settings.search, warnings)

    # Cross-section checks
    port_labels: list[tuple[int, str]] = [
        (settings.webui.port, "webui.port"),
        (settings.webui.streamlit_port, "webui.streamlit_port"),
        (settings.webui.gradio_port, "webui.gradio_port"),
        (settings.observability.metrics_port, "observability.metrics_port"),
        (settings.observability.health_port, "observability.health_port"),
        (settings.fritz.metrics_port, "fritz.metrics_port"),
        (settings.storage.trino_port, "storage.trino_port"),
    ]
    seen_ports: dict[int, str] = {}
    for port, label in port_labels:
        if port in seen_ports:
            warnings.append(
                f"port conflict: {label} and {seen_ports[port]} both use "
                f"port {port}"
            )
        else:
            seen_ports[port] = label

    return warnings


def validate_or_warn(settings: TinkerSettings) -> None:
    """Run all validation checks and log each warning.

    This is the intended entry point for startup code — call it right
    after loading settings to surface misconfigurations early.
    """
    warnings = validate_settings(settings)
    for w in warnings:
        logger.warning("config: %s", w)
    if warnings:
        logger.warning(
            "config: %d warning(s) found — review your environment / .env file",
            len(warnings),
        )
