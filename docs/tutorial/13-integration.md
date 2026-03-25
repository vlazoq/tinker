# Chapter 13 — Integration: Wiring It All Together

## The Problem

We have built many individual components across previous chapters:

- `llm/` — model client and router
- `memory/` — four storage adapters and a unified manager
- `tools/` — web search, scraper, artifact writer
- `prompts/` — architect, critic, synthesizer
- `tasks/` — registry, engine, generator
- `context/` — context assembler
- `orchestrator/` — micro, meso, macro loops
- `resilience/` — circuit breakers, rate limiter, DLQ, distributed lock
- `stagnation/` — stagnation monitor
- `observability/` — audit log, SLA tracker
- `ui/web/` — FastAPI + React dashboard

Each component works in isolation (we tested them one by one).  Now we need
to *wire* them together into a running system.

---

## The Architecture Decision: `main.py`

`main.py` is the single entry point.  It does **three things only**:

1. **Configure** — read `.env` and command-line arguments
2. **Build** — instantiate every component and connect them
3. **Start** — hand off to the Orchestrator

The components themselves never import each other.  They only import their
own dependencies (e.g. `memory/manager.py` does not import `llm/router.py`).
This pattern is called **dependency injection**: every component receives what
it needs as constructor arguments, instead of going to find it.

Why does this matter?

- **Testing** — you can pass a stub LLM client without touching any other code
- **Flexibility** — swap Redis for an in-memory dict by passing a different adapter
- **Readability** — `main.py` is the complete wiring diagram.  You can see every
  connection in one file.

---

## Step 1 — Command-line Interface

```python
# main.py (top)

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Load .env if it exists
_env_file = ROOT / ".env"
if _env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file)
    except ImportError:
        pass   # python-dotenv not installed — env vars from shell still work

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt = "%H:%M:%S",
)
logger = logging.getLogger("tinker.main")


def _parse_args():
    parser = argparse.ArgumentParser(description="Tinker — AI architecture design loop")
    parser.add_argument("--problem", required=True,
                        help="The architectural problem to explore")
    parser.add_argument("--stubs",  action="store_true",
                        help="Use in-process stubs instead of real services (for testing)")
    parser.add_argument("--dashboard", action="store_true",
                        help="Launch the TUI dashboard in-process")
    return parser.parse_args()
```

---

## Step 2 — Building the Components

