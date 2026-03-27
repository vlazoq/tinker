"""
Tinker Memory — Trino/Presto session storage adapter.

What this file does
-------------------
Provides a ``TrinoSessionStore`` that implements the same interface as
``DuckDBSessionStore`` but stores session artifacts in a Trino (or Presto)
catalog.  This is useful when you want to query Tinker's artifacts alongside
other data in a data warehouse, or when DuckDB is not suitable for your
deployment.

When to use this
----------------
Most Tinker deployments should use the default DuckDB adapter — it is
simpler, faster for single-node setups, and has zero external dependencies.
Use Trino when:

- You already have a Trino/Presto cluster and want unified querying.
- You need to share artifacts across multiple Tinker instances.
- Your deployment is large enough to benefit from distributed SQL.

Setup
-----
1. Install the Python driver: ``pip install trino``
2. Set ``TINKER_SESSION_BACKEND=trino`` in your ``.env`` file.
3. Set ``TINKER_TRINO_HOST``, ``TINKER_TRINO_PORT``, etc.

The adapter auto-creates the required table on first use.

How it fits into Tinker
-----------------------
The ``StorageFactory`` (in ``storage_factory.py``) reads the
``TINKER_SESSION_BACKEND`` environment variable.  When set to ``"trino"``,
the factory creates a ``TrinoSessionStore`` instead of the default
``DuckDBAdapter``.  The rest of Tinker never knows the difference — the
MemoryManager interacts with whichever store the factory selected.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy Trino import — the ``trino`` package is an optional dependency.
# We try to import it here, and if it's missing we set a flag.  The actual
# error is only raised when someone tries to construct a TrinoSessionStore,
# so importing this module never fails.
# ---------------------------------------------------------------------------

try:
    from trino.dbapi import connect as trino_connect

    _TRINO_AVAILABLE = True
except ImportError:
    trino_connect = None  # type: ignore[assignment]
    _TRINO_AVAILABLE = False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class TrinoConfig:
    """
    Connection settings for a Trino/Presto cluster.

    Each field maps to a ``TINKER_TRINO_*`` environment variable so the
    connection can be configured without editing code (12-factor style).

    Fields
    ------
    host        : Trino coordinator hostname (default ``localhost``).
    port        : Trino coordinator HTTP port (default ``8080``).
    user        : Username sent with each query (default ``tinker``).
                  Trino uses this for access control and query attribution.
    catalog     : Trino catalog (connector) to use (default ``memory``).
                  Use ``hive`` or ``iceberg`` for persistent storage.
    schema_name : Schema (database) within the catalog (default ``tinker``).
    table_name  : Table that stores artifacts (default ``session_artifacts``).
    """

    host: str = "localhost"
    port: int = 8080
    user: str = "tinker"
    catalog: str = "memory"
    schema_name: str = "tinker"
    table_name: str = "session_artifacts"

    @classmethod
    def from_env(cls) -> "TrinoConfig":
        """
        Build a TrinoConfig from ``TINKER_TRINO_*`` environment variables.

        Every field has a sensible default, so you only need to set the
        variables that differ from the defaults.
        """
        import os

        return cls(
            host=os.getenv("TINKER_TRINO_HOST", "localhost"),
            port=int(os.getenv("TINKER_TRINO_PORT", "8080")),
            user=os.getenv("TINKER_TRINO_USER", "tinker"),
            catalog=os.getenv("TINKER_TRINO_CATALOG", "memory"),
            schema_name=os.getenv("TINKER_TRINO_SCHEMA", "tinker"),
            table_name=os.getenv("TINKER_TRINO_TABLE", "session_artifacts"),
        )


# ---------------------------------------------------------------------------
# Trino session store
# ---------------------------------------------------------------------------


class TrinoSessionStore:
    """
    Session artifact storage backed by Trino/Presto.

    Implements the same public interface as ``DuckDBSessionStore`` so it
    can be used as a drop-in replacement.  The orchestrator and agents
    interact with whichever store the ``StorageFactory`` selects — they
    never import this class directly.

    How connections work
    --------------------
    Trino connections are lightweight HTTP connections — each query opens a
    new connection, sends the SQL, reads the result, and closes.  This is
    intentional: Trino is designed for this pattern, and it avoids issues
    with stale connections in long-running processes like Tinker.

    Thread safety
    -------------
    Each method creates its own connection, so this class is safe to use
    from multiple asyncio tasks (though calls are synchronous — Trino's
    Python driver does not support async natively).

    Parameters
    ----------
    config : TrinoConfig, optional
        Connection settings.  Defaults to ``TrinoConfig.from_env()`` which
        reads from environment variables.

    Raises
    ------
    ImportError
        If the ``trino`` Python package is not installed.
    """

    def __init__(self, config: TrinoConfig | None = None) -> None:
        if not _TRINO_AVAILABLE:
            raise ImportError(
                "The 'trino' package is required for Trino session storage. "
                "Install it with: pip install trino"
            )
        self._config = config or TrinoConfig.from_env()
        self._ensure_table()
        logger.info(
            "TrinoSessionStore connected to %s:%d/%s.%s.%s",
            self._config.host,
            self._config.port,
            self._config.catalog,
            self._config.schema_name,
            self._config.table_name,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_connection(self) -> Any:
        """
        Create a fresh Trino connection.

        Trino connections are lightweight (just HTTP), so creating one per
        operation is the recommended pattern.  There's no TCP connection
        pool to manage.
        """
        return trino_connect(
            host=self._config.host,
            port=self._config.port,
            user=self._config.user,
            catalog=self._config.catalog,
            schema=self._config.schema_name,
        )

    def _ensure_table(self) -> None:
        """
        Create the artifacts table if it doesn't exist.

        Called once during __init__.  Uses IF NOT EXISTS so it's safe to
        call repeatedly (idempotent).
        """
        ddl = f"""
        CREATE TABLE IF NOT EXISTS {self._config.table_name} (
            id            VARCHAR,
            session_id    VARCHAR,
            content       VARCHAR,
            artifact_type VARCHAR,
            task_id       VARCHAR,
            metadata_json VARCHAR,
            created_at    VARCHAR,
            archived      BOOLEAN
        )
        """
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute(ddl)
            logger.debug("Trino table '%s' ensured.", self._config.table_name)
        except Exception as exc:
            # Non-fatal — the table may already exist, or the catalog may not
            # support DDL (e.g. read-only connectors).
            logger.warning("Could not create Trino table: %s", exc)
        finally:
            conn.close()

    def _row_to_dict(
        self, row: tuple, columns: list[str]
    ) -> dict[str, Any]:
        """
        Convert a Trino result row (tuple) into a dict with parsed metadata.

        The ``metadata_json`` column is stored as a JSON string.  This
        helper parses it back into a Python dict and renames the field to
        ``metadata`` for consistency with the DuckDB adapter's output.
        """
        record = dict(zip(columns, row))
        if "metadata_json" in record:
            try:
                record["metadata"] = json.loads(record.pop("metadata_json"))
            except (json.JSONDecodeError, TypeError):
                record["metadata"] = {}
        return record

    # ------------------------------------------------------------------
    # Public API — matches DuckDBSessionStore interface
    # ------------------------------------------------------------------

    def store_artifact(
        self,
        content: str,
        artifact_type: str,
        task_id: str = "",
        session_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """
        Store a session artifact in Trino and return its unique ID.

        Parameters
        ----------
        content       : The artifact text (markdown, JSON, etc.).
        artifact_type : Category label (e.g. "research_note",
                        "architecture_analysis").
        task_id       : The task that produced this artifact.
        session_id    : Current orchestrator session ID.
        metadata      : Optional key-value metadata stored as JSON.

        Returns
        -------
        str : The generated artifact ID (UUID).
        """
        artifact_id = str(uuid.uuid4())
        created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        meta_json = json.dumps(metadata or {})

        sql = f"""
        INSERT INTO {self._config.table_name}
            (id, session_id, content, artifact_type, task_id,
             metadata_json, created_at, archived)
        VALUES (?, ?, ?, ?, ?, ?, ?, false)
        """
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                sql,
                (
                    artifact_id,
                    session_id,
                    content,
                    artifact_type,
                    task_id,
                    meta_json,
                    created_at,
                ),
            )
            logger.debug(
                "Stored artifact %s in Trino (type=%s)", artifact_id, artifact_type
            )
        finally:
            conn.close()

        return artifact_id

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        """
        Retrieve a single artifact by ID.

        Returns None if the artifact doesn't exist.
        """
        sql = f"SELECT * FROM {self._config.table_name} WHERE id = ?"
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute(sql, (artifact_id,))
            row = cur.fetchone()
            if row is None:
                return None
            columns = [desc[0] for desc in cur.description]
            return self._row_to_dict(row, columns)
        finally:
            conn.close()

    def get_recent_artifacts(
        self,
        artifact_type: str | None = None,
        limit: int = 20,
        session_id: str = "",
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Retrieve recent artifacts, optionally filtered by type and session.

        Parameters
        ----------
        artifact_type    : Filter by artifact type (None = all types).
        limit            : Maximum number of results (default 20).
        session_id       : Filter by session (empty = all sessions).
        include_archived : Whether to include archived artifacts.

        Returns
        -------
        list[dict] : Artifacts sorted by created_at descending.
        """
        conditions: list[str] = []
        params: list[Any] = []

        if not include_archived:
            conditions.append("archived = false")
        if artifact_type:
            conditions.append("artifact_type = ?")
            params.append(artifact_type)
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        sql = f"""
        SELECT * FROM {self._config.table_name}
        {where}
        ORDER BY created_at DESC
        LIMIT {limit}
        """
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            columns = [desc[0] for desc in cur.description]
            return [self._row_to_dict(row, columns) for row in cur.fetchall()]
        finally:
            conn.close()

    def count_artifacts(
        self, session_id: str = "", include_archived: bool = False
    ) -> int:
        """
        Count artifacts, optionally filtered by session.

        Parameters
        ----------
        session_id       : Filter by session (empty = count all).
        include_archived : Whether to include archived artifacts.

        Returns
        -------
        int : Number of matching artifacts.
        """
        conditions: list[str] = []
        params: list[Any] = []

        if not include_archived:
            conditions.append("archived = false")
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        sql = f"SELECT COUNT(*) FROM {self._config.table_name} {where}"
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            row = cur.fetchone()
            return row[0] if row else 0
        finally:
            conn.close()

    def archive_artifact(self, artifact_id: str) -> bool:
        """
        Mark an artifact as archived (soft-delete).

        Archived artifacts are excluded from ``get_recent_artifacts()`` by
        default but can still be retrieved with ``include_archived=True``.

        Returns True if the operation succeeded (Trino doesn't reliably
        report affected row counts, so we assume success if no exception).
        """
        sql = f"""
        UPDATE {self._config.table_name}
        SET archived = true
        WHERE id = ?
        """
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute(sql, (artifact_id,))
            logger.debug("Archived artifact %s in Trino", artifact_id)
            return True
        except Exception as exc:
            logger.warning("Failed to archive artifact %s: %s", artifact_id, exc)
            return False
        finally:
            conn.close()

    def get_artifacts_by_task_id(
        self, task_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """
        Retrieve all artifacts produced by a specific task.

        Parameters
        ----------
        task_id : The task ID to filter by.
        limit   : Maximum results (default 50).

        Returns
        -------
        list[dict] : Matching artifacts, newest first.
        """
        sql = f"""
        SELECT * FROM {self._config.table_name}
        WHERE task_id = ?
        ORDER BY created_at DESC
        LIMIT {limit}
        """
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute(sql, (task_id,))
            columns = [desc[0] for desc in cur.description]
            return [self._row_to_dict(row, columns) for row in cur.fetchall()]
        finally:
            conn.close()
