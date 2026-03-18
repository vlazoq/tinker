"""
tasks/postgres_registry.py
===========================
PostgreSQL-backed task registry — a drop-in replacement for
``SQLiteTaskRegistry`` intended for multi-process and multi-machine
deployments.

When to use this
----------------
``SQLiteTaskRegistry`` writes to a single local file; two separate
processes cannot safely share it unless you mount the same directory on
both (NFS/Samba).  ``PostgresTaskRegistry`` solves that by pointing all
processes at a single shared PostgreSQL server:

  Machine 1 (Tinker design loop)
  Machine 2 (Grub worker 1)
  Machine 3 (Grub worker 2)
        │
        └─────── PostgreSQL server ───── tasks table

Key differences from SQLiteTaskRegistry
----------------------------------------
| Concern          | SQLite                        | PostgreSQL                  |
|------------------|-------------------------------|------------------------------|
| Placeholder      | ``?``                         | ``%s``                       |
| Upsert           | ``INSERT OR REPLACE``         | ``INSERT … ON CONFLICT …``  |
| Cursor factory   | ``sqlite3.Row`` row_factory   | ``psycopg2.extras.RealDictCursor`` |
| Schema setup     | ``executescript`` (multi-stmt)| ``execute`` per statement    |
| Thread-safety    | Single shared connection      | ThreadedConnectionPool       |
| WAL mode         | ``PRAGMA journal_mode=WAL``   | Not needed (PostgreSQL default) |
| Boolean storage  | INTEGER (0/1)                 | INTEGER (0/1) — kept same   |

Connection pooling
------------------
The registry manages a ``psycopg2.pool.ThreadedConnectionPool`` so that
multiple threads in the same process can each hold their own connection
without blocking one another.  The pool size is configurable::

    registry = PostgresTaskRegistry(
        dsn      = "postgresql://tinker:pw@db.host/tinker_tasks",
        min_conn = 1,
        max_conn = 10,
    )

Testability
-----------
For unit tests that do not have a PostgreSQL server available, pass a
``connection_factory`` callable that returns a mock connection::

    from unittest.mock import MagicMock
    mock_conn = MagicMock()
    registry  = PostgresTaskRegistry(connection_factory=lambda: mock_conn)

This lets every method be tested without a real database.

Environment variable
--------------------
``TINKER_POSTGRES_DSN`` — overrides the ``dsn`` constructor argument when
set.  Useful for 12-factor deployments where credentials live in the
environment, not the source tree.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager
from typing import Any, Callable, Generator

from .abstract_registry import AbstractTaskRegistry
from .schema import Task, TaskStatus, Subsystem

log = logging.getLogger(__name__)


# =============================================================================
# PostgreSQL DDL
# =============================================================================
# Executed once at startup.  Mirrors the SQLite schema in tasks/registry.py
# but uses PostgreSQL syntax.
#
# Differences from the SQLite version:
#   • No PRAGMA statements — PostgreSQL manages its own WAL automatically.
#   • CREATE INDEX … CONCURRENTLY is safer for production (no table lock)
#     but requires running outside a transaction; we use the simpler
#     CREATE INDEX IF NOT EXISTS here since this only runs once at startup.

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
)
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_tasks_status    ON tasks(status)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_subsystem ON tasks(subsystem)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_priority  ON tasks(priority_score DESC)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_parent    ON tasks(parent_id)",
]

# Column list — must match the table definition above.
_COLUMNS = [
    "id", "parent_id", "title", "description", "type", "subsystem",
    "status", "dependencies", "outputs", "confidence_gap",
    "staleness_hours", "dependency_depth", "last_subsystem_work_hours",
    "priority_score", "tags", "metadata", "is_exploration",
    "created_at", "updated_at", "started_at", "completed_at",
    "critique_notes", "attempt_count",
]

# Upsert SET clause: "col = EXCLUDED.col" for every non-PK column.
_UPSERT_SET = ", ".join(f"{c} = EXCLUDED.{c}" for c in _COLUMNS if c != "id")


# =============================================================================
# Row helpers
# =============================================================================

def _pg_row_to_dict(row: dict) -> dict:
    """Normalise a psycopg2 RealDictCursor row to a plain Python dict.

    The only conversion needed is INTEGER → bool for the ``is_exploration``
    column (same as the SQLite backend).
    """
    d = dict(row)
    d["is_exploration"] = bool(d.get("is_exploration", 0))
    return d


def _task_to_row(task: Task) -> dict:
    """Convert a Task dataclass to a flat dict suitable for PostgreSQL.

    JSON-serialises list/dict fields; converts bool → int for
    ``is_exploration``.  Mirrors ``SQLiteTaskRegistry._task_to_row``.
    """
    d = task.to_dict()
    for field in ("dependencies", "outputs", "tags", "metadata"):
        d[field] = json.dumps(d[field])
    d["is_exploration"] = int(d["is_exploration"])
    return d


# =============================================================================
# PostgresTaskRegistry
# =============================================================================

class PostgresTaskRegistry(AbstractTaskRegistry):
    """
    Task registry backed by a PostgreSQL database.

    Parameters
    ----------
    dsn : str, optional
        PostgreSQL connection string, e.g.
        ``"postgresql://user:password@localhost/tinker_tasks"``.
        Falls back to the ``TINKER_POSTGRES_DSN`` environment variable.
    min_conn : int
        Minimum number of connections in the pool (default 1).
    max_conn : int
        Maximum number of connections in the pool (default 10).
    connection_factory : callable, optional
        ``() -> connection`` — for unit testing only.  When supplied,
        ``dsn`` is ignored and no pool is created; all operations use the
        single connection returned by the factory.
    """

    def __init__(
        self,
        dsn:                str      = "",
        *,
        min_conn:           int      = 1,
        max_conn:           int      = 10,
        connection_factory: Callable | None = None,
    ) -> None:
        self._lock    = threading.Lock()
        self._pool    = None
        self._single  = None       # used only when connection_factory is set

        if connection_factory is not None:
            # Test / mock mode: use a single connection, no pool.
            self._single = connection_factory()
            log.debug("PostgresTaskRegistry: using injected connection (test mode)")
        else:
            effective_dsn = dsn or os.getenv("TINKER_POSTGRES_DSN", "")
            if not effective_dsn:
                raise ValueError(
                    "PostgresTaskRegistry requires a DSN.  "
                    "Pass dsn=... or set TINKER_POSTGRES_DSN."
                )
            try:
                import psycopg2
                import psycopg2.pool as _pool
            except ImportError as exc:
                raise ImportError(
                    "psycopg2 is required for the PostgreSQL backend.  "
                    "Install it with:  pip install psycopg2-binary"
                ) from exc

            self._psycopg2 = psycopg2
            self._pool = _pool.ThreadedConnectionPool(
                minconn=min_conn, maxconn=max_conn, dsn=effective_dsn
            )
            log.info(
                "PostgresTaskRegistry: connected pool (min=%d max=%d) → %s",
                min_conn, max_conn,
                effective_dsn.split("@")[-1],  # hide credentials in log
            )

        self._init_schema()

    # ── Connection management ─────────────────────────────────────────────────

    @contextmanager
    def _conn(self) -> Generator[Any, None, None]:
        """
        Yield a connection and cursor in a transaction.

        In pool mode: borrows a connection from the pool, commits on
        success, rolls back on exception, then returns it to the pool.

        In single-connection mode (tests): yields the injected connection
        directly, still committing / rolling back.
        """
        if self._single is not None:
            conn = self._single
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return

        conn = self._pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def _cursor(self, conn):
        """Return a RealDictCursor so rows come back as plain dicts."""
        try:
            from psycopg2.extras import RealDictCursor
            return conn.cursor(cursor_factory=RealDictCursor)
        except ImportError:
            # Fallback for mock connections in tests (no real psycopg2).
            return conn.cursor()

    # ── Schema init ───────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        """Create the tasks table and indexes if they do not exist."""
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(_CREATE_TABLE)
            for idx_sql in _CREATE_INDEXES:
                cur.execute(idx_sql)
        log.info("PostgresTaskRegistry: schema initialised")

    # ── Write ─────────────────────────────────────────────────────────────────

    def save(self, task: Task) -> Task:
        """
        Insert or update (upsert) a task using ``ON CONFLICT DO UPDATE``.

        PostgreSQL's ``ON CONFLICT (id) DO UPDATE SET …`` is the standard
        upsert idiom; it atomically inserts the row or overwrites it if the
        primary key already exists.  This is equivalent to SQLite's
        ``INSERT OR REPLACE``.
        """
        row = _task_to_row(task)
        cols  = ", ".join(_COLUMNS)
        vals  = ", ".join("%s" for _ in _COLUMNS)
        sql   = (
            f"INSERT INTO tasks ({cols}) VALUES ({vals}) "
            f"ON CONFLICT (id) DO UPDATE SET {_UPSERT_SET}"
        )
        params = [row[c] for c in _COLUMNS]

        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(sql, params)

        log.debug("Saved task %s (%s)", task.id, task.status.value)
        return task

    def update(self, task: Task) -> Task:
        """Refresh ``updated_at`` and persist the task."""
        task.touch()
        return self.save(task)

    def delete(self, task_id: str) -> bool:
        """Permanently remove a task.  Returns True if a row was deleted."""
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
            return cur.rowcount > 0

    # ── Read ──────────────────────────────────────────────────────────────────

    def get(self, task_id: str) -> Task | None:
        """Fetch a task by its unique ID, or ``None`` if not found."""
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute("SELECT * FROM tasks WHERE id = %s", (task_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return Task.from_dict(_pg_row_to_dict(row))

    def list_all(self) -> list[Task]:
        """Return every task, in no particular order."""
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute("SELECT * FROM tasks")
            rows = cur.fetchall()
        return [Task.from_dict(_pg_row_to_dict(r)) for r in rows]

    def by_status(self, *statuses: TaskStatus) -> list[Task]:
        """Return all tasks matching any of the given statuses."""
        placeholders = ", ".join("%s" for _ in statuses)
        vals = [s.value for s in statuses]
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                f"SELECT * FROM tasks WHERE status IN ({placeholders})", vals
            )
            rows = cur.fetchall()
        return [Task.from_dict(_pg_row_to_dict(r)) for r in rows]

    def by_subsystem(self, subsystem: Subsystem) -> list[Task]:
        """Return all tasks belonging to a particular subsystem."""
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT * FROM tasks WHERE subsystem = %s", (subsystem.value,)
            )
            rows = cur.fetchall()
        return [Task.from_dict(_pg_row_to_dict(r)) for r in rows]

    def children_of(self, parent_id: str) -> list[Task]:
        """Return all tasks whose ``parent_id`` matches."""
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT * FROM tasks WHERE parent_id = %s", (parent_id,)
            )
            rows = cur.fetchall()
        return [Task.from_dict(_pg_row_to_dict(r)) for r in rows]

    def pending_ordered(self) -> list[Task]:
        """Return PENDING tasks sorted by ``priority_score`` descending."""
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT * FROM tasks WHERE status = %s "
                "ORDER BY priority_score DESC",
                (TaskStatus.PENDING.value,),
            )
            rows = cur.fetchall()
        return [Task.from_dict(_pg_row_to_dict(r)) for r in rows]

    def count_by_status(self) -> dict[str, int]:
        """Return a ``{status: count}`` mapping for all statuses."""
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")
            rows = cur.fetchall()
        return {r["status"]: r["n"] for r in rows}

    def oldest_pending(self) -> Task | None:
        """Return the PENDING task that has been waiting the longest."""
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT * FROM tasks WHERE status = %s "
                "ORDER BY created_at ASC LIMIT 1",
                (TaskStatus.PENDING.value,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return Task.from_dict(_pg_row_to_dict(row))

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close all pooled connections."""
        if self._pool is not None:
            self._pool.closeall()
            log.info("PostgresTaskRegistry: connection pool closed")
        elif self._single is not None:
            self._single.close()
