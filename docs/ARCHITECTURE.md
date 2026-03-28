# Tinker — Architecture Reference

Tinker is an autonomous, self-directed AI architecture design engine.
It runs three nested reasoning loops continuously, producing versioned
architecture documents that improve over time.

---

## System Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              main.py (entry point)                          │
│   args → env → enterprise stack → components → Orchestrator → asyncio loop │
└─────────────────────────────────────┬───────────────────────────────────────┘
                                      │
                         ┌────────────▼────────────┐
                         │      ORCHESTRATOR        │
                         │  orchestrator/           │
                         │  ┌──────────────────┐   │
                         │  │   Macro Loop     │   │  every 4 h
                         │  │  (macro_loop.py) │   │
                         │  └────────┬─────────┘   │
                         │  ┌────────▼─────────┐   │
                         │  │   Meso Loop      │   │  every N micro loops
                         │  │  (meso_loop.py)  │   │
                         │  └────────┬─────────┘   │
                         │  ┌────────▼─────────┐   │
                         │  │   Micro Loop     │   │  continuous
                         │  │  (micro_loop.py) │   │
                         │  └──────────────────┘   │
                         └────────────┬────────────┘
                                      │
         ┌────────────────────────────┼────────────────────────┐
         │                            │                        │
┌────────▼────────┐        ┌──────────▼──────────┐  ┌─────────▼──────────┐
│   TASK ENGINE   │        │   CONTEXT ASSEMBLER  │  │     AI AGENTS      │
│   tasks/        │        │   context/           │  │     agents.py      │
│                 │        │                      │  │                    │
│ TaskRegistry    │        │ MemoryAdaptor        │  │ ArchitectAgent     │
│ TaskQueue       │        │ PromptBuilderAdapter │  │ CriticAgent        │
│ TaskGenerator   │        └──────────┬───────────┘  │ SynthesizerAgent   │
│ PriorityScorer  │                   │              └─────────┬──────────┘
└────────┬────────┘                   │                        │
         │                   ┌────────▼────────┐     ┌─────────▼──────────┐
         │                   │  MEMORY MANAGER │     │    MODEL ROUTER    │
         │                   │  memory/        │     │    llm/router.py   │
         │                   │                 │     │                    │
         │                   │ ┌─────────────┐ │     │ Architect → 7B     │
         │                   │ │   Redis     │ │     │ Critic    → 2-3B   │
         │                   │ │ (working)   │ │     └─────────┬──────────┘
         │                   │ ├─────────────┤ │               │
         │                   │ │  DuckDB     │ │     ┌─────────▼──────────┐
         │                   │ │ (session)   │ │     │   OLLAMA CLIENT    │
         │                   │ ├─────────────┤ │     │   llm/client.py    │
         │                   │ │  ChromaDB   │ │     │                    │
         │                   │ │ (research)  │ │     │ retry + backoff    │
         │                   │ ├─────────────┤ │     │ circuit breaker    │
         │                   │ │  SQLite     │ │     └────────────────────┘
         │                   │ │ (tasks)     │ │
         └───────────────────► └─────────────┘ │
                             └─────────────────┘
```

---

## Three Reasoning Loops

```
TIME ──────────────────────────────────────────────────────────────────────────►

MACRO  ════════════════════════════════════╗     ════════════════════════════════
LOOP   (every 4 hours — full arch snapshot)║    (next macro)
       ════════════════════════════════════╝
                                │
MESO   ═══════╗     ═══════╗   │   ═══════╗
LOOP   (5 μ)  ║    (5 μ)  ║   │   (5 μ)  ║
       ═══════╝     ═══════╝   │   ═══════╝
                │               │
MICRO  μ μ μ μ μ│μ μ μ μ μ│μ μ μ│μ μ μ μ μ│  (continuous — seconds/minutes)
LOOP   ─────────►──────────►────►──────────►

       Each μ = 1 complete design cycle:
       select task → assemble context → architect → critic → store → new tasks
