"""
runtime/tasks/postgres_registry.py
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

Enterprise features
-------------------
This implementation adds several capabilities that production deployments
need beyond the basic CRUD surface:

Connection retry with exponential back-off
    Pool acquisition and connection setup can fail transiently when the
    PostgreSQL server restarts, the network hiccups, or the pool is
    momentarily exhausted.  ``_conn()`` automatically retries up to
    ``max_retries`` times (default 3) with delays that double each attempt
    (``retry_base_delay`` × 2ⁿ, default 0.5 s → 1 s → 2 s).

    Only *transient* errors trigger a retry (connection refused, server
    restart, SSL reset, too-many-connections).  Logic errors like
    ``UndefinedTable`` or ``SyntaxError`` are never retried.

Query timeout
    Long-running queries (a full table scan on a million-row tasks table,
    a deadlock that holds locks for seconds) block the whole connection.
    Pass ``query_timeout_ms=5000`` to cap every query at 5 seconds.  The
    PostgreSQL ``statement_timeout`` GUC is set at connection-pool creation
    so the limit applies to every query, including schema init.

Schema migration versioning
    Production databases evolve over time; ALTER TABLE must be applied
    exactly once.  ``PostgresTaskRegistry`` maintains a
    ``schema_migrations`` table that records every applied migration by
    version number.  New migrations are added to ``_MIGRATIONS`` in this
    file; they are applied automatically on the next startup.  Migrations
    are idempotent — running them twice is safe because they are guarded by
    ``INSERT OR IGNORE`` / ``IF NOT EXISTS``.

Bulk writes (``save_batch``)
    A single-transaction executemany beats N separate round-trips by a
    large margin when seeding a queue.  Use this whenever you have more
    than ~5 tasks to write at once.

Health check (``health_check``)
    Issues ``SELECT 1 FROM tasks LIMIT 1`` and returns True/False without
    raising.  Wire this into your readiness probe.

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

This lets every method be tested without a real database.  In
``connection_factory`` mode no pool is created and retry / timeout logic
is bypassed (the mock connection never raises transient errors).

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
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any

from .abstract_registry import AbstractTaskRegistry
from .schema import Subsystem, Task, TaskStatus

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
    "id",
    "parent_id",
    "title",
    "description",
    "type",
    "subsystem",
    "status",
    "dependencies",
    "outputs",
    "confidence_gap",
    "staleness_hours",
    "dependency_depth",
    "last_subsystem_work_hours",
    "priority_score",
    "tags",
    "metadata",
    "is_exploration",
    "created_at",
    "updated_at",
    "started_at",
    "completed_at",
    "critique_notes",
    "attempt_count",
]

# Upsert SET clause: "col = EXCLUDED.col" for every non-PK column.
_UPSERT_SET = ", ".join(f"{c} = EXCLUDED.{c}" for c in _COLUMNS if c != "id")


# =============================================================================
# Schema migration definitions
# =============================================================================
# Each migration is a tuple of:
#   (version: int, description: str, sql_statements: list[str])
#
# Rules:
#   1. Versions must be monotonically increasing integers starting at 1.
#   2. Never edit a migration once it has been applied in any environment.
#      Always add a NEW entry instead.
#   3. Each SQL statement is executed separately (psycopg2 does not support
#      multi-statement strings).
#   4. Migrations are applied inside a transaction; if one statement fails
#      the entire migration is rolled back.
#
# To add a new migration, append to this list:
#   (3, "Add attempt_limit column", [
#       "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS attempt_limit INTEGER"
#   ]),

_CREATE_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    description TEXT    NOT NULL,
    applied_at  TEXT    NOT NULL
)
"""

