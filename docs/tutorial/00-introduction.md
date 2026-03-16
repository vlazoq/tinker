# Chapter 00 — Introduction & The Big Picture

Before writing a single line of code, you need to understand *what* you are
building and *why* it is designed the way it is.  Skip this chapter and you
will spend the rest of the tutorial confused about why things are the way
they are.

---

## The Problem We Are Solving

Imagine you are an architect designing a large software system.  You sit
down every morning and work through a list of design questions:

- "How should the API gateway handle authentication?"
- "What's the right caching strategy for user sessions?"
- "Should the message queue be pull-based or push-based?"

You write notes, critique your own ideas, synthesise findings across
subsystems, and eventually commit a design document.

**Tinker automates this loop.**  It runs 24/7, picking design questions
from a task queue, asking an AI to think through them, asking a second AI
to critique the answer, summarising the findings, and periodically
committing a high-level architectural snapshot to a Git repository.

---

## The Three Reasoning Loops

Tinker has three loops nested inside each other, running at different
speeds.  This is the most important concept to understand.

```
┌─────────────────────────────────────────────────────────────────┐
│  MACRO loop — fires every 4 hours                               │
│  "Write a full architectural snapshot and commit it to Git"     │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  MESO loop — fires every N micro loops per subsystem      │  │
│  │  "Synthesise all recent micro results for one subsystem"  │  │
│  │                                                           │  │
│  │  ┌─────────────────────────────────────────────────────┐  │  │
│  │  │  MICRO loop — runs constantly                       │  │  │
│  │  │  "Pick one task, think about it, critique it,       │  │  │
│  │  │   store the result, generate follow-up tasks"       │  │  │
│  │  └─────────────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

The micro loop is the workhorse — it might complete hundreds of times per
hour.  The meso loop fires when a subsystem has accumulated enough micro
results to be worth synthesising (default: every 5 micro loops).  The macro
loop fires on a timer and commits a system-wide design snapshot to Git.

---

## The Component Map

Here is every module we will build and how they connect:

```
main.py  (the wiring harness — we build this last)
  │
  ├── llm/           ← Chapter 02: talks to Ollama AI models
  │     client.py      async HTTP client
  │     router.py      routes calls between primary and secondary models
  │
  ├── memory/        ← Chapter 03: stores everything the AI produces
  │     storage.py    four adapters (Redis, DuckDB, ChromaDB, SQLite)
  │     manager.py    unified interface over all four
  │
  ├── tools/         ← Chapter 04: gives the AI access to the internet
  │     search.py     web search via SearXNG
  │     scraper.py    web page content fetching
  │     writer.py     writing artifacts to disk
  │
  ├── prompts/       ← Chapter 05: what we say to the AI
  │     architect.py  the main design prompt
  │     critic.py     the review prompt
  │     synthesizer.py the summarisation prompt
  │
  ├── tasks/         ← Chapter 06: tracks what needs to be done
  │     registry.py   SQLite task database
  │     engine.py     scoring and selection
  │     generator.py  creates follow-up tasks
  │
  ├── context/       ← Chapter 07: assembles context for the AI
  │     assembler.py  token-budgeted context window assembly
  │
  ├── orchestrator/  ← Chapter 08: the main loop controller
  │     state.py       what the orchestrator knows about itself
  │     micro_loop.py  one full task cycle
  │     meso_loop.py   subsystem synthesis
  │     macro_loop.py  architectural snapshot
  │     orchestrator.py the main class
  │
  ├── resilience/    ← Chapter 09: handles failures gracefully
  │     circuit_breaker.py
  │     distributed_lock.py
  │     dlq.py        dead letter queue
  │     rate_limiter.py
  │
  ├── stagnation/    ← Chapter 10: prevents the AI from going in circles
  │     monitor.py
  │
  ├── health/        ← Chapter 11: observability
  │     http_server.py HTTP health endpoints
  │
  ├── metrics/       ← Chapter 11: Prometheus metrics
  │
  ├── dashboard/     ← Chapter 11: terminal UI
  │
  └── webui/         ← Chapter 12: browser UI
        app.py        FastAPI routes
        core.py       shared data helpers
        templates/    React single-page app
        static/       CSS
```

---

## Key Design Decisions (and Why)

Understanding these decisions will make the code make sense.

### Decision 1: Dependency Injection

Every major component is built in isolation and *injected* into the
orchestrator at startup.  The orchestrator never imports `ModelClient`
directly — it receives a `router` object and calls `router.complete()`.

**Why?**  In tests you inject a fake (`StubRouter`) that returns canned
responses.  The orchestrator code doesn't change at all — only what you
pass in changes.  This is called *dependency injection* and it is one of
the most important patterns in software engineering.

### Decision 2: Four Memory Tiers

Different types of data have different lifespans:

| Store | Lifespan | Used for |
|-------|---------|----------|
| Redis | Per-task, minutes | The AI's working scratchpad for the current task |
| DuckDB | Per-session, hours | Artifacts produced by each micro loop |
| ChromaDB | Permanent, vector search | Research archive that grows forever |
| SQLite | Permanent, relational | Task registry, audit log, DLQ |

Using the right store for the right data keeps the system fast and avoids
using expensive vector search for simple task lookups.

### Decision 3: Asyncio Throughout

Every component is `async`.  This means the orchestrator can wait for an
AI response (which takes several seconds) without freezing — it can check
the shutdown flag, write state snapshots, and handle other events *while*
waiting.

**Why not threads?**  Asyncio is simpler for I/O-heavy code (AI calls,
database writes, HTTP requests).  You don't have to worry about shared
memory corruption, and the code reads sequentially from top to bottom.

### Decision 4: State File for Dashboard Communication

The orchestrator writes its state to a JSON file after every micro loop.
The dashboard (web UI, TUI) reads that file.  They don't share memory.

**Why?**  It is trivially simple, crash-safe (the dashboard never brings
down the orchestrator), and works across processes, machines, and even
after restart.

### Decision 5: Feature Flags

Almost every significant feature can be turned off via a feature flag.  This
lets you run Tinker in a minimal mode (flags off) for testing, then turn
features on one at a time in production.

---

## What You Will Have at the End

After completing all 14 chapters you will have:

1. A fully working autonomous AI reasoning system
2. A FastAPI web dashboard with a React front-end
3. Two alternative UIs (Gradio, Streamlit)
4. A TUI terminal dashboard
5. Production-grade resilience (circuit breakers, rate limiting, DLQ)
6. Full observability (health endpoints, Prometheus metrics, audit log)
7. Cross-platform support (Linux, macOS, Windows)
8. A complete understanding of *why* every design decision was made

---

## A Note on "Junior-Friendly"

This tutorial does not hide complexity.  It explains it.

When you see something unfamiliar — a decorator, a dataclass, an `async`
keyword — there will be a short explanation of what it does and *why* it
exists.  By the end you will be comfortable with patterns that many
senior engineers use every day.

---

**Ready?  Let's start with the Python concepts you need.**

→ Next: [Chapter 01 — Python Prerequisites](./01-python-prerequisites.md)
