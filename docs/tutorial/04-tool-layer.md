# Chapter 04 — The Tool Layer

## The Problem

The Architect AI only knows what it was trained on.  It cannot:
- Search the web for the latest best practices
- Read the content of a specific article or documentation page
- Write structured output files to disk

We need to give it tools.

---

## The Architecture Decision

We build a `ToolLayer` class that holds individual tools.  The Architect
can request a tool call by returning a structured JSON response.  The
orchestrator sees the request, executes the tool, and feeds the result
back to the Architect.

```
Architect AI:
  "I need to research consistent hashing.
   TOOL_CALL: web_search("consistent hashing distributed systems")"

Orchestrator:
  1. Sees TOOL_CALL in the response
  2. Calls tool_layer.web_search("consistent hashing distributed systems")
  3. Gets back 5 search results
  4. Adds results to the Architect's next prompt

Architect AI:
  "Based on those results, consistent hashing works by..."
```

We build three tools:
1. `WebSearchTool` — queries SearXNG for search results
2. `WebScraperTool` — fetches and cleans a web page's text
3. `ArtifactWriterTool` — writes design documents to disk

---

## Step 1 — Directory Structure

```
tinker/
  tools/
    __init__.py
    search.py
    scraper.py
    writer.py
    layer.py    ← the ToolLayer that combines all tools
```

---

## Step 2 — Web Search Tool

SearXNG is a self-hosted search engine that queries Google, DuckDuckGo,
and Bing on your behalf.  We query its JSON API:

```python
# tinker/tools/search.py

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class SearchResult:
    """One web search result."""
    def __init__(self, title: str, url: str, snippet: str) -> None:
        self.title   = title
        self.url     = url
        self.snippet = snippet

    def to_text(self) -> str:
        return f"**{self.title}**\n{self.url}\n{self.snippet}"


class WebSearchTool:
    """
    Queries a SearXNG instance for web search results.

    SearXNG exposes a JSON API at /search?format=json&q=<query>.
    We parse the results and return the top N.
    """

    def __init__(self, searxng_url: str, timeout: float = 15.0) -> None:
        self.searxng_url = searxng_url.rstrip("/")
        self.timeout = timeout

    async def search(self, query: str, n_results: int = 5) -> list[SearchResult]:
        """
        Search the web and return up to n_results results.
        Returns an empty list if SearXNG is unreachable.
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{self.searxng_url}/search",
                    params={"q": query, "format": "json", "language": "en"},
                )
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            logger.warning("Web search failed for query=%r: %s", query, exc)
            return []

        results = []
        for item in data.get("results", [])[:n_results]:
            results.append(SearchResult(
                title   = item.get("title",   ""),
                url     = item.get("url",     ""),
                snippet = item.get("content", item.get("snippet", "")),
            ))
        return results

    async def search_as_text(self, query: str, n_results: int = 5) -> str:
        """Return search results as a formatted string, ready to inject into a prompt."""
        results = await self.search(query, n_results)
        if not results:
            return f"No web results found for: {query}"
        lines = [f"Web search results for: {query}\n"]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r.to_text()}\n")
        return "\n".join(lines)
```

---

## Step 3 — Web Scraper Tool

```python
# tinker/tools/scraper.py

from __future__ import annotations

import logging
import re

import httpx

logger = logging.getLogger(__name__)


def _strip_html(html: str) -> str:
    """
    Very basic HTML → plain text conversion.
    Removes tags, collapses whitespace.
    For production use, consider 'html2text' or 'markdownify'.
    """
    # Remove script and style blocks entirely
    html = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", "", html,
                  flags=re.DOTALL | re.IGNORECASE)
    # Remove all remaining HTML tags
    text = re.sub(r"<[^>]+>", " ", html)
    # Collapse multiple spaces and newlines
    text = re.sub(r"\s+", " ", text).strip()
    return text


class WebScraperTool:
    """
    Fetches the plain-text content of a web page.

    Used when the Architect wants to read the full content of a URL
    it found via web search.
    """

    def __init__(self, timeout: float = 20.0, max_chars: int = 8000) -> None:
        self.timeout  = timeout
        self.max_chars = max_chars   # truncate to avoid filling the context window

    async def scrape(self, url: str) -> str:
        """
        Fetch and return the plain text of a URL.
        Returns an error string if the fetch fails.
        """
        try:
            headers = {
                # Identify ourselves politely
                "User-Agent": "Tinker/1.0 (research bot; +https://github.com/tinker)"
            }
            async with httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                headers=headers,
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                html = response.text
        except Exception as exc:
            logger.warning("Scrape failed for %s: %s", url, exc)
            return f"[Failed to fetch {url}: {exc}]"

        text = _strip_html(html)

        # Truncate to max_chars to avoid overwhelming the context window
        if len(text) > self.max_chars:
            text = text[:self.max_chars] + f"\n... [truncated at {self.max_chars} chars]"

        return text
```

