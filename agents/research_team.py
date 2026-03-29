"""
agents/research_team.py
========================

Research Team: parallel research agents for concurrent knowledge-gap filling.

Instead of sequentially researching one gap at a time, the ResearchTeam
dispatches multiple research queries concurrently (up to a configurable
concurrency limit).  This dramatically reduces latency when the Architect
flags multiple knowledge gaps.

Architecture
------------
The ResearchTeam is NOT a new agent role — it's a coordinator that wraps
the existing tool_layer.research() method with concurrency control.  It
plugs into the micro loop's researcher routing without changing the
orchestrator code.

Usage::

    from agents.research_team import ResearchTeam

    team = ResearchTeam(tool_layer=tool_layer, max_concurrent=3)

    # Research multiple gaps in parallel:
    results = await team.research_gaps(
        gaps=["How does RAFT consensus work?", "What is CRDTs?"],
        task=task_dict,
        timeout=30.0,
    )

Integration
-----------
The micro loop can use ResearchTeam as a drop-in enhancement:
- Without ResearchTeam: gaps researched sequentially (existing behaviour)
- With ResearchTeam: gaps researched in parallel with concurrency limit

The Orchestrator constructor accepts an optional ``research_team`` parameter.
If provided, ``_route_researcher`` uses it instead of sequential calls.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("tinker.research_team")


class ResearchTeam:
    """Coordinate parallel research across multiple knowledge gaps.

    Parameters
    ----------
    tool_layer : ToolRegistry
        The tool layer that provides the ``research()`` method.
    memory_manager : optional
        Memory manager for auto-archiving research results.
    max_concurrent : int
        Maximum number of simultaneous research queries (default 3).
        Higher values = faster but uses more network/compute resources.
    """

    def __init__(
        self,
        tool_layer: Any,
        memory_manager: Any = None,
        max_concurrent: int = 3,
    ) -> None:
        self._tool_layer = tool_layer
        self._memory_manager = memory_manager
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._max_concurrent = max_concurrent
        # Session-level deduplication cache
        self._cache: dict[str, dict] = {}

    async def research_gaps(
        self,
        gaps: list[str],
        task: dict | None = None,
        timeout: float = 30.0,
    ) -> list[dict[str, Any]]:
        """Research multiple knowledge gaps concurrently.

        Parameters
        ----------
        gaps : list of gap descriptions to research
        task : current task dict (for archiving metadata)
        timeout : per-gap timeout in seconds

        Returns
        -------
        list of {"gap": str, "result": dict} for each successfully resolved gap
        """
        if not gaps:
            return []

        # Deduplicate gaps by normalised form
        unique_gaps: list[tuple[str, str]] = []  # (original, normalised)
        seen_norms: set[str] = set()
        for gap in gaps:
            norm = self._normalize(gap)
            if norm not in seen_norms:
                seen_norms.add(norm)
                unique_gaps.append((gap, norm))

        logger.info(
            "ResearchTeam: researching %d gaps (%d unique) with concurrency=%d",
            len(gaps),
            len(unique_gaps),
            self._max_concurrent,
        )

        # Launch all research tasks concurrently (bounded by semaphore)
        tasks = [
            self._research_one(original, norm, task, timeout)
            for original, norm in unique_gaps
        ]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect successful results
        results = []
        for item in raw_results:
            if isinstance(item, dict):
                results.append(item)
            elif isinstance(item, Exception):
                logger.debug("ResearchTeam: gap failed: %s", item)

        logger.info(
            "ResearchTeam: resolved %d/%d gaps",
            len(results),
            len(unique_gaps),
        )
        return results

    async def _research_one(
        self,
        gap: str,
        norm: str,
        task: dict | None,
        timeout: float,
    ) -> dict[str, Any]:
        """Research a single gap with semaphore-bounded concurrency."""
        # Check cache first
        if norm in self._cache:
            logger.debug("ResearchTeam: cache hit for %r", gap)
            return {"gap": gap, "result": self._cache[norm]}

        async with self._semaphore:
            result = await asyncio.wait_for(
                self._tool_layer.research(query=gap),
                timeout=timeout,
            )

        # Cache the result
        self._cache[norm] = result

        # Auto-archive if memory manager is available
        await self._try_archive(gap, result, task)

        return {"gap": gap, "result": result}

    async def _try_archive(
        self,
        gap: str,
        result: dict,
        task: dict | None,
    ) -> None:
        """Archive research result to memory (non-fatal on failure)."""
        if self._memory_manager is None:
            return
        if not hasattr(self._memory_manager, "store_research"):
            return

        content = result.get("result", "")
        if not content or len(content) < 50:
            return

        sources = result.get("sources", [])
        try:
            await asyncio.wait_for(
                self._memory_manager.store_research(
                    content=content,
                    topic=gap,
                    tags=["auto-archived", "web-research", "parallel"],
                    source=", ".join(sources[:3]) if sources else "web-search",
                    task_id=task.get("id", "") if task else "",
                    metadata={
                        "subsystem": task.get("subsystem", "") if task else "",
                        "auto_archived": True,
                        "sources": sources,
                    },
                ),
                timeout=10.0,
            )
        except Exception as exc:
            logger.debug("ResearchTeam: archive failed for %r: %s", gap, exc)

    @staticmethod
    def _normalize(query: str) -> str:
        """Normalise a query for deduplication."""
        return " ".join(query.lower().split())