```

---

## Micro Loop Data Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ Step 1  Task Selection                                                      │
│         TaskEngine.select_task() — pop highest-priority PENDING task        │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
┌──────────────────────────────────────▼──────────────────────────────────────┐
│ Step 2  Idempotency Check                                                   │
│         SHA-256(operation + task_id) → skip if already completed this run  │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
┌──────────────────────────────────────▼──────────────────────────────────────┐
│ Step 3  Context Assembly                                                    │
│         ContextAssembler.build(task, max_artifacts)                         │
│           ├── arch state summary (20% of token budget)                      │
│           ├── semantic search → similar recent artifacts (30%)              │
│           └── relevant research notes (15%)                                 │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
┌──────────────────────────────────────▼──────────────────────────────────────┐
│ Step 4  Architect AI Call                                                   │
│         ArchitectAgent.call(task, context)                                  │
│           → { content, knowledge_gaps, decisions, open_questions }          │
│                                                                             │
│         If knowledge_gaps present:                                          │
│           ToolRegistry.execute("web_search", query=gap) × N                │
│           Re-run Architect with enriched context                            │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
┌──────────────────────────────────────▼──────────────────────────────────────┐
│ Step 5  Refinement Loop                                                     │
│         while score < min_critic_score and iterations < max:                │
│           CriticAgent.call(task, architect_result)                          │
│           → { content, score, flags }                                       │
│           If score too low → inject feedback → re-run Architect             │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
┌──────────────────────────────────────▼──────────────────────────────────────┐
│ Step 6  Quality Gate                                                        │
│         if score < quality_gate_threshold:                                  │
│           AlertManager.alert(QUALITY_GATE_BREACH)                           │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
┌──────────────────────────────────────▼──────────────────────────────────────┐
│ Step 7  Artifact Storage                                                    │
│         MemoryManager.store_artifact(architect + critic → markdown)         │
│           ├── DuckDB INSERT (session memory)                                │
│           └── ChromaDB embed + upsert (research archive)                   │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
┌──────────────────────────────────────▼──────────────────────────────────────┐
│ Step 8  Task Completion + New Task Generation                               │
│         TaskEngine.complete_task(task_id, artifact_id)                      │
│         TaskEngine.generate_tasks(parent, arch_result, critic_result)       │
│           ├── open_questions → exploration tasks                            │
│           └── weaknesses     → research tasks                               │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Module Dependency Graph

```
main.py
│
├── bootstrap/              ← Application wiring (DI root)
│   ├── components.py       ← builds and injects all components at startup
│   ├── enterprise_stack.py ← wires resilience, observability, DLQ, backups
│   └── logging_config.py   ← unified logging setup (loguru + stdlib fallback)
│
├── config/                 ← Centralized configuration
│   ├── settings.py         ← TinkerSettings: nested frozen dataclasses for all ~110 env vars
│   └── validation.py       ← startup validator (checks URLs, ports, paths, conflicts)
│
├── runtime/orchestrator/   ← Main control loop
│   ├── orchestrator.py     ← Orchestrator (inherits from 4 mixins below)
│   ├── _loop_runners.py    ← LoopRunnerMixin: micro/meso/macro dispatch
│   ├── _resilience.py      ← ResilienceMixin: DLQ replay, backpressure
│   ├── _stagnation.py      ← StagnationMixin: stagnation detection + intervention
│   ├── _lifecycle.py       ← LifecycleMixin: shutdown, signal handling
│   ├── _micro_helpers.py   ← extracted micro loop utilities
│   ├── micro_loop.py       ← per-iteration logic
│   ├── meso_loop.py        ← subsystem synthesis
│   ├── macro_loop.py       ← full arch snapshot
│   ├── state.py            ← live state model (OrchestratorState)
│   ├── config.py           ← OrchestratorConfig (all knobs)
│   └── stubs.py            ← in-process test doubles
│
├── agents/                 ← AI agent roles (one file per role)
│   ├── __init__.py         ← thin re-export shim — all existing imports still work
│   ├── architect.py        ← ArchitectAgent (satisfies ArchitectStrategy)
│   ├── critic.py           ← CriticAgent (satisfies CriticStrategy)
│   ├── synthesizer.py      ← SynthesizerAgent (satisfies SynthesizerStrategy)
│   ├── _shared.py          ← shared helpers: trace ID · prompt builders · rate limiter hooks
│   ├── protocols.py        ← ArchitectStrategy · CriticStrategy · SynthesizerStrategy
│   ├── agent_factory.py    ← AgentFactory.get() / register_agent() — runtime substitution
│   ├── fritz/              ← Git / GitHub / Gitea VCS integration agent
│   │   ├── agent.py        ← FritzAgent (satisfies VCSAgentProtocol)
│   │   ├── protocol.py     ← VCSAgentProtocol (@runtime_checkable Protocol)
│   │   ├── git_ops.py      ← bare git operations
│   │   ├── github_ops.py   ← GitHub PR / push helpers
│   │   └── gitea_ops.py    ← Gitea PR / push helpers
│   └── grub/               ← Code-generation agent with minion pipeline
│       ├── agent.py        ← GrubAgent (minion orchestrator)
│       └── minions/        ← Coder · Tester · Reviewer · Debugger · Refactorer
│
├── core/                   ← Core domain logic
│   ├── protocols.py        ← TaskEngineProtocol · ContextAssemblerProtocol
│   ├── llm/
│   │   ├── router.py       ← ModelRouter (ARCHITECT→7B, CRITIC→2-3B)
│   │   ├── client.py       ← OllamaClient (HTTP + retry)
│   │   ├── types.py        ← AgentRole · Machine · ModelRequest/Response
│   │   └── parsing.py      ← JSON extraction from model output
│   │
│   ├── memory/
│   │   ├── manager.py      ← MemoryManager (inherits from 4 mixins below)
│   │   ├── _working_memory.py   ← WorkingMemoryMixin: Redis key/value ops
│   │   ├── _session_memory.py   ← SessionMemoryMixin: DuckDB artifact storage
│   │   ├── _research_archive.py ← ResearchArchiveMixin: ChromaDB semantic search
│   │   ├── _task_registry.py    ← TaskRegistryMixin: SQLite task CRUD
│   │   ├── storage.py      ← Redis · DuckDB · Chroma · SQLite adapters
│   │   ├── schemas.py      ← Artifact · ResearchNote · Task · MemoryConfig
│   │   ├── embeddings.py   ← text → vector (sentence-transformers / TF-IDF)
│   │   └── compression.py  ← archive old artifacts when threshold hit
│   │
│   ├── context/
│   │   ├── assembler.py    ← ContextAssembler (prompt dict builder)
│   │   └── memory_adapter.py
│   │
│   ├── tools/
│   │   ├── registry.py     ← ToolRegistry
│   │   ├── base.py         ← BaseTool protocol
│   │   ├── web_search.py   ← SearXNG
│   │   ├── web_scraper.py  ← Playwright
│   │   ├── artifact_writer.py ← write files to workspace
│   │   ├── diagram_generator.py ← Mermaid / Graphviz
│   │   └── memory_query.py ← semantic search tool
│   │
│   ├── models/             ← model presets and library management
│   ├── events/             ← internal event bus
│   ├── mcp/                ← Model Context Protocol server
│   └── validation/         ← boundary validation (problem stmt · task · URL · path · JSON)
│
├── runtime/tasks/
│   ├── engine.py           ← TaskEngine façade
│   ├── queue.py            ← priority queue
│   ├── registry.py         ← SQLite-backed task registry
│   ├── generator.py        ← parse AI output → Task objects
│   ├── scorer.py           ← 5-factor priority scorer
│   ├── resolver.py         ← dependency topological sort
│   └── schema.py           ← Task · TaskStatus · TaskType
│
├── runtime/stagnation/
│   ├── monitor.py          ← StagnationMonitor (5 detectors)
│   ├── detectors.py        ← Semantic · Fixation · Critique · Research · Starvation
│   ├── models.py           ← InterventionDirective · StagnationEvent
│   ├── embeddings.py
│   ├── config.py
│   └── event_log.py        ← SQLite stagnation history
│
├── infra/architecture/
│   ├── manager.py          ← ArchitectureStateManager (inherits from 5 mixins below)
│   ├── _persistence.py     ← PersistenceMixin: save/load/archive snapshots
│   ├── _git_integration.py ← GitIntegrationMixin: auto-commit to git
│   ├── _summarizer.py      ← SummarizerMixin: LLM-powered summaries
│   ├── _diffing.py         ← DiffingMixin: diff/rollback between versions
│   ├── _queries.py         ← QueriesMixin: low-confidence, unresolved, etc.
│   ├── schema.py           ← ArchitectureState · Component · Decision
│   └── merger.py           ← intelligent conflict-resolution merge
│
├── infra/resilience/
│   ├── circuit_breaker.py  ← CircuitBreaker · CircuitBreakerRegistry
│   ├── rate_limiter.py     ← RateLimiterRegistry (token-bucket)
│   ├── idempotency.py      ← IdempotencyCache (SHA-256 dedup)
│   ├── backpressure.py     ← BackpressureController
│   ├── dead_letter_queue.py← DeadLetterQueue (SQLite)
│   ├── distributed_lock.py ← Redis-backed distributed lock
│   ├── retry.py            ← retry_async() with exponential backoff
│   └── migrations.py       ← SQLite schema migration runner
│
├── infra/observability/
│   ├── audit_log.py        ← AuditLog (append-only SQLite)
│   ├── tracing.py          ← Tracer (distributed trace spans)
│   ├── sla_tracker.py      ← p50/p95/p99 per loop type
│   ├── alerting.py         ← AlertManager (Slack / webhook)
│   ├── structured_logging.py ← JSON + human-readable formatters, trace context
│   └── otlp.py             ← OpenTelemetry export
│
├── infra/health/           ← /health · /ready · /status (Kubernetes probes)
├── infra/backup/           ← BackupManager (DuckDB + SQLite + Chroma snapshots)
├── infra/security/         ← AES-256 encryption at rest · secrets management
├── infra/capacity/         ← CapacityPlanner (token/disk growth projections)
│
├── ui/tui/                 ← Textual TUI dashboard
├── ui/web/                 ← FastAPI web UI (9 route modules, per-IP rate limiting)
├── ui/gradio/              ← Gradio web interface
├── ui/streamlit/           ← Streamlit web interface
│
├── utils/                  ← Shared utility helpers
│   ├── io.py               ← atomic_write · safe_json_load · safe_json_dump
│   └── retry.py            ← retry_with_backoff (async decorator)
│
├── services/               ← Background services
├── tinker_platform/        ← Feature flags · experiments · A/B testing · lineage
│
├── exceptions.py           ← TinkerError hierarchy (single source of truth)
└── conftest.py             ← Shared pytest fixtures (mock_router, dummy_deps, etc.)
```

### Mixin Architecture Pattern

Several large classes are decomposed into focused **mixin modules** for readability.
The main class inherits from all its mixins and keeps only `__init__` + core
orchestration logic.  Example:

```python
# runtime/orchestrator/orchestrator.py
class Orchestrator(LoopRunnerMixin, ResilienceMixin, StagnationMixin, LifecycleMixin):
    def __init__(self, ...): ...
    async def run(self): ...

