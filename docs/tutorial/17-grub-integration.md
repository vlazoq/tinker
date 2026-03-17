# Chapter 17 — Grub + Tinker Integration

## The Full Loop

This chapter describes how Tinker and Grub work together to form the
complete system you envisioned — from problem statement to working,
tested, reviewed code.

```
You: "Design a multi-tenant billing system"
         │
         ▼
  ┌──────────────────────────────────────────────────────┐
  │  TINKER                                               │
  │                                                      │
  │  1. Seeds task queue with design tasks               │
  │  2. Micro loop: architect → critic → store           │
  │  3. Meso loop: synthesise subsystem designs          │
  │  4. Writes design artifacts to tinker_artifacts/     │
  │  5. Creates 'implementation' tasks in SQLite         │
  └──────────────────────┬───────────────────────────────┘
                         │  SQLite: type='implementation'
                         │  artifact_path='tinker_artifacts/billing.md'
                         ▼
  ┌──────────────────────────────────────────────────────┐
  │  GRUB                                                │
  │                                                      │
  │  1. TinkerBridge.fetch_implementation_tasks()        │
  │  2. Coder → Reviewer → Tester → Debugger → Refactor  │
  │  3. TinkerBridge.report_result() →                   │
  │       - marks Tinker task 'completed'                │
  │       - creates 'review' task in Tinker              │
  │  4. Writes implementation note to grub_artifacts/    │
  └──────────────────────┬───────────────────────────────┘
                         │  SQLite: type='review'
                         │  "Review Grub implementation of billing"
                         ▼
  ┌──────────────────────────────────────────────────────┐
  │  TINKER (again)                                       │
  │                                                      │
  │  1. Picks up 'review' task                           │
  │  2. Reads implementation notes                       │
  │  3. Architect refines design based on what was built │
  │  4. May create new 'implementation' tasks            │
  └──────────────────────────────────────────────────────┘
              ↑___________________|  (loop forever)
```

---

## The Integration Point: SQLite

Tinker and Grub communicate through **shared SQLite files**.  No network
calls, no message broker — just files both processes agree on.

| What | File | Who writes | Who reads |
|------|------|-----------|-----------|
| Design tasks | `tinker_tasks_engine.sqlite` | Tinker | Grub |
| Design artifacts | `tinker_artifacts/*.md` | Tinker | Grub |
| Impl results | `grub_artifacts/*.md` | Grub | Tinker |
| Review tasks | `tinker_tasks_engine.sqlite` | Grub | Tinker |

Both processes run **independently**.  Tinker doesn't know Grub exists.
Grub just reads from Tinker's database.  The only coupling is:
1. The database schema (Grub reads `tasks` table, writes rows back)
2. The artifact directory location (configured in `.env`)

---

## How Tinker Creates Implementation Tasks

When Tinker finishes designing a subsystem, it creates a task with
`type='implementation'`.  This tells Grub "this subsystem is ready
to be coded up".

Tinker's `TaskGenerator` needs a small addition to emit these:

```python
# In tasks/generator.py — add this method

async def create_implementation_task(
    self,
    title:         str,
    description:   str,
    subsystem:     str,
    artifact_path: str,
) -> str:
    """
    Create an 'implementation' task that Grub will pick up.

    Call this when a design artifact is complete and ready for coding.

    Parameters
    ----------
    title         : Short task title.
    description   : What to implement (reference to design artifact).
    subsystem     : Which subsystem this is for.
    artifact_path : Path to the Tinker design document.

    Returns
    -------
    str : The new task ID.
    """
    import uuid, json
    from datetime import datetime, timezone
    from tasks.schema import TaskType, TaskStatus

    task_id = str(uuid.uuid4())
    now     = datetime.now(timezone.utc).isoformat()
    meta    = json.dumps({"artifact_path": artifact_path})

    await self._registry._engine.execute(
        """INSERT OR IGNORE INTO tasks
           (id, title, description, type, subsystem, status,
            confidence_gap, is_exploration, created_at, updated_at,
            priority_score, staleness_hours, dependency_depth,
            last_subsystem_work_hours, attempt_count,
            dependencies, outputs, tags, metadata)
           VALUES (?,?,?,?,?,'pending',0.5,0,?,?,0.7,0.0,0,0.0,0,
                   '[]','[]','["grub_ready"]',?)""",
        (task_id, title, description,
         TaskType.IMPLEMENTATION.value, subsystem,
         now, now, meta)
    )
    return task_id
```

