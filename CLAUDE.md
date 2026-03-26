# CLAUDE.md — Tinker Contributor Guide for Claude Code

This file helps Claude Code (and humans using it) understand the Tinker codebase
quickly so contributions land correctly.

---

## What Is Tinker?

Tinker is a **fully local, autonomous AI architecture research engine**.  It runs
three nested reasoning loops (Micro → Meso → Macro) using two Ollama models:

- **Main model** (7B, e.g. Qwen3-7B) — Architect + Synthesizer roles
- **Judge model** (2-3B, e.g. Phi-3-mini) — Critic role

No paid APIs, no cloud, no API keys.  Designed for homelab hardware (i7-7700k,
64 GB RAM, RTX 3090).

---

## Project Layout — Key Directories

```
agents/             One file per agent role + shared helpers + protocols
  architect.py      ArchitectAgent
  critic.py         CriticAgent
  synthesizer.py    SynthesizerAgent
  _shared.py        Shared: trace ID, prompt builders, lazy loaders
  protocols.py      @runtime_checkable Protocols for the three roles
  agent_factory.py  AgentFactory — maps AgentRole enum → class
  fritz/            Git/GitHub/Gitea VCS integration
    agent.py        FritzAgent (commit, push, PR creation)
    protocol.py     VCSAgentProtocol

bootstrap/          Application wiring (DI root)
  components.py     Builds and injects all components at startup

core/               Cross-cutting infrastructure
  llm/              Model client, router, types
  tools/            Web search, scraper, artifact writer, diagram generator
  prompts/          Prompt templates and builder

infra/              Infrastructure services
  resilience/       Circuit breaker, rate limiter, retry, idempotency

orchestrator/       The three reasoning loops
  micro_loop.py
  meso_loop.py
  macro_loop.py

ui/
  tui/              Textual TUI dashboard
  web/              FastAPI web UI (with per-IP rate limiting)

docs/               Reference docs and step-by-step tutorials
  ARCHITECTURE.md   Module dependency graph + data flow
  Overview.md       Beginner-friendly codebase tour
  SETUP.md          Cross-platform install guide
  tutorial/         Numbered chapters (00-introduction through 19-new-features)
```

---

## Architecture Patterns — Read Before Touching Agent Code

### 1. Protocols, not concrete classes

Every agent role is a `@runtime_checkable` Protocol:

```python
# agents/protocols.py
class ArchitectStrategy(Protocol):
    async def call(self, task: dict, context: dict) -> dict: ...

# agents/fritz/protocol.py
class VCSAgentProtocol(Protocol):
    async def commit_and_ship(self, message: str, ...) -> Any: ...
```

The orchestrator and UI code type-hint against the Protocol.  Only
`bootstrap/components.py` imports the concrete class.

### 2. Agent factory for runtime substitution

```python
from agents.agent_factory import register_agent
from core.llm.types import AgentRole

# Swap in a test double without touching the orchestrator:
register_agent(AgentRole.CRITIC, MyFastCritic)
```

### 3. Shared helpers in `agents/_shared.py`

`_shared.py` holds everything the three agent classes share:

- `_current_trace_id` — ContextVar for correlation IDs across async tasks
- `_get_retry_async()` / `_get_rate_limiter_registry()` — lazy loaders
  (avoid circular imports; return `None` if not installed)
- `_build_architect_prompts()`, `_build_critic_prompts()`, `_build_synthesizer_prompts()`
- `_validate_agent_response()`, `_extract_score()`, `_parse_architect_structured()`

### 4. Backward-compatible re-exports in `agents/__init__.py`

`agents/__init__.py` is a **thin shim**.  All existing import sites
(`from agents import ArchitectAgent`, `from agents import _current_trace_id`, etc.)
continue to work unchanged.  Do not move logic into `__init__.py`.

---

## Rate Limiting

`infra/resilience/rate_limiter.py` provides `TokenBucketRateLimiter` with two call
styles:

- `await limiter.acquire()` — blocking, waits for a token
- `await limiter.try_acquire()` → `(acquired: bool, retry_after_seconds: float)` — non-blocking

The FastAPI web UI uses `try_acquire()` to return HTTP 429 immediately (with
`Retry-After` and `X-RateLimit-*` headers) rather than blocking the event loop.

---

## Testing

```bash
# Run all tests
pytest

# Run with stubs (no Ollama or external services needed)
python main.py --problem "Test problem" --stubs

# Run a single test file
pytest tests/test_agents.py -v
```

Key test conventions:
- Stubs live in `orchestrator/stubs.py` (orchestrator-level) and `context/stubs.py`
- Agent tests mock `_router.complete()`, not the full Ollama HTTP stack
- Use `pytest.mark.asyncio` for async tests

---

## Development Branch

All contributions go to feature branches off `main`.  See `.env.example` for the
full list of environment variables.

---

## Where to Start

| Goal | File to read first |
|------|--------------------|
| Understand the full system | `docs/Overview.md` |
| Understand the data flow | `docs/ARCHITECTURE.md` |
| Set up the project | `docs/SETUP.md` |
| Learn step by step | `docs/tutorial/00-introduction.md` |
| Add a new agent | `agents/protocols.py` + `agents/agent_factory.py` |
| Modify the micro loop | `orchestrator/micro_loop.py` |
| Add a new tool | `core/tools/base.py` + `core/tools/registry.py` |
| Change rate limiting | `infra/resilience/rate_limiter.py` |
