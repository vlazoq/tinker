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
│   │   ├── ModelConnectionError   ← TCP failure (retryable)
│   │   ├── ModelTimeoutError      ← request timed out (retryable)
│   │   ├── ModelRateLimitError    ← HTTP 429 (retryable)
│   │   ├── ModelServerError       ← HTTP 5xx (retryable)
│   │   └── ResponseParseError     ← bad JSON (not retryable)
│   ├── ModelRouterError           ← router misconfigured
│   └── PromptBuilderError         ← template / context error
├── OrchestratorError
│   ├── MicroLoopError             ← loop iteration failed (retryable)
│   └── ConfigurationError         ← bad env var / out-of-range value
├── MemoryStoreError               ← Redis/DuckDB/SQLite failure (retryable)
├── TaskError
│   └── DependencyCycleError       ← circular task dependency
├── ResilienceError
│   └── CircuitBreakerOpenError    ← circuit is OPEN (retryable after cooldown)
├── ToolError                      ← web search / scraping / shell failure
│   └── ToolNotFoundError          ← no tool with that name registered
├── ContextError                   ← context assembly failed
├── ArchitectureError              ← state machine or diagram failure
├── ValidationError                ← bad user input (also inherits ValueError)
└── ExperimentError                ← A/B experiment misconfigured
```

---

## The `retryable` Flag

Every `TinkerError` carries a `retryable: bool` attribute.

```python
# In exceptions.py
class ModelConnectionError(ModelClientError):
    retryable = True   # TCP failures are worth retrying

class ResponseParseError(ModelClientError):
    retryable = False  # Garbage response won't get better on retry
```

The orchestrator's retry logic reads this flag:

```python
from exceptions import TinkerError

try:
    result = await call_model(...)
except TinkerError as exc:
    if exc.retryable:
        schedule_retry(exc)
    else:
        log_and_fail(exc)
```

No string matching.  No isinstance chains.  One attribute.

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

## The Golden Rules

1. **Raise `TinkerError` subclasses, never bare `ValueError` / `RuntimeError`**
   (unless they cross a public API boundary where the caller expects `ValueError`).

2. **Always pass `context=`** with at least the field name, URL, or ID
   that caused the failure.

3. **Never catch `TinkerError` and discard it silently** — at minimum
   `logger.warning("...", exc)`.

4. **Use the `retryable` flag** rather than catching individual exception types
   when writing retry or circuit-breaker logic.

---

## Implementation Reference

The full source is `exceptions.py` in the project root.  The `__all__`
list at the bottom is the stable public API — import from there rather
than from individual sub-packages.

```python
from exceptions import (
    TinkerError,
    ModelConnectionError,
    MicroLoopError,
    # …
)
```

→ Next: [Chapter 02 — The Model Client](./02-model-client.md)