# Each mixin lives in its own file:
#   _loop_runners.py  → LoopRunnerMixin
#   _resilience.py    → ResilienceMixin
#   _stagnation.py    → StagnationMixin
#   _lifecycle.py     → LifecycleMixin
```

The same pattern is used by `MemoryManager` (4 mixins) and
`ArchitectureStateManager` (5 mixins).

---

## Memory Hierarchy

```
┌──────────────────────────────────────────────────────────────────────────┐
│                           MEMORY MANAGER                                 │
└──────┬──────────────────┬─────────────────┬────────────────────┬────────┘
       │                  │                 │                    │
       ▼                  ▼                 ▼                    ▼
 ┌───────────┐    ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
 │  Redis    │    │  DuckDB      │   │  ChromaDB    │   │  SQLite      │
 │           │    │              │   │              │   │              │
 │ Working   │    │ Session      │   │ Research     │   │ Task         │
 │ Memory    │    │ Memory       │   │ Archive      │   │ Registry     │
 │           │    │              │   │              │   │              │
 │ Ephemeral │    │ Columnar     │   │ Vector DB    │   │ Durable      │
 │ Fast k/v  │    │ Analytics    │   │ Semantic     │   │ Queryable    │
 │ TTL-based │    │ Per-run      │   │ search       │   │ All-time     │
 │           │    │ artifacts    │   │ Cross-run    │   │ task log     │
 └───────────┘    └──────────────┘   └──────────────┘   └──────────────┘
   set_context     store_artifact     store_research      store_task
   get_context     get_artifact       search_research     get_task
                   get_recent_*       get_research        update_task_status