When to call this: after `run_meso_loop()` completes a synthesis and
writes a design artifact.  The orchestrator calls it like:

```python
# In orchestrator/orchestrator.py — after meso synthesis

if synthesis_artifact_path:
    await task_gen.create_implementation_task(
        title         = f"Implement {subsystem} from design",
        description   = f"Read {synthesis_artifact_path} and implement it.",
        subsystem     = subsystem,
        artifact_path = synthesis_artifact_path,
    )
    logger.info("Created implementation task for %s", subsystem)
```

---

## TinkerBridge — the integration class

`grub/feedback.py` contains `TinkerBridge` — the only class that
knows about both Tinker and Grub.

### Fetching tasks (Tinker → Grub)

```python
bridge = TinkerBridge(
    tinker_tasks_db      = "tinker_tasks_engine.sqlite",
    tinker_artifacts_dir = "./tinker_artifacts",
    grub_artifacts_dir   = "./grub_artifacts",
)

# Returns list[GrubTask] converted from Tinker's implementation tasks
tasks = bridge.fetch_implementation_tasks(limit=10)
```

Under the hood, this runs:
```sql
SELECT id, title, description, subsystem, metadata
FROM tasks
WHERE type='implementation' AND status='pending'
ORDER BY priority_score DESC
LIMIT 10
```

### Reporting results (Grub → Tinker)

```python
# After the pipeline runs:
bridge.report_result(result, tinker_task_id=original_task.tinker_task_id)
```

Under the hood, this does two things:
1. `UPDATE tasks SET status='completed' WHERE id=?`
2. Inserts a new task with `type='review'` and the implementation summary
   as the description, so Tinker picks it up in its next loop

---

## Running Both Together

### Option 1 — Two terminals (recommended for development)

```bash
# Terminal 1: Tinker (design loop)
cd tinker/
python main.py --problem "Design a multi-tenant billing system"

# Terminal 2: Grub (implementation loop)
cd tinker/
python -m grub
```

Both read/write the same SQLite files.  They run independently — if one
crashes, the other keeps going.

### Option 2 — Two machines (production setup)

```
Machine 1 (RTX 3090, 24GB VRAM):
  - Runs Tinker with qwen3:14b or larger
  - Runs Grub with qwen2.5-coder:32b
  - Shared directory mounted on both machines (NFS, Samba, or rsync)

Machine 2 (daily PC):
  - Runs Grub in queue worker mode with a smaller model:
    GRUB_EXEC_MODE=queue python -m grub --mode worker --worker-id daily-pc
```

Both machines point to the same SQLite files (shared mount).

### Option 3 — Sequential (single machine, cautious start)

If you just want to verify everything works end-to-end before committing:

```python
# scripts/run_one_cycle.py
import asyncio
from grub.agent import GrubAgent

async def main():
    agent = GrubAgent.from_config()
    # Fetch exactly the pending implementation tasks right now
    tasks = agent.bridge.fetch_implementation_tasks(limit=3)
    print(f"Found {len(tasks)} implementation tasks")
    results = await agent.run_tasks(tasks)
    for task, result in zip(tasks, results):
        print(f"  {task.title}: {result.status.value} (score={result.score:.2f})")
        agent.bridge.report_result(result, task.tinker_task_id)

asyncio.run(main())
```

---

## Environment Variables

Add these to your `.env` file to configure the integration:

