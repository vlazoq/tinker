# Chapter 01b — The Exception Hierarchy

**Read this before Chapter 02.**
This chapter is short, but every subsequent chapter depends on understanding it.

---

## The Problem

Before this chapter existed, Tinker raised a mix of bare Python exceptions:

```python
# 🚫 Hard to catch precisely
raise ValueError("Config field 'timeout' must be a number, got 'fast'")
raise RuntimeError("ModelRouter not started")
raise KeyError("Experiment 'prompt_v2' not found")
```

This made it impossible to:
- Write `except SomeSpecificError` without matching strings
- Know at a glance whether an error was worth retrying
- Attach structured diagnostic context (task id, URL, model name, …) without
  string-parsing
- See the full error surface of the system in one place

---

## The Solution: A Single Typed Hierarchy

All Tinker exceptions live in one file: `exceptions.py` at the project root.
Every exception inherits from `TinkerError`.

```
TinkerError (base)
├── LLMError
│   ├── ModelClientError
│   │   ├── ModelConnectionError   ← TCP failure              (retryable ✓)
│   │   ├── ModelTimeoutError      ← request timed out        (retryable ✓)
│   │   ├── ModelRateLimitError    ← HTTP 429                 (retryable ✓)
│   │   ├── ModelServerError       ← HTTP 5xx                 (retryable ✓)
│   │   └── ResponseParseError     ← bad JSON                 (retryable ✗)
│   ├── ModelRouterError           ← router misconfigured     (retryable ✗)
│   └── PromptBuilderError         ← template / context error (retryable ✗)
├── OrchestratorError
│   ├── MicroLoopError             ← loop iteration failed    (retryable ✓)
│   └── ConfigurationError         ← bad env var / out-of-range value
├── MemoryStoreError               ← Redis/DuckDB/SQLite failure (retryable ✓)
├── TaskError
│   └── DependencyCycleError       ← circular task dependency (retryable ✗)
├── ResilienceError
│   └── CircuitBreakerOpenError    ← circuit is OPEN          (retryable ✓)
├── ToolError                      ← web search / scraping / shell failure
│   └── ToolNotFoundError          ← no tool with that name registered
├── ContextError                   ← context assembly failed  (retryable ✗)
├── ArchitectureError              ← state machine or diagram failure
├── ValidationError                ← bad user input (also inherits ValueError)
└── ExperimentError                ← A/B experiment misconfigured
```

---

## The `retryable` Flag

Every `TinkerError` carries a `retryable: bool` class attribute. Every
concrete exception has a table-driven value verified by `tests/test_exceptions.py`.

```python
# In exceptions.py
class ModelConnectionError(ModelClientError):
    retryable = True   # TCP failures are worth retrying

class ResponseParseError(ModelClientError):
    retryable = False  # Garbage response won't get better on retry
```

You can also override `retryable` per instance when the same exception class
can be either retryable or not depending on the specific failure:

```python
raise ConfigurationError("Computed value out of range", retryable=True)
```

---

## The `context` Dict

Every `TinkerError` instance carries a `context` dict of structured
diagnostics — the information an on-call engineer needs without parsing
log strings.

```python
raise ModelConnectionError(
    "Cannot connect to Ollama",
    context={"url": "http://192.168.1.10:11434", "attempt": 3},
)
```

The `__str__` method includes it automatically:

```
ModelConnectionError: Cannot connect to Ollama [url='http://192.168.1.10:11434', attempt=3]
```

The observability layer picks up `exc.context` and includes it in
structured log records as a proper JSON field — no regex needed to
extract the URL from the error message.

---

## Automatic Retry with `with_retry`

The `retryable` flag is meaningless without a retry loop that reads it.
`resilience/retry.py` provides the production retry decorator:

```python
from resilience.retry import with_retry, RetryConfig

@with_retry(RetryConfig(max_attempts=4, base_delay=1.0))
async def call_model(prompt: str) -> str:
    """Retry up to 4 times on retryable errors, back off 1→2→4s with jitter."""
    return await client.complete(prompt)
```

Key behaviour:
- **`exc.retryable is False` → propagates immediately.** `ResponseParseError`,
  `ValidationError`, `ConfigurationError` will never be retried regardless of
  `max_attempts`.
- **Full jitter** prevents thundering-herd retry storms in distributed
  deployments.
- **`max_delay` cap** (default 60 s) prevents exponential growth from
  producing unreasonably long waits.
- Every retry attempt is logged at WARNING level with attempt number, delay,
  exception class, and `exc.context`.

Pre-built configs for common scenarios:

```python
from resilience.retry import AGGRESSIVE, CONSERVATIVE, ONCE

# 5 attempts, 0.5 s base, 30 s cap — high-frequency transient failures
@with_retry(AGGRESSIVE)
async def query_redis(): ...

# 3 attempts, 2 s base, 60 s cap — expensive external service calls
@with_retry(CONSERVATIVE)
async def call_chromadb(): ...

# 1 attempt — never retry (e.g. audit log writes that must not duplicate)
@with_retry(ONCE)
async def write_audit_event(): ...
```