```

---

## Anti-Stagnation System

```
After every micro loop, StagnationMonitor.check() runs 5 detectors in parallel:

  ┌──────────────────────────────────────────────────────────────────────┐
  │                     StagnationMonitor                                │
  │                                                                      │
  │  ┌─────────────────────┐    ┌─────────────────────────────────────┐ │
  │  │ SemanticLoop        │    │ Fires: ALTERNATIVE_FORCING          │ │
  │  │ Detector            │    │ (recent outputs too similar)        │ │
  │  │ (cosine similarity) │    └─────────────────────────────────────┘ │
  │  └─────────────────────┘                                            │
  │  ┌─────────────────────┐    ┌─────────────────────────────────────┐ │
  │  │ SubsystemFixation   │    │ Fires: FORCE_BRANCH                 │ │
  │  │ Detector            │    │ (early meso to pivot subsystem)     │ │
  │  │ (tag frequency)     │    └─────────────────────────────────────┘ │
  │  └─────────────────────┘                                            │
  │  ┌─────────────────────┐    ┌─────────────────────────────────────┐ │
  │  │ CritiqueCollapse    │    │ Fires: INJECT_CONTRADICTION          │ │
  │  │ Detector            │    │ (critic told to be harsher)         │ │
  │  │ (score trending ↑)  │    └─────────────────────────────────────┘ │
  │  └─────────────────────┘                                            │
  │  ┌─────────────────────┐    ┌─────────────────────────────────────┐ │
  │  │ ResearchSaturation  │    │ Fires: SPAWN_EXPLORATION            │ │
  │  │ Detector            │    │ (create new search tasks)           │ │
  │  │ (repeated URLs)     │    └─────────────────────────────────────┘ │
  │  └─────────────────────┘                                            │
  │  ┌─────────────────────┐    ┌─────────────────────────────────────┐ │
  │  │ TaskStarvation      │    │ Fires: ESCALATE_LOOP                │ │
  │  │ Detector            │    │ (inject high-priority explore task) │ │
  │  │ (queue empty/slow)  │    └─────────────────────────────────────┘ │
  │  └─────────────────────┘                                            │
  └──────────────────────────────────────────────────────────────────────┘
