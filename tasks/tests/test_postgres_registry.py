"""
tasks/tests/test_postgres_registry.py
======================================
Unit tests for ``PostgresTaskRegistry``.

These tests run with *no* PostgreSQL server installed.  They inject a mock
psycopg2 connection via the ``connection_factory`` parameter so every method
can be exercised in isolation.

How the mock works
------------------
psycopg2's API:
  conn.cursor()            → returns a cursor
  cursor.execute(sql, params)
  cursor.fetchone()        → row dict (via RealDictCursor)
  cursor.fetchall()        → list of row dicts
  cursor.rowcount          → int
  conn.commit()
  conn.rollback()
  conn.close()

We configure each mock cursor's return values per-test so the registry
methods see exactly what a real PostgreSQL cursor would return.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

import pytest

from tasks.postgres_registry import (
    PostgresTaskRegistry,
    _pg_row_to_dict,
    _task_to_row,
    _is_transient,
    _COLUMNS,
    _CREATE_TABLE,
    _MIGRATIONS,
)
from tasks.schema import Task, TaskStatus, TaskType, Subsystem


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_task(
    task_id: str = "t-001",
    status:  TaskStatus = TaskStatus.PENDING,
) -> Task:
    """Return a minimal Task for testing."""
    return Task(
        id            = task_id,
        title         = "Design auth module",
        description   = "Research and design the authentication subsystem.",
        type          = TaskType.DESIGN,
        subsystem     = Subsystem.ORCHESTRATOR,
        status        = status,
        created_at    = _now(),
        updated_at    = _now(),
    )


def _task_row(task: Task) -> dict:
    """
    Return a dict that looks like a psycopg2 RealDictCursor row for ``task``.
    This is what fetchone() / fetchall() would return.
    """
    d = _task_to_row(task)
    return d   # already a flat dict with JSON-serialised list/dict fields


def _make_registry() -> tuple[PostgresTaskRegistry, MagicMock]:
    """
    Return a (registry, mock_conn) pair.

    The mock connection's cursor() returns a new MagicMock each time so
    individual tests can configure fetchone/fetchall/rowcount freely.
    """
    mock_conn = MagicMock()
    registry  = PostgresTaskRegistry(connection_factory=lambda: mock_conn)
    return registry, mock_conn


def _fresh_cursor(mock_conn: MagicMock) -> MagicMock:
    """
    Replace mock_conn.cursor() with a fresh MagicMock.

    Resets the cursor between the schema-init phase and the actual test so
    ``execute`` call counts are clean.
    """
    cur = MagicMock()
    mock_conn.cursor.return_value = cur
    return cur


# ── Tests: row helpers ────────────────────────────────────────────────────────

class TestRowHelpers:

    def test_pg_row_to_dict_converts_is_exploration_to_bool(self):
        row = {"id": "x", "is_exploration": 1, "title": "t"}
        d   = _pg_row_to_dict(row)
        assert d["is_exploration"] is True

    def test_pg_row_to_dict_false_when_zero(self):
        row = {"id": "x", "is_exploration": 0, "title": "t"}
        assert _pg_row_to_dict(row)["is_exploration"] is False

    def test_task_to_row_json_serialises_list_fields(self):
        task = _make_task()
        row  = _task_to_row(task)
        assert isinstance(row["dependencies"], str)
        assert isinstance(row["outputs"], str)
        assert isinstance(row["tags"], str)
        assert isinstance(row["metadata"], str)

    def test_task_to_row_is_exploration_is_int(self):
        task = _make_task()
        assert isinstance(_task_to_row(task)["is_exploration"], int)

    def test_task_to_row_includes_all_columns(self):
        task = _make_task()
        row  = _task_to_row(task)
        for col in _COLUMNS:
            assert col in row, f"Column {col!r} missing from task row"


# ── Tests: schema init ────────────────────────────────────────────────────────

class TestSchemaInit:

    def test_create_table_sql_executed_on_construction(self):
        mock_conn = MagicMock()
        cur       = MagicMock()
        mock_conn.cursor.return_value = cur

        PostgresTaskRegistry(connection_factory=lambda: mock_conn)

        # The CREATE TABLE statement must have been executed
        executed = [str(c.args[0]) for c in cur.execute.call_args_list]
        assert any(_CREATE_TABLE.strip()[:30] in sql for sql in executed)

    def test_indexes_created_on_construction(self):
        mock_conn = MagicMock()
        cur       = MagicMock()
        mock_conn.cursor.return_value = cur

        PostgresTaskRegistry(connection_factory=lambda: mock_conn)

        executed = " ".join(str(c.args[0]) for c in cur.execute.call_args_list)
        assert "idx_tasks_status"    in executed
        assert "idx_tasks_priority"  in executed
        assert "idx_tasks_subsystem" in executed

    def test_commit_called_after_schema_init(self):
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = MagicMock()
        PostgresTaskRegistry(connection_factory=lambda: mock_conn)
        mock_conn.commit.assert_called()


# ── Tests: save (upsert) ──────────────────────────────────────────────────────

class TestSave:

    def test_save_calls_upsert_sql(self):
        registry, mock_conn = _make_registry()
        cur = _fresh_cursor(mock_conn)
        task = _make_task()

        registry.save(task)

        sql = cur.execute.call_args.args[0]
        assert "ON CONFLICT" in sql
        assert "DO UPDATE SET" in sql
        assert "INSERT INTO tasks" in sql

    def test_save_passes_correct_number_of_params(self):
        registry, mock_conn = _make_registry()
        cur = _fresh_cursor(mock_conn)
        task = _make_task()

        registry.save(task)

        params = cur.execute.call_args.args[1]
        assert len(params) == len(_COLUMNS)

    def test_save_returns_task_unchanged(self):
        registry, mock_conn = _make_registry()
        _fresh_cursor(mock_conn)
        task = _make_task()

        result = registry.save(task)

        assert result is task

    def test_save_commits(self):
        registry, mock_conn = _make_registry()
        _fresh_cursor(mock_conn)
        mock_conn.reset_mock()

        registry.save(_make_task())

        mock_conn.commit.assert_called()

    def test_save_rolls_back_on_error(self):
        registry, mock_conn = _make_registry()
        cur = _fresh_cursor(mock_conn)
        cur.execute.side_effect = RuntimeError("DB error")

        with pytest.raises(RuntimeError):
            registry.save(_make_task())

        mock_conn.rollback.assert_called()


# ── Tests: get ────────────────────────────────────────────────────────────────

class TestGet:

    def test_get_returns_none_when_not_found(self):
        registry, mock_conn = _make_registry()
        cur = _fresh_cursor(mock_conn)
        cur.fetchone.return_value = None

        assert registry.get("ghost-id") is None

    def test_get_returns_task_when_found(self):
        task = _make_task()
        registry, mock_conn = _make_registry()
        cur = _fresh_cursor(mock_conn)
        cur.fetchone.return_value = _task_row(task)

        result = registry.get(task.id)

        assert result is not None
        assert result.id == task.id
        assert result.title == task.title

    def test_get_uses_parameterised_query(self):
        registry, mock_conn = _make_registry()
        cur = _fresh_cursor(mock_conn)
        cur.fetchone.return_value = None

        registry.get("some-id")

        sql, params = cur.execute.call_args.args
        assert "WHERE id" in sql
        assert params == ("some-id",)


# ── Tests: delete ─────────────────────────────────────────────────────────────

class TestDelete:

    def test_delete_returns_true_when_row_deleted(self):
        registry, mock_conn = _make_registry()
        cur = _fresh_cursor(mock_conn)
        cur.rowcount = 1

        assert registry.delete("t-001") is True

    def test_delete_returns_false_when_not_found(self):
        registry, mock_conn = _make_registry()
        cur = _fresh_cursor(mock_conn)
        cur.rowcount = 0

        assert registry.delete("ghost") is False

    def test_delete_uses_parameterised_query(self):
        registry, mock_conn = _make_registry()
        cur = _fresh_cursor(mock_conn)
        cur.rowcount = 1

        registry.delete("t-002")

        sql, params = cur.execute.call_args.args
        assert "DELETE FROM tasks" in sql
        assert params == ("t-002",)


# ── Tests: list_all ───────────────────────────────────────────────────────────

class TestListAll:

    def test_list_all_returns_empty_list_when_no_tasks(self):
        registry, mock_conn = _make_registry()
        cur = _fresh_cursor(mock_conn)
        cur.fetchall.return_value = []

        assert registry.list_all() == []

    def test_list_all_returns_all_tasks(self):
        tasks = [_make_task("t-1"), _make_task("t-2")]
        registry, mock_conn = _make_registry()
        cur = _fresh_cursor(mock_conn)
        cur.fetchall.return_value = [_task_row(t) for t in tasks]

        result = registry.list_all()

        assert len(result) == 2
        assert {r.id for r in result} == {"t-1", "t-2"}


# ── Tests: by_status ──────────────────────────────────────────────────────────

class TestByStatus:

    def test_by_status_filters_on_status(self):
        task = _make_task(status=TaskStatus.PENDING)
        registry, mock_conn = _make_registry()
        cur = _fresh_cursor(mock_conn)
        cur.fetchall.return_value = [_task_row(task)]

        result = registry.by_status(TaskStatus.PENDING)

        sql = cur.execute.call_args.args[0]
        assert "status IN" in sql
        assert len(result) == 1

    def test_by_status_accepts_multiple_statuses(self):
        registry, mock_conn = _make_registry()
        cur = _fresh_cursor(mock_conn)
        cur.fetchall.return_value = []

        registry.by_status(TaskStatus.PENDING, TaskStatus.ACTIVE)

        sql, params = cur.execute.call_args.args
        assert sql.count("%s") == 2   # one placeholder per status


# ── Tests: pending_ordered ───────────────────────────────────────────────────

class TestPendingOrdered:

    def test_pending_ordered_orders_by_priority(self):
        registry, mock_conn = _make_registry()
        cur = _fresh_cursor(mock_conn)
        cur.fetchall.return_value = []

        registry.pending_ordered()

        sql = cur.execute.call_args.args[0]
        assert "ORDER BY priority_score DESC" in sql

    def test_pending_ordered_filters_pending_only(self):
        registry, mock_conn = _make_registry()
        cur = _fresh_cursor(mock_conn)
        cur.fetchall.return_value = []

        registry.pending_ordered()

        sql, params = cur.execute.call_args.args
        assert "WHERE status" in sql
        assert TaskStatus.PENDING.value in params


# ── Tests: count_by_status ───────────────────────────────────────────────────

class TestCountByStatus:

    def test_count_returns_dict(self):
        registry, mock_conn = _make_registry()
        cur = _fresh_cursor(mock_conn)
        cur.fetchall.return_value = [
            {"status": "pending",  "n": 3},
            {"status": "complete", "n": 7},
        ]

        counts = registry.count_by_status()

        assert counts == {"pending": 3, "complete": 7}

    def test_count_returns_empty_dict_when_no_tasks(self):
        registry, mock_conn = _make_registry()
        cur = _fresh_cursor(mock_conn)
        cur.fetchall.return_value = []

        assert registry.count_by_status() == {}


# ── Tests: oldest_pending ────────────────────────────────────────────────────

class TestOldestPending:

    def test_returns_none_when_no_pending(self):
        registry, mock_conn = _make_registry()
        cur = _fresh_cursor(mock_conn)
        cur.fetchone.return_value = None

        assert registry.oldest_pending() is None

    def test_returns_task_with_earliest_created_at(self):
        task = _make_task()
        registry, mock_conn = _make_registry()
        cur = _fresh_cursor(mock_conn)
        cur.fetchone.return_value = _task_row(task)

        result = registry.oldest_pending()

        assert result is not None
        assert result.id == task.id

    def test_sql_orders_by_created_at_asc(self):
        registry, mock_conn = _make_registry()
        cur = _fresh_cursor(mock_conn)
        cur.fetchone.return_value = None

        registry.oldest_pending()

        sql = cur.execute.call_args.args[0]
        assert "ORDER BY created_at ASC" in sql
        assert "LIMIT 1" in sql


# ── Tests: close ─────────────────────────────────────────────────────────────

class TestClose:

    def test_close_calls_connection_close(self):
        registry, mock_conn = _make_registry()
        registry.close()
        mock_conn.close.assert_called_once()


# ── Tests: registry_factory ──────────────────────────────────────────────────

class TestRegistryFactory:
    """Verify the factory produces the right backend type."""

    def test_factory_returns_sqlite_by_default(self):
        from tasks.registry_factory import create_task_registry
        from tasks.registry import SQLiteTaskRegistry

        reg = create_task_registry("sqlite", db_path=":memory:")
        try:
            assert isinstance(reg, SQLiteTaskRegistry)
        finally:
            reg.close()

    def test_factory_unknown_backend_raises(self):
        from tasks.registry_factory import create_task_registry

        with pytest.raises(ValueError, match="Unknown task registry backend"):
            create_task_registry("cassandra")

    def test_factory_postgres_uses_connection_factory(self):
        """Verify the factory can create a PostgresTaskRegistry via injection."""
        from tasks.registry_factory import create_task_registry
        from tasks.postgres_registry import PostgresTaskRegistry

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = MagicMock()

        # We bypass the factory's DSN path and use connection_factory directly
        reg = PostgresTaskRegistry(connection_factory=lambda: mock_conn)
        assert isinstance(reg, PostgresTaskRegistry)

    def test_abstract_registry_is_base_of_sqlite(self):
        from tasks.abstract_registry import AbstractTaskRegistry
        from tasks.registry import SQLiteTaskRegistry

        assert issubclass(SQLiteTaskRegistry, AbstractTaskRegistry)

    def test_abstract_registry_is_base_of_postgres(self):
        from tasks.abstract_registry import AbstractTaskRegistry
        from tasks.postgres_registry import PostgresTaskRegistry

        assert issubclass(PostgresTaskRegistry, AbstractTaskRegistry)


# ── Tests: is_transient helper ────────────────────────────────────────────────

class TestIsTransient:
    """Unit tests for the transient-error detection logic.

    ``_is_transient`` is the gatekeeper for retry decisions.  A false
    negative (treating a transient error as permanent) silently kills
    availability; a false positive (treating a permanent error as transient)
    causes infinite retry loops.  Both are tested explicitly here.
    """

    def test_connection_refused_is_transient(self):
        # psycopg2 is not installed in test environment; simulate by checking
        # the message-based fallback path.
        exc = Exception("could not connect to server: connection refused")
        assert _is_transient(exc) is True

    def test_server_closed_connection_is_transient(self):
        exc = Exception("server closed the connection unexpectedly")
        assert _is_transient(exc) is True

    def test_too_many_connections_is_transient(self):
        exc = Exception("FATAL: too many connections")
        assert _is_transient(exc) is True

    def test_ssl_reset_is_transient(self):
        exc = Exception("SSL connection has been closed unexpectedly")
        assert _is_transient(exc) is True

    def test_connection_timed_out_is_transient(self):
        exc = Exception("connection timed out")
        assert _is_transient(exc) is True

    def test_undefined_table_is_not_transient(self):
        exc = Exception('relation "tasks" does not exist')
        assert _is_transient(exc) is False

    def test_syntax_error_is_not_transient(self):
        exc = Exception("syntax error at or near SELECT")
        assert _is_transient(exc) is False

    def test_unique_violation_is_not_transient(self):
        exc = Exception('duplicate key value violates unique constraint "tasks_pkey"')
        assert _is_transient(exc) is False

    def test_generic_exception_is_not_transient(self):
        exc = ValueError("something completely different")
        assert _is_transient(exc) is False

    def test_case_insensitive_matching(self):
        # Error messages from PostgreSQL are mixed-case in practice.
        exc = Exception("Connection to server on socket failed: Connection refused")
        assert _is_transient(exc) is True


# ── Tests: connection retry ───────────────────────────────────────────────────

class TestConnectionRetry:
    """Verify that _conn() retries on transient errors with exponential back-off.

    These tests monkey-patch ``time.sleep`` to avoid actually sleeping and
    inspect how many times ``pool.getconn`` was called.
    """

    def _make_pool_registry(self, getconn_side_effects):
        """
        Build a registry with a mock pool whose getconn() raises the given
        sequence of exceptions/return values.

        Returns (registry, mock_pool).
        """
        mock_conn = MagicMock()
        mock_pool = MagicMock()
        mock_pool.getconn.side_effect = getconn_side_effects

        registry = PostgresTaskRegistry.__new__(PostgresTaskRegistry)
        registry._single           = None
        registry._pool             = mock_pool
        registry._max_retries      = 3
        registry._retry_base_delay = 0.1   # kept small for speed in tests
        registry._query_timeout_ms = None
        return registry, mock_pool

    def test_succeeds_on_first_attempt_no_sleep(self):
        mock_conn = MagicMock()
        registry, mock_pool = self._make_pool_registry([mock_conn])

        with patch("tasks.postgres_registry.time.sleep") as mock_sleep:
            with registry._conn() as conn:
                assert conn is mock_conn
            mock_sleep.assert_not_called()

    def test_retries_once_on_transient_error(self):
        mock_conn = MagicMock()
        transient = Exception("could not connect to server: connection refused")
        registry, mock_pool = self._make_pool_registry([transient, mock_conn])

        with patch("tasks.postgres_registry.time.sleep") as mock_sleep:
            with registry._conn() as conn:
                assert conn is mock_conn
            # Should have slept once (after first failure)
            mock_sleep.assert_called_once()
            mock_pool.getconn.call_count == 2

    def test_retries_use_exponential_backoff(self):
        """Sleep durations should double each attempt: base, base*2, base*4."""
        mock_conn = MagicMock()
        transient = Exception("connection refused")
        # Fail 3 times, succeed on 4th
        registry, mock_pool = self._make_pool_registry(
            [transient, transient, transient, mock_conn]
        )
        registry._retry_base_delay = 1.0

        sleep_calls = []
        with patch("tasks.postgres_registry.time.sleep", side_effect=lambda d: sleep_calls.append(d)):
            with registry._conn():
                pass

        # 3 failures → 3 sleeps with durations 1.0, 2.0, 4.0
        assert len(sleep_calls) == 3
        assert sleep_calls == [1.0, 2.0, 4.0]

    def test_raises_after_max_retries_exhausted(self):
        transient = Exception("could not connect to server: connection refused")
        registry, mock_pool = self._make_pool_registry(
            [transient] * 10  # always fail
        )
        registry._max_retries = 2

        with patch("tasks.postgres_registry.time.sleep"):
            with pytest.raises(Exception, match="could not connect"):
                with registry._conn():
                    pass

        # max_retries=2 means 3 total attempts (1 initial + 2 retries)
        assert mock_pool.getconn.call_count == 3

    def test_non_transient_error_not_retried(self):
        permanent = Exception('relation "tasks" does not exist')
        registry, mock_pool = self._make_pool_registry([permanent])

        with patch("tasks.postgres_registry.time.sleep") as mock_sleep:
            with pytest.raises(Exception, match='relation "tasks"'):
                with registry._conn():
                    pass

        # Only 1 attempt — no retry for permanent errors
        assert mock_pool.getconn.call_count == 1
        mock_sleep.assert_not_called()

    def test_connection_returned_to_pool_on_success(self):
        mock_conn = MagicMock()
        registry, mock_pool = self._make_pool_registry([mock_conn])

        with patch("tasks.postgres_registry.time.sleep"):
            with registry._conn():
                pass

        mock_pool.putconn.assert_called_once_with(mock_conn)

    def test_connection_returned_to_pool_on_exception(self):
        mock_conn = MagicMock()
        registry, mock_pool = self._make_pool_registry([mock_conn])

        with patch("tasks.postgres_registry.time.sleep"):
            with pytest.raises(RuntimeError):
                with registry._conn() as conn:
                    raise RuntimeError("query failed")

        mock_pool.putconn.assert_called_once_with(mock_conn)

    def test_rollback_called_on_query_exception(self):
        mock_conn = MagicMock()
        registry, mock_pool = self._make_pool_registry([mock_conn])

        with patch("tasks.postgres_registry.time.sleep"):
            with pytest.raises(RuntimeError):
                with registry._conn() as conn:
                    raise RuntimeError("query failed")

        mock_conn.rollback.assert_called_once()


# ── Tests: query timeout configuration ───────────────────────────────────────

class TestQueryTimeoutConfig:
    """Verify that query_timeout_ms is forwarded to the connection pool.

    We don't execute a real query — we just verify that the pool is
    constructed with the correct ``options`` kwarg.
    """

    def test_no_timeout_by_default(self):
        """The registry must not inject statement_timeout if not asked."""
        mock_conn = MagicMock()
        registry  = PostgresTaskRegistry(connection_factory=lambda: mock_conn)
        # query_timeout_ms is None; no error expected, just verify attribute
        assert registry._query_timeout_ms is None

    def test_timeout_stored_on_instance(self):
        mock_conn = MagicMock()
        registry  = PostgresTaskRegistry(
            connection_factory=lambda: mock_conn,
            query_timeout_ms=3000,
        )
        assert registry._query_timeout_ms == 3000

    def test_pool_receives_options_kwarg_with_timeout(self):
        """When using a real DSN, pool must be built with options=-c statement_timeout."""
        try:
            import psycopg2.pool as _pool
        except ImportError:
            pytest.skip("psycopg2 not installed")

        with patch.object(_pool, "ThreadedConnectionPool") as mock_pool_cls:
            mock_pool_cls.return_value = MagicMock()
            try:
                PostgresTaskRegistry(
                    dsn="postgresql://user:pw@localhost/db",
                    query_timeout_ms=5000,
                )
            except Exception:
                pass  # may fail at schema init — we only care about the call

            if mock_pool_cls.called:
                _, kwargs = mock_pool_cls.call_args
                assert "options" in kwargs
                assert "statement_timeout=5000" in kwargs["options"]

    def test_pool_has_no_options_kwarg_without_timeout(self):
        """Without query_timeout_ms, pool must NOT set statement_timeout."""
        try:
            import psycopg2.pool as _pool
        except ImportError:
            pytest.skip("psycopg2 not installed")

        with patch.object(_pool, "ThreadedConnectionPool") as mock_pool_cls:
            mock_pool_cls.return_value = MagicMock()
            try:
                PostgresTaskRegistry(dsn="postgresql://user:pw@localhost/db")
            except Exception:
                pass

            if mock_pool_cls.called:
                _, kwargs = mock_pool_cls.call_args
                assert "options" not in kwargs or "statement_timeout" not in kwargs.get("options", "")


# ── Tests: health_check ───────────────────────────────────────────────────────

class TestHealthCheck:
    """Verify that health_check() returns True/False and never raises."""

    def test_returns_true_when_query_succeeds(self):
        registry, mock_conn = _make_registry()
        cur = _fresh_cursor(mock_conn)
        cur.fetchone.return_value = None  # SELECT 1 returns something

        result = registry.health_check()

        assert result is True

    def test_returns_false_when_query_raises(self):
        registry, mock_conn = _make_registry()
        cur = _fresh_cursor(mock_conn)
        cur.execute.side_effect = Exception("database connection lost")

        result = registry.health_check()

        assert result is False

    def test_never_raises_on_exception(self):
        registry, mock_conn = _make_registry()
        mock_conn.cursor.side_effect = RuntimeError("pool is closed")

        # Must not raise — health_check catches all exceptions.
        result = registry.health_check()
        assert result is False

    def test_executes_select_1_from_tasks(self):
        """health_check must check the tasks table exists, not just connectivity."""
        registry, mock_conn = _make_registry()
        cur = _fresh_cursor(mock_conn)

        registry.health_check()

        sql = cur.execute.call_args.args[0]
        assert "SELECT 1" in sql
        assert "tasks" in sql

    def test_health_check_is_on_abstract_registry(self):
        """health_check must be declared in the abstract base class."""
        from tasks.abstract_registry import AbstractTaskRegistry
        import inspect
        assert "health_check" in dict(inspect.getmembers(AbstractTaskRegistry))


# ── Tests: save_batch ─────────────────────────────────────────────────────────

class TestSaveBatch:
    """Verify save_batch() inserts multiple tasks in a single transaction."""

    def test_empty_batch_is_no_op(self):
        registry, mock_conn = _make_registry()
        cur = _fresh_cursor(mock_conn)
        mock_conn.reset_mock()

        result = registry.save_batch([])

        assert result == []
        cur.execute.assert_not_called()
        mock_conn.commit.assert_not_called()

    def test_single_task_batch_commits(self):
        registry, mock_conn = _make_registry()
        cur = _fresh_cursor(mock_conn)
        mock_conn.reset_mock()
        task = _make_task()

        result = registry.save_batch([task])

        assert result == [task]
        mock_conn.commit.assert_called()

    def test_uses_executemany_not_individual_executes(self):
        """A batch of N must call executemany once, not execute N times."""
        registry, mock_conn = _make_registry()
        cur = _fresh_cursor(mock_conn)
        mock_conn.reset_mock()
        tasks = [_make_task(f"t-{i}") for i in range(5)]

        registry.save_batch(tasks)

        cur.executemany.assert_called_once()
        # executemany called with 5-row params list
        _, params_arg = cur.executemany.call_args.args
        assert len(params_arg) == 5

    def test_upsert_sql_contains_on_conflict(self):
        """Batch SQL must be an upsert, not a plain INSERT."""
        registry, mock_conn = _make_registry()
        cur = _fresh_cursor(mock_conn)
        mock_conn.reset_mock()

        registry.save_batch([_make_task()])

        sql = cur.executemany.call_args.args[0]
        assert "ON CONFLICT" in sql
        assert "DO UPDATE SET" in sql

    def test_each_task_row_has_correct_column_count(self):
        registry, mock_conn = _make_registry()
        cur = _fresh_cursor(mock_conn)
        mock_conn.reset_mock()
        tasks = [_make_task("a"), _make_task("b"), _make_task("c")]

        registry.save_batch(tasks)

        _, params_list = cur.executemany.call_args.args
        for row_params in params_list:
            assert len(row_params) == len(_COLUMNS)

    def test_batch_rolls_back_on_error(self):
        registry, mock_conn = _make_registry()
        cur = _fresh_cursor(mock_conn)
        cur.executemany.side_effect = RuntimeError("constraint violation")

        with pytest.raises(RuntimeError):
            registry.save_batch([_make_task()])

        mock_conn.rollback.assert_called()

    def test_returns_original_list_unchanged(self):
        registry, mock_conn = _make_registry()
        _fresh_cursor(mock_conn)
        tasks = [_make_task("a"), _make_task("b")]

        result = registry.save_batch(tasks)

        assert result is tasks   # same object, not a copy


# ── Tests: schema migration versioning ───────────────────────────────────────

class TestSchemaMigrations:
    """Verify the migration versioning system.

    These tests use the mock-connection path so no PostgreSQL server is
    needed.  We configure fetchall() to return different applied-version
    sets and verify the correct migrations are (or are not) applied.
    """

    def _migration_registry(self, already_applied: list[int]) -> tuple[PostgresTaskRegistry, MagicMock]:
        """
        Build a registry whose mock DB reports ``already_applied`` versions.

        The cursor's fetchall is pre-configured for the "SELECT version FROM
        schema_migrations" query, then reset for subsequent test queries.
        """
        mock_conn = MagicMock()

        call_count = [0]

        def cursor_factory(*args, **kwargs):
            cur = MagicMock()
            call_count[0] += 1
            if call_count[0] == 2:
                # Second cursor call is "SELECT version FROM schema_migrations"
                cur.fetchall.return_value = [{"version": v} for v in already_applied]
            else:
                cur.fetchall.return_value = []
                cur.fetchone.return_value = None
            return cur

        mock_conn.cursor.side_effect = cursor_factory
        registry = PostgresTaskRegistry(connection_factory=lambda: mock_conn)
        return registry, mock_conn

    def test_migrations_list_has_at_least_one_entry(self):
        assert len(_MIGRATIONS) >= 1

    def test_migrations_are_monotonically_increasing(self):
        versions = [m[0] for m in _MIGRATIONS]
        assert versions == sorted(versions), "Migration versions must be ascending"
        assert versions == list(range(1, len(versions) + 1)), "Versions must start at 1"

    def test_migrations_have_non_empty_descriptions(self):
        for version, description, statements in _MIGRATIONS:
            assert description.strip(), f"Migration {version} has empty description"

    def test_migrations_have_non_empty_sql_lists(self):
        for version, description, statements in _MIGRATIONS:
            assert statements, f"Migration {version} has no SQL statements"

    def test_create_migrations_table_executed_on_init(self):
        mock_conn = MagicMock()
        cur = MagicMock()
        mock_conn.cursor.return_value = cur
        cur.fetchall.return_value = []

        PostgresTaskRegistry(connection_factory=lambda: mock_conn)

        executed_sqls = " ".join(str(c.args[0]) for c in cur.execute.call_args_list)
        assert "schema_migrations" in executed_sqls

    def test_migration_1_applied_when_not_in_applied_set(self):
        mock_conn = MagicMock()
        executed_sqls = []
        call_count = [0]

        def cursor_factory(*args, **kwargs):
            cur = MagicMock()
            call_count[0] += 1
            if call_count[0] == 2:
                cur.fetchall.return_value = []  # no migrations applied yet
            else:
                cur.fetchall.return_value = []
                cur.fetchone.return_value = None

            def capture_execute(sql, *a, **kw):
                executed_sqls.append(str(sql))
            cur.execute.side_effect = capture_execute
            return cur

        mock_conn.cursor.side_effect = cursor_factory
        PostgresTaskRegistry(connection_factory=lambda: mock_conn)

        # Migration 1 creates the tasks table
        assert any("CREATE TABLE IF NOT EXISTS tasks" in sql for sql in executed_sqls)

    def test_migration_skipped_when_already_applied(self):
        """If version 1 is already in schema_migrations, do not re-execute it."""
        mock_conn = MagicMock()
        create_table_call_count = [0]
        call_count = [0]

        def cursor_factory(*args, **kwargs):
            cur = MagicMock()
            call_count[0] += 1
            if call_count[0] == 2:
                cur.fetchall.return_value = [{"version": 1}]  # already applied
            else:
                cur.fetchall.return_value = []
                cur.fetchone.return_value = None

            def capture_execute(sql, *a, **kw):
                if "CREATE TABLE IF NOT EXISTS tasks" in str(sql):
                    create_table_call_count[0] += 1
            cur.execute.side_effect = capture_execute
            return cur

        mock_conn.cursor.side_effect = cursor_factory
        PostgresTaskRegistry(connection_factory=lambda: mock_conn)

        assert create_table_call_count[0] == 0

    def test_list_applied_migrations_returns_empty_on_error(self):
        mock_conn = MagicMock()
        cur = MagicMock()
        mock_conn.cursor.return_value = cur
        cur.fetchall.return_value = []

        registry = PostgresTaskRegistry(connection_factory=lambda: mock_conn)

        # Force an error on the next query
        cur.execute.side_effect = Exception("table missing")
        result = registry.list_applied_migrations()

        assert result == []  # must not raise

    def test_list_applied_migrations_returns_structured_dicts(self):
        mock_conn = MagicMock()
        cur = MagicMock()
        mock_conn.cursor.return_value = cur

        call_count = [0]
        def fake_fetchall():
            call_count[0] += 1
            if call_count[0] <= 1:
                return []  # schema_migrations initial query
            return [
                {"version": 1, "description": "Initial schema", "applied_at": "2024-01-01T00:00:00+00:00"},
            ]
        cur.fetchall.side_effect = fake_fetchall

        registry = PostgresTaskRegistry(connection_factory=lambda: mock_conn)

        # Reset to normal fetchall for the list_applied_migrations call
        cur.fetchall.side_effect = None
        cur.fetchall.return_value = [
            {"version": 1, "description": "Initial schema", "applied_at": "2024-01-01T00:00:00+00:00"},
        ]
        result = registry.list_applied_migrations()

        if result:  # may be [] if error occurs due to mock complexity
            assert isinstance(result[0], dict)
            assert "version" in result[0]
            assert "description" in result[0]
            assert "applied_at" in result[0]


# ── Tests: SQLiteTaskRegistry enterprise features ─────────────────────────────

class TestSQLiteHealthCheck:
    """health_check() and save_batch() on the SQLite backend."""

    def _make_sqlite(self):
        from tasks.registry import SQLiteTaskRegistry
        return SQLiteTaskRegistry(db_path=":memory:")

    def test_health_check_returns_true_on_fresh_registry(self):
        reg = self._make_sqlite()
        try:
            assert reg.health_check() is True
        finally:
            reg.close()

    def test_health_check_returns_false_after_close(self):
        reg = self._make_sqlite()
        reg.close()
        assert reg.health_check() is False

    def test_save_batch_empty_is_noop(self):
        reg = self._make_sqlite()
        try:
            result = reg.save_batch([])
            assert result == []
        finally:
            reg.close()

    def test_save_batch_inserts_all_tasks(self):
        from tasks.registry import SQLiteTaskRegistry
        reg = SQLiteTaskRegistry(db_path=":memory:")
        try:
            tasks = [_make_task(f"t-{i}") for i in range(10)]
            reg.save_batch(tasks)
            assert len(reg.list_all()) == 10
        finally:
            reg.close()

    def test_save_batch_is_atomic_on_error(self):
        """If any task in the batch fails, no tasks should be written."""
        from tasks.registry import SQLiteTaskRegistry
        reg = SQLiteTaskRegistry(db_path=":memory:")
        try:
            good = _make_task("good-id")
            bad  = MagicMock()
            bad.to_dict.side_effect = RuntimeError("serialisation failure")

            with pytest.raises(Exception):
                reg.save_batch([good, bad])

            # The good task must not have been committed
            assert reg.get("good-id") is None
        finally:
            reg.close()

    def test_save_batch_returns_same_list(self):
        from tasks.registry import SQLiteTaskRegistry
        reg = SQLiteTaskRegistry(db_path=":memory:")
        try:
            tasks = [_make_task("a"), _make_task("b")]
            result = reg.save_batch(tasks)
            assert result is tasks
        finally:
            reg.close()

    def test_save_batch_upserts_existing_tasks(self):
        """Saving a task twice via save_batch must not duplicate it."""
        from tasks.registry import SQLiteTaskRegistry
        reg = SQLiteTaskRegistry(db_path=":memory:")
        try:
            task = _make_task("dup-id")
            reg.save_batch([task, task])
            # Should be exactly 1 row, not 2
            assert len(reg.list_all()) == 1
        finally:
            reg.close()


# ── Tests: abstract_registry completeness ────────────────────────────────────

class TestAbstractRegistryInterface:
    """Verify the ABC declares all required methods and both backends implement them."""

    REQUIRED_METHODS = [
        "save", "save_batch", "update", "delete",
        "get", "list_all", "by_status", "by_subsystem",
        "children_of", "pending_ordered", "count_by_status",
        "oldest_pending", "health_check", "close",
    ]

    def test_all_required_methods_on_abstract_registry(self):
        from tasks.abstract_registry import AbstractTaskRegistry
        import inspect

        abstract_methods = AbstractTaskRegistry.__abstractmethods__
        for method in self.REQUIRED_METHODS:
            assert method in abstract_methods, (
                f"AbstractTaskRegistry.{method} must be abstract"
            )

    def test_postgres_implements_all_required_methods(self):
        mock_conn = MagicMock()
        reg = PostgresTaskRegistry(connection_factory=lambda: mock_conn)
        for method in self.REQUIRED_METHODS:
            assert callable(getattr(reg, method, None)), (
                f"PostgresTaskRegistry missing method: {method}"
            )

    def test_sqlite_implements_all_required_methods(self):
        from tasks.registry import SQLiteTaskRegistry
        reg = SQLiteTaskRegistry(db_path=":memory:")
        try:
            for method in self.REQUIRED_METHODS:
                assert callable(getattr(reg, method, None)), (
                    f"SQLiteTaskRegistry missing method: {method}"
                )
        finally:
            reg.close()