---

## Step 4 — Artifact Writer Tool

```python
# tinker/tools/writer.py

from __future__ import annotations

import logging
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class ArtifactWriterTool:
    """
    Writes design artifacts (markdown documents) to the filesystem.

    Each artifact is saved as a timestamped markdown file.
    The web UI can later read and display these.
    """

    def __init__(self, output_dir: str | Path) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        # mkdir creates the directory (and any parent dirs) if it doesn't exist
        # exist_ok=True means don't error if it already exists

    async def write(
        self,
        artifact_id: str,
        subsystem: str,
        content: str,
        artifact_type: str = "design",
    ) -> Path:
        """
        Write content to a markdown file.
        Returns the path of the file that was written.
        """
        # Build a readable filename: subsystem_type_20240115_123456.md
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"{subsystem}_{artifact_type}_{ts}.md"
        path = self.output_dir / filename

        header = f"""# {subsystem.replace('_', ' ').title()} — {artifact_type.title()}

**Artifact ID:** `{artifact_id}`
**Generated:** {datetime.now(timezone.utc).isoformat()}
**Type:** {artifact_type}

---

"""
        path.write_text(header + content, encoding="utf-8")
        logger.debug("Artifact written to %s", path)
        return path
```

---

## Step 5 — The Tool Layer (Combining Everything)

```python
# tinker/tools/layer.py

from __future__ import annotations

import logging

from .search  import WebSearchTool
from .scraper import WebScraperTool
from .writer  import ArtifactWriterTool

logger = logging.getLogger(__name__)


class ToolLayer:
    """
    The single tool-access object the orchestrator receives.

    Usage:
        tools = ToolLayer(search=..., scraper=..., writer=...)
        results = await tools.web_search("consistent hashing")
        content = await tools.scrape_url("https://example.com/article")
    """

    def __init__(
        self,
        search:  WebSearchTool,
        scraper: WebScraperTool,
        writer:  ArtifactWriterTool,
    ) -> None:
        self._search  = search
        self._scraper = scraper
        self._writer  = writer

    async def web_search(self, query: str, n: int = 5) -> str:
        """Search the web. Returns formatted text ready for a prompt."""
        return await self._search.search_as_text(query, n)

    async def scrape_url(self, url: str) -> str:
        """Fetch the plain text content of a URL."""
        return await self._scraper.scrape(url)

    async def write_artifact(
        self,
        artifact_id: str,
        subsystem: str,
        content: str,
        artifact_type: str = "design",
    ):
        """Write a design artifact to disk."""
        return await self._writer.write(artifact_id, subsystem, content, artifact_type)
```

---

## Step 6 — Try It

```python
# test_tools.py
import asyncio
from tools.search  import WebSearchTool
from tools.scraper import WebScraperTool
from tools.writer  import ArtifactWriterTool
from tools.layer   import ToolLayer

async def main():
    tools = ToolLayer(
        search  = WebSearchTool(searxng_url="http://localhost:8888"),
        scraper = WebScraperTool(),
        writer  = ArtifactWriterTool(output_dir="./test_artifacts"),
    )

    # Test search (requires docker compose up -d)
    print("Searching...")
    results = await tools.web_search("redis consistent hashing", n=3)
    print(results[:300])

    # Test artifact writing
    path = await tools.write_artifact(
        artifact_id="test-001",
        subsystem="cache_layer",
        content="Use consistent hashing to distribute cache keys across nodes.",
        artifact_type="design",
    )
    print(f"Written to: {path}")

asyncio.run(main())
```

---

## Integration Check

At this point we have:

```
tinker/
  llm/         ✅  model client + router
  memory/      ✅  four adapters + unified manager
  tools/       ✅  search + scraper + writer + layer
```

These three systems don't know about each other yet — the orchestrator
(Chapter 08) will wire them together.  But we can already imagine the
flow:

```
micro loop:
  1. tools.web_search(query)          → search results string
  2. memory.get_working(sid, "context") → previous context
  3. llm.complete(assembled_prompt)   → AI response text
  4. memory.store_artifact(content)   → DuckDB + Chroma
```

---

## Key Concepts Introduced

| Concept | Where | Why |
|---------|-------|-----|
| Tool results as plain text | search.py | Strings are easy to inject into prompts |
| Graceful error strings | scraper.py | Failed fetch returns `"[Failed to fetch ...]"` rather than raising |
| `Path.mkdir(parents=True)` | writer.py | Create nested directories safely |
| Composing small classes | layer.py | ToolLayer combines three focused classes |

---

→ Next: [Chapter 05 — Agent Prompts](./05-agent-prompts.md)
