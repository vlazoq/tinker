"""
core/tools/research_crawler.py
===============================

Continuous Research Crawler — the batch-and-crawl pipeline for indefinite
context gathering.

This module implements the iterative research cycle:

    1. Search for the topic → get first N results
    2. For each result: scrape, judge usefulness, extract key content
    3. Follow relevant sublinks (max depth 2–3 levels)
    4. Add useful findings to the knowledge pool
    5. Compact/summarise the pool — deduplicate, prune, preserve examples
    6. Move to next batch of N results and repeat

The crawler is designed to run alongside the Architect mode, continuously
feeding enriched context into the architect's loops.  It can also run
standalone in Research mode.

Integration
-----------
The Orchestrator can attach a ResearchCrawler instance and call
``crawler.run_batch()`` periodically (e.g., between micro loops or on a
background task).  Accumulated knowledge is stored in ChromaDB via the
memory manager and automatically surfaced to the Architect via the
context assembler's ``research_notes`` budget.

All LLM calls use AgentRole.CRITIC (the small/fast judge model) to
minimise resource usage.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("tinker.research_crawler")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CrawlFinding:
    """A single piece of extracted knowledge."""

    url: str
    title: str
    content: str
    relevance_score: float  # 0.0–1.0 from the judge
    key_points: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    source_depth: int = 0  # 0 = direct search result, 1+ = sublink


@dataclass
class KnowledgePool:
    """Accumulated research findings with compaction support."""

    topic: str
    findings: list[CrawlFinding] = field(default_factory=list)
    summary: str = ""
    total_sources_seen: int = 0
    total_sources_kept: int = 0
    _seen_urls: set = field(default_factory=set)
    _content_hashes: set = field(default_factory=set)

    def add(self, finding: CrawlFinding) -> bool:
        """Add a finding if not a content duplicate. Returns True if added.

        Note: URL dedup is handled separately by _process_url adding to
        _seen_urls before scraping.  This method only checks content-level
        dedup so that pages with different URLs but identical content are
        still filtered.
        """
        content_hash = hashlib.md5(
            finding.content[:500].encode(), usedforsecurity=False
        ).hexdigest()
        if content_hash in self._content_hashes:
            return False
        self._seen_urls.add(finding.url)
        self._content_hashes.add(content_hash)
        self.findings.append(finding)
        self.total_sources_kept += 1
        return True

    def to_context_str(self, max_chars: int = 15000) -> str:
        """Serialise the pool into a context string for the Architect."""
        parts = []
        if self.summary:
            parts.append(f"## Research Summary\n{self.summary}\n")
        parts.append(
            f"Sources examined: {self.total_sources_seen} | "
            f"Sources kept: {self.total_sources_kept} | "
            f"Findings: {len(self.findings)}\n"
        )
        for i, f in enumerate(self.findings, 1):
            entry = f"### Finding {i} (relevance: {f.relevance_score:.1f})\n"
            entry += f"**Source**: {f.url}\n"
            if f.key_points:
                entry += "**Key points**: " + "; ".join(f.key_points) + "\n"
            if f.examples:
                entry += "**Examples**: " + "; ".join(f.examples) + "\n"
            entry += f.content[:500] + "\n"
            parts.append(entry)
        text = "\n".join(parts)
        return text[:max_chars]


# ---------------------------------------------------------------------------
# ResearchCrawler
# ---------------------------------------------------------------------------

class ResearchCrawler:
    """Continuous batch-and-crawl research pipeline.

    Parameters
    ----------
    search_tool : WebSearchTool
        The SearXNG search tool instance.
    scraper_tool : WebScraperTool
        The web scraper tool instance.
    router : ModelRouter
        LLM router for judge calls (relevance assessment, summarisation).
    memory_manager : optional
        Memory manager for persisting findings to ChromaDB.
    batch_size : int
        Number of search results per batch (default 5).
    max_sublink_depth : int
        How many levels of sublinks to follow (default 2).
    max_sublinks_per_page : int
        Max sublinks to follow from a single page (default 3).
    relevance_threshold : float
        Minimum relevance score to keep a finding (default 0.4).
    compact_every : int
        Compact the knowledge pool after this many batches (default 3).
    llm_timeout : float
        Timeout for each LLM call in seconds (default 15).
    url_timeout : float
        Total timeout for processing a single URL (scrape + judge), default 45.
    """

    def __init__(
        self,
        search_tool: Any,
        scraper_tool: Any,
        router: Any = None,
        memory_manager: Any = None,
        batch_size: int = 5,
        max_sublink_depth: int = 2,
        max_sublinks_per_page: int = 3,
        relevance_threshold: float = 0.4,
        compact_every: int = 3,
        llm_timeout: float = 15.0,
        url_timeout: float = 45.0,
    ) -> None:
        self._search = search_tool
        self._scraper = scraper_tool
        self._router = router
        self._memory = memory_manager
        self.batch_size = batch_size
        self.max_sublink_depth = max_sublink_depth
        self.max_sublinks_per_page = max_sublinks_per_page
        self.relevance_threshold = relevance_threshold
        self.compact_every = compact_every
        self._llm_timeout = llm_timeout
        self._url_timeout = url_timeout
        self._llm_semaphore = asyncio.Semaphore(2)

        # State
        self._pools: dict[str, KnowledgePool] = {}
        self._batches_completed: dict[str, int] = {}
        self._search_offset: dict[str, int] = {}
        self._stats = {
            "batches_run": 0,
            "pages_scraped": 0,
            "pages_judged_useful": 0,
            "pages_skipped_timeout": 0,
            "sublinks_followed": 0,
            "compactions_run": 0,
        }

    def get_pool(self, topic: str) -> KnowledgePool:
        """Get or create the knowledge pool for a topic."""
        if topic not in self._pools:
            self._pools[topic] = KnowledgePool(topic=topic)
        return self._pools[topic]

    def get_stats(self) -> dict:
        return {**self._stats}

    # ------------------------------------------------------------------
    # Main entry point: run one batch
    # ------------------------------------------------------------------

    async def run_batch(self, topic: str, query: str | None = None) -> KnowledgePool:
        """Run one batch of the research crawl cycle.

        1. Search for ``query`` (or ``topic`` if query is None)
        2. Scrape and judge each result
        3. Follow sublinks on useful pages
        4. Add findings to the knowledge pool
        5. Compact if due

        Returns the updated KnowledgePool.
        """
        pool = self.get_pool(topic)
        search_query = query or topic
        offset = self._search_offset.get(topic, 0)

        logger.info(
            "ResearchCrawler: batch start for %r (offset=%d, batch_size=%d)",
            topic, offset, self.batch_size,
        )

        # Step 1: Search — use public execute() API
        try:
            search_result = await self._search.execute(
                query=search_query,
                num_results=self.batch_size,
            )
            results = search_result.data if search_result.success else []
            if not search_result.success:
                logger.warning(
                    "ResearchCrawler: search returned error: %s",
                    search_result.error,
                )
        except Exception as exc:
            logger.warning("ResearchCrawler: search failed: %s", exc)
            return pool

        if not isinstance(results, list):
            results = []

        # Advance offset for next batch
        self._search_offset[topic] = offset + self.batch_size
        pool.total_sources_seen += len(results)

        # Step 2–3: Scrape, judge, follow sublinks for each result
        for result in results:
            url = result.get("url", "") if isinstance(result, dict) else ""
            if not url or url in pool._seen_urls:
                continue

            try:
                await asyncio.wait_for(
                    self._process_url(
                        url=url,
                        topic=topic,
                        pool=pool,
                        snippet=result.get("snippet", ""),
                        depth=0,
                    ),
                    timeout=self._url_timeout,
                )
            except TimeoutError:
                logger.debug("ResearchCrawler: timeout processing %s — skipping", url)
                self._stats["pages_skipped_timeout"] += 1
                pool._seen_urls.add(url)  # don't retry

        self._stats["batches_run"] += 1
        self._batches_completed[topic] = self._batches_completed.get(topic, 0) + 1

        # Step 5: Compact if due
        if self._batches_completed[topic] % self.compact_every == 0:
            await self.compact(topic)

        logger.info(
            "ResearchCrawler: batch done for %r — pool has %d findings | %s",
            topic, len(pool.findings), self._stats_summary(),
        )
        return pool

    def _stats_summary(self) -> str:
        """One-line stats for log messages."""
        s = self._stats
        return (
            f"scraped={s['pages_scraped']} useful={s['pages_judged_useful']} "
            f"sublinks={s['sublinks_followed']} timeouts={s['pages_skipped_timeout']}"
        )

    # ------------------------------------------------------------------
    # Process a single URL
    # ------------------------------------------------------------------

    async def _process_url(
        self,
        url: str,
        topic: str,
        pool: KnowledgePool,
        snippet: str = "",
        depth: int = 0,
    ) -> CrawlFinding | None:
        """Scrape, judge, and optionally follow sublinks for a URL."""
        if url in pool._seen_urls:
            return None
        pool._seen_urls.add(url)

        # Scrape the page — use public execute() API
        try:
            scrape_result = await self._scraper.execute(
                url=url,
                include_links=(depth < self.max_sublink_depth),
            )
            if not scrape_result.success:
                logger.debug(
                    "ResearchCrawler: scrape error for %s: %s",
                    url, scrape_result.error,
                )
                return None
            scrape_data = scrape_result.data or {}
        except Exception as exc:
            logger.debug("ResearchCrawler: scrape failed for %s: %s", url, exc)
            return None

        text = scrape_data.get("text", "")
        title = scrape_data.get("title", "")
        self._stats["pages_scraped"] += 1

        if not text or len(text) < 50:
            return None

        # Judge relevance and extract key content
        assessment = await self._judge_relevance(topic, text, title, snippet)
        relevance = assessment.get("relevance_score", 0.0)

        if relevance < self.relevance_threshold:
            logger.debug(
                "ResearchCrawler: skipping %s (relevance=%.2f < %.2f)",
                url, relevance, self.relevance_threshold,
            )
            return None

        self._stats["pages_judged_useful"] += 1

        finding = CrawlFinding(
            url=url,
            title=title,
            content=assessment.get("extracted_content", text[:2000]),
            relevance_score=relevance,
            key_points=assessment.get("key_points", []),
            examples=assessment.get("examples", []),
            source_depth=depth,
        )

        added = pool.add(finding)
        if added:
            await self._archive_finding(topic, finding)

        # Follow sublinks if within depth limit
        if depth < self.max_sublink_depth:
            links = scrape_data.get("links") or []
            sublinks = self._filter_sublinks(links, url, pool)
            for sublink in sublinks[: self.max_sublinks_per_page]:
                self._stats["sublinks_followed"] += 1
                await self._process_url(
                    url=sublink,
                    topic=topic,
                    pool=pool,
                    depth=depth + 1,
                )

        return finding

    # ------------------------------------------------------------------
    # LLM-powered relevance judgement
    # ------------------------------------------------------------------

    async def _judge_relevance(
        self,
        topic: str,
        text: str,
        title: str,
        snippet: str,
    ) -> dict:
        """Use the judge model to assess page relevance and extract key content.

        Returns a dict with:
          - relevance_score: float 0.0–1.0
          - extracted_content: cleaned/summarised useful text
          - key_points: list of key takeaways
          - examples: list of concrete examples found
        """
        if self._router is None:
            # No LLM — simple heuristic: assume moderate relevance
            return {
                "relevance_score": 0.6,
                "extracted_content": text[:2000],
                "key_points": [],
                "examples": [],
            }

        try:
            from core.llm.types import AgentRole

            sample = text[:4000]
            prompt = (
                f"You are evaluating a web page for research on: \"{topic}\"\n\n"
                f"Page title: {title}\n"
                f"Snippet: {snippet}\n\n"
                f"Page content (first 4000 chars):\n{sample}\n\n"
                "Respond with a JSON object:\n"
                "{\n"
                '  "relevance_score": <float 0.0-1.0, how relevant to the topic>,\n'
                '  "extracted_content": "<concise summary of useful information, max 500 words>",\n'
                '  "key_points": ["<point1>", "<point2>", ...],\n'
                '  "examples": ["<any concrete examples, code snippets, or data points>"]\n'
                "}\n"
                "If the page is irrelevant, set relevance_score below 0.3 and "
                "leave other fields minimal."
            )
            async with self._llm_semaphore:
                resp = await asyncio.wait_for(
                    self._router.complete_text(
                        role=AgentRole.CRITIC,
                        prompt=prompt,
                        system="You are a research relevance judge. Return only valid JSON.",
                        temperature=0.2,
                    ),
                    timeout=self._llm_timeout,
                )

            raw = resp.raw_text.strip()
            return self._parse_judge_json(raw, text)
        except TimeoutError:
            logger.warning("ResearchCrawler: judge LLM timed out for %r", title[:60])
        except json.JSONDecodeError as exc:
            logger.warning("ResearchCrawler: judge returned invalid JSON: %s", exc)
        except Exception as exc:
            logger.debug("ResearchCrawler: judge failed: %s", exc)

        # Fallback
        return {
            "relevance_score": 0.5,
            "extracted_content": text[:2000],
            "key_points": [],
            "examples": [],
        }

    @staticmethod
    def _parse_judge_json(raw: str, text: str) -> dict:
        """Extract and parse JSON from the judge model's response.

        Handles cases where the model wraps JSON in markdown fences or
        includes preamble text before/after the JSON object.
        """
        # Strip markdown code fences if present
        cleaned = raw
        if "```" in cleaned:
            # Extract content between first ``` and last ```
            parts = cleaned.split("```")
            if len(parts) >= 3:
                cleaned = parts[1]
                # Strip optional language tag (e.g., ```json)
                if cleaned.startswith("json"):
                    cleaned = cleaned[4:]
                cleaned = cleaned.strip()

        # Find the outermost JSON object
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise json.JSONDecodeError("No JSON object found", raw, 0)

        parsed = json.loads(cleaned[start:end + 1])

        # Validate and clamp relevance_score
        score = parsed.get("relevance_score", 0.5)
        if not isinstance(score, (int, float)):
            score = 0.5
        parsed["relevance_score"] = max(0.0, min(1.0, float(score)))

        # Ensure required fields exist
        if "extracted_content" not in parsed or not parsed["extracted_content"]:
            parsed["extracted_content"] = text[:2000]
        if "key_points" not in parsed or not isinstance(parsed["key_points"], list):
            parsed["key_points"] = []
        if "examples" not in parsed or not isinstance(parsed["examples"], list):
            parsed["examples"] = []

        return parsed

    # ------------------------------------------------------------------
    # Sublink filtering
    # ------------------------------------------------------------------

    def _filter_sublinks(
        self,
        links: list[str],
        source_url: str,
        pool: KnowledgePool,
    ) -> list[str]:
        """Filter sublinks to only follow promising, unseen ones."""
        source_domain = urlparse(source_url).netloc
        filtered = []
        for link in links:
            if link in pool._seen_urls:
                continue
            parsed = urlparse(link)
            if parsed.scheme not in ("http", "https"):
                continue
            path_lower = parsed.path.lower()
            skip_patterns = (
                "/login", "/signup", "/register", "/cart", "/checkout",
                "/privacy", "/terms", "/cookie", "/ads", "/sponsor",
                ".pdf", ".zip", ".tar", ".exe", ".dmg",
                "/wp-admin", "/feed", "/rss",
            )
            if any(pat in path_lower for pat in skip_patterns):
                continue
            link_domain = parsed.netloc
            if link_domain == source_domain:
                filtered.insert(0, link)  # prioritise same-domain
            else:
                filtered.append(link)
        return filtered

    # ------------------------------------------------------------------
    # Knowledge pool compaction
    # ------------------------------------------------------------------

    async def compact(self, topic: str) -> None:
        """Compact the knowledge pool: summarise, deduplicate, prune.

        Preserves examples and high-relevance findings. Removes redundant
        or low-value entries. Generates an updated summary.
        """
        pool = self.get_pool(topic)
        if len(pool.findings) < 3:
            return

        logger.info(
            "ResearchCrawler: compacting pool for %r (%d findings)",
            topic, len(pool.findings),
        )

        # Sort by relevance — keep the best, prune the worst
        pool.findings.sort(key=lambda f: f.relevance_score, reverse=True)

        # Deduplicate by content similarity (simple hash-based)
        seen_hashes: set[str] = set()
        deduped: list[CrawlFinding] = []
        for finding in pool.findings:
            h = hashlib.md5(
                finding.content[:300].encode(), usedforsecurity=False
            ).hexdigest()
            if h not in seen_hashes:
                seen_hashes.add(h)
                deduped.append(finding)

        pool.findings = deduped

        # Generate a new summary via LLM
        if self._router is not None:
            pool.summary = await self._generate_summary(topic, pool)

        self._stats["compactions_run"] += 1
        logger.info(
            "ResearchCrawler: compaction done — %d findings remain",
            len(pool.findings),
        )

    async def _generate_summary(self, topic: str, pool: KnowledgePool) -> str:
        """Generate a concise summary of the knowledge pool."""
        try:
            from core.llm.types import AgentRole

            digest_parts = []
            for f in pool.findings[:20]:
                entry = f"- [{f.relevance_score:.1f}] {f.title}: "
                if f.key_points:
                    entry += "; ".join(f.key_points[:3])
                else:
                    entry += f.content[:200]
                if f.examples:
                    entry += f" | Examples: {', '.join(f.examples[:2])}"
                digest_parts.append(entry)
            digest = "\n".join(digest_parts)

            prompt = (
                f"Research topic: \"{topic}\"\n\n"
                f"Sources examined: {pool.total_sources_seen}\n"
                f"Sources kept: {pool.total_sources_kept}\n\n"
                f"Findings digest:\n{digest}\n\n"
                "Write a concise research summary (max 500 words) that:\n"
                "1. Identifies the main themes and conclusions\n"
                "2. Notes areas of agreement and contradiction between sources\n"
                "3. Preserves specific examples and data points\n"
                "4. Highlights gaps that need more research\n"
                "5. Does NOT repeat information — synthesise, don't enumerate"
            )
            async with self._llm_semaphore:
                resp = await asyncio.wait_for(
                    self._router.complete_text(
                        role=AgentRole.CRITIC,
                        prompt=prompt,
                        system=(
                            "You are a research summariser. Be concise but thorough. "
                            "Preserve all concrete examples and data points."
                        ),
                        temperature=0.3,
                    ),
                    timeout=self._llm_timeout * 2,
                )
            summary = resp.raw_text.strip()
            if summary and len(summary) > 50:
                return summary
        except Exception as exc:
            logger.warning("ResearchCrawler: summary generation failed: %s", exc)

        return pool.summary  # keep existing summary on failure

    # ------------------------------------------------------------------
    # Memory archival
    # ------------------------------------------------------------------

    async def _archive_finding(self, topic: str, finding: CrawlFinding) -> None:
        """Archive a finding to ChromaDB for cross-session reuse."""
        if self._memory is None or not hasattr(self._memory, "store_research"):
            return
        try:
            await asyncio.wait_for(
                self._memory.store_research(
                    content=finding.content,
                    topic=topic,
                    tags=[
                        "research-crawler",
                        f"depth-{finding.source_depth}",
                        f"relevance-{finding.relevance_score:.1f}",
                    ],
                    source=finding.url,
                    metadata={
                        "title": finding.title,
                        "key_points": finding.key_points,
                        "examples": finding.examples,
                        "relevance_score": finding.relevance_score,
                        "source_depth": finding.source_depth,
                    },
                ),
                timeout=10.0,
            )
        except Exception as exc:
            logger.warning(
                "ResearchCrawler: archive failed for %s: %s", finding.url, exc
            )

    # ------------------------------------------------------------------
    # Run continuously (for background usage)
    # ------------------------------------------------------------------

    async def run_continuous(
        self,
        topic: str,
        query: str | None = None,
        max_batches: int = 0,
        max_runtime_seconds: float = 0,
        pause_seconds: float = 5.0,
        on_batch_complete: Any = None,
    ) -> KnowledgePool:
        """Run the crawl loop indefinitely (or up to max_batches/max_runtime).

        Parameters
        ----------
        topic : str
            The research topic.
        query : str | None
            Initial search query (defaults to topic).
        max_batches : int
            Stop after this many batches (0 = no batch limit).
        max_runtime_seconds : float
            Stop after this many seconds (0 = no time limit).
            At least one of max_batches or max_runtime_seconds should be
            set to prevent truly unbounded execution.
        pause_seconds : float
            Pause between batches to avoid hammering search engines.
        on_batch_complete : callable | None
            Optional callback ``fn(pool, batch_num)`` called after each batch.

        Returns
        -------
        The final KnowledgePool when the loop exits.
        """
        batch_num = 0
        start_time = time.monotonic()

        while True:
            # Check batch limit
            if max_batches > 0 and batch_num >= max_batches:
                break
            # Check runtime limit
            if max_runtime_seconds > 0:
                elapsed = time.monotonic() - start_time
                if elapsed >= max_runtime_seconds:
                    logger.info(
                        "ResearchCrawler: runtime limit reached (%.0fs)",
                        elapsed,
                    )
                    break

            pool = await self.run_batch(topic, query=query)
            batch_num += 1

            if on_batch_complete is not None:
                try:
                    result = on_batch_complete(pool, batch_num)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:
                    logger.debug("on_batch_complete callback failed: %s", exc)

            # Refine query based on knowledge gaps found so far
            if self._router is not None and pool.findings:
                refined = await self._suggest_next_query(topic, pool)
                if refined:
                    query = refined

            await asyncio.sleep(pause_seconds)

        return self.get_pool(topic)

    async def _suggest_next_query(self, topic: str, pool: KnowledgePool) -> str | None:
        """Use the judge to suggest what to search for next."""
        if self._router is None:
            return None
        try:
            from core.llm.types import AgentRole

            summary = pool.summary or "No summary yet."
            key_points = []
            for f in pool.findings[-5:]:
                key_points.extend(f.key_points[:2])

            prompt = (
                f"Research topic: \"{topic}\"\n\n"
                f"Current summary: {summary[:1000]}\n\n"
                f"Recent key points: {', '.join(key_points[:10])}\n\n"
                "Based on what we've found so far, suggest ONE search query "
                "that would fill the biggest remaining gap in our knowledge. "
                "Return ONLY the search query, nothing else."
            )
            async with self._llm_semaphore:
                resp = await asyncio.wait_for(
                    self._router.complete_text(
                        role=AgentRole.CRITIC,
                        prompt=prompt,
                        system="Suggest the next search query. Output only the query.",
                        temperature=0.4,
                    ),
                    timeout=self._llm_timeout,
                )
            query = resp.raw_text.strip()
            if query and len(query) > 5 and len(query) < 200:
                logger.info("ResearchCrawler: next query for %r: %r", topic, query)
                return query
        except Exception as exc:
            logger.debug("ResearchCrawler: query suggestion failed: %s", exc)
        return None
