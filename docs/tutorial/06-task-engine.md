# Chapter 06 — The Task Engine

## The Problem

Tinker needs work to do.  We need:

1. A place to store tasks ("Design the authentication strategy for the API gateway")
2. A way to pick the *best* task to work on next (not just first-in-first-out)
3. A way to generate new follow-up tasks based on what the AI discovers

---

## The Architecture Decision

We use **SQLite** as the task store.  It is:
- Durable (survives restarts, crashes)
- Zero-configuration (no server needed)
- Queryable with SQL (easy to sort, filter, count)
- Available everywhere including Windows

The task schema has a `priority_score` column that the orchestrator
uses to pick the next task.  A higher score means "work on me next".
The scoring algorithm considers:

- **Confidence gap** — how uncertain is the design in this area?  High uncertainty → higher priority
- **Dependency depth** — is this a foundational decision others depend on?
- **Staleness** — has this subsystem been ignored for a long time?
- **Exploration flag** — is this a new area worth exploring?

We also have a `TaskGenerator` that creates follow-up tasks from the
Architect's output (it reads the `knowledge_gaps` field from `ArchitectResult`).

---

## Step 1 — Directory Structure

```
tinker/
  tasks/
    __init__.py
    registry.py    ← SQLite task store
    engine.py      ← scoring and selection
    generator.py   ← creating new tasks from AI output
```

---

## Step 2 — Task Registry

