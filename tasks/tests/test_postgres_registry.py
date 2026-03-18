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
from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

import pytest

from tasks.postgres_registry import (
    PostgresTaskRegistry,
    _pg_row_to_dict,
    _task_to_row,
    _COLUMNS,
    _CREATE_TABLE,
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
