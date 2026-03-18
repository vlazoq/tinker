"""
tasks/registry_factory.py
==========================
Factory function for creating a task registry backend.

Why a factory?
--------------
The Orchestrator and ``TaskEngine`` should not hard-code which registry
implementation they use.  The factory centralises the decision: it reads
the ``TINKER_DB_BACKEND`` environment variable (or accepts an explicit
argument) and returns the appropriate ``AbstractTaskRegistry`` subclass,
already initialised.

Supported backends
------------------
``sqlite`` (default)
    ``SQLiteTaskRegistry`` — single-file, zero dependencies, works everywhere.
    Best for development, single-machine production, and CI.

    Required: ``db_path`` keyword argument (or ``TINKER_TASK_DB`` env var).

``postgres``
    ``PostgresTaskRegistry`` — PostgreSQL pool, suitable for multi-process
    or multi-machine deployments.

    Required: ``dsn`` keyword argument (or ``TINKER_POSTGRES_DSN`` env var).

Usage
-----
::

    # Let the env var decide (12-factor style):
    registry = create_task_registry()

    # Explicit SQLite:
    registry = create_task_registry("sqlite", db_path="./tasks.sqlite")

    # Explicit PostgreSQL:
    registry = create_task_registry(
        "postgres",
        dsn="postgresql://tinker:pw@db.host/tinker_tasks",
    )
"""

from __future__ import annotations

import os
from typing import Any

from .abstract_registry import AbstractTaskRegistry
from .registry          import SQLiteTaskRegistry
from .postgres_registry import PostgresTaskRegistry


def create_task_registry(
    backend: str | None = None,
    **kwargs: Any,
) -> AbstractTaskRegistry:
    """
    Create and return a configured task registry.

    Parameters
    ----------
    backend : str, optional
        ``"sqlite"`` or ``"postgres"``.
        Defaults to the value of ``TINKER_DB_BACKEND`` env var, or
        ``"sqlite"`` if the env var is not set.
    **kwargs
        Passed to the registry constructor.  For ``sqlite``: ``db_path``.
        For ``postgres``: ``dsn``, ``min_conn``, ``max_conn``.

    Returns
    -------
    AbstractTaskRegistry
        A fully initialised registry ready for use.

    Raises
    ------
    ValueError
        If an unsupported backend name is given.
    ImportError
        If the ``postgres`` backend is requested but ``psycopg2`` is not
        installed.
    """
    effective_backend = (
        backend
        or os.getenv("TINKER_DB_BACKEND", "sqlite")
    ).lower().strip()

    if effective_backend == "sqlite":
        db_path = kwargs.get("db_path") or os.getenv(
            "TINKER_TASK_DB", "tinker_tasks_engine.sqlite"
        )
        return SQLiteTaskRegistry(db_path=db_path)

    if effective_backend in ("postgres", "postgresql"):
        dsn      = kwargs.get("dsn")      or os.getenv("TINKER_POSTGRES_DSN", "")
        min_conn = kwargs.get("min_conn", 1)
        max_conn = kwargs.get("max_conn", 10)
        return PostgresTaskRegistry(dsn=dsn, min_conn=min_conn, max_conn=max_conn)

    raise ValueError(
        f"Unknown task registry backend: {effective_backend!r}.  "
        f"Supported values: 'sqlite', 'postgres'."
    )
