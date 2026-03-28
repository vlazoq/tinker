"""
_task_registry.py — TaskRegistryMixin: SQLite-backed task persistence.

Split from manager.py to keep each memory layer in its own focused module.
"""

from __future__ import annotations

import logging
from typing import Optional

from .schemas import Task, TaskStatus

logger = logging.getLogger(__name__)


class TaskRegistryMixin:
    """Methods for task persistence (SQLite)."""

    async def store_task(self, task: Task) -> Task:
        """
        Persist a task to the Task Registry (SQLite).

        If the task has no session_id set, the manager's session_id is used.
        Returns the task (for chaining).
        """
        if not task.session_id:
            task.session_id = self.session_id
        await self._sqlite.upsert_task(task)
        logger.debug("Stored task %s ('%s')", task.id, task.title)
        return task

    async def get_task(self, task_id: str) -> Optional[Task]:
        """Retrieve a task by its UUID. Returns None if not found."""
        row = await self._sqlite.get_task(task_id)
        return Task.from_dict(row) if row else None

    async def update_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        result: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Update a task's status, result, and/or error in the Task Registry."""
        await self._sqlite.update_task_status(task_id, status.value, result, error)

    async def get_pending_tasks(self, limit: int = 50) -> list[Task]:
        rows = await self._sqlite.get_tasks_by_status("pending", limit)
        return [Task.from_dict(r) for r in rows]

    async def get_running_tasks(self, limit: int = 50) -> list[Task]:
        rows = await self._sqlite.get_tasks_by_status("running", limit)
        return [Task.from_dict(r) for r in rows]

    async def get_session_tasks(
        self, session_id: Optional[str] = None, limit: int = 200
    ) -> list[Task]:
        sid = session_id or self.session_id
        rows = await self._sqlite.get_tasks_by_session(sid, limit)
        return [Task.from_dict(r) for r in rows]

    async def get_child_tasks(self, parent_task_id: str) -> list[Task]:
        rows = await self._sqlite.get_child_tasks(parent_task_id)
        return [Task.from_dict(r) for r in rows]