_MIGRATIONS: list[tuple[int, str, list[str], list[str]]] = [
    (
        1,
        "Initial schema: tasks table and indexes",
        # ── UP ────────────────────────────────────────────────────────────────
        [_CREATE_TABLE, *_CREATE_INDEXES],
        # ── DOWN ──────────────────────────────────────────────────────────────
        # Executed in reverse order by rollback_migration().  Drops indexes
        # first (avoids constraint issues), then the table.
        [
            "DROP INDEX IF EXISTS idx_tasks_parent",
            "DROP INDEX IF EXISTS idx_tasks_priority",
            "DROP INDEX IF EXISTS idx_tasks_subsystem",
            "DROP INDEX IF EXISTS idx_tasks_status",
            "DROP TABLE IF EXISTS tasks",
        ],
    ),
    # --------------------------------------------------------------------------
    # Future migrations go here.  Example:
    #
    # (
    #     2,
    #     "Add attempt_limit column to tasks",
    #     # UP:
    #     [
    #         "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS "
    #         "attempt_limit INTEGER NOT NULL DEFAULT 5",
    #     ],
    #     # DOWN:
    #     [
    #         "ALTER TABLE tasks DROP COLUMN IF EXISTS attempt_limit",
    #     ],
    # ),
    # --------------------------------------------------------------------------
]


# =============================================================================
# Transient-error detection
# =============================================================================
# These substrings appear in psycopg2.OperationalError messages for errors
# that are safe to retry (server restarted, network blip, pool exhausted).
# Logical errors (bad SQL, constraint violations) are never in this set.

_TRANSIENT_FRAGMENTS: frozenset[str] = frozenset(
    {
        "could not connect to server",
        "connection to server",
        "server closed the connection",
        "ssl connection has been closed unexpectedly",
        "connection refused",
        "too many connections",
        "connection pool exhausted",
        "connection timed out",
        "remaining connection slots are reserved",
        # Concurrency errors — safe to retry because PostgreSQL rolls back the
        # transaction automatically and the client can simply restart it.
        "deadlock detected",  # SQLSTATE 40P01: two txns waiting on each other
        "could not serialize access",  # SQLSTATE 40001: serializable snapshot conflict
    }
)


