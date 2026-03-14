"""
tinker/task_engine/registry.py
──────────────────────────────
SQLite-backed TaskRegistry.

All writes go through _save(); all reads reconstruct Task objects via
Task.from_dict().  The DB schema mirrors the Task dataclass 1-to-1.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from .schema import Task, TaskStatus, TaskType, Subsystem

log = logging.getLogger(__name__)


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS tasks (
    id                        TEXT PRIMARY KEY,
    parent_id                 TEXT,
    title                     TEXT NOT NULL,
    description               TEXT NOT NULL DEFAULT '',
    type                      TEXT NOT NULL,
    subsystem                 TEXT NOT NULL,
    status                    TEXT NOT NULL,
    dependencies              TEXT NOT NULL DEFAULT '[]',
    outputs                   TEXT NOT NULL DEFAULT '[]',
    confidence_gap            REAL NOT NULL DEFAULT 0.5,
    staleness_hours           REAL NOT NULL DEFAULT 0.0,
    dependency_depth          INTEGER NOT NULL DEFAULT 0,
    last_subsystem_work_hours REAL NOT NULL DEFAULT 0.0,
    priority_score            REAL NOT NULL DEFAULT 0.0,
    tags                      TEXT NOT NULL DEFAULT '[]',
    metadata                  TEXT NOT NULL DEFAULT '{}',
    is_exploration            INTEGER NOT NULL DEFAULT 0,
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL,
    started_at                TEXT,
    completed_at              TEXT,
    critique_notes            TEXT,
    attempt_count             INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_tasks_status    ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_subsystem ON tasks(subsystem);
CREATE INDEX IF NOT EXISTS idx_tasks_priority  ON tasks(priority_score DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_parent    ON tasks(parent_id);
"""

_COLUMNS = [
    "id", "parent_id", "title", "description", "type", "subsystem",
    "status", "dependencies", "outputs", "confidence_gap",
    "staleness_hours", "dependency_depth", "last_subsystem_work_hours",
    "priority_score", "tags", "metadata", "is_exploration",
    "created_at", "updated_at", "started_at", "completed_at",
    "critique_notes", "attempt_count",
]


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    # SQLite stores booleans as 0/1
    d["is_exploration"] = bool(d["is_exploration"])
    return d


class TaskRegistry:
    """
    Persistent store for all tasks.  Thread-safe via per-connection locking.
    Supports full CRUD + several useful query helpers.
    """

    def __init__(self, db_path: str | Path = ":memory:"):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._init_schema()
        log.info("TaskRegistry initialised at %s", self.db_path)

    # ── Schema ────────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        self._conn.executescript(_CREATE_TABLE)
        self._conn.commit()

    # ── Context manager helpers ───────────────────────────────────────────

    @contextmanager
    def _tx(self) -> Generator[sqlite3.Connection, None, None]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ── Internal serialisation ────────────────────────────────────────────

    @staticmethod
    def _task_to_row(t: Task) -> dict:
        d = t.to_dict()
        # Serialise list/dict fields
        for f in ("dependencies", "outputs", "tags", "metadata"):
            d[f] = json.dumps(d[f])
        d["is_exploration"] = int(d["is_exploration"])
        return d

    # ── CRUD ──────────────────────────────────────────────────────────────

    def save(self, task: Task) -> Task:
        """Insert or replace a task (upsert)."""
        row = self._task_to_row(task)
        placeholders = ", ".join(f":{c}" for c in _COLUMNS)
        cols = ", ".join(_COLUMNS)
        sql = f"INSERT OR REPLACE INTO tasks ({cols}) VALUES ({placeholders})"
        with self._tx() as conn:
            conn.execute(sql, row)
        log.debug("Saved task %s (%s)", task.id, task.status.value)
        return task

    def get(self, task_id: str) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return None
        return Task.from_dict(_row_to_dict(row))

    def update(self, task: Task) -> Task:
        task.touch()
        return self.save(task)

    def delete(self, task_id: str) -> bool:
        with self._tx() as conn:
            cursor = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return cursor.rowcount > 0

    # ── Query helpers ─────────────────────────────────────────────────────

    def list_all(self) -> list[Task]:
        rows = self._conn.execute("SELECT * FROM tasks").fetchall()
        return [Task.from_dict(_row_to_dict(r)) for r in rows]

    def by_status(self, *statuses: TaskStatus) -> list[Task]:
        placeholders = ",".join("?" * len(statuses))
        vals = [s.value for s in statuses]
        rows = self._conn.execute(
            f"SELECT * FROM tasks WHERE status IN ({placeholders})", vals
        ).fetchall()
        return [Task.from_dict(_row_to_dict(r)) for r in rows]

    def by_subsystem(self, subsystem: Subsystem) -> list[Task]:
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE subsystem = ?", (subsystem.value,)
        ).fetchall()
        return [Task.from_dict(_row_to_dict(r)) for r in rows]

    def children_of(self, parent_id: str) -> list[Task]:
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE parent_id = ?", (parent_id,)
        ).fetchall()
        return [Task.from_dict(_row_to_dict(r)) for r in rows]

    def pending_ordered(self) -> list[Task]:
        """Return PENDING tasks sorted by priority_score descending."""
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY priority_score DESC",
            (TaskStatus.PENDING.value,),
        ).fetchall()
        return [Task.from_dict(_row_to_dict(r)) for r in rows]

    def count_by_status(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) as n FROM tasks GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def oldest_pending(self) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY created_at ASC LIMIT 1",
            (TaskStatus.PENDING.value,),
        ).fetchone()
        return Task.from_dict(_row_to_dict(row)) if row else None

    def close(self) -> None:
        self._conn.close()
