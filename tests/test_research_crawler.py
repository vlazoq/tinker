"""
tests/test_research_crawler.py
===============================
Tests for the continuous research crawler pipeline.

Covers:
  - KnowledgePool add/dedup/context serialisation
  - ResearchCrawler batch execution with mocked tools
  - Sublink filtering logic
  - Relevance judgement fallback (no LLM)
  - Knowledge pool compaction
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.tools.research_crawler import CrawlFinding, KnowledgePool, ResearchCrawler


# ---------------------------------------------------------------------------
# KnowledgePool tests
# ---------------------------------------------------------------------------


class TestKnowledgePool:
    def test_add_finding(self):
        pool = KnowledgePool(topic="test")
        f = CrawlFinding(url="http://a.com", title="A", content="content A", relevance_score=0.8)
        assert pool.add(f) is True
        assert len(pool.findings) == 1
        assert pool.total_sources_kept == 1

    def test_dedup_same_url_different_content(self):
        """URL dedup is handled at process_url level; add() allows same URL
        if content differs (shouldn't normally happen, but add() focuses on
        content dedup)."""
        pool = KnowledgePool(topic="test")
        f1 = CrawlFinding(url="http://a.com", title="A", content="content A unique", relevance_score=0.8)
        f2 = CrawlFinding(url="http://a.com", title="A copy", content="different unique content", relevance_score=0.9)
        assert pool.add(f1) is True
        assert pool.add(f2) is True  # different content hash → allowed
        assert len(pool.findings) == 2

    def test_dedup_same_content(self):
        pool = KnowledgePool(topic="test")
        f1 = CrawlFinding(url="http://a.com", title="A", content="identical content here", relevance_score=0.8)
        f2 = CrawlFinding(url="http://b.com", title="B", content="identical content here", relevance_score=0.7)
        assert pool.add(f1) is True
        assert pool.add(f2) is False

    def test_to_context_str(self):
        pool = KnowledgePool(topic="test")
        pool.summary = "This is the summary."
        pool.total_sources_seen = 10
        pool.total_sources_kept = 3
        f = CrawlFinding(
            url="http://a.com",
            title="A",
            content="Important findings about X.",
            relevance_score=0.9,
            key_points=["point 1", "point 2"],
            examples=["example A"],
        )
        pool.add(f)
        ctx = pool.to_context_str()
        assert "Research Summary" in ctx
        assert "This is the summary" in ctx
        assert "Sources examined: 10" in ctx
        assert "point 1" in ctx
        assert "example A" in ctx

    def test_to_context_str_max_chars(self):
        pool = KnowledgePool(topic="test")
        for i in range(50):
            pool.add(CrawlFinding(
                url=f"http://site{i}.com",
                title=f"Site {i}",
                content="x" * 500,
                relevance_score=0.5,
            ))
        ctx = pool.to_context_str(max_chars=1000)
        assert len(ctx) <= 1000


# ---------------------------------------------------------------------------
# ResearchCrawler tests (with mocked tools)
# ---------------------------------------------------------------------------


class _ToolResult:
    """Minimal stand-in for a tool result with .success / .data / .error."""
    def __init__(self, data, success=True, error=None):
        self.data = data
        self.success = success
        self.error = error


def _make_search_tool(results=None):
    """Create a mock search tool."""
    tool = MagicMock()
    if results is None:
        results = [
            {"title": "Result 1", "url": "http://example.com/1", "snippet": "Snippet 1"},
            {"title": "Result 2", "url": "http://example.com/2", "snippet": "Snippet 2"},
        ]
    tool.execute = AsyncMock(return_value=_ToolResult(data=results))
    return tool


def _make_scraper_tool(text="This is useful research content about the topic. " * 10, links=None):
    """Create a mock scraper tool that returns text for any URL."""
    tool = MagicMock()

    async def _scrape(url, include_links=False, **kwargs):
        return _ToolResult(data={
            "url": url,
            "title": f"Page at {url}",
            "text": text,
            "word_count": len(text.split()),
            "links": links or [],
        })

    tool.execute = AsyncMock(side_effect=_scrape)
    return tool


class TestResearchCrawler:
    def _make_crawler(self, search_results=None, scrape_text=None, links=None, unique_content=True, **kwargs):
        search = _make_search_tool(search_results)
        if unique_content and scrape_text is None:
            # Make scraper return unique content per URL
            _call_count = {"n": 0}

            async def _scrape_unique(url, include_links=False, **kw):
                _call_count["n"] += 1
                return _ToolResult(data={
                    "url": url,
                    "title": f"Page at {url}",
                    "text": f"Unique research content about topic number {_call_count['n']}. " * 20,
                    "word_count": 100,
                    "links": links or [],
                })

            scraper = MagicMock()
            scraper.execute = AsyncMock(side_effect=_scrape_unique)
        else:
            scraper = _make_scraper_tool(text=scrape_text or "Useful content " * 20, links=links)
        return ResearchCrawler(
            search_tool=search,
            scraper_tool=scraper,
            router=None,  # no LLM — uses heuristic fallback
            memory_manager=None,
            batch_size=2,
            max_sublink_depth=1,
            relevance_threshold=0.3,
            compact_every=5,
            **kwargs,
        )

    @pytest.mark.asyncio
    async def test_run_batch_basic(self):
        crawler = self._make_crawler()
        pool = await crawler.run_batch("machine learning")
        assert isinstance(pool, KnowledgePool)
        assert pool.topic == "machine learning"
        assert pool.total_sources_seen == 2
        assert len(pool.findings) > 0
        assert crawler.get_stats()["batches_run"] == 1

    @pytest.mark.asyncio
    async def test_run_batch_deduplicates(self):
        crawler = self._make_crawler()
        pool1 = await crawler.run_batch("test topic")
        pool2 = await crawler.run_batch("test topic")
        # Second batch should not re-add the same URLs
        assert pool1 is pool2  # same pool object
        assert pool2.total_sources_seen == 4  # saw 2+2

    @pytest.mark.asyncio
    async def test_run_batch_search_failure(self):
        crawler = self._make_crawler()
        crawler._search.execute = AsyncMock(side_effect=Exception("search down"))
        pool = await crawler.run_batch("failing topic")
        assert len(pool.findings) == 0

    @pytest.mark.asyncio
    async def test_short_content_skipped(self):
        crawler = self._make_crawler(scrape_text="too short")
        pool = await crawler.run_batch("test")
        assert len(pool.findings) == 0  # content too short to keep

    @pytest.mark.asyncio
    async def test_sublinks_followed(self):
        crawler = self._make_crawler(
            links=["http://example.com/sub1", "http://example.com/sub2"],
        )
        pool = await crawler.run_batch("test")
        assert crawler.get_stats()["sublinks_followed"] > 0

    @pytest.mark.asyncio
    async def test_get_pool_creates_if_missing(self):
        crawler = self._make_crawler()
        pool = crawler.get_pool("new topic")
        assert pool.topic == "new topic"
        assert len(pool.findings) == 0


class TestSublinkFiltering:
    def test_filters_non_content_urls(self):
        crawler = ResearchCrawler(
            search_tool=MagicMock(),
            scraper_tool=MagicMock(),
        )
        pool = KnowledgePool(topic="test")
        links = [
            "http://example.com/article",
            "http://example.com/login",
            "http://example.com/privacy",
            "http://example.com/good-page",
            "http://example.com/file.pdf",
            "ftp://example.com/data",
        ]
        filtered = crawler._filter_sublinks(links, "http://example.com/source", pool)
        urls = set(filtered)
        assert "http://example.com/article" in urls
        assert "http://example.com/good-page" in urls
        assert "http://example.com/login" not in urls
        assert "http://example.com/privacy" not in urls
        assert "http://example.com/file.pdf" not in urls
        assert "ftp://example.com/data" not in urls

    def test_skips_already_seen_urls(self):
        crawler = ResearchCrawler(
            search_tool=MagicMock(),
            scraper_tool=MagicMock(),
        )
        pool = KnowledgePool(topic="test")
        pool._seen_urls.add("http://example.com/already-seen")
        links = ["http://example.com/already-seen", "http://example.com/new"]
        filtered = crawler._filter_sublinks(links, "http://example.com/source", pool)
        assert len(filtered) == 1
        assert filtered[0] == "http://example.com/new"

    def test_prioritises_same_domain(self):
        crawler = ResearchCrawler(
            search_tool=MagicMock(),
            scraper_tool=MagicMock(),
        )
        pool = KnowledgePool(topic="test")
        links = [
            "http://other.com/page",
            "http://example.com/same-domain",
        ]
        filtered = crawler._filter_sublinks(links, "http://example.com/source", pool)
        # Same-domain should come first
        assert filtered[0] == "http://example.com/same-domain"


class TestRelevanceJudgement:
    @pytest.mark.asyncio
    async def test_heuristic_fallback_no_router(self):
        crawler = ResearchCrawler(
            search_tool=MagicMock(),
            scraper_tool=MagicMock(),
            router=None,
        )
        result = await crawler._judge_relevance("topic", "some text", "title", "snippet")
        assert result["relevance_score"] == 0.6
        assert "some text" in result["extracted_content"]


class TestCompaction:
    @pytest.mark.asyncio
    async def test_compact_deduplicates(self):
        crawler = ResearchCrawler(
            search_tool=MagicMock(),
            scraper_tool=MagicMock(),
            router=None,
        )
        pool = crawler.get_pool("test")
        # Add findings with different URLs but same content prefix
        for i in range(5):
            pool.findings.append(CrawlFinding(
                url=f"http://site{i}.com",
                title=f"Site {i}",
                content=f"unique content {i}" + " padding" * 50,
                relevance_score=0.5 + i * 0.1,
            ))
        await crawler.compact("test")
        # All should survive since content is unique
        assert len(pool.findings) == 5
        # Should be sorted by relevance (highest first)
        assert pool.findings[0].relevance_score >= pool.findings[-1].relevance_score

    @pytest.mark.asyncio
    async def test_compact_removes_true_duplicates(self):
        crawler = ResearchCrawler(
            search_tool=MagicMock(),
            scraper_tool=MagicMock(),
            router=None,
        )
        pool = crawler.get_pool("dedup-test")
        for i in range(4):
            pool.findings.append(CrawlFinding(
                url=f"http://site{i}.com",
                title=f"Site {i}",
                content="exactly the same content everywhere",
                relevance_score=0.5 + i * 0.1,
            ))
        await crawler.compact("dedup-test")
        assert len(pool.findings) == 1  # all duplicates removed

    @pytest.mark.asyncio
    async def test_compact_skips_small_pool(self):
        crawler = ResearchCrawler(
            search_tool=MagicMock(),
            scraper_tool=MagicMock(),
            router=None,
        )
        pool = crawler.get_pool("small")
        pool.findings.append(CrawlFinding(
            url="http://a.com", title="A", content="x", relevance_score=0.5
        ))
        await crawler.compact("small")
        assert len(pool.findings) == 1  # no change — too few to compact
