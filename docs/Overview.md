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
├── agents.py            ← The three AI agent wrappers (Architect, Critic, Synthesizer).
├── pyproject.toml       ← Python package config and dependency list.
├── .env.example         ← Template for your environment variables (copy → .env).
├── Overview.md          ← This file.
├── INSTRUCTIONS.md      ← How to install and run everything.
│
├── llm/                 ← Talks to the AI models (Ollama)
│   ├── client.py        ← Low-level HTTP client for one Ollama server
│   ├── router.py        ← Routes requests to the right server (main vs secondary)
│   ├── types.py         ← Data types: MachineConfig, Message, AgentRole, etc.
│   ├── context.py       ← Manages conversation history / token budgets
│   └── parsing.py       ← Extracts clean JSON from messy AI responses
│
├── memory/              ← Stores and retrieves everything Tinker learns
│   ├── manager.py       ← Single entry point for all storage operations
│   ├── storage.py       ← Four storage backends in one place (Redis/DuckDB/Chroma/SQLite)
│   ├── schemas.py       ← Data shapes: Artifact, ResearchNote, Task, MemoryConfig
│   ├── embeddings.py    ← Converts text → numbers for semantic search
│   └── compression.py   ← Shrinks old artifacts when memory gets too large
│
├── tools/               ← Actions the AI can take (search, scrape, write, draw)
│   ├── registry.py      ← Manages all tools; the AI calls tools through here
│   ├── base.py          ← The base class every tool inherits from
│   ├── web_search.py    ← Search the web via a local SearXNG instance
│   ├── web_scraper.py   ← Read a webpage's content (Playwright + trafilatura)
│   ├── memory_query.py  ← Search Tinker's own research archive
│   ├── artifact_writer.py  ← Write results to structured files on disk
│   └── diagram_generator.py ← Generate architecture diagrams (Graphviz)
│
├── prompts/             ← Prompt templates for the AI agents
│   ├── builder.py       ← Assembles complete prompts from parts
│   ├── templates.py     ← The actual text templates for each agent/loop level
│   ├── schemas.py       ← JSON output schemas the AI must follow
│   ├── variants.py      ← Personality tweaks (harder critic, socratic architect, etc.)
│   ├── validator.py     ← Checks that AI output matches the expected schema
│   └── examples.py      ← Few-shot examples injected into prompts
│
├── tasks/               ← Work queue: what should Tinker think about next?
│   ├── engine.py        ← Façade: one simple interface over the queue, registry, generator
│   ├── queue.py         ← Priority queue with exploration randomisation
│   ├── registry.py      ← SQLite database of all tasks ever created
│   ├── generator.py     ← Parses Architect output to create new child tasks
│   ├── scorer.py        ← 5-factor scoring algorithm (confidence gap, recency, etc.)
│   ├── resolver.py      ← Dependency resolution with Kahn's topological sort
│   └── schema.py        ← The Task data class with all its fields
│
├── context/             ← Builds the prompt context for each AI call
│   ├── assembler.py     ← Fetches from memory, fits within token budget, builds prompt
│   └── stubs.py         ← Fake memory objects used in tests
│
├── orchestrator/        ← The main control loop that drives everything
│   ├── orchestrator.py  ← The Orchestrator class: starts loops, handles shutdown
│   ├── micro_loop.py    ← One complete micro-loop iteration
│   ├── meso_loop.py     ← Meso synthesis pass
│   ├── macro_loop.py    ← Macro snapshot pass
│   ├── config.py        ← All tunable settings (timeouts, intervals, etc.)
│   ├── state.py         ← Live state snapshot (loop counts, current task, etc.)
│   └── stubs.py         ← Fake components for testing without real AI
│
├── architecture/        ← Tracks the growing architecture design document
│   ├── manager.py       ← Stores/retrieves/merges architecture snapshots
│   ├── schema.py        ← The ArchitectureState data model
│   └── merger.py        ← Merges new AI output into the existing design
│
├── stagnation/          ← Detects when Tinker gets "stuck" and intervenes
│   ├── monitor.py       ← Runs all detectors and decides if intervention is needed
│   ├── detectors.py     ← 5 detectors: repetition, fixation, critique collapse, etc.
│   ├── embeddings.py    ← Text similarity (TF-IDF fallback, or Ollama embeddings)
│   ├── config.py        ← Thresholds for each detector
│   ├── models.py        ← Data types: StagnationEvent, InterventionDirective
│   └── event_log.py     ← SQLite log of all stagnation events
│
└── dashboard/           ← Real-time terminal UI showing what Tinker is doing
    ├── app.py           ← The Textual TUI application
    ├── subscriber.py    ← Receives state updates (queue or Redis)
    ├── state.py         ← Shared state store for the UI panels
    ├── panels.py        ← All the UI panels (re-exported from panel files)
    ├── active_task.py   ← Panel: current task being worked on
    ├── architect_critic.py ← Panels: last architect/critic outputs
    ├── loop_status.py   ← Panel: micro/meso/macro loop counters
    ├── task_queue.py    ← Panel: upcoming tasks in the queue
    ├── health_arch.py   ← Panels: system health + architecture state
    ├── log_stream.py    ← Panel: live log output
    ├── log_handler.py   ← Hooks Python's logging into the dashboard
    └── detail_view.py   ← Full-screen detail overlay for any panel
```

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