```python
async def _build_components(args) -> dict:
    """
    Instantiate every component and return them in a dict.
    This function is the 'wiring diagram' of the system.
    """

    # ── 1. LLM ────────────────────────────────────────────────────────────────
    from llm.client import ModelClient
    from llm.router import ModelRouter

    server_url = os.getenv("TINKER_SERVER_URL", "http://localhost:11434")
    client = ModelClient(base_url=server_url)
    router = ModelRouter(client)

    # ── 2. Memory ─────────────────────────────────────────────────────────────
    from memory.storage import RedisAdapter, DuckDBAdapter, ChromaAdapter, SQLiteAdapter
    from memory.manager import MemoryManager

    redis_url   = os.getenv("TINKER_REDIS_URL",   "redis://localhost:6379")
    session_id  = os.getenv("TINKER_SESSION_ID",  "default")

    memory = MemoryManager(
        redis  = RedisAdapter(redis_url),
        duckdb = DuckDBAdapter(os.getenv("TINKER_DUCKDB_PATH", "tinker_session.duckdb")),
        chroma = ChromaAdapter(os.getenv("TINKER_CHROMA_DIR",  "./tinker_chroma")),
        sqlite = SQLiteAdapter(os.getenv("TINKER_SQLITE_PATH", "tinker_tasks_engine.sqlite")),
    )
    await memory.connect()

    # ── 3. Tools ──────────────────────────────────────────────────────────────
    from tools.layer import ToolLayer

    tools = ToolLayer(
        searxng_url   = os.getenv("TINKER_SEARXNG_URL", "http://localhost:8888"),
        artifacts_dir = os.getenv("TINKER_ARTIFACTS_DIR", "./tinker_artifacts"),
        memory        = memory,
        session_id    = session_id,
    )

    # ── 4. Tasks ──────────────────────────────────────────────────────────────
    from tasks.registry import TaskRegistry
    from tasks.engine   import TaskEngine
    from tasks.generator import TaskGenerator

    task_registry = TaskRegistry(
        os.getenv("TINKER_TASK_DB", "tinker_tasks_engine.sqlite")
    )
    task_engine   = TaskEngine(task_registry)
    task_gen      = TaskGenerator(task_engine)
    await task_engine.initialise()

    # ── 5. Context assembler ──────────────────────────────────────────────────
    from context.assembler import ContextAssembler

    assembler = ContextAssembler(
        memory       = memory,
        token_budget = int(os.getenv("TINKER_CONTEXT_BUDGET", "4000")),
    )

    # ── 6. Resilience ─────────────────────────────────────────────────────────
    from resilience.circuit_breaker import CircuitBreaker
    from resilience.dlq             import DeadLetterQueue
    from resilience.distributed_lock import DistributedLock

    cb  = CircuitBreaker("llm_primary", failure_threshold=3, recovery_timeout=30.0)
    dlq = DeadLetterQueue(os.getenv("TINKER_DLQ_PATH", "tinker_dlq.sqlite"))
    dlq.initialise()

    lock = DistributedLock(redis_url)
    await lock.connect()

    # ── 7. Observability ──────────────────────────────────────────────────────
    from observability.audit_log   import AuditLog, AuditEventType
    from observability.sla_tracker import SLATracker

    audit = AuditLog(os.getenv("TINKER_AUDIT_LOG_PATH", "tinker_audit.sqlite"))
    await audit.connect()

    sla = SLATracker()
    sla.define("micro_loop", p95_seconds=60.0,  p99_seconds=120.0, max_seconds=300.0)
    sla.define("meso_loop",  p95_seconds=180.0, p99_seconds=300.0, max_seconds=600.0)
    sla.define("macro_loop", p95_seconds=300.0, p99_seconds=600.0, max_seconds=1800.0)

    await audit.log(AuditEventType.SYSTEM_START, actor="main",
                    details={"problem": args.problem[:200]})

    return {
        "router":   router,
        "memory":   memory,
        "tools":    tools,
        "task_engine": task_engine,
        "task_gen":    task_gen,
        "assembler":   assembler,
        "cb":          cb,
        "dlq":         dlq,
        "lock":        lock,
        "audit":       audit,
        "sla":         sla,
        "session_id":  session_id,
    }
```

---

## Step 3 — Starting the Orchestrator

```python
from orchestrator.orchestrator import Orchestrator

async def main():
    args = _parse_args()
    components = await _build_components(args)

    # Seed the task queue if it is empty
    task_gen = components["task_gen"]
    task_engine = components["task_engine"]
    if await task_engine._registry.is_empty():
        n = await task_gen.seed_from_problem(
            problem    = args.problem,
            subsystems = [
                "api_gateway", "worker_pool", "queue_manager",
                "storage_layer", "auth_service", "observability",
            ],
        )
        logger.info("Seeded %d initial tasks", n)

    # Build and start the orchestrator
    orch = Orchestrator(
        problem    = args.problem,
        session_id = components["session_id"],
        llm        = components["router"],
        memory     = components["memory"],
        tools      = components["tools"],
        task_engine= components["task_engine"],
        task_gen   = components["task_gen"],
        assembler  = components["assembler"],
        circuit_breaker = components["cb"],
        dlq        = components["dlq"],
        audit      = components["audit"],
        sla        = components["sla"],
    )

    await orch.run()


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Step 4 — The Full Component Map

Here is the complete picture of how everything connects:

```
main.py
  │
  ├─ ModelRouter ─────────────────────────────────────────────────────────────►
  │    └─ ModelClient (HTTP → Ollama)
  │
  ├─ MemoryManager ──────────────────────────────────────────────────────────►
  │    ├─ RedisAdapter   (hot cache, session data)
  │    ├─ DuckDBAdapter  (columnar analytics, artifact storage)
  │    ├─ ChromaAdapter  (vector similarity search)
  │    └─ SQLiteAdapter  (task queue, config)
  │
  ├─ ToolLayer ──────────────────────────────────────────────────────────────►
  │    ├─ WebSearchTool  (SearXNG)
  │    ├─ WebScraperTool (HTML → text)
  │    └─ ArtifactWriterTool (MemoryManager)
  │
  ├─ TaskEngine ─────────────────────────────────────────────────────────────►
  │    ├─ TaskRegistry   (SQLite)
  │    └─ TaskGenerator  (creates follow-up tasks)
  │
  ├─ ContextAssembler ────────────────────────────────────────────────────────►
  │    └─ MemoryManager (artifacts + research)
  │
  ├─ CircuitBreaker, DeadLetterQueue, DistributedLock (resilience)
  │
  ├─ AuditLog, SLATracker (observability)
  │
  └─ Orchestrator ────────────────────────────────────────────────────────────►
       │
       ├─ micro loop (every iteration)
       │    1. task_engine.next_task()
       │    2. assembler.assemble()
       │    3. router.complete(architect_prompt)
       │    4. [optional] tools.web_search()
       │    5. router.complete(critic_prompt)
       │    6. memory.store_artifact()
       │    7. task_gen.from_knowledge_gaps()
       │    8. task_engine.mark_complete()
       │
       ├─ meso loop (when subsystem hits threshold)
       │    → router.complete(synthesizer_prompt)
       │    → memory.store_artifact(type="synthesis")
       │
       └─ macro loop (every 4 hours)
            → router.complete(macro_synthesizer_prompt)
            → memory.store_artifact(type="macro_synthesis")