```

---

## Resilience Stack

```
  External Call (Ollama / Redis / SearXNG / ChromaDB)
         │
         ▼
  ┌─────────────────────────────────────────────────┐
  │  RateLimiter.acquire() / try_acquire()          │  token-bucket throttling
  │  try_acquire() → (ok, retry_after_s) non-block  │  used by web API middleware
  └─────────────────────────┬───────────────────────┘
                            │
  ┌─────────────────────────▼───────────────────────┐
  │        CircuitBreaker.call(fn, *args)            │
  │                                                 │
  │   CLOSED  ──► call passes through               │
  │   OPEN    ──► CircuitBreakerOpenError (fast fail)│
  │   HALF_OPEN ► one probe allowed through          │
  └─────────────────────────┬───────────────────────┘
                            │
  ┌─────────────────────────▼───────────────────────┐
  │           retry_async(coro, config)             │  exp. backoff + jitter
  └─────────────────────────┬───────────────────────┘
                            │
                     actual network call
```

---

## Enterprise Observability

```
  Every micro loop step emits:

  ┌───────────────┐   ┌───────────────┐   ┌───────────────┐   ┌──────────────┐
  │  AuditLog     │   │  Tracer       │   │  SLATracker   │   │ Prometheus   │
  │  (SQLite)     │   │  (in-memory)  │   │  (p50/p95/p99)│   │ /metrics     │
  │               │   │               │   │               │   │              │
  │ TASK_SELECTED │   │ span start    │   │ record        │   │ counters     │
  │ TASK_COMPLETE │   │ span end      │   │ latency       │   │ histograms   │
  │ ARTIFACT_STOR │   │ trace_id      │   │ alert on SLA  │   │ gauges       │
  │ MESO_SYNTHESI │   │ propagated    │   │ breach        │   │              │
  │ STAGNATION    │   │ via contextvars│  │               │   │              │
  └───────────────┘   └───────────────┘   └───────────────┘   └──────────────┘
