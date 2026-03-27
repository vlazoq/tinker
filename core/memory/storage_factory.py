"""
core/memory/storage_factory.py
================================

Factory for creating memory backend adapters.

Why a factory?
--------------
Storage adapters (Redis, DuckDB, Chroma, SQLite) should not be instantiated
inline in application code.  This factory centralises the decision and reads
environment variables so the backend can be swapped without code changes
(12-factor style, OCP / DIP).

Each adapter type maps to one of the four Tinker memory layers:

  ``redis``   → Working memory  (ephemeral, per-task context)
  ``duckdb``  → Session memory  (structured artifacts, current run)
  ``chroma``  → Research archive (vector search, cross-session)
  ``sqlite``  → Task/audit store (durable, long-lived records)

Usage
-----
::

    # Use env var to pick backend:
    redis_adapter = create_storage_adapter("redis")

    # Explicit path override:
    sqlite_adapter = create_storage_adapter(
        "sqlite",
        db_path="./custom_tasks.sqlite",
    )
"""

from __future__ import annotations

import os
from typing import Any


def create_storage_adapter(
    backend: str,
    **kwargs: Any,
) -> Any:
    """Create and return a configured storage adapter.

    Parameters
    ----------
    backend : str
        ``"redis"``, ``"duckdb"``, ``"chroma"``, or ``"sqlite"``.
    **kwargs
        Passed to the adapter constructor.

        * ``redis``  → ``url`` (default: ``TINKER_REDIS_URL``), ``default_ttl``
        * ``duckdb`` → ``db_path`` (default: ``TINKER_DUCKDB_PATH``)
        * ``chroma`` → ``path`` (default: ``TINKER_CHROMA_PATH``)
        * ``sqlite`` → ``db_path`` (default: ``TINKER_SQLITE_PATH``)

    Returns
    -------
    RedisAdapter | DuckDBAdapter | ChromaAdapter | SQLiteAdapter

    Raises
    ------
    ValueError
        If an unsupported backend name is given.
    """
    key = backend.lower().strip()

    if key == "redis":
        from core.memory.storage import RedisAdapter

        return RedisAdapter(
            url=kwargs.get("url") or os.getenv(
                "TINKER_REDIS_URL", "redis://localhost:6379"
            ),
            default_ttl=kwargs.get(
                "default_ttl",
                int(os.getenv("TINKER_REDIS_TTL", "3600")),
            ),
        )

    if key == "duckdb":
        from core.memory.storage import DuckDBAdapter

        return DuckDBAdapter(
            path=kwargs.get("path") or kwargs.get("db_path") or os.getenv(
                "TINKER_DUCKDB_PATH", "tinker_session.duckdb"
            ),
        )

    if key in ("chroma", "chromadb"):
        from core.memory.storage import ChromaAdapter

        return ChromaAdapter(
            path=kwargs.get("path") or os.getenv(
                "TINKER_CHROMA_PATH", "./chroma_db"
            ),
            collection_name=kwargs.get("collection_name", "tinker_research"),
        )

    if key == "sqlite":
        from core.memory.storage import SQLiteAdapter

        return SQLiteAdapter(
            path=kwargs.get("path") or kwargs.get("db_path") or os.getenv(
                "TINKER_SQLITE_PATH", "tinker_tasks.sqlite"
            ),
        )

    if key == "trino":
        # Trino is an optional backend — requires ``pip install trino``.
        # If the package is missing, fall back to DuckDB with a warning.
        try:
            from core.memory.trino_store import TrinoSessionStore, TrinoConfig

            config = TrinoConfig.from_env()
            return TrinoSessionStore(config=config)
        except ImportError:
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "TINKER_SESSION_BACKEND=trino but 'trino' package is not "
                "installed.  Falling back to DuckDB.  Install with: "
                "pip install trino"
            )
            from core.memory.storage import DuckDBAdapter

            return DuckDBAdapter(
                path=kwargs.get("path") or os.getenv(
                    "TINKER_DUCKDB_PATH", "tinker_session.duckdb"
                ),
            )

    raise ValueError(
        f"Unknown storage backend: {backend!r}.  "
        f"Supported values: 'redis', 'duckdb', 'chroma', 'sqlite', 'trino'."
    )
