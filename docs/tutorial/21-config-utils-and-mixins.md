# Chapter 21 — Configuration, Utilities, and the Mixin Architecture

This chapter covers three recent additions to Tinker's codebase:

1. **Centralized configuration** (`config/`) — one place for all settings
2. **Shared utilities** (`utils/`) — common helpers used across modules
3. **Mixin-based class decomposition** — how large classes are kept readable

---

## 1. Centralized Configuration

### The Problem

Tinker reads ~110 environment variables across dozens of files.  Before the
`config/` package, each file called `os.getenv()` independently:

```python
# Scattered across 40+ files:
url = os.getenv("TINKER_SERVER_URL", "http://localhost:11434")
timeout = int(os.getenv("TINKER_ARCHITECT_TIMEOUT", "120"))
```

This made it hard to know what variables existed, what their defaults were,
or whether two files used the same variable with different defaults.

### The Solution: `config/settings.py`

All settings are now defined in **nested frozen dataclasses**:

```python
from config.settings import get_settings

settings = get_settings()   # singleton — reads env once, caches result

settings.llm.server_url     # "http://localhost:11434"
settings.llm.server_model   # "qwen3:7b"
settings.web.port           # 8082
settings.storage.redis_url  # "redis://localhost:6379"
```

Each section is its own dataclass:

| Dataclass | Prefix | Examples |
|-----------|--------|----------|
| `LLMSettings` | `TINKER_SERVER_*`, `TINKER_SECONDARY_*` | model, timeout, context size |
| `StorageSettings` | `TINKER_REDIS_*`, `TINKER_DUCKDB_*` | URLs, paths, TTLs |
| `WebUISettings` | `TINKER_WEBUI_*` | port, rate limits |
| `OrchestratorSettings` | `TINKER_MESO_*`, `TINKER_MACRO_*` | intervals, triggers |
| `ObservabilitySettings` | `TINKER_LOG_*`, `TINKER_OTLP_*` | log level, metrics port |
| ... | ... | 13 sections total |

The top-level `TinkerSettings.from_env()` classmethod reads every variable from
the environment and returns an immutable settings object.

### Startup Validation: `config/validation.py`

```python
from config.settings import get_settings
from config.validation import validate_or_warn

settings = get_settings()
validate_or_warn(settings)   # logs warnings for bad config
```

The validator checks:
- URLs parse correctly (scheme + hostname present)
- Port numbers are in range 1–65535
- No two services share the same port
- Timeouts and intervals are positive
- Required combos are present (e.g., postgres backend needs a DSN)
- Directories exist on disk

---

## 2. Shared Utilities (`utils/`)

### `utils/io.py` — Safe File Operations

```python
from utils import atomic_write, safe_json_load, safe_json_dump

# Write atomically (temp file → os.replace) — no partial writes on crash
atomic_write(Path("state.json"), json.dumps(data))

# Load JSON safely — returns default on missing file or bad JSON
config = safe_json_load(Path("config.json"), default={})

# Dump JSON atomically
safe_json_dump(Path("state.json"), {"version": 3})
```

### `utils/retry.py` — Async Retry with Backoff

```python
from utils import retry_with_backoff

# As a decorator:
@retry_with_backoff(max_retries=3, base_delay=1.0)
async def fetch_data():
    ...

# As a plain decorator (uses defaults):
@retry_with_backoff
async def another_function():
    ...
```

Uses exponential backoff with jitter to avoid thundering herds.

---

## 3. The Mixin Architecture

### The Problem

Several classes grew to 800–1400 lines: `Orchestrator`, `MemoryManager`,
`ArchitectureStateManager`.  A single file with 20+ methods is hard to
navigate and review.

### The Solution: Mixin Decomposition

Each large class is split into **focused mixin modules**.  The main class
inherits from all its mixins and keeps only `__init__` + core logic.

#### Example: Orchestrator

