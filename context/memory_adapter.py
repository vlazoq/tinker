"""
context/memory_adapter.py
==========================
Adapter that bridges the real ``MemoryManager`` (memory/manager.py) to
the ``_MemoryManagerProtocol`` interface that ``ContextAssembler`` expects.

Why an adapter?
---------------
``ContextAssembler`` was designed with its own memory interface
(``get_arch_state_summary``, ``semantic_search_session``, etc.) that does
not exactly match the real ``MemoryManager`` API.  Rather than coupling
either component to the other's API, we keep them independent and bridge
the gap here using the *Adapter* design pattern.

Session memory retrieval strategy
----------------------------------
``semantic_search_session`` uses a two-phase strategy for relevance:

1. **Task-based lookup first** — if the query contains a task-like substring
   (e.g. starts with a UUID or matches "task:..."), call
   ``MemoryManager.get_artifacts_by_task(query, limit=top_k)`` which returns
   artifacts directly associated with that task.  These have score=1.0.

2. **Recency fallback** — for general queries, call
   ``MemoryManager.get_recent_artifacts(limit=top_k * 2)`` and return the
   top-k most recent ones with score=0.8.  This is an approximation (true
   semantic session search would require DuckDB + embeddings), but it is
   correct and fast for Tinker's usage pattern where recent context is almost
   always the most relevant context.

Using this in production
------------------------
::

    from memory.manager import MemoryManager
    from context.memory_adapter import MemoryAdaptor
    from context.assembler import ContextAssembler

    memory_manager = MemoryManager(...)
    await memory_manager.connect()

    assembler = ContextAssembler(
        memory_manager = MemoryAdaptor(memory_manager),
        prompt_builder = ...,
    )

All methods degrade gracefully: if the real memory backend is unavailable
(not connected, service down, etc.) they return empty results rather than
raising, so the orchestrator can continue with reduced context.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .assembler import MemoryItem, _MemoryManagerProtocol

if TYPE_CHECKING:
    from memory.manager import MemoryManager   # avoid circular import at module level

logger = logging.getLogger(__name__)


class MemoryAdaptor(_MemoryManagerProtocol):
    """
    Adapts ``MemoryManager`` to the ``_MemoryManagerProtocol`` interface.

    All methods are async and degrade gracefully on error — they log a
    warning and return an empty result so the orchestrator can always
    continue with whatever context is available.

    Parameters
    ----------
    memory_manager : MemoryManager
        A fully initialised (and connected) ``MemoryManager`` instance.
    max_content_chars : int
        Hard cap applied to each item's content before it is returned,
        preventing accidental over-sized prompts.  Default 1 000 chars.
    """

    def __init__(
        self,
        memory_manager: "MemoryManager",
        max_content_chars: int = 1_000,
    ) -> None:
        self._mm    = memory_manager
        self._limit = max_content_chars

    # ── Protocol implementation ───────────────────────────────────────────────

    async def get_arch_state_summary(self) -> str:
        """
        Return the content of the most recent synthesis / design document.

        Calls ``MemoryManager.get_all_documents()`` and returns the *last*
        entry (most recent synthesis), truncated to ``max_content_chars``.
        Returns ``""`` if the store is empty or unavailable.
        """
        try:
            docs = await self._mm.get_all_documents()
            if not docs:
                return ""
            latest = docs[-1]
            content = latest.get("content", "")
            return content[: self._limit]
        except Exception as exc:
            logger.warning("MemoryAdaptor.get_arch_state_summary: %s", exc)
            return ""

    async def semantic_search_session(
        self, query: str, top_k: int = 5
    ) -> list[MemoryItem]:
        """
        Return DuckDB session artifacts relevant to *query*.

        Strategy (see module docstring):
        1. If *query* looks like a task ID (UUID prefix or ``"task:..."`` prefix),
           attempt a direct task-based lookup with score=1.0 (exact relevance).
        2. Otherwise fall back to recency-based retrieval (most recent
           ``top_k * 2`` artifacts, scored at 0.8).

        All errors are caught and logged; returns [] on failure so the
        orchestrator always has a usable (possibly empty) context bundle.
        """
        import re as _re

        try:
            # Phase 1: task-ID based lookup for precise artifact retrieval.
            # UUID v4 format: 8-4-4-4-12 hex digits.
            uuid_pattern = _re.compile(
                r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
                _re.IGNORECASE,
            )
            task_id: str | None = None
            stripped = query.strip()
            if uuid_pattern.match(stripped):
                task_id = stripped
            elif stripped.lower().startswith("task:"):
                task_id = stripped[5:].strip()

            if task_id:
                try:
                    artifacts = await self._mm.get_artifacts_by_task(task_id, limit=top_k)
                    if artifacts:
                        return [
                            MemoryItem(
                                id      = a.id,
                                content = a.content[: self._limit],
                                score   = 1.0,   # exact task match
                                source  = "session",
                            )
                            for a in artifacts[:top_k]
                        ]
                except Exception:
                    pass  # fall through to recency retrieval

            # Phase 2: recency-based fallback for general queries.
            artifacts = await self._mm.get_recent_artifacts(limit=top_k * 2)
            return [
                MemoryItem(
                    id      = a.id,
                    content = a.content[: self._limit],
                    score   = 0.8,   # approximate relevance (recency-based)
                    source  = "session",
                )
                for a in artifacts[:top_k]
            ]
        except Exception as exc:
            logger.warning("MemoryAdaptor.semantic_search_session: %s", exc)
            return []

    async def semantic_search_archive(
        self, query: str, top_k: int = 5
    ) -> list[MemoryItem]:
        """
        Search the ChromaDB research archive for notes relevant to *query*.

        Delegates to ``MemoryManager.search_research(query, n_results=top_k)``.
        Returns an empty list if ChromaDB is not available.
        """
        try:
            notes = await self._mm.search_research(query=query, n_results=top_k)
            return [
                MemoryItem(
                    id      = n.id,
                    content = n.content[: self._limit],
                    score   = 0.75,
                    source  = "archive",
                )
                for n in notes
            ]
        except Exception as exc:
            logger.warning("MemoryAdaptor.semantic_search_archive: %s", exc)
            return []

    async def get_prior_critique(self, task_id: str) -> list[MemoryItem]:
        """
        Retrieve earlier Architect+Critic artifacts stored under *task_id*.

        Calls ``MemoryManager.get_artifacts_by_task(task_id, limit=3)``.
        Score is ``1.0`` — these are direct prior results for this exact task.
        """
        try:
            artifacts = await self._mm.get_artifacts_by_task(task_id, limit=3)
            return [
                MemoryItem(
                    id      = a.id,
                    content = a.content[: self._limit],
                    score   = 1.0,
                    source  = "critique",
                )
                for a in artifacts
            ]
        except Exception as exc:
            logger.warning("MemoryAdaptor.get_prior_critique: %s", exc)
            return []