```python
# tinker/tasks/registry.py

"""
TaskRegistry — SQLite-backed task storage.

A 'task' represents one unit of architectural design work.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    PENDING   = "pending"
    ACTIVE    = "active"
    COMPLETE  = "complete"
    FAILED    = "failed"
    BLOCKED   = "blocked"


class TaskType(str, Enum):
    DESIGN      = "design"
    RESEARCH    = "research"
    CRITIQUE    = "critique"
    SYNTHESIS   = "synthesis"
    EXPLORATION = "exploration"


@dataclass
class Task:
    """One unit of design work."""
    id:               str
    title:            str
    description:      str
    type:             TaskType
    subsystem:        str
    status:           TaskStatus
    priority_score:   float          = 0.5
    confidence_gap:   float          = 0.5    # 0=confident, 1=very uncertain
    is_exploration:   bool           = False
    attempt_count:    int            = 0
    dependency_depth: int            = 0
    staleness_hours:  float          = 0.0
    created_at:       str            = ""
    updated_at:       str            = ""
    dependencies:     list[str]      = field(default_factory=list)
    tags:             list[str]      = field(default_factory=list)
    metadata:         dict           = field(default_factory=dict)


# SQL to create the tasks table the first time we connect
_CREATE_TASKS_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id               TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    type             TEXT NOT NULL DEFAULT 'design',
    subsystem        TEXT NOT NULL DEFAULT 'cross_cutting',
    status           TEXT NOT NULL DEFAULT 'pending',
    priority_score   REAL NOT NULL DEFAULT 0.5,
    confidence_gap   REAL NOT NULL DEFAULT 0.5,
    is_exploration   INTEGER NOT NULL DEFAULT 0,
    attempt_count    INTEGER NOT NULL DEFAULT 0,
    dependency_depth INTEGER NOT NULL DEFAULT 0,
    staleness_hours  REAL NOT NULL DEFAULT 0.0,
    last_subsystem_work_hours REAL NOT NULL DEFAULT 0.0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    dependencies     TEXT NOT NULL DEFAULT '[]',
    tags             TEXT NOT NULL DEFAULT '[]',
    metadata         TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_tasks_status   ON tasks (status);
CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks (priority_score DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_subsystem ON tasks (subsystem);
"""


class TaskRegistry:
    """
    Async task store backed by SQLite.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=5, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        return con

    async def initialise(self) -> None:
        """Create tables if they don't exist."""
        def _run():
            con = self._connect()
            con.executescript(_CREATE_TASKS_SQL)
            con.commit()
            con.close()
        await asyncio.to_thread(_run)
        logger.info("TaskRegistry initialised at %s", self.db_path)

    async def add_task(self, task: Task) -> None:
        """Insert a new task.  Does nothing if the ID already exists."""
        ts = datetime.now(timezone.utc).isoformat()
        def _run():
            con = self._connect()
            con.execute(
                """INSERT OR IGNORE INTO tasks
                   (id, title, description, type, subsystem, status,
                    priority_score, confidence_gap, is_exploration,
                    attempt_count, dependency_depth, staleness_hours,
                    last_subsystem_work_hours,
                    created_at, updated_at, dependencies, tags, metadata)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)""",
                (
                    task.id, task.title, task.description,
                    task.type.value if isinstance(task.type, TaskType) else task.type,
                    task.subsystem,
                    task.status.value if isinstance(task.status, TaskStatus) else task.status,
                    task.priority_score, task.confidence_gap,
                    1 if task.is_exploration else 0,
                    task.attempt_count, task.dependency_depth, task.staleness_hours,
                    ts, ts,
                    json.dumps(task.dependencies),
                    json.dumps(task.tags),
                    json.dumps(task.metadata),
                )
            )
            con.commit()
            con.close()
        await asyncio.to_thread(_run)

    async def get_next_pending(self) -> Optional[Task]:
        """Return the highest-priority pending task, or None if empty."""
        def _run():
            con = self._connect()
            row = con.execute(
                """SELECT * FROM tasks
                   WHERE status = 'pending'
                   ORDER BY priority_score DESC, created_at ASC
                   LIMIT 1"""
            ).fetchone()
            con.close()
            return dict(row) if row else None
        data = await asyncio.to_thread(_run)
        if data is None:
            return None
        return _row_to_task(data)

    async def set_status(self, task_id: str, status: TaskStatus) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        def _run():
            con = self._connect()
            con.execute(
                "UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                (status.value, ts, task_id)
            )
            con.commit()
            con.close()
        await asyncio.to_thread(_run)

    async def increment_attempt(self, task_id: str) -> None:
        def _run():
            con = self._connect()
            con.execute(
                "UPDATE tasks SET attempt_count = attempt_count + 1 WHERE id=?",
                (task_id,)
            )
            con.commit()
            con.close()
        await asyncio.to_thread(_run)

    async def count_by_status(self) -> dict[str, int]:
        """Return {status: count} for all tasks."""
        def _run():
            con = self._connect()
            rows = con.execute(
                "SELECT status, COUNT(*) as n FROM tasks GROUP BY status"
            ).fetchall()
            con.close()
            return {r["status"]: r["n"] for r in rows}
        return await asyncio.to_thread(_run)

    async def is_empty(self) -> bool:
        def _run():
            con = self._connect()
            n = con.execute("SELECT COUNT(*) FROM tasks WHERE status='pending'").fetchone()[0]
            con.close()
            return n == 0
        return await asyncio.to_thread(_run)


def _row_to_task(data: dict) -> Task:
    """Convert a SQLite row dict to a Task dataclass."""
    return Task(
        id              = data["id"],
        title           = data["title"],
        description     = data.get("description", ""),
        type            = TaskType(data.get("type", "design")),
        subsystem       = data.get("subsystem", "cross_cutting"),
        status          = TaskStatus(data.get("status", "pending")),
        priority_score  = float(data.get("priority_score", 0.5)),
        confidence_gap  = float(data.get("confidence_gap", 0.5)),
        is_exploration  = bool(data.get("is_exploration", 0)),
        attempt_count   = int(data.get("attempt_count", 0)),
        dependency_depth= int(data.get("dependency_depth", 0)),
        staleness_hours = float(data.get("staleness_hours", 0.0)),
        created_at      = data.get("created_at", ""),
        updated_at      = data.get("updated_at", ""),
        dependencies    = json.loads(data.get("dependencies", "[]")),
        tags            = json.loads(data.get("tags", "[]")),
        metadata        = json.loads(data.get("metadata", "{}")),
    )
```

