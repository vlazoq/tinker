# Tinker — Autonomous Architecture Thinking Engine

Tinker is a self-running AI system that thinks about software architecture continuously, without you having to ask it anything. You give it a problem statement like *"Design a distributed job queue"*, and it runs forever — generating ideas, critiquing them, synthesising conclusions, and building up a growing knowledge base about the problem.

It uses two local AI models (running via [Ollama](https://ollama.ai)) and does all its work on your machines — no cloud required, no API keys, fully private.

---

## What Does It Actually Do?

Tinker runs three nested loops, like a clock with three hands:

```
┌─────────────────────────────────────────────────────────┐
│  MACRO LOOP  (every 4 hours)                            │
│  Takes a full snapshot of everything learned so far.    │
│  ┌───────────────────────────────────────────────────┐  │
│  │  MESO LOOP  (every 5 micro loops per subsystem)  │  │
│  │  Synthesises insights for one part of the system │  │
│  │  ┌─────────────────────────────────────────────┐ │  │
│  │  │  MICRO LOOP  (runs continuously)            │ │  │
│  │  │  Pick task → Architect thinks → Critic      │ │  │
│  │  │  reviews → Store result → Generate tasks    │ │  │
│  │  └─────────────────────────────────────────────┘ │  │
│  └───────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

**Micro loop** (the workhorse): Picks the highest-priority task from a queue, assembles relevant context from memory, sends it to the Architect AI to design a solution, then sends both the task and design to the Critic AI for review. The result is stored in memory and new tasks are generated.

**Meso loop** (the synthesiser): After several micro loops on the same part of the system, runs a synthesis pass that summarises what was learned into a coherent document.

**Macro loop** (the historian): Every few hours, takes a full architectural snapshot and commits it to disk.

---

## Repository Structure

```
tinker/
│
├── main.py              ← Start here. Wires everything together and runs Tinker.
├── pyproject.toml       ← Python package config, dependency list, ruff & mypy settings.
├── Makefile             ← lint, format, typecheck, test, clean targets.
├── .env.example         ← Template for all ~110 environment variables (copy → .env).
├── CLAUDE.md            ← Quick-start for Claude Code users contributing to Tinker.
├── exceptions.py        ← TinkerError hierarchy (single source of truth for all errors).
│
├── agents/              ← The AI agent roles (one file per responsibility)
│   ├── __init__.py      ← Thin re-export shim — existing imports work unchanged.
│   ├── architect.py     ← ArchitectAgent: generates design proposals (7B model).
│   ├── critic.py        ← CriticAgent: scores and flags issues (2-3B model).
│   ├── synthesizer.py   ← SynthesizerAgent: writes meso/macro summary docs.
│   ├── _shared.py       ← Shared helpers: trace ID, prompt builders, rate limiter hooks.
│   ├── protocols.py     ← ArchitectStrategy, CriticStrategy, SynthesizerStrategy.
│   ├── agent_factory.py ← AgentFactory: maps AgentRole → class, supports swapping.
│   ├── fritz/           ← Git/GitHub/Gitea integration agent.
│   │   ├── agent.py     ← FritzAgent: commit, push, create PRs.
│   │   └── protocol.py  ← VCSAgentProtocol: the interface Fritz satisfies.
│   └── grub/            ← Code-generation agent with minion pipeline.
│       ├── agent.py     ← GrubAgent: orchestrates coding minions.
│       └── minions/     ← Coder, Tester, Reviewer, Debugger, Refactorer.
│
├── bootstrap/           ← Application wiring (dependency injection root)
│   ├── components.py    ← Builds and injects all components at startup.
│   ├── enterprise_stack.py ← Wires resilience, observability, DLQ, backups.
│   └── logging_config.py   ← Unified logging setup (loguru + stdlib fallback).
│
├── config/              ← Centralized configuration (NEW)
│   ├── settings.py      ← TinkerSettings: nested frozen dataclasses for all env vars.
│   └── validation.py    ← Startup validator (checks URLs, ports, paths, conflicts).
│
├── core/                ← Core domain logic
│   ├── protocols.py     ← TaskEngineProtocol, ContextAssemblerProtocol.
│   ├── llm/             ← Talks to the AI models (Ollama)
│   │   ├── client.py    ← Low-level HTTP client for one Ollama server.
│   │   ├── router.py    ← Routes requests to the right model (7B vs 2-3B).
│   │   ├── types.py     ← Data types: MachineConfig, Message, AgentRole, etc.
│   │   └── parsing.py   ← Extracts clean JSON from messy AI responses.
│   ├── memory/          ← Stores and retrieves everything Tinker learns
│   │   ├── manager.py   ← MemoryManager (unified interface, inherits from 4 mixins).
│   │   ├── _working_memory.py   ← WorkingMemoryMixin: Redis key/value ops.
│   │   ├── _session_memory.py   ← SessionMemoryMixin: DuckDB artifact storage.
│   │   ├── _research_archive.py ← ResearchArchiveMixin: ChromaDB semantic search.
│   │   ├── _task_registry.py    ← TaskRegistryMixin: SQLite task CRUD.
│   │   ├── storage.py   ← Four storage backends (Redis/DuckDB/Chroma/SQLite).
│   │   ├── schemas.py   ← Data shapes: Artifact, ResearchNote, Task, MemoryConfig.
│   │   ├── embeddings.py← Converts text → vectors for semantic search.
│   │   └── compression.py ← Shrinks old artifacts when memory grows too large.
│   ├── context/         ← Builds the prompt context for each AI call
│   │   ├── assembler.py ← Fetches from memory, fits within token budget, builds prompt.
│   │   └── stubs.py     ← Fake memory objects used in tests.
│   ├── tools/           ← Actions the AI can take (search, scrape, write, draw)
│   │   ├── registry.py  ← Manages all tools; the AI calls tools through here.
│   │   ├── base.py      ← The base class every tool inherits from.
│   │   ├── web_search.py← Search the web via a local SearXNG instance.
│   │   ├── web_scraper.py ← Read a webpage's content (Playwright + trafilatura).
│   │   ├── memory_query.py ← Search Tinker's own research archive.
│   │   ├── artifact_writer.py ← Write results to structured files on disk.
│   │   └── diagram_generator.py ← Generate architecture diagrams (Graphviz).
│   ├── prompts/         ← Prompt templates for the AI agents
│   │   ├── builder.py   ← Assembles complete prompts from parts.
│   │   ├── templates.py ← The actual text templates for each agent/loop level.
│   │   ├── schemas.py   ← JSON output schemas the AI must follow.
│   │   ├── variants.py  ← Personality tweaks (harder critic, socratic architect, etc.).
│   │   └── validator.py ← Checks that AI output matches the expected schema.
│   ├── models/          ← Model presets and library management
│   ├── events/          ← Internal event bus
│   ├── mcp/             ← Model Context Protocol server
│   └── validation/      ← Input validation at system boundaries
│
├── runtime/             ← Execution engine
│   ├── orchestrator/    ← The main control loop that drives everything
│   │   ├── orchestrator.py  ← Orchestrator class (inherits from 4 mixins).
│   │   ├── _loop_runners.py ← LoopRunnerMixin: micro/meso/macro dispatch.
│   │   ├── _resilience.py   ← ResilienceMixin: DLQ replay, backpressure.
│   │   ├── _stagnation.py   ← StagnationMixin: stagnation detection + intervention.
│   │   ├── _lifecycle.py    ← LifecycleMixin: shutdown, signal handling.
│   │   ├── _micro_helpers.py← Extracted micro loop utilities.
│   │   ├── micro_loop.py    ← One complete micro-loop iteration.
│   │   ├── meso_loop.py     ← Meso synthesis pass.
│   │   ├── macro_loop.py    ← Macro snapshot pass.
│   │   ├── config.py        ← All tunable settings (timeouts, intervals, etc.).
│   │   ├── state.py         ← Live state snapshot (loop counts, current task, etc.).
│   │   └── stubs.py         ← Fake components for testing without real AI.
│   ├── tasks/           ← Work queue: what should Tinker think about next?
│   │   ├── engine.py    ← Façade: one simple interface over queue, registry, generator.
│   │   ├── queue.py     ← Priority queue with exploration randomisation.
│   │   ├── registry.py  ← SQLite database of all tasks ever created.
│   │   ├── generator.py ← Parses Architect output to create new child tasks.
│   │   ├── scorer.py    ← 5-factor scoring algorithm (confidence gap, recency, etc.).
│   │   ├── resolver.py  ← Dependency resolution with Kahn's topological sort.
│   │   └── schema.py    ← The Task data class with all its fields.
│   └── stagnation/      ← Detects when Tinker gets "stuck" and intervenes
│       ├── monitor.py   ← Runs all detectors and decides if intervention is needed.
│       ├── detectors.py ← 5 detectors: repetition, fixation, critique collapse, etc.
│       ├── embeddings.py← Text similarity (TF-IDF fallback, or Ollama embeddings).
│       ├── config.py    ← Thresholds for each detector.
│       ├── models.py    ← Data types: StagnationEvent, InterventionDirective.
│       └── event_log.py ← SQLite log of all stagnation events.
│
├── infra/               ← Infrastructure services
│   ├── architecture/    ← Tracks the growing architecture design document
│   │   ├── manager.py   ← ArchitectureStateManager (inherits from 5 mixins).
│   │   ├── _persistence.py    ← PersistenceMixin: save/load/archive snapshots.
│   │   ├── _git_integration.py← GitIntegrationMixin: auto-commit to git.
│   │   ├── _summarizer.py     ← SummarizerMixin: LLM-powered summaries.
│   │   ├── _diffing.py        ← DiffingMixin: diff/rollback between versions.
│   │   ├── _queries.py        ← QueriesMixin: low-confidence, unresolved, etc.
│   │   ├── schema.py   ← The ArchitectureState data model.
│   │   └── merger.py    ← Merges new AI output into the existing design.
│   ├── resilience/      ← Circuit breaker, rate limiter, retry, idempotency.
│   ├── observability/   ← Audit log, tracing, SLA tracker, alerting, OTLP.
│   ├── health/          ← HTTP health probes (/health, /ready, /status).
│   ├── backup/          ← Periodic backup manager (DuckDB + SQLite + Chroma).
│   ├── security/        ← Encryption at rest, secrets management.
│   └── capacity/        ← Token/disk growth projections.
│
├── ui/                  ← User interfaces
│   ├── tui/             ← Textual TUI dashboard (terminal)
│   ├── web/             ← FastAPI web UI (with per-IP rate limiting)
│   │   ├── app.py       ← Composition root: middleware, router includes, SPA shell.
│   │   └── routes/      ← 9 route modules (health, config, tasks, fritz, models, etc.).
│   ├── gradio/          ← Gradio web interface
│   └── streamlit/       ← Streamlit web interface
│
├── utils/               ← Shared utility helpers (NEW)
│   ├── io.py            ← atomic_write, safe_json_load, safe_json_dump.
│   └── retry.py         ← retry_with_backoff (async decorator with exp. backoff).
│
├── services/            ← Background services
├── tinker_platform/     ← Platform features (feature flags, experiments, A/B testing)
├── tests/               ← Top-level test suite
├── docs/                ← Reference docs and step-by-step tutorials
│   ├── ARCHITECTURE.md  ← Module dependency graph + data flow diagrams.
│   ├── Overview.md      ← This file (beginner-friendly codebase tour).
│   ├── SETUP.md         ← Cross-platform install guide.
│   └── tutorial/        ← 21 numbered chapters (00-introduction through 21-config).
└── conftest.py          ← Shared pytest fixtures (mock_router, dummy_deps, etc.)
```

> **Tip for newcomers:** Start with `main.py` → follow the imports into
> `bootstrap/components.py` → see how the Orchestrator, agents, memory, and
> task engine are wired together. Then read any module you're curious about.

---

## The Two AI Models

Tinker is designed for a two-machine setup, but works on one machine too:

| Role | Model | Used For |
|------|-------|----------|
| **Architect** | Qwen3 7B (primary server) | Generates architecture designs |
| **Critic** | Phi-3 Mini (secondary) | Reviews and scores designs |
| **Synthesizer** | Either | Produces meso/macro summaries |
| **Researcher** | Either | Determines what to search for |

Configure the URLs via `TINKER_SERVER_URL` and `TINKER_SECONDARY_URL` in your `.env` file. If you only have one machine, set both to `http://localhost:11434`.

---

## The Four Memory Stores

Everything Tinker learns is stored in a layered memory system:

| Store | Technology | What's In It | Lifetime |
|-------|-----------|--------------|---------|
| **Working memory** | Redis | Active task context, recent results | Ephemeral |
| **Session store** | DuckDB | All artifacts from this run | Session |
| **Research archive** | ChromaDB | Semantically-searchable research notes | Persistent |
| **Task registry** | SQLite | Every task ever created/completed | Persistent |

---

## Data Flow (One Micro Loop)

```
TaskEngine.select_task()
        │
        ▼
ContextAssembler.build(task)
  ├─ memory.get_artifacts()     ← recent work
  ├─ memory.search_research()   ← relevant past research
  └─ architecture state summary ← current design
        │
        ▼
ArchitectAgent.call(task, context)   ← Qwen3 7B thinks
        │
        ▼
CriticAgent.call(task, architect_output)  ← Phi-3 Mini reviews
        │
        ▼
memory.store_artifact(result)
        │
        ▼
TaskEngine.generate_tasks(result)    ← creates follow-up tasks
        │
        ▼
dashboard ← state snapshot published
```

---

## Running Modes

```bash
# Production: real AI models required
python main.py --problem "Design a distributed cache layer"

# Smoke-test: no Ollama needed, uses in-process stubs
python main.py --problem "..." --stubs

# With live terminal dashboard in the same window
python main.py --problem "..." --dashboard

# Dashboard in a separate terminal (while main.py runs elsewhere)
python -m dashboard
```

---

## Key Design Decisions

**Dependency injection everywhere.** The Orchestrator never imports the AI models, memory, or tools directly. Everything is passed in at startup. This makes testing easy — just pass stubs.

**Async throughout.** Every I/O operation (HTTP calls to Ollama, Redis, database reads) is non-blocking async/await. This means Tinker can do multiple things concurrently without threads.

**Fault-tolerant loops.** If a micro loop fails (model timeout, network error), it logs the error and tries the next task. The system never crashes — it backs off briefly and continues.

**State snapshots for the dashboard.** The orchestrator writes its state to a JSON file after every loop. The dashboard reads this file. They don't need to be in the same process.

**Protocols, not concrete classes.** Every agent role (`ArchitectStrategy`, `CriticStrategy`, `SynthesizerStrategy`, `VCSAgentProtocol`) is a `@runtime_checkable` Protocol. The orchestrator never imports the concrete classes — only the bootstrap layer does. This makes swapping an agent for a test double or a different model as simple as passing a different object.

**Agent factory for runtime substitution.** `agents/agent_factory.py` maps `AgentRole` enums to classes. You can replace any agent without changing the orchestrator:

```python
from agents.agent_factory import register_agent
from core.llm.types import AgentRole

register_agent(AgentRole.CRITIC, MyFasterCritic)
```
