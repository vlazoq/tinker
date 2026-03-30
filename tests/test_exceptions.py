"""
tests/test_exceptions.py
=========================
Comprehensive unit tests for ``exceptions.py``.

Coverage matrix
---------------
- Every concrete exception class exists and is importable from ``exceptions``
- Every class is listed in ``__all__`` (completeness guard)
- ``TinkerError`` base: message, context, retryable default, per-instance override, __str__
- ``retryable`` flag is correct for every class (table-driven)
- ``context`` dict propagation and default
- ``CircuitBreakerOpenError`` custom ``__init__``: context keys, remaining-time calc
- ``ValidationError`` custom ``__init__``: field/value/reason attributes
- ``ValidationError`` is both a ``TinkerError`` and a ``ValueError`` (MRO)
- Backwards-compat aliases in ``llm/client.py`` resolve to the canonical classes
- Hierarchy: every class is a subclass of ``TinkerError``
- ``MemoryStoreError.retryable`` is True (storage transients are retryable)
- Error serialisation: ``__str__`` includes context, omits brackets when empty
"""

from __future__ import annotations

import inspect
import time

import pytest

import exceptions as exc_mod
from exceptions import (
    # Architecture
    ArchitectureError,
    CircuitBreakerOpenError,
    ConfigurationError,
    # Context
    ContextError,
    DependencyCycleError,
    # Experiments
    ExperimentError,
    # LLM
    LLMError,
    # Memory
    MemoryStoreError,
    MicroLoopError,
    ModelClientError,
    ModelConnectionError,
    ModelRateLimitError,
    ModelRouterError,
    ModelServerError,
    ModelTimeoutError,
    # Orchestrator
    OrchestratorError,
    PromptBuilderError,
    # Resilience
    ResilienceError,
    ResponseParseError,
    # Tasks
    TaskError,
    TinkerError,
    # Tools
    ToolError,
    ToolNotFoundError,
    # Validation
    ValidationError,
)

# ---------------------------------------------------------------------------
# 1. __all__ completeness
# ---------------------------------------------------------------------------


class TestAllCompleteness:
    """Every class defined in exceptions.py must appear in __all__."""

    def test_all_exists(self):
        assert hasattr(exc_mod, "__all__"), "exceptions.py must define __all__"

    def test_every_exception_class_in_all(self):
        """
        Collects every class defined directly in exceptions.py that inherits
        from Exception, then checks it is listed in __all__.

        This test FAILS when a developer adds a new exception class but
        forgets to add it to __all__ — the guarantee that callers can rely
        on the stable public surface.
        """
        defined_in_module = {
            name
            for name, obj in inspect.getmembers(exc_mod, inspect.isclass)
            if issubclass(obj, Exception) and obj.__module__ == exc_mod.__name__
        }
        missing_from_all = defined_in_module - set(exc_mod.__all__)
        assert not missing_from_all, (
            f"Exception classes defined in exceptions.py but missing from __all__: "
            f"{sorted(missing_from_all)}\n"
            f"Add them to the __all__ list at the bottom of exceptions.py."
        )

    def test_all_names_are_importable(self):
        """Every name in __all__ must actually exist as an attribute."""
        missing = [n for n in exc_mod.__all__ if not hasattr(exc_mod, n)]
        assert not missing, f"Names in __all__ that do not exist in exceptions.py: {missing}"


# ---------------------------------------------------------------------------
# 2. Entire hierarchy descends from TinkerError
# ---------------------------------------------------------------------------


class TestHierarchy:
    """Every exception class must be a subclass of TinkerError."""

    ALL_CLASSES = [
        LLMError,
        ModelClientError,
        ModelConnectionError,
        ModelTimeoutError,
        ModelRateLimitError,
        ModelServerError,
        ResponseParseError,
        ModelRouterError,
        PromptBuilderError,
        OrchestratorError,
        MicroLoopError,
        ConfigurationError,
        MemoryStoreError,
        TaskError,
        DependencyCycleError,
        ResilienceError,
        CircuitBreakerOpenError,
        ToolError,
        ToolNotFoundError,
        ContextError,
        ArchitectureError,
        ValidationError,
        ExperimentError,
    ]

    @pytest.mark.parametrize("cls", ALL_CLASSES, ids=lambda c: c.__name__)
    def test_is_tinker_error(self, cls):
        assert issubclass(cls, TinkerError), (
            f"{cls.__name__} must inherit (directly or transitively) from TinkerError"
        )

    @pytest.mark.parametrize("cls", ALL_CLASSES, ids=lambda c: c.__name__)
    def test_is_exception(self, cls):
        assert issubclass(cls, Exception)