---

## Step 3 — Task Engine (Scoring + Selection)

```python
# tinker/tasks/engine.py

"""
TaskEngine — computes priority scores and selects the next task.

Priority score formula (all values 0.0–1.0, weighted sum):
  score = (
    0.4 * confidence_gap          # high uncertainty = high priority
  + 0.2 * depth_factor            # foundational tasks (deeper deps) score higher
  + 0.2 * staleness_factor        # subsystems not recently worked on score higher
  + 0.1 * exploration_bonus       # exploration tasks get a small boost
  + 0.1 * (1 - attempt_penalty)   # tasks tried many times score lower
  )
"""
from __future__ import annotations

import logging
import math
from typing import Optional

from .registry import Task, TaskRegistry, TaskStatus

logger = logging.getLogger(__name__)

# Subsystems that haven't been worked on in this many hours get a staleness boost
STALENESS_THRESHOLD_HOURS = 2.0


class TaskEngine:
    """
    Wraps TaskRegistry with priority scoring and next-task selection.
    """

    def __init__(self, registry: TaskRegistry) -> None:
        self._registry = registry

    async def initialise(self) -> None:
        await self._registry.initialise()

    async def next_task(self) -> Optional[Task]:
        """
        Re-score all pending tasks and return the highest-priority one.
        Returns None if no pending tasks exist.
        """
        # For simplicity we rely on the priority_score already in the DB.
        # A more sophisticated implementation would re-score here.
        return await self._registry.get_next_pending()

    @staticmethod
    def compute_score(
        confidence_gap: float,
        dependency_depth: int,
        staleness_hours: float,
        is_exploration: bool,
        attempt_count: int,
    ) -> float:
        """
        Compute a priority score for a task.
        Higher score = pick this task sooner.
        """
        # Normalise depth: more dependencies = more foundational
        depth_factor = min(dependency_depth / 5.0, 1.0)

        # Staleness: scale from 0 to 1 over STALENESS_THRESHOLD_HOURS
        staleness = min(staleness_hours / STALENESS_THRESHOLD_HOURS, 1.0)

        # Exploration bonus
        exploration = 0.1 if is_exploration else 0.0

        # Attempt penalty: tasks tried many times might be stuck
        attempt_penalty = min(attempt_count * 0.1, 0.3)

        score = (
            0.4 * confidence_gap
            + 0.2 * depth_factor
            + 0.2 * staleness
            + 0.1 * exploration
            + 0.1 * (1.0 - attempt_penalty)
        )
        return round(min(max(score, 0.0), 1.0), 4)

    async def mark_active(self, task_id: str) -> None:
        await self._registry.set_status(task_id, TaskStatus.ACTIVE)
        await self._registry.increment_attempt(task_id)

    async def mark_complete(self, task_id: str) -> None:
        await self._registry.set_status(task_id, TaskStatus.COMPLETE)

    async def mark_failed(self, task_id: str) -> None:
        await self._registry.set_status(task_id, TaskStatus.FAILED)

    async def requeue(self, task_id: str) -> None:
        """Put a task back to pending (e.g. after a transient failure)."""
        await self._registry.set_status(task_id, TaskStatus.PENDING)
```

---

## Step 4 — Task Generator