```

---

## Step 5 — First Run

### Prerequisites

Make sure you have:

1. Ollama running (`ollama serve`)
2. Models pulled (`ollama pull qwen3:7b`)
3. (Optional) Redis running — Tinker degrades gracefully without it
4. (Optional) SearXNG running for web research

### Running

```bash
cd tinker/
cp .env.example .env
# Edit .env — set TINKER_SERVER_URL, adjust ports if needed

python main.py --problem "Design a multi-tenant SaaS billing system"
```

You should see output like:

```
10:00:00  INFO      tinker.main             Seeded 6 initial tasks
10:00:00  INFO      tinker.orchestrator     Starting orchestrator loop
10:00:00  INFO      tinker.orchestrator     [micro] task: Initial design exploration: api_gateway
10:00:05  INFO      tinker.orchestrator     [micro] Architect response received (847 tokens)
10:00:08  INFO      tinker.orchestrator     [micro] Critic score: 0.72
10:00:08  INFO      tinker.orchestrator     [micro] Artifact stored: abc123
10:00:08  INFO      tinker.orchestrator     [micro] Loop complete in 8.3s
```

### While it's running

In a separate terminal, start the web UI:

```bash
python -m tinker.ui.web
# Open http://localhost:8082
```

Or the TUI dashboard:

```bash
python -m dashboard
```

---

## Step 6 — Common Issues

### "Connection refused" to Ollama

```
httpx.ConnectError: [Errno 111] Connection refused
```

Ollama isn't running.  Start it:
```bash
# Linux / macOS
ollama serve

# Windows
# Open Ollama from the Start menu — it runs as a tray icon
```

### Redis connection error (non-fatal)

```
WARNING  tinker.memory.storage  Redis unavailable: Connection refused
```

This is *expected* if Redis isn't running.  Tinker continues without Redis —
the DLQ and task queue use SQLite instead.  The only feature that degrades is
the distributed lock (no-op when Redis is absent).

### Empty task queue after restart

The task queue is stored in SQLite.  If you restart Tinker against the same
database, it will continue from where it left off.  If you want a fresh start:

```bash
rm tinker_tasks_engine.sqlite
python main.py --problem "..."
```

### `ModuleNotFoundError: No module named 'llm'`

You ran `python main.py` from the wrong directory.  Always run from the
`tinker/` directory:

```bash
cd /path/to/tinker
python main.py --problem "..."
```

---

## Key Concepts Introduced

| Concept | What it means |
|---------|---------------|
| Dependency injection | Components receive dependencies as constructor arguments |
| Single entry point | `main.py` is the only place where components are wired together |
| Graceful degradation | Missing Redis/SearXNG → reduced features, not a crash |
| State file as integration point | Orchestrator writes JSON; UIs read it |
| Seeding the task queue | First run creates initial exploration tasks |

The most important lesson is **dependency injection**.  The `Orchestrator`
class has no `import` statements for `llm`, `memory`, or `tools`.  It just
accepts them as parameters.  This means you can pass in anything that has the
right interface — a real LLM, a stub, a mock — without changing a single line
inside the orchestrator.  Testing becomes trivial.

---

→ Next: [Chapter 14 — Code Review: Real Bugs Found and Fixed](./14-code-review.md)