```
runtime/orchestrator/
├── orchestrator.py       ← Orchestrator class (inherits from 4 mixins)
├── _loop_runners.py      ← LoopRunnerMixin: _run_micro(), _run_meso(), _run_macro()
├── _resilience.py        ← ResilienceMixin: DLQ replay, backpressure
├── _stagnation.py        ← StagnationMixin: detection + intervention
└── _lifecycle.py          ← LifecycleMixin: shutdown, signal handling
```

```python
# runtime/orchestrator/orchestrator.py
class Orchestrator(LoopRunnerMixin, ResilienceMixin, StagnationMixin, LifecycleMixin):
    def __init__(self, ...):
        # all state lives here
        ...

    async def run(self):
        # main loop — calls methods from mixins
        await self._run_micro(task)       # from LoopRunnerMixin
        self._check_stagnation(result)    # from StagnationMixin
        ...
```

#### Example: MemoryManager (4 mixins)

```
core/memory/
├── manager.py               ← MemoryManager (inherits from 4 mixins)
├── _working_memory.py       ← WorkingMemoryMixin: set/get/delete context
├── _session_memory.py       ← SessionMemoryMixin: store/search artifacts
├── _research_archive.py     ← ResearchArchiveMixin: store/search research
└── _task_registry.py        ← TaskRegistryMixin: store/update tasks
```

#### Example: ArchitectureStateManager (5 mixins)

```
infra/architecture/
├── manager.py               ← ArchitectureStateManager (inherits from 5 mixins)
├── _persistence.py          ← PersistenceMixin: save/load/archive
├── _git_integration.py      ← GitIntegrationMixin: auto-commit
├── _summarizer.py           ← SummarizerMixin: LLM-powered summaries
├── _diffing.py              ← DiffingMixin: diff/rollback versions
└── _queries.py              ← QueriesMixin: low-confidence, unresolved, etc.
```

### How Mixins Work

1. Each mixin is a plain class (no `__init__`) that defines methods
2. Methods access `self.*` attributes set by the main class's `__init__`
3. Files are prefixed with `_` to signal they're internal implementation
4. The main class's `__init__` is the single source of truth for state

**Key rule:** Mixins never define `__init__`.  All state setup happens in the
main class.  Mixins just add methods.

### When to Use This Pattern

Use mixins when a class has **distinct groups of methods** that can be
understood independently (e.g., "persistence methods" vs. "query methods").
Don't use mixins for classes under ~300 lines — the indirection isn't worth it.

---

## Core Protocols (`core/protocols.py`)

In addition to the agent protocols in `agents/protocols.py`, the `core/`
package defines infrastructure protocols:

```python
from core.protocols import TaskEngineProtocol, ContextAssemblerProtocol

# These are @runtime_checkable — you can check at startup:
assert isinstance(task_engine, TaskEngineProtocol)
```

This lets the orchestrator depend on abstract interfaces rather than
concrete classes, making it easy to swap implementations for testing.

---

## Where These Files Live

| Package | Key Files | Purpose |
|---------|-----------|---------|
| `config/` | `settings.py`, `validation.py` | Centralized env-var config |
| `utils/` | `io.py`, `retry.py` | Shared helpers |
| `core/` | `protocols.py` | Infrastructure protocol contracts |
| `runtime/orchestrator/` | `_loop_runners.py`, `_resilience.py`, `_stagnation.py`, `_lifecycle.py` | Orchestrator mixins |
| `core/memory/` | `_working_memory.py`, `_session_memory.py`, `_research_archive.py`, `_task_registry.py` | Memory mixins |
| `infra/architecture/` | `_persistence.py`, `_git_integration.py`, `_summarizer.py`, `_diffing.py`, `_queries.py` | Architecture mixins |

---

## Next Steps

- Read `config/settings.py` to see all available settings sections
- Run `python -c "from config.validation import validate_settings; ..."` to test validation
- Browse any `_*.py` mixin file to see how methods are organized
- See Chapter 20 for the agent protocols and factory pattern
