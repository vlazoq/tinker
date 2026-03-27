"""
exceptions.py — Tinker's canonical typed exception hierarchy.

Every exception raised by Tinker inherits from ``TinkerError``.
This single file is the authoritative reference for the complete error surface
of the system.

Design goals
------------
1. **Catch-all convenience** — ``except TinkerError`` handles anything Tinker
   raises without catching unrelated Python exceptions.

2. **Targeted handling** — callers can catch the exact subclass they care about,
   e.g. ``except ModelConnectionError`` to retry only connection failures.

3. **Retryability signal** — ``exc.retryable`` tells the caller whether the
   operation is worth retrying without inspecting error message strings.

4. **Structured context** — ``exc.context`` carries a typed dict of key/value
   diagnostics (task id, attempt number, URL, …) that is included in log
   records and alert payloads without string parsing.

5. **No builtin shadowing** — none of the exception names shadow Python
   builtins (``ConnectionError``, ``TimeoutError``, ``MemoryError``, …).

Usage
-----
Raise::

    from exceptions import ModelConnectionError
    raise ModelConnectionError("Cannot reach Ollama", context={"url": url})

Catch broadly::

    from exceptions import TinkerError
    try:
        ...
    except TinkerError as exc:
        logger.error("tinker error [retryable=%s]: %s", exc.retryable, exc)

Catch narrowly::

    from exceptions import ModelConnectionError, ModelTimeoutError
    try:
        ...
    except (ModelConnectionError, ModelTimeoutError) as exc:
        schedule_retry()

Retryability guard::

    from exceptions import TinkerError
    try:
        ...
    except TinkerError as exc:
        if exc.retryable:
            schedule_retry(exc)
        else:
            raise

Module ownership
----------------
Each subsystem re-exports its own exceptions from this file.  Prefer importing
from ``exceptions`` rather than from the individual module to keep imports
stable when implementation files are reorganised.
"""

from __future__ import annotations

import uuid
from typing import Any


# ===========================================================================
# Base
# ===========================================================================


