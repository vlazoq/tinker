"""
tasks/abstract_registry.py
===========================
Abstract base class that defines the full interface every task-registry
backend must implement.

Why an ABC?
-----------
Tinker ships with two storage backends:

  * ``SQLiteTaskRegistry``  — the default; zero extra dependencies, great
    for development, single-machine deployments, and CI.
  * ``PostgresTaskRegistry`` — for multi-process or multi-machine setups
    that need a shared, centralised task store (e.g. a cluster of Grub
    workers all pulling from the same queue).

Both backends expose exactly the same set of methods.  The orchestrator,
task engine, and Grub bridge never know which backend is active — they
program to this abstract interface.

Adding a new backend (e.g. MySQL, DynamoDB) requires only:
  1. Subclassing ``AbstractTaskRegistry``.
  2. Implementing every ``@abstractmethod`` below.
  3. Registering the new name in ``registry_factory.create_task_registry``.

Design notes
------------
* All methods are synchronous.  The async boundary lives one level up in
  ``TaskEngine``, which uses ``asyncio.to_thread`` to call the registry
  without blocking the event loop.
* No transactions are exposed to callers; each method is its own atomic
  operation.  Backends handle transactions internally.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .schema import Task, TaskStatus, Subsystem


class AbstractTaskRegistry(ABC):
    """
    Contract shared by SQLiteTaskRegistry and PostgresTaskRegistry.

    Every public method here corresponds to an operation the rest of
    Tinker needs.  Do not add methods that only one backend can support —
    that defeats the point of the abstraction.
    """

    # ── Write ─────────────────────────────────────────────────────────────────

    @abstractmethod
    def save(self, task: Task) -> Task:
        """
        Insert or replace (upsert) a task.

        If a task with the same ``id`` already exists it is overwritten.
        Returns the task unchanged (for convenient chaining).
        """

    @abstractmethod
    def update(self, task: Task) -> Task:
        """
        Refresh ``updated_at`` and save the task.

        Convenience wrapper: ``task.touch(); return self.save(task)``.
        """

    @abstractmethod
    def delete(self, task_id: str) -> bool:
        """
        Permanently remove a task.

        Returns ``True`` if a row was deleted, ``False`` if the ID was not
        found.  Prefer setting ``status=ARCHIVED`` for audit purposes.
        """

    # ── Read ──────────────────────────────────────────────────────────────────

    @abstractmethod
    def get(self, task_id: str) -> Task | None:
        """Fetch a single task by its unique ID, or ``None`` if not found."""

    @abstractmethod
    def list_all(self) -> list[Task]:
        """Return every task in the store, in no particular order."""

    @abstractmethod
    def by_status(self, *statuses: TaskStatus) -> list[Task]:
        """
        Return all tasks whose status is one of the given values.

        Example::

            registry.by_status(TaskStatus.PENDING, TaskStatus.BLOCKED)
        """

    @abstractmethod
    def by_subsystem(self, subsystem: Subsystem) -> list[Task]:
        """Return all tasks belonging to a particular subsystem."""

    @abstractmethod
    def children_of(self, parent_id: str) -> list[Task]:
        """Return all tasks whose ``parent_id`` matches the given ID."""

    @abstractmethod
    def pending_ordered(self) -> list[Task]:
        """
        Return PENDING tasks sorted by ``priority_score`` descending.

        This is the primary read path for ``TaskQueue`` — it fetches the
        sorted work list before picking the next task.
        """

    @abstractmethod
    def count_by_status(self) -> dict[str, int]:
        """
        Return a ``{status_string: count}`` mapping.

        Example return value::

            {"pending": 5, "active": 1, "complete": 12}
        """

    @abstractmethod
    def oldest_pending(self) -> Task | None:
        """
        Return the PENDING task with the earliest ``created_at``.

        Useful for anti-starvation checks: if the oldest pending task
        has been waiting more than N hours, the priority logic may need
        to be adjusted.
        """

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @abstractmethod
    def close(self) -> None:
        """
        Release the underlying database connection.

        Call at process shutdown.  After calling ``close()``, further
        method calls have undefined behaviour.
        """
