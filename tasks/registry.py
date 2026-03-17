"""
tasks/registry.py — SQLite-backed task registry
================================================

What this file does
--------------------
This is the SQLite implementation of ``AbstractTaskRegistry``.  The
canonical class name is ``SQLiteTaskRegistry``; ``TaskRegistry`` is kept
as a backwards-compatible alias so existing code does not break.

For deployments that need a shared task store across multiple processes or
machines, use ``PostgresTaskRegistry`` (see ``tasks/postgres_registry.py``)
or call the ``create_task_registry(backend=...)`` factory function.

SQLite is a lightweight database engine that stores everything in a single
file on disk.  It requires no separate server process, making it ideal for
an embedded system like Tinker.

How data flows through this file
----------------------------------
  1. When a Task is saved, ``_task_to_row()`` converts its Python fields
     into a flat dictionary of strings/numbers that SQLite can store.
     Lists and dicts (like ``dependencies`` and ``metadata``) are converted
     to JSON strings because SQLite columns only hold primitive types.

  2. When a Task is loaded, ``_row_to_dict()`` converts the raw SQLite row
     back into a Python dict, and ``Task.from_dict()`` reconstructs the
     full Task dataclass (JSON strings → lists/dicts, string → Enum, etc.).

  3. All writes are wrapped in a transaction (``_tx()``).  If anything goes
     wrong mid-write, the transaction is rolled back so the database never
     ends up in a half-written, inconsistent state.

Why SQLite and not an in-memory dict?
---------------------------------------
Using SQLite means tasks survive if Tinker crashes and restarts.  You can
also inspect the database with any SQLite tool for debugging.  The special
path ``":memory:"`` creates a temporary in-memory database — useful for
fast unit tests that don't need persistence.

Thread safety note
-------------------
``check_same_thread=False`` lets multiple threads share one connection.
The WAL (Write-Ahead Logging) journal mode allows concurrent reads while
a write is happening, which improves throughput.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from .abstract_registry import AbstractTaskRegistry
from .schema import Task, TaskStatus, TaskType, Subsystem

log = logging.getLogger(__name__)


# =============================================================================
# Database schema (SQL DDL)
# =============================================================================
# This string is executed once at startup to create the tasks table and its
# indexes.  "CREATE TABLE IF NOT EXISTS" means it's safe to run every time —
# it does nothing if the table already exists.

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

-- Index on status lets us quickly fetch all PENDING or BLOCKED tasks
-- without scanning every row in the table.
CREATE INDEX IF NOT EXISTS idx_tasks_status    ON tasks(status);

-- Index on subsystem allows fast filtering by component area.
CREATE INDEX IF NOT EXISTS idx_tasks_subsystem ON tasks(subsystem);

-- Index on priority_score (descending) speeds up the "get next task" query.
CREATE INDEX IF NOT EXISTS idx_tasks_priority  ON tasks(priority_score DESC);

-- Index on parent_id makes "get all children of task X" fast.
CREATE INDEX IF NOT EXISTS idx_tasks_parent    ON tasks(parent_id);
"""

# The list of column names in the same order they appear in the table.
# Used to build parameterised INSERT/SELECT SQL without typos.
_COLUMNS = [
    "id", "parent_id", "title", "description", "type", "subsystem",
    "status", "dependencies", "outputs", "confidence_gap",
    "staleness_hours", "dependency_depth", "last_subsystem_work_hours",
    "priority_score", "tags", "metadata", "is_exploration",
    "created_at", "updated_at", "started_at", "completed_at",
    "critique_notes", "attempt_count",
]


# =============================================================================
# Row conversion helper
# =============================================================================

