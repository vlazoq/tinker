"""
infra/resilience/tests/test_migrations.py
=====================================
Tests for SQLiteMigrationRunner in resilience/migrations.py.

Uses tmp_path to create temporary SQLite databases — no real Redis/Ollama.
"""

from __future__ import annotations

import sqlite3

import pytest

from infra.resilience.migrations import SQLiteMigrationRunner


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    """Return a path to a temporary SQLite database file."""
    return str(tmp_path / "test.db")


@pytest.fixture
def runner(db_path):
    """Return a fresh SQLiteMigrationRunner for each test."""
    return SQLiteMigrationRunner(db_path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMigrate:
    def test_migrate_creates_schema_migrations_table(self, runner, db_path):
        """migrate() should create the schema_migrations table."""
        runner.migrate([])

        conn = sqlite3.connect(db_path)
        try:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchall()
            assert len(tables) == 1
        finally:
            conn.close()

    def test_migrate_applies_pending_migrations_in_order(self, runner, db_path):
        """migrate() should apply migrations sorted by version."""
        migrations = [
            (2, "CREATE TABLE foo (id INTEGER);"),
            (1, "CREATE TABLE bar (id INTEGER);"),
        ]
        applied = runner.migrate(migrations)

        assert applied == 2

        conn = sqlite3.connect(db_path)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "bar" in tables
            assert "foo" in tables
        finally:
            conn.close()

    def test_migrate_is_idempotent(self, runner):
        """Running migrate() twice should apply each migration exactly once."""
        migrations = [
            (1, "CREATE TABLE IF NOT EXISTS widgets (id INTEGER);"),
        ]
        first = runner.migrate(migrations)
        second = runner.migrate(migrations)

        assert first == 1
        assert second == 0  # no new migrations on the second call

    def test_migrate_only_applies_pending_versions(self, runner):
        """Only migrations with version > current_version() are applied."""
        runner.migrate([(1, "CREATE TABLE alpha (x INTEGER);")])
        assert runner.current_version() == 1

        # Now add a second migration
        applied = runner.migrate([
            (1, "CREATE TABLE alpha (x INTEGER);"),
            (2, "CREATE TABLE beta (x INTEGER);"),
        ])
        assert applied == 1  # only v2 was pending
        assert runner.current_version() == 2

    def test_migrate_with_empty_list_returns_zero(self, runner):
        """migrate([]) should return 0 and not raise."""
        applied = runner.migrate([])
        assert applied == 0

    def test_migrate_creates_tracking_entries(self, runner, db_path):
        """Each applied migration should have a row in schema_migrations."""
        runner.migrate([
            (1, "CREATE TABLE t1 (x INTEGER);"),
            (2, "CREATE TABLE t2 (x INTEGER);"),
        ])

        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
            versions = [row[0] for row in rows]
            assert versions == [1, 2]
        finally:
            conn.close()


class TestCurrentVersion:
    def test_current_version_zero_before_any_migrations(self, runner):
        """current_version() returns 0 when no migrations have been applied."""
        assert runner.current_version() == 0

    def test_current_version_zero_on_fresh_db_without_migrate(self, db_path):
        """current_version() is safe even before migrate() is ever called."""
        runner = SQLiteMigrationRunner(db_path)
        assert runner.current_version() == 0

    def test_current_version_after_single_migration(self, runner):
        """After applying migration v1, current_version() returns 1."""
        runner.migrate([(1, "CREATE TABLE x (a TEXT);")])
        assert runner.current_version() == 1

    def test_current_version_returns_highest_applied(self, runner):
        """current_version() returns the highest version in schema_migrations."""
        runner.migrate([
            (1, "CREATE TABLE a (id INTEGER);"),
            (3, "CREATE TABLE b (id INTEGER);"),
            (7, "CREATE TABLE c (id INTEGER);"),
        ])
        assert runner.current_version() == 7

    def test_current_version_stable_between_calls(self, runner):
        """current_version() should return the same value on repeated calls."""
        runner.migrate([(1, "CREATE TABLE z (id INTEGER);")])
        v1 = runner.current_version()
        v2 = runner.current_version()
        assert v1 == v2 == 1


class TestCustomMigrationsTable:
    def test_custom_table_name(self, db_path):
        """SQLiteMigrationRunner should use the custom migrations_table name."""
        runner = SQLiteMigrationRunner(db_path, migrations_table="my_versions")
        runner.migrate([(1, "CREATE TABLE foo (id INTEGER);")])

        conn = sqlite3.connect(db_path)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "my_versions" in tables
            assert "schema_migrations" not in tables
        finally:
            conn.close()

    def test_two_runners_same_db_different_tables(self, db_path):
        """Two runners on the same DB with different table names operate independently."""
        r1 = SQLiteMigrationRunner(db_path, migrations_table="ver_a")
        r2 = SQLiteMigrationRunner(db_path, migrations_table="ver_b")

        r1.migrate([(1, "CREATE TABLE aa (x INTEGER);")])
        r2.migrate([(1, "CREATE TABLE bb (x INTEGER);")])

        assert r1.current_version() == 1
        assert r2.current_version() == 1