```

---

## External Service Topology

```
  ┌───────────────────────────────────────────────────────────────┐
  │                         TINKER PROCESS                        │
  │  ┌──────────────┐   ┌──────────────┐   ┌────────────────────┐│
  │  │ Orchestrator │   │MemoryManager │   │   ToolRegistry     ││
  │  └──────┬───────┘   └──────┬───────┘   └────────┬───────────┘│
  └─────────┼──────────────────┼─────────────────────┼────────────┘
            │                  │                     │
     ┌──────▼──────┐   ┌───────▼──────┐      ┌──────▼─────────────┐
     │   OLLAMA    │   │    REDIS     │      │      SEARXNG        │
     │  :11434     │   │   :6379      │      │      :8080          │
     │             │   │              │      │  (web search)       │
     │  7B model   │   │ working mem  │      └────────────────────┘
     │  2-3B model │   │ rate limits  │
     └─────────────┘   │ dist locks   │      ┌────────────────────┐
                       └──────────────┘      │    CHROMADB        │
                                             │  ./chroma_db/      │
                                             │ (research archive) │
                                             └────────────────────┘
                                                      │
                                             ┌────────▼───────────┐
                                             │    DUCKDB          │
                                             │ session.duckdb     │
                                             │ (session artifacts)│
                                             └────────────────────┘
```

---

## Execution Modes

| Mode | Command | Services needed |
|------|---------|-----------------|
| Production | `python main.py --problem "..."` | Ollama + Redis + DuckDB + ChromaDB |
| Stub / test | `python main.py --problem "..." --stubs` | None |
| With TUI | `python main.py --problem "..." --dashboard` | Ollama + Redis |
| Dashboard only | `python -m dashboard` | Running Tinker (reads state file) |

---

## Key Configuration Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `TINKER_SERVER_URL` | `http://localhost:11434` | Primary Ollama server |
| `TINKER_SECONDARY_URL` | — | Secondary Ollama (for Critic) |
| `TINKER_REDIS_URL` | `redis://localhost:6379` | Redis working memory |
| `TINKER_ARCHITECT_TIMEOUT` | `120` | Architect call timeout (s) |
| `TINKER_CRITIC_TIMEOUT` | `60` | Critic call timeout (s) |
| `TINKER_MACRO_INTERVAL` | `14400` | Seconds between macro loops |
| `TINKER_MESO_TRIGGER` | `5` | Micro loops per meso synthesis |
| `TINKER_STATE_PATH` | `./tinker_state.json` | Dashboard state file |
| `TINKER_JSON_LOGS` | `false` | Emit JSON logs (for log aggregators) |