class TinkerError(Exception):
    """
    Root of the Tinker exception hierarchy.

    All exceptions raised by Tinker (whether from the orchestrator, LLM
    client, memory stores, or enterprise layers) inherit from this class.

    Parameters
    ----------
    message : str
        Human-readable description of the error.
    context : dict[str, Any], optional
        Structured key/value diagnostics.  Include whatever a developer or
        on-call engineer would need to diagnose the problem:
        task IDs, URLs, attempt numbers, model names, etc.
        Logged automatically by the observability layer.
    retryable : bool, optional
        Override the class-level ``retryable`` default for this instance.
        Useful when the same exception class can be either retryable or not
        depending on the specific failure.

    Attributes
    ----------
    retryable : bool
        ``True`` if the operation that raised this error is worth retrying
        (e.g. a transient connection failure).  ``False`` for permanent
        failures (e.g. invalid input, parse error).
    context : dict[str, Any]
        Structured diagnostics attached to this error instance.
    """

    #: Class-level default — subclasses override this.
    retryable: bool = False

    def __init__(
        self,
        message: str = "",
        *,
        context: dict[str, Any] | None = None,
        retryable: bool | None = None,
        trace_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.context: dict[str, Any] = context or {}
        # Allow per-instance override of the class-level default
        if retryable is not None:
            self.retryable = retryable  # type: ignore[assignment]
        # Correlation ID for distributed tracing.  When an exception crosses
        # a process boundary (e.g. a task fails on a Grub worker and the
        # exception is re-raised on the Tinker side) the same trace_id
        # allows log aggregators (Loki, CloudWatch, Datadog) to correlate
        # all log lines belonging to the same top-level operation.
        #
        # Callers can inject a trace_id they received from an upstream request
        # (e.g. an X-Trace-ID HTTP header).  When none is provided a random
        # UUID is generated so every exception is always traceable.
        self.trace_id: str = trace_id or str(uuid.uuid4())
        self.context.setdefault("trace_id", self.trace_id)

    def __str__(self) -> str:
        base = super().__str__()
        if self.context:
            ctx_str = ", ".join(f"{k}={v!r}" for k, v in self.context.items())
            return f"{base} [{ctx_str}]"
        return base


# ===========================================================================
# LLM / Model Client
# ===========================================================================


class LLMError(TinkerError):
    """Base for all errors from the LLM and prompt-building subsystems."""


class ModelClientError(LLMError):
    """
    Base for all errors raised by ``OllamaClient`` / the model HTTP layer.

    Catch this class to handle any model-client failure in one place.
    Catch a more specific subclass to handle only one kind of failure.
    """


class ModelConnectionError(ModelClientError):
    """
    The client cannot establish a TCP connection to the Ollama server.

    Common causes: Ollama is not running, wrong base URL, firewall rules.
    This error is retryable — the server may come back up shortly.
    """

    retryable = True


class ModelTimeoutError(ModelClientError):
    """
    A request or connection attempt exceeded the configured timeout.

    Retryable — the server may be under temporary heavy load.
    """

    retryable = True


class ModelRateLimitError(ModelClientError):
    """
    The server responded with HTTP 429 ("Too Many Requests").

    Retryable — the retry logic backs off and tries again after a delay.
    """

    retryable = True


class ModelServerError(ModelClientError):
    """
    The server responded with a 5xx status code (500, 502, 503, …).

    Retryable — the server may recover quickly.
    """

    retryable = True


class ModelNotFoundError(ModelClientError):
    """
    The requested model is not available in Ollama.

    This usually means the model has not been pulled yet.  Run
    ``ollama pull <model>`` to download it.

    Not retryable — the model won't appear by itself.
    """

    retryable = False


class ResponseParseError(ModelClientError):
    """
    The server's response was not valid JSON or did not match the expected
    schema.

    Not retryable — retrying will produce the same malformed response.
    """

    retryable = False


class ModelRouterError(LLMError):
    """
    The ``ModelRouter`` is in an invalid state (e.g. not started, no machines
    registered, or an unexpected response structure from Ollama).

    Not retryable — configuration or lifecycle issue.
    """

    retryable = False


class PromptBuilderError(LLMError):
    """
    The ``PromptBuilder`` could not assemble a prompt.

    Causes: missing template, conflicting variants, incomplete context dict.
    Not retryable — these are programmer / configuration errors.
    """

    retryable = False


# ===========================================================================
# Orchestrator
# ===========================================================================


class OrchestratorError(TinkerError):
    """Base for all errors raised by the orchestrator and its loops."""


class MicroLoopError(OrchestratorError):
    """
    A micro loop iteration could not complete successfully.

    This is the "expected" operational failure class for the micro loop.
    The orchestrator catches it, increments the consecutive-failure counter,
    and decides whether to back off.

    Retryable by default — the next iteration may succeed.
    """

    retryable = True


class ConfigurationError(OrchestratorError):
    """
    A configuration value is missing, out of range, or the wrong type.

    Not retryable — operator intervention is required.
    """

    retryable = False


# ===========================================================================
# Memory
# ===========================================================================


class MemoryStoreError(TinkerError):
    """
    A memory backend (Redis, DuckDB, ChromaDB, SQLite) encountered an error.

    Retryable by default — storage backends often recover from transient
    connection or lock errors.
    """

    retryable = True


# ===========================================================================
# Tasks
# ===========================================================================


class TaskError(TinkerError):
    """Base for task management errors."""


class DependencyCycleError(TaskError):
    """
    A circular dependency was detected in the task graph.

    Raised by ``DependencyResolver.topological_order()`` when the graph
    cannot be fully ordered.  Not retryable — the cycle must be resolved
    in the task data.
    """

    retryable = False


# ===========================================================================
# Resilience
# ===========================================================================


class ResilienceError(TinkerError):
    """Base for resilience-layer errors (circuit breakers, rate limiters, …)."""


class CircuitBreakerOpenError(ResilienceError):
    """
    A call was attempted on an OPEN circuit breaker.

    The service is considered unavailable.  Callers should apply graceful
    degradation rather than propagating this as a fatal error.

    After ``recovery_at`` (monotonic seconds) the breaker enters HALF_OPEN
    and allows one probe through.

    Retryable — but only after the recovery window has elapsed.
    """

    retryable = True

    def __init__(self, name: str, recovery_at: float) -> None:
        import time

        self.name = name
        self.recovery_at = recovery_at
        remaining = max(0.0, recovery_at - time.monotonic())
        super().__init__(
            f"Circuit '{name}' is OPEN — service unavailable. "
            f"Recovery probe in {remaining:.1f}s.",
            context={"circuit": name, "recovery_in_seconds": round(remaining, 1)},
        )


# ===========================================================================
# Tool Layer
# ===========================================================================


class ToolError(TinkerError):
    """
    Base for errors raised by the Tool Layer (web search, scraping, diagrams,
    shell execution, …).

    Retryable by default — many tool failures (network, process timeout) are
    transient.
    """

    retryable = True


class ToolNotFoundError(ToolError):
    """A tool with the requested name is not registered."""

    retryable = False


# ===========================================================================
# Grub (Implementation Agent)
# ===========================================================================


class GrubError(TinkerError):
    """Base for all errors raised by the Grub implementation agent."""

    retryable = True


class MinionExecutionError(GrubError):
    """
    A Grub minion failed during execution.

    Carries the minion name and task ID for easy diagnosis in logs.
    Retryable — minion failures are often transient (model timeout, etc.).
    """

    retryable = True


class SyntaxCheckError(GrubError):
    """
    Code generated by a minion failed a syntax check (e.g. ``py_compile``).

    Not retryable by default — the same prompt will likely produce the same
    broken syntax.  The pipeline should route to the Debugger minion instead.
    """

    retryable = False


class MinionTimeoutError(GrubError):
    """
    A minion's LLM call exceeded the configured timeout.

    Retryable — model may have been under temporary load.
    """

    retryable = True


# ===========================================================================
# Context Assembly
# ===========================================================================


class ContextError(TinkerError):
    """
    The Context Assembler could not build a prompt context for an agent.

    Not retryable — context assembly failures indicate a data or
    configuration problem that must be resolved.
    """

    retryable = False


# ===========================================================================
# Architecture State
# ===========================================================================


class ArchitectureError(TinkerError):
    """
    The Architecture Manager encountered an invalid state transition,
    missing snapshot, or diagram render failure.
    """

    retryable = False


# ===========================================================================
# Validation
# ===========================================================================


class ValidationError(TinkerError, ValueError):
    """
    Input failed validation (type, length, content policy).

    Also inherits from ``ValueError`` so code that catches ``ValueError``
    continues to work without changes (backwards compatibility).

    Not retryable — validation failures reflect bad input, not transient
    infrastructure issues.

    Parameters
    ----------
    field  : str  — the name of the field that failed validation
    value  : Any  — the offending value (truncated in __str__ if large)
    reason : str  — human-readable explanation of why validation failed
    """

    retryable = False

    def __init__(
        self,
        field: str,
        value: Any,
        reason: str,
        *,
        trace_id: str | None = None,
    ) -> None:
        self.field = field
        self.value = value
        self.reason = reason
        super().__init__(
            f"Validation failed for '{field}': {reason}",
            context={"field": field, "reason": reason},
            trace_id=trace_id,
        )


# ===========================================================================
# Experiments / A/B Testing
# ===========================================================================


class ExperimentError(TinkerError):
    """
    An A/B experiment is misconfigured or referenced by a name that does not
    exist.

    Not retryable — experiment configuration errors require operator action.
    """

    retryable = False


# ===========================================================================
# Convenience re-exports
#
# Import everything from this module rather than hunting through individual
# sub-packages.  Stable surface: names here will not move.
# ===========================================================================

__all__ = [
    # Base
    "TinkerError",
    # LLM
    "LLMError",
    "ModelClientError",
    "ModelConnectionError",
    "ModelTimeoutError",
    "ModelRateLimitError",
    "ModelServerError",
    "ModelNotFoundError",
    "ResponseParseError",
    "ModelRouterError",
    "PromptBuilderError",
    # Orchestrator
    "OrchestratorError",
    "MicroLoopError",
    "ConfigurationError",
    # Memory
    "MemoryStoreError",
    # Tasks
    "TaskError",
    "DependencyCycleError",
    # Resilience
    "ResilienceError",
    "CircuitBreakerOpenError",
    # Tools
    "ToolError",
    "ToolNotFoundError",
    # Grub
    "GrubError",
    "MinionExecutionError",
    "SyntaxCheckError",
    "MinionTimeoutError",
    # Context
    "ContextError",
    # Architecture
    "ArchitectureError",
    # Validation
    "ValidationError",
    # Experiments
    "ExperimentError",
]
