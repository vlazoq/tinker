"""
infra/resilience/migrations.py
========================
Lightweight schema migration runner for SQLite databases.

Usage::

    runner = SQLiteMigrationRunner(db_path="tinker_dlq.sqlite")
    runner.migrate(MIGRATIONS)

MIGRATIONS is a list of (version: int, sql: str) tuples, sorted ascending.
The runner creates a `schema_migrations` table, tracks applied versions,
and applies any pending ones in order — idempotently.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


class SQLiteMigrationRunner:
    """
    Idempotent schema migration runner for SQLite databases.

    Parameters
    ----------
    db_path          : Path to the SQLite database file.
    migrations_table : Name of the table used to track applied versions.
                       Defaults to ``schema_migrations``.
    """

    def __init__(
        self,
        db_path: str,
        migrations_table: str = "schema_migrations",
    ) -> None:
        self._db_path = db_path
        self._migrations_table = migrations_table

    def migrate(self, migrations: list[tuple[int, str]]) -> int:
        """
        Apply any pending migrations in ascending version order.

        The migrations table is created if it doesn't exist.  Only
        migrations whose version is greater than ``current_version()``
        are applied.  Each migration runs inside its own transaction so
        a failure rolls back only that migration.

        Parameters
        ----------
        migrations : List of ``(version, sql)`` tuples sorted ascending.

        Returns
        -------
        int : Number of migrations applied in this call.
        """
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        try:
            # Ensure the tracking table exists
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._migrations_table} (
                    version    INTEGER PRIMARY KEY,
                    applied_at TEXT
                )
                """
            )
            conn.commit()

            current = self._current_version_conn(conn)
            applied = 0

            for version, sql in sorted(migrations, key=lambda x: x[0]):
                if version <= current:
                    continue
                try:
                    with conn:  # transaction — auto-commits or rolls back
                        if sql and sql.strip() and not sql.strip().startswith("--"):
                            conn.executescript(sql)
                        now = datetime.now(UTC).isoformat()
                        conn.execute(
                            f"INSERT INTO {self._migrations_table} (version, applied_at) VALUES (?, ?)",
                            (version, now),
                        )
                    applied += 1
                    logger.info(
                        "SQLiteMigrationRunner: applied migration v%d to %s",
                        version,
                        self._db_path,
                    )
                except Exception as exc:
                    logger.error(
                        "SQLiteMigrationRunner: failed to apply migration v%d to %s: %s",
                        version,
                        self._db_path,
                        exc,
                    )
                    raise

            return applied
        finally:
            conn.close()

    def current_version(self) -> int:
        """
        Return the highest applied migration version, or 0 if none.

        Opens a short-lived connection — safe to call at any time.
        """
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        try:
            return self._current_version_conn(conn)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _current_version_conn(self, conn: sqlite3.Connection) -> int:
        """Return the highest applied version using an existing connection."""
        try:
            row = conn.execute(f"SELECT MAX(version) FROM {self._migrations_table}").fetchone()
            return row[0] if row and row[0] is not None else 0
        except sqlite3.OperationalError:
            # Table doesn't exist yet — no migrations applied
            return 0