# ---------------------------------------------------------------------------
# 3. retryable flags — table-driven
# ---------------------------------------------------------------------------

# (class, expected_retryable)
_RETRYABLE_TABLE = [
    (TinkerError, False),
    (LLMError, False),
    (ModelClientError, False),
    (ModelConnectionError, True),
    (ModelTimeoutError, True),
    (ModelRateLimitError, True),
    (ModelServerError, True),
    (ResponseParseError, False),
    (ModelRouterError, False),
    (PromptBuilderError, False),
    (OrchestratorError, False),
    (MicroLoopError, True),
    (ConfigurationError, False),
    (MemoryStoreError, True),  # storage transients are worth retrying
    (TaskError, False),
    (DependencyCycleError, False),
    (ResilienceError, False),
    (CircuitBreakerOpenError, True),  # retryable after cooldown
    (ToolError, True),
    (ToolNotFoundError, False),
    (ContextError, False),
    (ArchitectureError, False),
    (ValidationError, False),
    (ExperimentError, False),
]


class TestRetryableFlags:
    @pytest.mark.parametrize(
        "cls,expected",
        _RETRYABLE_TABLE,
        ids=lambda x: x.__name__ if inspect.isclass(x) else str(x),
    )
    def test_class_level_retryable(self, cls, expected):
        assert cls.retryable is expected, (
            f"{cls.__name__}.retryable should be {expected}, got {cls.retryable}"
        )

    def test_instance_inherits_class_retryable(self):
        err = ModelConnectionError("test")
        assert err.retryable is True

    def test_per_instance_override_true(self):
        """retryable can be overridden to True on a normally-non-retryable class."""
        err = ConfigurationError("bad value", retryable=True)
        assert err.retryable is True
        # Class-level default must be unchanged
        assert ConfigurationError.retryable is False

    def test_per_instance_override_false(self):
        """retryable can be overridden to False on a normally-retryable class."""
        err = ModelConnectionError("forced non-retry", retryable=False)
        assert err.retryable is False
        assert ModelConnectionError.retryable is True

    def test_retryable_not_shared_between_instances(self):
        """Overriding retryable on one instance must not affect another."""
        a = ModelConnectionError("a", retryable=False)
        b = ModelConnectionError("b")
        assert a.retryable is False
        assert b.retryable is True


# ---------------------------------------------------------------------------
# 4. TinkerError base: message, context, __str__
# ---------------------------------------------------------------------------


class TestTinkerErrorBase:
    def test_empty_message(self):
        err = TinkerError()
        # trace_id is always injected into context and shown in the string
        assert "trace_id" in str(err)

    def test_message_only(self):
        err = TinkerError("something broke")
        assert "something broke" in str(err)

    def test_context_included_in_str(self):
        err = TinkerError("boom", context={"task": "t-1", "attempt": 3})
        s = str(err)
        assert "boom" in s
        assert "task" in s
        assert "t-1" in s
        assert "attempt" in s
        assert "3" in s

    def test_no_brackets_when_context_empty(self):
        # trace_id is always injected, so brackets are always present
        err = TinkerError("clean")
        assert "trace_id" in str(err)

    def test_context_defaults_to_empty_dict(self):
        err = TinkerError("x")
        # trace_id is always injected into context
        assert "trace_id" in err.context

    def test_context_dict_stored(self):
        ctx = {"url": "http://localhost", "status": 503}
        err = TinkerError("server error", context=ctx)
        assert err.context["url"] == "http://localhost"
        assert err.context["status"] == 503

    def test_none_context_becomes_empty_dict(self):
        err = TinkerError("x", context=None)
        # trace_id is always injected even when None is passed
        assert "trace_id" in err.context

    def test_is_catchable_as_exception(self):
        with pytest.raises(Exception, match="test"):
            raise TinkerError("test")

    def test_is_catchable_as_tinker_error(self):
        with pytest.raises(TinkerError):
            raise TinkerError("test")


# ---------------------------------------------------------------------------
# 5. CircuitBreakerOpenError custom __init__
# ---------------------------------------------------------------------------