---

## Data Flow Lifecycle

```
user problem statement
        │
        ▼
  TaskEngine seeds initial task
        │
        ▼
  ┌─── MICRO LOOP ─────────────────────────────────────────────────────────┐
  │  task → context → architect → (researcher?) → critic → artifact         │
  │                                                      │                  │
  │                                         new tasks ◄──┘                 │
  └────────────────────────────────────────────────────────────────────────┘
        │ (every N micro loops per subsystem)
        ▼
  ┌─── MESO LOOP ──────────────────────────────────────────────────────────┐
  │  subsystem artifacts → synthesizer → subsystem design doc              │
  └────────────────────────────────────────────────────────────────────────┘
        │ (every 4 hours)
        ▼
  ┌─── MACRO LOOP ─────────────────────────────────────────────────────────┐
  │  all subsystem docs → synthesizer → full arch snapshot → git commit    │
  └────────────────────────────────────────────────────────────────────────┘
        │
        ▼
  tinker_workspace/architecture_state.json  (versioned, committed to git)
```

---

## Agent Protocols & Substitutability

Every agent role is defined by a `@runtime_checkable` Protocol, not a concrete class.
UI code and the orchestrator depend on the protocol; only the bootstrap layer touches the
concrete class.

```
agents/protocols.py
  ArchitectStrategy   async def call(task, context) -> dict
  CriticStrategy      async def call(task, architect_result) -> dict
  SynthesizerStrategy async def call(level, **kwargs) -> dict

agents/fritz/protocol.py
  VCSAgentProtocol    async def setup() -> None
                      async def commit_and_ship(message, ...) -> Any
                      async def push(branch, force) -> Any
                      async def create_pr(title, ...) -> Any
                      async def verify_connections() -> dict[str, bool]
```

The `AgentFactory` (`agents/agent_factory.py`) maps `AgentRole` → class and supports
runtime substitution:

```python
from agents.agent_factory import register_agent
from core.llm.types import AgentRole

register_agent(AgentRole.ARCHITECT, MyTestArchitect)
```

This lets tests pass lightweight stubs, and lets the application swap implementations
(e.g., a remote agent over HTTP) without touching the orchestrator.

---

## Exception Hierarchy

```
TinkerError (base — all exceptions carry .retryable + .context)
├── LLMError
│   ├── ModelClientError
│   │   ├── ModelConnectionError   (retryable=True)
│   │   ├── ModelTimeoutError      (retryable=True)
│   │   ├── ModelRateLimitError    (retryable=True)
│   │   ├── ModelServerError       (retryable=True)
│   │   └── ResponseParseError     (retryable=False)
│   ├── ModelRouterError           (retryable=False)
│   └── PromptBuilderError         (retryable=False)
├── OrchestratorError
│   ├── MicroLoopError             (retryable=True)
│   └── ConfigurationError         (retryable=False)
├── MemoryStoreError               (retryable=True)
├── TaskError
│   └── DependencyCycleError       (retryable=False)
├── ResilienceError
│   └── CircuitBreakerOpenError    (retryable=True — after recovery window)
├── ToolError                      (retryable=True)
│   └── ToolNotFoundError          (retryable=False)
├── ContextError                   (retryable=False)
├── ArchitectureError              (retryable=False)
├── ValidationError                (retryable=False — also inherits ValueError)
└── ExperimentError                (retryable=False)
```

---

## Deployment

### Docker Compose (local)

```bash
docker compose up -d        # start Redis + SearXNG
ollama serve                # start Ollama
ollama pull qwen3:7b        # pull primary model
python main.py --problem "Design a distributed job queue"
```

### Kubernetes

```bash
kubectl apply -f k8s/
# Applies: namespace · configmap · secret · pvc · deployment · service
```

### Health Probes

- **Liveness** `GET /health` — process is alive
- **Readiness** `GET /ready` — all storage connections established
- **Detail** `GET /status` — per-component status dict
