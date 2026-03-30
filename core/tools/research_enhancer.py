"""
core/tools/research_enhancer.py
===============================

Advanced research enhancements that bring Tinker's web research close to
Claude/Manus quality — fully local, zero cloud dependency.

Four enhancements:

1. **Query Rewriting** — Uses the Judge model (small, fast) to rewrite vague
   knowledge gaps into precise, search-engine-optimized queries.

2. **Memory-First Lookup** — Checks ChromaDB for previously archived research
   before hitting the web, saving time and reducing redundant scraping.

3. **Content Summarization** — Uses the Judge model to summarize scraped
   content instead of blindly truncating at N characters.

4. **Iterative Deepening** — If initial research quality is low, refines the
   query and tries again (up to a configurable number of rounds).

All LLM calls use AgentRole.CRITIC which routes to the secondary (2-3B)
model — cheap and fast, no heavy resources consumed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("tinker.tools.research_enhancer")


class ResearchEnhancer:
    """Wraps the base research pipeline with LLM-powered enhancements.

    Parameters
    ----------
    router : ModelRouter
        LLM router for Judge model calls (query rewriting, summarization).
    memory_manager : MemoryManager | None
        For memory-first lookup.  If None, memory lookup is skipped.
    query_rewrite : bool
        Enable LLM query rewriting (default True).
    memory_first : bool
        Check memory before web search (default True).
    summarize : bool
        Summarize scraped content via LLM (default True).
    iterative_max_rounds : int
        Max iterative deepening rounds (default 2, 0 = disabled).
    summarize_threshold : int
        Summarize only if scraped content exceeds this many chars (default 3000).
    memory_min_score : float
        Minimum relevance score for memory results to count as a hit (default 0.7).
    llm_timeout : float
        Timeout for each LLM call in seconds (default 15).
    """

    def __init__(
        self,
        router: Any = None,
        memory_manager: Any = None,
        query_rewrite: bool = True,
        memory_first: bool = True,
        summarize: bool = True,
        iterative_max_rounds: int = 2,
        summarize_threshold: int = 3000,
        memory_min_score: float = 0.7,
        llm_timeout: float = 15.0,
        llm_max_concurrent: int = 2,
    ) -> None:
        self._router = router
        self._memory = memory_manager
        self.query_rewrite = query_rewrite
        self.memory_first = memory_first
        self.summarize = summarize
        self.iterative_max_rounds = iterative_max_rounds
        self._summarize_threshold = summarize_threshold
        self._memory_min_score = memory_min_score
        self._llm_timeout = llm_timeout
        self._llm_semaphore = asyncio.Semaphore(llm_max_concurrent)
        self._stats = {
            "queries_rewritten": 0,
            "memory_hits": 0,
            "memory_misses": 0,
            "summaries_generated": 0,
            "iterative_rounds": 0,
            "total_enhanced": 0,
        }

    def get_stats(self) -> dict:
        return {**self._stats}

    # ------------------------------------------------------------------
    # 1. Query Rewriting
    # ------------------------------------------------------------------

    async def rewrite_query(self, gap: str) -> list[str]:
        """Rewrite a vague knowledge gap into 1-2 precise search queries.

        Uses the Judge model (CRITIC role, small & fast) to transform
        natural-language gaps into search-engine-optimized queries.

        Returns a list of 1-2 query strings.  Falls back to [gap] on error.
        """
        if not self.query_rewrite or self._router is None:
            return [gap]

        try:
            from core.llm.types import AgentRole

            prompt = (
                "You are a search query optimizer. Rewrite the following "
                "knowledge gap into 1-2 precise web search queries that would "
                "find the most relevant technical information. Return ONLY the "
                "queries, one per line, no numbering or explanation.\n\n"
                f"Knowledge gap: {gap}"
            )
            async with self._llm_semaphore:
                resp = await asyncio.wait_for(
                    self._router.complete_text(
                        role=AgentRole.CRITIC,
                        prompt=prompt,
                        system="Output only search queries, one per line.",
                        temperature=0.3,
                    ),
                    timeout=self._llm_timeout,
                )
            raw = resp.raw_text.strip()
            queries = [q.strip() for q in raw.splitlines() if q.strip()]
            if queries:
                self._stats["queries_rewritten"] += 1
                logger.debug("query_rewrite: %r → %s", gap, queries)
                return queries[:2]  # cap at 2
        except Exception as exc:
            logger.debug("query_rewrite: failed for %r: %s — using original", gap, exc)

        return [gap]

    # ------------------------------------------------------------------
    # 2. Memory-First Lookup
    # ------------------------------------------------------------------

    async def check_memory(self, gap: str) -> dict | None:
        """Check ChromaDB for previously archived research on this topic.

        Returns a research-result dict if a high-confidence match is found,
        or None if nothing relevant exists in memory.
        """
        if not self.memory_first or self._memory is None:
            return None

        try:
            # Use the memory manager's search interface
            search_fn = getattr(self._memory, "search", None)
            if search_fn is None:
                return None

            results = await asyncio.wait_for(
                _ensure_coro(search_fn(query=gap, top_k=3)),
                timeout=5.0,
            )

            if not results:
                return None

            # Check if best match exceeds relevance threshold
            if isinstance(results, list) and results:
                best = results[0]
                score = (
                    best.get("score", 0) if isinstance(best, dict) else getattr(best, "score", 0)
                )
                if score >= self._memory_min_score:
                    if isinstance(best, dict):
                        content = (
                            best.get("content", "")
                            or best.get("text", "")
                            or best.get("snippet", "")
                        )
                    else:
                        content = getattr(best, "content", "")
                    if content and len(content) > 50:
                        self._stats["memory_hits"] += 1
                        logger.info(
                            "memory_first: hit for %r (score=%.3f, %d chars)",
                            gap,
                            score,
                            len(content),
                        )
                        return {
                            "query": gap,
                            "result": content,
                            "sources": ["memory-archive"],
                            "from_memory": True,
                            "memory_score": score,
                        }
        except Exception as exc:
            logger.debug("memory_first: lookup failed for %r: %s", gap, exc)

        self._stats["memory_misses"] += 1
        return None

    # ------------------------------------------------------------------
    # 3. Content Summarization
    # ------------------------------------------------------------------

    async def summarize_content(self, content: str, gap: str, max_chars: int = 5000) -> str:
        """Summarize scraped content using the Judge model.

        Only activates if content exceeds summarize_threshold.  Falls back
        to truncation if the LLM call fails.
        """
        if not self.summarize or self._router is None or len(content) <= self._summarize_threshold:
            return content[:max_chars]

        try:
            from core.llm.types import AgentRole

            # Truncate input to a reasonable size for the Judge model context
            input_text = content[:12000]  # keep within small model's context

            prompt = (
                "Summarize the following research content concisely, keeping "
                "all key technical details, patterns, trade-offs, and specific "
                "recommendations. Remove boilerplate, ads, and navigation text. "
                f"The research was about: {gap}\n\n"
                f"---\n{input_text}\n---\n\n"
                "Provide a focused technical summary:"
            )
            async with self._llm_semaphore:
                resp = await asyncio.wait_for(
                    self._router.complete_text(
                        role=AgentRole.CRITIC,
                        prompt=prompt,
                        system="You are a technical research summarizer. Be concise but thorough.",
                        temperature=0.2,
                    ),
                    timeout=self._llm_timeout,
                )
            summary = resp.raw_text.strip()
            if summary and len(summary) > 50:
                self._stats["summaries_generated"] += 1
                logger.debug(
                    "summarize: %d chars → %d chars for %r",
                    len(content),
                    len(summary),
                    gap,
                )
                return summary[:max_chars]
        except Exception as exc:
            logger.debug("summarize: failed for %r: %s — truncating", gap, exc)

        return content[:max_chars]

    # ------------------------------------------------------------------
    # 4. Iterative Deepening
    # ------------------------------------------------------------------

    async def assess_and_refine(self, gap: str, result: dict, round_num: int) -> str | None:
        """Assess research quality and return a refined query if insufficient.

        Returns a new query string if research should be retried, or None
        if the result is adequate.
        """
        if (
            self.iterative_max_rounds <= 0
            or round_num >= self.iterative_max_rounds
            or self._router is None
        ):
            return None

        content = result.get("result", "")
        if not content or len(content) < 100:
            # Too short — always refine
            return f"{gap} detailed explanation examples"

        try:
            from core.llm.types import AgentRole

            prompt = (
                f'A researcher searched for: "{gap}"\n\n'
                f"They found this content ({len(content)} chars):\n"
                f"{content[:2000]}\n\n"
                "Is this research sufficient to answer the knowledge gap? "
                "Reply with ONLY one of:\n"
                "- SUFFICIENT (if the research adequately covers the topic)\n"
                "- REFINE: <new search query> (if more/better research is needed)"
            )
            async with self._llm_semaphore:
                resp = await asyncio.wait_for(
                    self._router.complete_text(
                        role=AgentRole.CRITIC,
                        prompt=prompt,
                        system="Evaluate research quality. Reply SUFFICIENT or REFINE: <query>.",
                        temperature=0.2,
                    ),
                    timeout=self._llm_timeout,
                )
            text = resp.raw_text.strip()
            if text.upper().startswith("REFINE"):
                refined = text.split(":", 1)[-1].strip() if ":" in text else None
                if refined:
                    self._stats["iterative_rounds"] += 1
                    logger.info(
                        "iterative: round %d — refining %r → %r",
                        round_num + 1,
                        gap,
                        refined,
                    )
                    return refined
        except Exception as exc:
            logger.debug("iterative: assessment failed for %r: %s", gap, exc)

        return None

    # ------------------------------------------------------------------
    # Full enhanced research pipeline
    # ------------------------------------------------------------------

    async def enhanced_research(
        self,
        gap: str,
        research_fn,
        *,
        max_results: int = 10,
        max_scrape: int = 5,
        max_content_chars: int = 8000,
    ) -> dict:
        """Run the full enhanced research pipeline for a single knowledge gap.

        Pipeline:
        1. Check memory for existing research
        2. Rewrite query for better search results
        3. Search + scrape with configurable depth
        4. Summarize content (if long)
        5. Assess quality and iterate if needed

        Parameters
        ----------
        gap : str
            The knowledge gap to research.
        research_fn : callable
            The base research function (tool_layer.research).
        max_results : int
            Number of search results to fetch (default 10).
        max_scrape : int
            Number of top results to deep-scrape (default 5).
        max_content_chars : int
            Max chars for final content (default 8000).

        Returns
        -------
        dict with keys: query, result, sources, from_memory (bool).
        """
        self._stats["total_enhanced"] += 1

        # Step 1: Memory-first lookup
        cached = await self.check_memory(gap)
        if cached is not None:
            return cached

        # Step 2: Rewrite query
        queries = await self.rewrite_query(gap)

        # Step 3: Search + scrape (use first query as primary)
        best_result = None
        all_sources = []

        for query in queries:
            try:
                result = await research_fn(
                    query=query,
                    max_scrape=max_scrape,
                    max_content_chars=max_content_chars,
                )
                content = result.get("result", "")
                sources = result.get("sources", [])
                all_sources.extend(sources)

                if best_result is None or len(content) > len(best_result.get("result", "")):
                    best_result = result
            except Exception as exc:
                logger.debug("enhanced_research: query %r failed: %s", query, exc)

        if best_result is None:
            return {
                "query": gap,
                "result": f"Research unavailable for '{gap}'.",
                "sources": [],
                "from_memory": False,
            }

        # Merge sources from all queries
        seen = set()
        unique_sources = []
        for s in all_sources:
            if s not in seen:
                seen.add(s)
                unique_sources.append(s)
        best_result["sources"] = unique_sources

        # Step 4: Summarize if content is long
        content = best_result.get("result", "")
        if content and len(content) > self._summarize_threshold:
            best_result["result"] = await self.summarize_content(
                content, gap, max_chars=max_content_chars
            )

        # Step 5: Iterative deepening
        for round_num in range(self.iterative_max_rounds):
            refined_query = await self.assess_and_refine(gap, best_result, round_num)
            if refined_query is None:
                break  # research is sufficient

            try:
                deeper = await research_fn(
                    query=refined_query,
                    max_scrape=max_scrape,
                    max_content_chars=max_content_chars,
                )
                deeper_content = deeper.get("result", "")
                if deeper_content and len(deeper_content) > len(best_result.get("result", "")):
                    # Combine: keep both the original and the deeper result
                    combined = (
                        best_result.get("result", "")
                        + "\n\n--- Additional research ---\n\n"
                        + deeper_content
                    )
                    best_result["result"] = combined[:max_content_chars]
                    # Merge sources
                    for s in deeper.get("sources", []):
                        if s not in seen:
                            seen.add(s)
                            unique_sources.append(s)
                    best_result["sources"] = unique_sources
            except Exception as exc:
                logger.debug("iterative: round %d search failed: %s", round_num + 1, exc)
                break

        best_result["from_memory"] = False
        best_result["query"] = gap  # preserve original gap as the query
        return best_result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _ensure_coro(maybe_coro):
    """Await a coroutine or return a plain value."""
    if asyncio.iscoroutine(maybe_coro) or asyncio.isfuture(maybe_coro):
        return await maybe_coro
    return maybe_coro