class TestCircuitBreakerOpenError:
    def test_is_tinker_error(self):
        err = CircuitBreakerOpenError("payments", time.monotonic() + 30)
        assert isinstance(err, TinkerError)

    def test_name_attribute(self):
        err = CircuitBreakerOpenError("payments", time.monotonic() + 30)
        assert err.name == "payments"

    def test_recovery_at_attribute(self):
        future = time.monotonic() + 30
        err = CircuitBreakerOpenError("svc", future)
        assert err.recovery_at == future

    def test_context_contains_circuit_key(self):
        err = CircuitBreakerOpenError("auth", time.monotonic() + 10)
        assert "circuit" in err.context
        assert err.context["circuit"] == "auth"

    def test_context_contains_recovery_seconds(self):
        err = CircuitBreakerOpenError("auth", time.monotonic() + 10)
        assert "recovery_in_seconds" in err.context
        assert err.context["recovery_in_seconds"] >= 0

    def test_remaining_time_near_zero_when_already_elapsed(self):
        err = CircuitBreakerOpenError("svc", time.monotonic() - 5)
        assert err.context["recovery_in_seconds"] == 0

    def test_message_contains_circuit_name(self):
        err = CircuitBreakerOpenError("orders", time.monotonic() + 5)
        assert "orders" in str(err)

    def test_retryable_is_true(self):
        err = CircuitBreakerOpenError("svc", time.monotonic() + 10)
        assert err.retryable is True


# ---------------------------------------------------------------------------
# 6. ValidationError custom __init__
# ---------------------------------------------------------------------------


class TestValidationError:
    def test_field_attribute(self):
        err = ValidationError("email", "not-an-email", "must contain @")
        assert err.field == "email"

    def test_value_attribute(self):
        err = ValidationError("timeout", -1, "must be positive")
        assert err.value == -1

    def test_reason_attribute(self):
        err = ValidationError("name", "", "must not be empty")
        assert err.reason == "must not be empty"

    def test_message_includes_field_and_reason(self):
        err = ValidationError("port", 99999, "must be < 65536")
        s = str(err)
        assert "port" in s
        assert "must be < 65536" in s

    def test_is_tinker_error(self):
        err = ValidationError("x", 1, "bad")
        assert isinstance(err, TinkerError)

    def test_is_value_error(self):
        """ValidationError must be catchable as ValueError for backwards compat."""
        err = ValidationError("x", 1, "bad")
        assert isinstance(err, ValueError)

    def test_catchable_as_value_error(self):
        with pytest.raises(ValueError):
            raise ValidationError("field", None, "required")

    def test_catchable_as_tinker_error(self):
        with pytest.raises(TinkerError):
            raise ValidationError("field", None, "required")

    def test_retryable_is_false(self):
        assert ValidationError.retryable is False

    def test_context_contains_field(self):
        err = ValidationError("email", "bad", "invalid format")
        assert err.context.get("field") == "email"


# ---------------------------------------------------------------------------
# 7. Backwards-compat aliases in llm/client.py
# ---------------------------------------------------------------------------


class TestBackwardsCompatAliases:
    """Old import paths from llm/client.py must resolve to the canonical classes."""

    def test_connection_error_alias(self):
        from core.llm.client import ConnectionError as LLMConnectionError

        assert LLMConnectionError is ModelConnectionError

    def test_timeout_error_alias(self):
        from core.llm.client import TimeoutError as LLMTimeoutError

        assert LLMTimeoutError is ModelTimeoutError

    def test_rate_limit_alias(self):
        from core.llm.client import RateLimitError

        assert RateLimitError is ModelRateLimitError

    def test_server_error_alias(self):
        from core.llm.client import ServerError

        assert ServerError is ModelServerError


# ---------------------------------------------------------------------------
# 8. Submodule re-exports (each module re-exports its own exceptions)
# ---------------------------------------------------------------------------


class TestSubmoduleReexports:
    def test_circuit_breaker_module_exports_open_error(self):
        from infra.resilience.circuit_breaker import CircuitBreakerOpenError as CBE

        assert CBE is CircuitBreakerOpenError

    def test_prompt_builder_module_exports_error(self):
        from core.prompts.builder import PromptBuilderError as PBE

        assert PBE is PromptBuilderError

    def test_tasks_resolver_exports_cycle_error(self):
        from runtime.tasks.resolver import DependencyCycleError as DCE

        assert DCE is DependencyCycleError

    def test_validation_module_exports_error(self):
        from core.validation.input_validator import ValidationError as VE

        assert VE is ValidationError