```python
# tinker/tasks/generator.py

"""
TaskGenerator — creates follow-up tasks from AI output.

When the Architect identifies knowledge gaps, the generator converts each
one into a new RESEARCH or DESIGN task and adds it to the registry.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from .registry import Task, TaskRegistry, TaskStatus, TaskType
from .engine   import TaskEngine

logger = logging.getLogger(__name__)


class TaskGenerator:
    """
    Creates new tasks from knowledge gaps and other signals.
    """

    def __init__(self, engine: TaskEngine) -> None:
        self._engine = engine

    async def from_knowledge_gaps(
        self,
        gaps: list[str],
        subsystem: str,
        parent_task_id: str,
        confidence_gap: float = 0.7,
    ) -> list[str]:
        """
        Convert a list of knowledge gap strings into new RESEARCH tasks.
        Returns a list of the new task IDs.
        """
        new_ids = []
        for gap in gaps[:5]:   # cap at 5 to avoid flooding the queue
            task_id = str(uuid.uuid4())
            ts      = datetime.now(timezone.utc).isoformat()
            task = Task(
                id             = task_id,
                title          = f"Research: {gap[:80]}",
                description    = (
                    f"The Architect flagged this as a knowledge gap while working "
                    f"on task {parent_task_id}.\n\nGap: {gap}"
                ),
                type           = TaskType.RESEARCH,
                subsystem      = subsystem,
                status         = TaskStatus.PENDING,
                priority_score = self._engine.compute_score(
                    confidence_gap   = confidence_gap,
                    dependency_depth = 0,
                    staleness_hours  = 0.0,
                    is_exploration   = True,
                    attempt_count    = 0,
                ),
                confidence_gap  = confidence_gap,
                is_exploration  = True,
                created_at      = ts,
                metadata        = {"parent_task_id": parent_task_id},
            )
            await self._engine._registry.add_task(task)
            new_ids.append(task_id)
            logger.debug("Generated research task: %s", task.title)

        return new_ids

    async def seed_from_problem(
        self,
        problem: str,
        subsystems: list[str],
    ) -> int:
        """
        Create initial exploration tasks from a problem statement.
        Called once at startup if the task queue is empty.
        Returns how many tasks were created.
        """
        tasks_created = 0
        for subsystem in subsystems:
            ts = datetime.now(timezone.utc).isoformat()
            task = Task(
                id            = str(uuid.uuid4()),
                title         = f"Initial design exploration: {subsystem}",
                description   = (
                    f"Explore the design space for the {subsystem} subsystem "
                    f"in the context of: {problem}"
                ),
                type          = TaskType.EXPLORATION,
                subsystem     = subsystem,
                status        = TaskStatus.PENDING,
                priority_score= 0.5,
                confidence_gap= 0.8,   # high uncertainty at the start
                is_exploration= True,
                created_at    = ts,
            )
            await self._engine._registry.add_task(task)
            tasks_created += 1

        logger.info("Seeded %d exploration tasks", tasks_created)
        return tasks_created
```

---

## Step 5 — Try It

```python
# test_tasks.py
import asyncio
from tasks.registry import TaskRegistry, Task, TaskType, TaskStatus
from tasks.engine   import TaskEngine
from tasks.generator import TaskGenerator

async def main():
    registry  = TaskRegistry("test_tasks.sqlite")
    engine    = TaskEngine(registry)
    generator = TaskGenerator(engine)

    await engine.initialise()

    # Seed some tasks
    n = await generator.seed_from_problem(
        problem="Design a distributed task queue",
        subsystems=["api_gateway", "queue_manager", "worker_pool"],
    )
    print(f"Created {n} initial tasks")

    # Pick the next task
    task = await engine.next_task()
    if task:
        print(f"Next task: [{task.type.value}] {task.title}")
        print(f"Priority: {task.priority_score}  Subsystem: {task.subsystem}")
        await engine.mark_active(task.id)

    # Check counts
    counts = await registry.count_by_status()
    print(f"Status counts: {counts}")

asyncio.run(main())
```

---

## What We Have So Far

```
tinker/
  llm/         ✅  model client + router
  memory/      ✅  four adapters + unified manager
  tools/       ✅  search + scraper + writer + layer
  prompts/     ✅  architect + critic + synthesizer + parser
  tasks/       ✅  registry + engine + generator
```

The task engine is what drives the whole system.  Without it, the
orchestrator wouldn't know what to work on next.

---

→ Next: [Chapter 07 — The Context Assembler](./07-context-assembler.md)