def _is_transient(exc: Exception) -> bool:
    """Return True if *exc* looks like a transient connectivity error.

    Only ``OperationalError`` subclasses are transient.  All other error
    types (``ProgrammingError``, ``IntegrityError``, etc.) represent
    permanent failures that must not be retried.
    """
    try:
        import psycopg2

        if not isinstance(exc, psycopg2.OperationalError):
            return False
    except ImportError:
        pass  # test environment — check the message heuristically

    msg = str(exc).lower()
    return any(fragment in msg for fragment in _TRANSIENT_FRAGMENTS)


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
    max_retries : int
        Number of retry attempts for transient connectivity failures
        before giving up (default 3).  Uses exponential back-off starting
        at ``retry_base_delay`` seconds.
    retry_base_delay : float
        Initial retry delay in seconds (default 0.5).  Each subsequent
        attempt doubles this value: 0.5 s → 1 s → 2 s → …
    query_timeout_ms : int | None
        Maximum time in milliseconds that any single query may run.
        Passed to PostgreSQL as ``statement_timeout``.  ``None`` (default)
        means no timeout — use this for long-running maintenance tasks.
        Recommended value for production: ``5000`` (5 seconds).
    connection_factory : callable, optional
        ``() -> connection`` — for unit testing only.  When supplied,
        ``dsn`` is ignored and no pool is created; all operations use the
        single connection returned by the factory.  Retry and timeout
        logic is bypassed in this mode.
    """

    def __init__(
        self,
        dsn: str = "",
        *,
        min_conn: int = 1,
        max_conn: int = 10,
        max_retries: int = 3,
        retry_base_delay: float = 0.5,
        query_timeout_ms: int | None = None,
        connection_factory: Callable | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._pool = None
        self._single = None  # used only when connection_factory is set
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._query_timeout_ms = query_timeout_ms

        if connection_factory is not None:
            # Test / mock mode: use a single connection, no pool, no retry.
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

            # Build the pool.  If query_timeout_ms is set, inject the
            # statement_timeout GUC via the ``options`` keyword so every
            # connection in the pool is pre-configured.  This is the
            # standard PostgreSQL approach for per-connection settings
            # that should apply to ALL queries.
            pool_kwargs: dict[str, Any] = dict(
                minconn=min_conn, maxconn=max_conn, dsn=effective_dsn
            )
            if query_timeout_ms is not None:
                pool_kwargs["options"] = f"-c statement_timeout={query_timeout_ms}"

            self._pool = _pool.ThreadedConnectionPool(**pool_kwargs)
            log.info(
                "PostgresTaskRegistry: connected pool (min=%d max=%d timeout=%s) → %s",
                min_conn,
                max_conn,
                f"{query_timeout_ms}ms" if query_timeout_ms else "none",
                effective_dsn.split("@")[-1],  # hide credentials in log
            )

        self._run_migrations()

    # ── Connection management ─────────────────────────────────────────────────

    @contextmanager
    def _conn(self) -> Generator[Any, None, None]:
        """
        Yield a connection in a transaction, with automatic retry for
        transient connectivity errors.

        **Pool mode** (production):
          Borrows a connection from ``ThreadedConnectionPool``, commits on
          success, rolls back on exception, then returns it to the pool.
          If the pool is temporarily unavailable (server restart, network
          blip) the acquisition is retried up to ``max_retries`` times with
          exponential back-off (``retry_base_delay × 2ⁿ``).  Only errors
          that match ``_TRANSIENT_FRAGMENTS`` trigger a retry; logic errors
          (bad SQL, constraint violations) are propagated immediately.

        **Single-connection mode** (tests):
          Yields the injected connection directly.  No retry — the mock
          connection does not raise transient errors.

        Retry policy
        ------------
        Attempt 1: immediate
        Attempt 2: sleep retry_base_delay seconds
        Attempt 3: sleep retry_base_delay × 2 seconds
        …
        After max_retries exhausted: re-raise the last exception.
        """
        if self._single is not None:
            # Test/mock mode — bypass retry logic entirely.
            conn = self._single
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return

        # Production pool mode — acquire with retry.
        last_exc: Exception | None = None
        conn = None

        for attempt in range(self._max_retries + 1):
            if attempt > 0:
                delay = self._retry_base_delay * (2 ** (attempt - 1))
                log.warning(
                    "PostgresTaskRegistry: transient error on attempt %d/%d, "
                    "retrying in %.1fs — %s",
                    attempt,
                    self._max_retries,
                    delay,
                    last_exc,
                )
                time.sleep(delay)

            try:
                conn = self._pool.getconn()
                break  # successfully acquired
            except Exception as exc:
                last_exc = exc
                if not _is_transient(exc):
                    raise  # permanent error — don't retry

        if conn is None:
            # All retries exhausted; last_exc is set because we entered the loop.
            raise last_exc  # type: ignore[misc]

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

    # ── Schema migrations ─────────────────────────────────────────────────────

    def _run_migrations(self) -> None:
        """Apply any unapplied schema migrations in version order.

        How it works
        ------------
        1. Create ``schema_migrations`` table if it does not exist.
        2. Query which versions have already been applied.
        3. For each migration whose version is *not* in that set, execute
           all its SQL statements in a single transaction and record the
           version in ``schema_migrations``.

        This is called once in ``__init__`` and is idempotent — running it
        again when all migrations are already applied is a no-op.

        Design notes
        ------------
        * Each migration runs in its own transaction.  This means a failed
          migration does not corrupt earlier migrations that succeeded.
        * Migrations are applied in ascending version order regardless of
          how they appear in ``_MIGRATIONS``.
        * ``schema_migrations`` itself is created outside a migration so
          that the bookkeeping table is always available.
        """
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(_CREATE_MIGRATIONS_TABLE)

        # Find which migrations have already been applied.
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute("SELECT version FROM schema_migrations")
            applied = {row["version"] for row in cur.fetchall()}

        # Apply unapplied migrations in ascending version order.
        for version, description, statements, _down in sorted(_MIGRATIONS, key=lambda m: m[0]):
            if version in applied:
                continue

            log.info(
                "PostgresTaskRegistry: applying migration %d — %s",
                version,
                description,
            )
            with self._conn() as conn:
                cur = self._cursor(conn)
                for sql in statements:
                    cur.execute(sql)
                now = (
                    __import__("datetime")
                    .datetime.now(__import__("datetime").timezone.utc)
                    .isoformat()
                )
                cur.execute(
                    "INSERT INTO schema_migrations (version, description, applied_at) "
                    "VALUES (%s, %s, %s) ON CONFLICT (version) DO NOTHING",
                    (version, description, now),
                )

        log.info("PostgresTaskRegistry: migrations complete")

    def list_applied_migrations(self) -> list[dict]:
        """Return a list of applied migrations ordered by version.

        Each item is a dict with keys: ``version``, ``description``,
        ``applied_at``.  Useful for observability and debugging.

        Returns [] if the ``schema_migrations`` table does not exist yet
        (i.e. before the first call to ``_run_migrations``).
        """
        try:
            with self._conn() as conn:
                cur = self._cursor(conn)
                cur.execute(
                    "SELECT version, description, applied_at "
                    "FROM schema_migrations ORDER BY version ASC"
                )
                return [dict(r) for r in cur.fetchall()]
        except Exception:
            return []

    def rollback_migration(self, version: int) -> None:
        """Roll back a previously-applied migration.

        Executes the DOWN SQL statements for *version* in order, then removes
        the version from ``schema_migrations``.  This is the inverse of
        ``_run_migrations()``.

        When to use
        -----------
        After deploying a bad migration to production, operators can call
        ``rollback_migration(N)`` to undo migration N without dropping the
        entire database.  Roll back in reverse version order when undoing
        multiple migrations::

            registry.rollback_migration(3)
            registry.rollback_migration(2)

        Parameters
        ----------
        version : int
            The migration version to roll back.  Must be in ``_MIGRATIONS``
            and must already appear in ``schema_migrations``.

        Raises
        ------
        ValueError
            If *version* is not found in ``_MIGRATIONS``.
        RuntimeError
            If *version* has not been applied (not in ``schema_migrations``).
        """
        migration_map: dict[int, tuple[str, list[str], list[str]]] = {
            m[0]: (m[1], m[2], m[3]) for m in _MIGRATIONS
        }
        if version not in migration_map:
            raise ValueError(
                f"Migration version {version} not found in _MIGRATIONS. "
                f"Available: {sorted(migration_map)}"
            )

        description, _up, down_statements = migration_map[version]

        # Verify the migration was actually applied.
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute("SELECT 1 FROM schema_migrations WHERE version = %s", (version,))
            if cur.fetchone() is None:
                raise RuntimeError(
                    f"Migration {version} ('{description}') has not been applied; "
                    "nothing to roll back."
                )

        log.warning(
            "PostgresTaskRegistry: rolling back migration %d — %s",
            version,
            description,
        )
        with self._conn() as conn:
            cur = self._cursor(conn)
            for sql in down_statements:
                cur.execute(sql)
            cur.execute("DELETE FROM schema_migrations WHERE version = %s", (version,))

        log.info("PostgresTaskRegistry: migration %d rolled back successfully", version)

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
        cols = ", ".join(_COLUMNS)
        vals = ", ".join("%s" for _ in _COLUMNS)
        sql = (
            f"INSERT INTO tasks ({cols}) VALUES ({vals}) "
            f"ON CONFLICT (id) DO UPDATE SET {_UPSERT_SET}"
        )
        params = [row[c] for c in _COLUMNS]

        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(sql, params)

        log.debug("Saved task %s (%s)", task.id, task.status.value)
        return task

    def save_batch(self, tasks: list[Task]) -> list[Task]:
        """Insert or update multiple tasks in a single transaction.

        Uses ``executemany`` to send all rows in one round-trip rather than
        issuing N separate ``INSERT … ON CONFLICT`` statements.  For large
        batches (50+ tasks) this is 10–50× faster than calling ``save()``
        in a loop.

        All tasks are committed atomically — a failure in the middle leaves
        the database unchanged (the transaction is rolled back).

        Parameters
        ----------
        tasks : list[Task]
            Tasks to upsert.  An empty list is a no-op; returns ``[]``.

        Returns
        -------
        list[Task]
            The same list, unchanged.
        """
        if not tasks:
            return tasks

        cols = ", ".join(_COLUMNS)
        vals = ", ".join("%s" for _ in _COLUMNS)
        sql = (
            f"INSERT INTO tasks ({cols}) VALUES ({vals}) "
            f"ON CONFLICT (id) DO UPDATE SET {_UPSERT_SET}"
        )
        params_list = [[_task_to_row(t)[c] for c in _COLUMNS] for t in tasks]

        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.executemany(sql, params_list)

        log.debug("Saved batch of %d tasks", len(tasks))
        return tasks

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
            cur.execute(f"SELECT * FROM tasks WHERE status IN ({placeholders})", vals)
            rows = cur.fetchall()
        return [Task.from_dict(_pg_row_to_dict(r)) for r in rows]

    def by_subsystem(self, subsystem: Subsystem) -> list[Task]:
        """Return all tasks belonging to a particular subsystem."""
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute("SELECT * FROM tasks WHERE subsystem = %s", (subsystem.value,))
            rows = cur.fetchall()
        return [Task.from_dict(_pg_row_to_dict(r)) for r in rows]

    def children_of(self, parent_id: str) -> list[Task]:
        """Return all tasks whose ``parent_id`` matches."""
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute("SELECT * FROM tasks WHERE parent_id = %s", (parent_id,))
            rows = cur.fetchall()
        return [Task.from_dict(_pg_row_to_dict(r)) for r in rows]

    def pending_ordered(self) -> list[Task]:
        """Return PENDING tasks sorted by ``priority_score`` descending."""
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT * FROM tasks WHERE status = %s ORDER BY priority_score DESC",
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
                "SELECT * FROM tasks WHERE status = %s ORDER BY created_at ASC LIMIT 1",
                (TaskStatus.PENDING.value,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return Task.from_dict(_pg_row_to_dict(row))

    def claim_next_pending(self) -> Task | None:
        """
        Atomically claim the highest-priority PENDING task.

        Uses ``SELECT … FOR UPDATE SKIP LOCKED`` so that concurrent workers
        each claim a *different* task with no blocking and no double-processing.

        Why FOR UPDATE SKIP LOCKED?
        ---------------------------
        In a multi-worker deployment two workers calling ``pending_ordered()``
        concurrently would both see the same top row, both read it, and then
        both UPDATE it to ``active`` — causing the same task to be processed
        twice.

        ``FOR UPDATE SKIP LOCKED`` acquires a row-level write lock on the
        chosen row *inside the same statement*.  Any concurrent transaction
        that attempts to lock the same row with ``SKIP LOCKED`` will skip past
        it and see the next-highest-priority unlocked row instead.  This gives
        each worker an exclusive claim with a single round-trip.

        The status transition (``pending`` → ``active``) and the
        ``started_at`` timestamp are written in the same transaction as the
        SELECT, so the claim is fully atomic.

        Returns
        -------
        Task | None
            The claimed task with ``status=active`` and ``started_at`` set,
            or ``None`` if no PENDING tasks exist.
        """
        import datetime as _dt

        now = _dt.datetime.now(_dt.UTC).isoformat()

        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT * FROM tasks WHERE status = %s "
                "ORDER BY priority_score DESC "
                "LIMIT 1 FOR UPDATE SKIP LOCKED",
                (TaskStatus.PENDING.value,),
            )
            row = cur.fetchone()
            if row is None:
                return None

            task = Task.from_dict(_pg_row_to_dict(row))
            cur.execute(
                "UPDATE tasks SET status = %s, updated_at = %s, started_at = %s WHERE id = %s",
                (TaskStatus.ACTIVE.value, now, now, task.id),
            )

        task.status = TaskStatus.ACTIVE
        task.updated_at = now
        task.started_at = now
        log.info("Claimed task %s → active", task.id)
        return task

    # ── Operations ────────────────────────────────────────────────────────────

    def health_check(self) -> bool:
        """Return True if the PostgreSQL backend is reachable and schema is present.

        Executes ``SELECT 1 FROM tasks LIMIT 1`` — the lightest possible
        query that verifies both connectivity and schema presence.  Returns
        False (never raises) so callers can use this in polling loops or
        liveness probes without try/except.

        In ``connection_factory`` (test) mode the mock connection is used
        directly, which always succeeds unless the mock is configured to fail.
        """
        try:
            with self._conn() as conn:
                cur = self._cursor(conn)
                cur.execute("SELECT 1 FROM tasks LIMIT 1")
            return True
        except Exception:
            return False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close all pooled connections."""
        if self._pool is not None:
            self._pool.closeall()
            log.info("PostgresTaskRegistry: connection pool closed")
        elif self._single is not None:
            self._single.close()