def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a raw SQLite row to a plain Python dictionary.

    SQLite.Row objects behave like dicts but aren't quite the same type.
    Converting to a real dict makes downstream code simpler.

    Also handles the boolean conversion: SQLite stores True/False as 1/0
    integers.  We convert the ``is_exploration`` column back to a Python
    bool here so the rest of the code doesn't have to think about it.
    """
    d = dict(row)
    # SQLite stores booleans as 0/1 integers; convert back to True/False
    d["is_exploration"] = bool(d["is_exploration"])
    return d


# =============================================================================
# TaskRegistry class
# =============================================================================

class SQLiteTaskRegistry(AbstractTaskRegistry):
    """
    Persistent task store backed by a local SQLite file.

    This is the default backend — it requires no external services and
    works on every platform Tinker supports.

    Thread safety
    -------------
    Multiple threads can read concurrently (WAL mode).  Writes are
    serialised by SQLite's internal locking.  For Tinker's usage patterns
    this is fine — the bottleneck is the AI API, not SQLite.

    For multi-process or multi-machine deployments use
    ``PostgresTaskRegistry`` instead.

    Parameters
    ----------
    db_path : str or Path
        Path to the SQLite file.  Use ``":memory:"`` for an ephemeral
        in-memory database (useful in tests).
    """

    def __init__(self, db_path: str | Path = ":memory:"):
        self.db_path = str(db_path)

        # Open (or create) the SQLite database file.
        # check_same_thread=False: allow use from multiple threads.
        # detect_types=PARSE_DECLTYPES: auto-convert some column types.
        self._conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )

        # row_factory lets us access columns by name: row["title"] instead
        # of row[2].  Much more readable and less error-prone.
        self._conn.row_factory = sqlite3.Row

        # WAL mode: readers don't block writers and writers don't block
        # readers.  Better concurrent performance than the default
        # "DELETE" journal mode.
        self._conn.execute("PRAGMA journal_mode=WAL;")

        self._init_schema()
        log.info("TaskRegistry initialised at %s", self.db_path)

    # =========================================================================
    # Schema initialisation
    # =========================================================================

    def _init_schema(self) -> None:
        """Create the tasks table and indexes if they don't already exist.

        ``executescript`` runs multiple SQL statements separated by
        semicolons in a single call.  It automatically commits at the end.
        """
        self._conn.executescript(_CREATE_TABLE)
        self._conn.commit()

    # =========================================================================
    # Transaction context manager
    # =========================================================================

    @contextmanager
    def _tx(self) -> Generator[sqlite3.Connection, None, None]:
        """Yield the connection inside a transaction.

        A ``@contextmanager`` lets you write ``with self._tx() as conn:``
        which is much cleaner than manually calling commit/rollback.

        If any exception occurs inside the ``with`` block, we roll back
        the transaction so the database stays in a consistent state.
        If everything succeeds, we commit.

        This is the standard "unit of work" pattern for database code.
        """
        try:
            yield self._conn
            self._conn.commit()   # All went well — make the changes permanent
        except Exception:
            self._conn.rollback() # Something broke — undo all changes in this tx
            raise                 # Re-raise so the caller knows something went wrong

    # =========================================================================
    # Serialisation helper
    # =========================================================================

    @staticmethod
    def _task_to_row(t: Task) -> dict:
        """Convert a Task dataclass to a flat dict suitable for SQLite.

        SQLite columns can only store simple types: TEXT, INTEGER, REAL.
        Python lists and dicts must be converted to JSON strings first.
        We also convert the boolean ``is_exploration`` to 0/1 because
        SQLite has no native boolean type.
        """
        d = t.to_dict()  # Gets us a plain dict with string Enum values

        # Serialise list/dict fields to JSON strings for SQLite TEXT columns.
        # e.g. ["task-1", "task-2"] → '["task-1", "task-2"]'
        for f in ("dependencies", "outputs", "tags", "metadata"):
            d[f] = json.dumps(d[f])

        # Convert Python bool → SQLite integer (True → 1, False → 0)
        d["is_exploration"] = int(d["is_exploration"])
        return d

    # =========================================================================
    # CRUD — Create, Read, Update, Delete
    # =========================================================================

    def save(self, task: Task) -> Task:
        """Insert a new task or replace an existing one (upsert).

        "Upsert" means: if a task with this ID already exists, overwrite it;
        otherwise insert a new row.  This makes ``save()`` safe to call for
        both new tasks and updated tasks without branching.

        Parameters
        ----------
        task : The Task object to persist.

        Returns
        -------
        The same task object (unchanged), for convenient chaining.
        """
        row = self._task_to_row(task)

        # Build the SQL dynamically from _COLUMNS so adding a new field only
        # requires updating _COLUMNS, not the SQL string.
        # ":column_name" is a named parameter — SQLite replaces it with the
        # value from the row dict, preventing SQL injection.
        placeholders = ", ".join(f":{c}" for c in _COLUMNS)
        cols = ", ".join(_COLUMNS)
        sql = f"INSERT OR REPLACE INTO tasks ({cols}) VALUES ({placeholders})"

        with self._tx() as conn:
            conn.execute(sql, row)
        log.debug("Saved task %s (%s)", task.id, task.status.value)
        return task

    def get(self, task_id: str) -> Task | None:
        """Fetch a single task by its unique ID.

        Returns None if no task with that ID exists, rather than raising
        an exception.  Callers should check for None.
        """
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return None
        return Task.from_dict(_row_to_dict(row))

    def update(self, task: Task) -> Task:
        """Refresh a task's updated_at timestamp and save it.

        A convenience wrapper for the common pattern of "change something,
        then save".  The ``touch()`` call updates the timestamp so you can
        tell when the task was last modified.
        """
        task.touch()
        return self.save(task)

    def delete(self, task_id: str) -> bool:
        """Remove a task from the database permanently.

        Returns True if a row was deleted, False if the ID wasn't found.
        Note: for audit/history purposes you may prefer to set the task's
        status to ARCHIVED instead of deleting it.
        """
        with self._tx() as conn:
            cursor = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return cursor.rowcount > 0  # rowcount is 0 if no row matched

    # =========================================================================
    # Query helpers
    # =========================================================================
    # These methods provide convenient ways to fetch subsets of tasks without
    # callers having to write raw SQL.

    def list_all(self) -> list[Task]:
        """Return every task in the database, in no particular order."""
        rows = self._conn.execute("SELECT * FROM tasks").fetchall()
        return [Task.from_dict(_row_to_dict(r)) for r in rows]

    def by_status(self, *statuses: TaskStatus) -> list[Task]:
        """Return all tasks whose status is one of the given values.

        Accepts multiple statuses so you can write:
            registry.by_status(TaskStatus.PENDING, TaskStatus.BLOCKED)

        The ``IN (?, ?, ...)`` SQL clause does the filtering efficiently
        using the idx_tasks_status index.
        """
        # Build a comma-separated list of "?" placeholders for the IN clause
        placeholders = ",".join("?" * len(statuses))
        vals = [s.value for s in statuses]  # Enum → string for SQLite
        rows = self._conn.execute(
            f"SELECT * FROM tasks WHERE status IN ({placeholders})", vals
        ).fetchall()
        return [Task.from_dict(_row_to_dict(r)) for r in rows]

    def by_subsystem(self, subsystem: Subsystem) -> list[Task]:
        """Return all tasks belonging to a particular subsystem."""
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE subsystem = ?", (subsystem.value,)
        ).fetchall()
        return [Task.from_dict(_row_to_dict(r)) for r in rows]

    def children_of(self, parent_id: str) -> list[Task]:
        """Return all tasks that were spawned by the given parent task.

        Useful for understanding the tree of work that grew from a single
        root task.
        """
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE parent_id = ?", (parent_id,)
        ).fetchall()
        return [Task.from_dict(_row_to_dict(r)) for r in rows]

    def pending_ordered(self) -> list[Task]:
        """Return PENDING tasks sorted by priority_score (highest first).

        This query is fast because it uses the idx_tasks_priority index.
        The TaskQueue calls this to build its sorted work list.
        """
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY priority_score DESC",
            (TaskStatus.PENDING.value,),
        ).fetchall()
        return [Task.from_dict(_row_to_dict(r)) for r in rows]

    def count_by_status(self) -> dict[str, int]:
        """Return a {status_string: count} mapping for all statuses.

        Useful for dashboards and the ``stats()`` call on TaskQueue.
        Example return value: {"pending": 5, "active": 1, "complete": 12}
        """
        rows = self._conn.execute(
            "SELECT status, COUNT(*) as n FROM tasks GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def oldest_pending(self) -> Task | None:
        """Return the single PENDING task that has been waiting the longest.

        This is handy for anti-starvation checks: if the oldest pending
        task has been waiting more than N hours, something may be wrong
        with the prioritisation logic.
        """
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY created_at ASC LIMIT 1",
            (TaskStatus.PENDING.value,),
        ).fetchone()
        # created_at is stored as ISO-8601 strings, which sort lexicographically,
        # so ORDER BY created_at ASC correctly returns the earliest timestamp.
        return Task.from_dict(_row_to_dict(row)) if row else None

    def close(self) -> None:
        """Close the database connection.

        Call this when you're done with the registry (e.g. at shutdown)
        to flush any pending writes and release the file lock.
        """
        self._conn.close()


# ---------------------------------------------------------------------------
# Backwards-compatibility alias
# ---------------------------------------------------------------------------
# Code written before the PostgreSQL backend was added imports ``TaskRegistry``
# directly.  Keep the old name pointing to the SQLite implementation so none
# of that code breaks.
#
#   from tasks.registry import TaskRegistry   # still works
#   from tasks import TaskRegistry            # still works (via __init__.py)
#
TaskRegistry = SQLiteTaskRegistry
