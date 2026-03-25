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
├── orchestrator/
│   ├── orchestrator.py     ← main loop driver
│   ├── micro_loop.py       ← per-iteration logic
│   ├── meso_loop.py        ← subsystem synthesis
│   ├── macro_loop.py       ← full arch snapshot
│   ├── state.py            ← live state model (OrchestratorState)
│   ├── config.py           ← OrchestratorConfig (all knobs)
│   ├── stubs.py            ← in-process test doubles
│   └── compat.py           ← coroutine_if_needed() helper
│
├── agents.py               ← ArchitectAgent · CriticAgent · SynthesizerAgent
│   ├── prompts/builder.py
│   ├── llm/router.py
│   └── resilience/retry.py
│
├── llm/
│   ├── router.py           ← ModelRouter (ARCHITECT→7B, CRITIC→2-3B)
│   ├── client.py           ← OllamaClient (HTTP + retry)
│   ├── types.py            ← AgentRole · Machine · ModelRequest/Response
│   ├── parsing.py          ← JSON extraction from model output
│   └── context.py          ← context-window trimming
│
├── tasks/
│   ├── engine.py           ← TaskEngine façade
│   ├── queue.py            ← priority queue
│   ├── registry.py         ← SQLite-backed task registry
│   ├── generator.py        ← parse AI output → Task objects
│   ├── scorer.py           ← 5-factor priority scorer
│   ├── resolver.py         ← dependency topological sort
│   └── schema.py           ← Task · TaskStatus · TaskType
│
├── memory/
│   ├── manager.py          ← MemoryManager (unified interface)
│   ├── storage.py          ← Redis · DuckDB · Chroma · SQLite adapters
│   ├── schemas.py          ← Artifact · ResearchNote · Task · MemoryConfig
│   ├── embeddings.py       ← text → vector (sentence-transformers / TF-IDF)
│   └── compression.py      ← archive old artifacts when threshold hit
│
├── context/
│   ├── assembler.py        ← ContextAssembler (prompt dict builder)
│   ├── memory_adapter.py   ← MemoryManager → assembler interface
│   └── prompt_builder_adapter.py
│
├── tools/
│   ├── registry.py         ← ToolRegistry
│   ├── base.py             ← BaseTool protocol
│   ├── web_search.py       ← SearXNG
│   ├── web_scraper.py      ← Playwright
│   ├── artifact_writer.py  ← write files to workspace
│   ├── diagram_generator.py← Mermaid / Graphviz
│   └── memory_query.py     ← semantic search tool
│
├── architecture/
│   ├── manager.py          ← ArchitectureStateManager (Git-backed)
│   ├── schema.py           ← ArchitectureState · Component · Decision
│   └── merger.py           ← intelligent conflict-resolution merge
│
├── stagnation/
│   ├── monitor.py          ← StagnationMonitor (5 detectors)
│   ├── detectors.py        ← Semantic · Fixation · Critique · Research · Starvation
│   ├── models.py           ← InterventionDirective · StagnationEvent
│   ├── embeddings.py
│   ├── config.py
│   └── event_log.py        ← SQLite stagnation history
│
├── resilience/
│   ├── circuit_breaker.py  ← CircuitBreaker · CircuitBreakerRegistry
│   ├── rate_limiter.py     ← RateLimiterRegistry (token-bucket)
│   ├── idempotency.py      ← IdempotencyCache (SHA-256 dedup)
│   ├── backpressure.py     ← BackpressureController
│   ├── dead_letter_queue.py← DeadLetterQueue (SQLite)
│   ├── distributed_lock.py ← Redis-backed distributed lock
│   ├── retry.py            ← retry_async() with exponential backoff
│   ├── auto_recovery.py
│   └── migrations.py       ← SQLite schema migration runner
│
├── observability/
│   ├── audit_log.py        ← AuditLog (append-only SQLite)
│   ├── tracing.py          ← Tracer (distributed trace spans)
│   ├── sla_tracker.py      ← p50/p95/p99 per loop type
│   ├── alerting.py         ← AlertManager (Slack / webhook)
│   ├── structured_logging.py
│   └── otlp.py             ← OpenTelemetry export
│
├── grub/                   ← Code Implementation Subsystem
│   ├── agent.py            ← GrubAgent (minion orchestrator)
│   ├── loop.py             ← minion pipeline
│   ├── minions/            ← Coder · Tester · Reviewer · Debugger · Refactorer
│   └── contracts/          ← GrubTask · MinionResult
│
├── dashboard/
│   ├── app.py              ← Textual TUI
│   ├── panels.py           ← LoopStatus · ActiveTask · TaskQueue · HealthArch
│   ├── subscriber.py       ← asyncio.Queue state subscriber
│   └── orchestrator_integration.py
│
├── health/
│   └── http_server.py      ← /health · /ready · /status  (Kubernetes probes)
│
├── features/
│   └── flags.py            ← FeatureFlags (runtime enable/disable)
│
├── backup/
│   └── backup_manager.py   ← BackupManager (DuckDB+SQLite+Chroma snapshots)
│
├── capacity/
│   └── planner.py          ← CapacityPlanner (token/disk growth projections)
│
├── lineage/
│   └── tracker.py          ← LineageTracker (artifact derivation graph)
│
├── experiments/
│   ├── ab_testing.py       ← ABTestingFramework
│   └── offline_eval.py     ← offline design quality metrics
│
├── security/
│   ├── encryption.py       ← AES-256 data at rest
│   └── secrets.py          ← secrets management
│
├── validation/
│   └── input_validator.py  ← boundary validation (problem stmt · task · URL · path · JSON)
│
└── exceptions.py           ← TinkerError hierarchy (single source of truth)
```

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
  │              RateLimiter.acquire()              │  token-bucket throttling
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
