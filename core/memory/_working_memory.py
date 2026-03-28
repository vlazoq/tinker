"""
_working_memory.py — WorkingMemoryMixin: Redis-backed ephemeral context storage.

Split from manager.py to keep each memory layer in its own focused module.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class WorkingMemoryMixin:
    """Methods for ephemeral per-task context storage (Redis)."""

    async def set_context(
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = None,
        session_id: Optional[str] = None,
    ) -> None:
        """
        Store an ephemeral key-value pair in Working Memory (Redis).

        Typical uses: current task state, in-progress reasoning, short-lived flags.
        """
        sid = session_id or self.session_id
        await self._redis.set(sid, key, value, ttl)

    async def get_context(
        self,
        key: str,
        session_id: Optional[str] = None,
    ) -> Optional[Any]:
        """Retrieve a value from Working Memory. Returns None if missing / expired."""
        sid = session_id or self.session_id
        return await self._redis.get(sid, key)

    async def delete_context(self, key: str, session_id: Optional[str] = None) -> None:
        sid = session_id or self.session_id
        await self._redis.delete(sid, key)

    async def clear_working_memory(self, session_id: Optional[str] = None) -> int:
        """Flush all Working Memory keys for a session. Returns count deleted."""
        sid = session_id or self.session_id
        return await self._redis.flush_session(sid)

    async def list_context_keys(self, session_id: Optional[str] = None) -> list[str]:
        sid = session_id or self.session_id
        return await self._redis.keys(sid)