You can also call the retry loop directly without a decorator:

```python
from resilience.retry import retry_async, RetryConfig

result = await retry_async(
    lambda: fetch_model_response(prompt),
    config=RetryConfig(max_attempts=3, base_delay=0.5),
)
```

---

## Attaching Exceptions to Traces

When a `TinkerError` occurs inside a tracing span, use
`record_tinker_exception` to attach its `context` dict and `retryable` flag
to the span:

```python
from observability import record_tinker_exception
from observability.tracing import default_tracer

with default_tracer.start_trace("micro_loop") as trace:
    with trace.span("architect_call") as span:
        try:
            result = await architect.call(task, context)
        except TinkerError as exc:
            record_tinker_exception(exc, span)
            raise
```

The span's serialised form will then include:

```json
{
  "name": "architect_call",
  "error": "ModelConnectionError: connect refused [url='http://...', attempt=2]",
  "attributes": {
    "exc.type":             "ModelConnectionError",
    "exc.retryable":        true,
    "exc.url":              "http://192.168.1.10:11434",
    "exc.attempt":          2
  }
}
```

No regex.  No string parsing.  The structured context is a first-class field
in the trace payload, available to any log aggregator or trace viewer.

---

## How Each Module Uses It

Modules do not redefine exceptions locally.  They import from
`exceptions.py` and re-export for callers:

```python
# llm/client.py
from exceptions import (
    ModelClientError,
    ModelConnectionError,
    ModelTimeoutError,
    ModelRateLimitError,
    ModelServerError,
    ResponseParseError,
)

# Backwards-compat aliases so old code still works
ConnectionError = ModelConnectionError   # noqa: A001
TimeoutError    = ModelTimeoutError      # noqa: A001
```

```python
# resilience/circuit_breaker.py
from exceptions import CircuitBreakerOpenError  # re-exported here
```

The `# noqa: A001` comment suppresses the linter warning about shadowing
built-in names — these are intentional aliases for backwards compatibility.

---

## Test Coverage

`tests/test_exceptions.py` verifies the complete hierarchy:

| Test class | What it verifies |
|-----------|-----------------|
| `TestAllCompleteness` | Every class in `exceptions.py` is in `__all__`; every `__all__` name exists |
| `TestHierarchy` | Every exception is a subclass of `TinkerError` |
| `TestRetryableFlags` | Table-driven check of `retryable` for all 24 exception classes; per-instance override; no cross-instance contamination |
| `TestTinkerErrorBase` | message, context, `__str__` with/without context, None context → empty dict |
| `TestCircuitBreakerOpenError` | Custom `__init__`, context keys, recovery time, `retryable=True` |
| `TestValidationError` | Custom `__init__`, MRO, catchable as both `TinkerError` and `ValueError` |
| `TestBackwardsCompatAliases` | `llm/client.py` aliases resolve to canonical classes |
| `TestSubmoduleReexports` | Each module's re-export is the same object as `exceptions.X` |

`resilience/tests/test_retry.py` verifies the retry decorator:

| Test class | What it verifies |
|-----------|-----------------|
| `TestRetryConfig` | Defaults, validation, immutability |
| `TestPrebuiltConfigs` | AGGRESSIVE / CONSERVATIVE / ONCE |
| `TestComputeDelay` | Exponential growth, max_delay cap, jitter bounds, deterministic no-jitter |
| `TestRetryAsyncSuccess` | First-attempt success, N-th attempt success |
| `TestRetryAsyncFailure` | Exhaustion, non-retryable propagation, non-TinkerError propagation, `only_if_retryable=False` |
| `TestRetryAsyncSleep` | Sleep called between attempts, durations non-negative, no sleep on success/non-retryable |
| `TestWithRetryDecorator` | `__name__`, `__doc__`, args/kwargs pass-through, custom config respected |

---

## The Golden Rules

1. **Raise `TinkerError` subclasses, never bare `ValueError` / `RuntimeError`**
   (unless crossing a public API boundary where the caller expects `ValueError`).

2. **Always pass `context=`** with at least the field name, URL, or ID
   that caused the failure.

3. **Never catch `TinkerError` and discard it silently** — at minimum
   `logger.warning("...", exc)`.

4. **Use the `retryable` flag** and `with_retry` rather than catching
   individual exception types when writing retry logic.

5. **Add every new exception class to `__all__`** in `exceptions.py`.
   The `TestAllCompleteness` test will fail if you forget.

---

## Implementation Reference

The full source:

- `exceptions.py` — hierarchy and `__all__`
- `resilience/retry.py` — `with_retry`, `retry_async`, `RetryConfig`
- `observability/tracing.py` — `record_tinker_exception`

```python
# All in one import:
from exceptions import TinkerError, ModelConnectionError, MicroLoopError
from resilience.retry import with_retry, RetryConfig, CONSERVATIVE
from observability import record_tinker_exception
```

→ Next: [Chapter 02 — The Model Client](./02-model-client.md)