```bash
# === Grub settings ===
GRUB_EXEC_MODE=sequential          # sequential | parallel | queue
GRUB_CODER_MODEL=qwen2.5-coder:32b # model for CoderMinion
GRUB_REVIEWER_MODEL=qwen3:7b        # model for ReviewerMinion
GRUB_TESTER_MODEL=qwen3:7b          # model for TesterMinion
GRUB_DEBUGGER_MODEL=qwen2.5-coder:32b
GRUB_REFACTORER_MODEL=qwen2.5-coder:7b
GRUB_QUALITY_THRESHOLD=0.75         # reviewer score needed to accept
GRUB_MAX_ITERATIONS=5               # max retries per minion
GRUB_OUTPUT_DIR=./grub_output       # where Grub writes code files
GRUB_ARTIFACTS_DIR=./grub_artifacts # where Grub writes notes

# === Tinker ↔ Grub shared paths ===
TINKER_TASK_DB=./tinker_tasks_engine.sqlite
TINKER_ARTIFACTS_DIR=./tinker_artifacts
```

---

## Troubleshooting

### "No implementation tasks found"

Tinker hasn't created any `implementation` type tasks yet.  Check:
```bash
sqlite3 tinker_tasks_engine.sqlite "SELECT type, status, COUNT(*) FROM tasks GROUP BY type, status"
```

If you see only `design/pending` tasks, Tinker is still in early design phase.
Either wait, or manually insert a test task:
```bash
sqlite3 tinker_tasks_engine.sqlite "
  INSERT INTO tasks
  (id, title, description, type, subsystem, status, confidence_gap, is_exploration,
   created_at, updated_at, priority_score, staleness_hours, dependency_depth,
   last_subsystem_work_hours, attempt_count, dependencies, outputs, tags, metadata)
  VALUES
  ('test-001', 'Test: implement hello world', 'Write hello.py that prints Hello World',
   'implementation', 'test', 'pending', 0.5, 0,
   datetime('now'), datetime('now'), 0.5, 0, 0, 0, 0, '[]', '[]', '[]', '{}')
"
```

### "Grub wrote files but they're in the wrong place"

Check `GRUB_OUTPUT_DIR` in your `.env`.  Default is `./grub_output` relative
to where you run `python -m grub`.  Use an absolute path to be explicit:
```bash
GRUB_OUTPUT_DIR=/home/you/my_project/src
```

### "Reviewer always scores below threshold"

Either:
- The model is too small (try a bigger model for the reviewer)
- The threshold is too high (try 0.6 instead of 0.75)
- The design artifact is missing (check `artifact_path` in the task)

Lower the threshold for initial testing:
```json
{ "quality_threshold": 0.6 }
```

### "Grub and Tinker are writing to different SQLite files"

Both must point to the same file.  Check:
- `TINKER_TASK_DB` in `.env`
- `tinker_tasks_db` in `grub_config.json`
They must be the same absolute path.

---

## Summary: The Complete System

You now have the full pipeline:

```
Problem statement
    │
    ▼
Tinker: thinks, researches, designs
  - Micro loop: one task → architect → critic → store
  - Meso loop: synthesise subsystem
  - Macro loop: high-level snapshot
  - Emits: design artifacts + implementation tasks
    │
    ▼
Grub: reads designs, writes code
  - Coder: implements from design
  - Reviewer: judges quality (score + feedback)
  - Tester: writes + runs pytest tests
  - Debugger: fixes failures
  - Refactorer: cleans up working code
  - Reports: back to Tinker with what was built
    │
    ▼
Tinker: sees what was built, refines design, generates more tasks
    │
    ▼
  (loop forever, or until you're happy)
```

This is the "infinite state machine" you originally described —
one model working, another judging, the loop never stopping until
quality is reached.

---

← Back to [Chapter 16 — Minions in Detail](./16-grub-minions.md)
← Back to [Tutorial Index](./README.md)
