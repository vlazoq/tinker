# Chapter 20 — Agent Protocols & Factory: Swappable AI Roles

## The Problem

The original `agents/__init__.py` was a 1,000+ line monolith — all three agent
classes, their shared helpers, and all prompt-building logic in one file.  This
created three concrete problems:

1. **Tight coupling** — the orchestrator imported `ArchitectAgent` by name.
   Swapping it for a test double or alternative implementation required editing
   every call site.
2. **No contract** — there was no formal definition of what an "Architect" must
   do.  Tests had to mock concrete internals rather than a clean interface.
3. **Hard to read** — finding any one piece of logic required scrolling past
   everything else.

---

## The Solution

### Step 1 — Extract a Protocol for each role

A `Protocol` is Python's way of writing an interface without inheritance.
`@runtime_checkable` lets you verify compliance at runtime with `isinstance`.

```python
# agents/protocols.py
from typing import Protocol, runtime_checkable

@runtime_checkable
class ArchitectStrategy(Protocol):
    async def call(self, task: dict, context: dict) -> dict:
        """Run one architect turn and return a design proposal."""
        ...

@runtime_checkable
class CriticStrategy(Protocol):
    async def call(self, task: dict, architect_result: dict) -> dict:
        """Run one critic turn and return an evaluation."""
        ...

@runtime_checkable
class SynthesizerStrategy(Protocol):
    async def call(self, level: str, **kwargs) -> dict:
        """Run one synthesis pass at meso or macro level."""
        ...
```

The orchestrator now type-hints against `ArchitectStrategy`, not `ArchitectAgent`.
It does not care how the implementation works — only that it has the right `call()`
method.

**Why not ABCs?** Abstract Base Classes require inheritance.  Protocols work by
structural compatibility (duck typing) — any class with the right method shapes
satisfies the protocol, even if it was written before the Protocol existed.

### Step 2 — Split the monolith into one file per role

```
agents/
├── __init__.py         ← thin re-export shim (backward compatible)
├── architect.py        ← ArchitectAgent only (~140 lines)
├── critic.py           ← CriticAgent only (~120 lines)
├── synthesizer.py      ← SynthesizerAgent only (~100 lines)
├── _shared.py          ← everything shared between the three
└── protocols.py        ← the three Protocol definitions
```

`agents/__init__.py` re-exports everything, so all existing import sites
(`from agents import ArchitectAgent`) continue to work without changes.

### Step 3 — Add an Agent Factory

The factory maps `AgentRole` enum values to concrete classes and provides a
single place to swap them:

```python
# agents/agent_factory.py
from core.llm.types import AgentRole

_registry: dict[AgentRole, type] = {}

def register_agent(role: AgentRole, cls: type) -> None:
    """Replace the default implementation for a role."""
    _registry[role] = cls

def get_agent(role: AgentRole, router) -> object:
    """Instantiate the registered (or default) class for a role."""
    cls = _registry.get(role) or _DEFAULTS[role]
    return cls(router)
```

Bootstrap code (`bootstrap/components.py`) calls `get_agent()` once at startup.
Test code calls `register_agent()` before the test to inject a stub.

---

## The Shared Helpers (`agents/_shared.py`)

Three agents share a lot of infrastructure.  Rather than duplicating it,
`_shared.py` centralises:

| Symbol | Purpose |
|--------|---------|
| `_current_trace_id` | `ContextVar[str]` — propagates correlation IDs across async tasks |
| `_get_retry_async()` | Lazy-loads `infra.resilience.retry.retry_async` (returns `None` if not installed) |
| `_get_rate_limiter_registry()` | Lazy-loads the rate limiter registry |
| `_build_architect_prompts()` | Returns `(system_prompt, user_prompt)` for the Architect |
| `_build_critic_prompts()` | Returns `(system_prompt, user_prompt)` for the Critic |
| `_build_synthesizer_prompts()` | Returns `(system_prompt, user_prompt)` for the Synthesizer |
| `_validate_agent_response()` | Checks required keys are present in a response dict |
| `_extract_score()` | Regex fallback to parse a float score from raw text |

The lazy loaders use `importlib.import_module` and return `None` on `ImportError`,
making the agents usable in minimal test environments where the resilience package
is not present.

---

## The VCS Agent Protocol

Fritz (the git/GitHub/Gitea agent) follows the same pattern:

```python
# agents/fritz/protocol.py
@runtime_checkable
class VCSAgentProtocol(Protocol):
    async def setup(self) -> None: ...
    async def commit_and_ship(self, message: str, ...) -> Any: ...
    async def push(self, branch: str | None = None, force: bool = False) -> Any: ...
    async def create_pr(self, title: str, ...) -> Any: ...
    async def verify_connections(self) -> dict[str, bool]: ...
```

UI layers (`ui/web/app.py`, `ui/tui/`) type-hint against `VCSAgentProtocol`.
Only the bootstrap layer imports `FritzAgent` directly.

---

## How to Add a New Agent Implementation

1. Write your class with the correct `call()` (or `setup()` etc.) signature.
2. Verify it satisfies the protocol:
   ```python
   assert isinstance(MyAgent(router), ArchitectStrategy)
   ```
3. Register it at startup or in your test:
   ```python
   register_agent(AgentRole.ARCHITECT, MyAgent)
   ```

No other code needs to change.

---

## Runtime Verification

```python
from agents import ArchitectAgent
from agents.protocols import ArchitectStrategy

router = ...   # any router stub
agent = ArchitectAgent(router)
assert isinstance(agent, ArchitectStrategy)   # True — no inheritance needed
```

---

## Before / After Summary

| Before | After |
|--------|-------|
| `agents/__init__.py` — 1,083 lines | `agents/__init__.py` — 65 lines (re-exports only) |
| One monolith file | `architect.py`, `critic.py`, `synthesizer.py`, `_shared.py`, `protocols.py` |
| No formal contract | `@runtime_checkable` Protocols for every role |
| Hard to swap agents | `register_agent(AgentRole.CRITIC, MyStub)` replaces at runtime |
| Orchestrator imports concrete class | Orchestrator depends on protocol only |
