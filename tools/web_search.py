"""
Web Search Tool — tools/web_search.py
=======================================

What this file does
--------------------
This file defines ``WebSearchTool``, which lets Tinker search the web.

Instead of sending searches to Google or Bing (which would expose user data to
third-party servers and may have rate limits or costs), Tinker uses a
self-hosted search engine called **SearXNG**.  SearXNG is an open-source
"meta search engine" — it queries multiple search engines on your behalf and
aggregates the results, but all traffic stays on your own infrastructure.

When the Researcher agent wants to look something up, it calls this tool with a
search query and gets back a list of results: title, URL, and a short snippet
for each result.

Why it exists
-------------
Architecture research requires up-to-date information from the web.  The AI
model's training data has a cutoff date and doesn't know about the latest
libraries, patterns, or blog posts.  This tool bridges that gap.

How it fits into Tinker
-----------------------
The Orchestrator registers this tool as "web_search" in the ToolRegistry.
The Researcher agent (and the ``registry.research()`` convenience method) calls
it with a query string.  The returned results are fed back into the AI's
context so it can cite sources and build on real information.

The SearXNG URL is configured via the environment variable TINKER_SEARXNG_URL
(preferred) or SEARXNG_URL (legacy fallback).  If neither is set, it defaults
to http://localhost:8080, which is where SearXNG runs when started locally via
Docker Compose.
"""

from __future__ import annotations

import os
from typing import Any

from .base import BaseTool, ToolSchema


# Read the SearXNG URL from the environment at module load time.
# We check TINKER_SEARXNG_URL first (the canonical name with the "TINKER_" prefix),
# then fall back to the bare SEARXNG_URL for backwards compatibility with older configs.
# The "or" chains the two os.getenv calls: if the first returns None, try the second.
# Honour both the Tinker-prefixed name (canonical) and the bare name (legacy)
SEARXNG_URL = os.getenv("TINKER_SEARXNG_URL") or os.getenv(
    "SEARXNG_URL", "http://localhost:8080"
)


class WebSearchTool(BaseTool):
    """
    Search the web via a locally-hosted SearXNG instance.

    This tool sends POST requests to the SearXNG JSON API and returns a
    cleaned, trimmed list of search results.  It does NOT scrape the full
    text of any page — for that, see ``WebScraperTool``.

    SearXNG categories
    ------------------
    SearXNG supports filtering results by category.  The most relevant ones
    for architecture research are:
      - "general"   — broad web search (default)
      - "it"        — technology and programming sources
      - "science"   — academic and scientific sources

    Concurrency note
    ----------------
    This tool is fully async.  It uses ``httpx.AsyncClient`` for the HTTP
    request, so it doesn't block other tools or agents while waiting for the
    search engine to respond.
    """

    def __init__(
        self,
        searxng_url: str = SEARXNG_URL,
        default_results: int = 10,
        timeout: float = 15.0,
    ) -> None:
        """
        Initialise the web search tool.

        Args:
            searxng_url:
                Full URL of the SearXNG instance to query.
                Trailing slashes are stripped so we can safely append "/search".

            default_results:
                How many results to return if the caller doesn't specify.
                Not currently used to override the caller's num_results argument,
                but stored here for potential future use.

            timeout:
                How many seconds to wait for SearXNG to respond before giving up.
                15 seconds is generous — local SearXNG usually responds in < 2s.
        """
        # Strip trailing slashes so we can safely do f"{self._url}/search"
        # without accidentally creating double slashes (http://host:8080//search).
        self._url = searxng_url.rstrip("/")
        self._default_results = default_results
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    @property
    def schema(self) -> ToolSchema:
        """
        Describe this tool to the ToolRegistry and the AI model.

        The ``parameters`` dict is a JSON Schema object.  The AI model reads
        this when deciding how to call the tool — it knows "query is required,
        num_results is optional and defaults to 10", etc.
        """
        return ToolSchema(
            name="web_search",
            description=(
                "Search the web for information using a private SearXNG instance. "
                "Returns a ranked list of results with title, URL, and snippet."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query string.",
                    },
                    "num_results": {
                        "type": "integer",
                        "description": "Number of results to return (1-20). Default 10.",
                        "default": 10,
                        "minimum": 1,
                        "maximum": 20,
                    },
                    "categories": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "SearXNG categories, e.g. ['general', 'it', 'science'].",
                        "default": ["general"],
                    },
                    "language": {
                        "type": "string",
                        "description": "Language code, e.g. 'en'. Default 'en'.",
                        "default": "en",
                    },
                },
                "required": [
                    "query"
                ],  # only "query" is mandatory; the rest have defaults
            },
            returns=(
                "List of dicts: [{title, url, snippet, engine, score}] "
                "sorted by relevance."
            ),
        )

    # ------------------------------------------------------------------
    # Implementation
    # ------------------------------------------------------------------

    async def _execute(  # type: ignore[override]
        self,
        query: str,
        num_results: int = 10,
        categories: list[str] | None = None,
        language: str = "en",
        **_: Any,  # absorb any extra kwargs the caller passes; we don't use them
    ) -> list[dict]:
        """
        Execute the web search and return a list of result dicts.

        This method:
          1. Builds the POST payload SearXNG expects.
          2. Sends the request using httpx (async HTTP client).
          3. Parses the JSON response.
          4. Trims the results to ``num_results`` and normalises field names.

        Why ``**_`` at the end?
        -----------------------
        The ToolRegistry passes all arguments from the model's tool call as
        keyword arguments.  If the model passes an unexpected extra field (like
        "max_results"), we want to silently ignore it rather than raise a
        TypeError.  The ``**_`` catch-all absorbs those extras.

        Args:
            query:       The search query string.
            num_results: Maximum number of results to return (1-20).
            categories:  SearXNG search categories (default: ["general"]).
            language:    Language preference code (default: "en").

        Returns:
            A list of dicts, each with keys: title, url, snippet, engine, score.
            The list is already sorted by relevance (SearXNG does the ranking).
        """
        # Default to "general" category if the caller didn't specify any.
        if categories is None:
            categories = ["general"]

        # Build the form data that SearXNG's /search endpoint expects.
        # SearXNG uses a POST request with form-encoded data (not JSON body).
        payload = {
            "q": query,  # "q" is the standard search query parameter name
            "format": "json",  # ask SearXNG to return JSON instead of HTML
            "language": language,
            # SearXNG expects categories as a comma-separated string, e.g. "general,it"
            "categories": ",".join(categories),
        }

        # Import httpx here (inside the function) rather than at the top of the file.
        # This is "lazy importing" — if httpx isn't installed, this tool fails with
        # a clear ImportError when called, rather than breaking all of tools/ at import time.
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            # POST to the /search endpoint with form data.
            resp = await client.post(f"{self._url}/search", data=payload)
            # raise_for_status() turns 4xx/5xx HTTP responses into exceptions,
            # which BaseTool.execute() will catch and wrap into a ToolResult error.
            resp.raise_for_status()
            raw = resp.json()  # parse the response body as JSON

        # SearXNG returns a dict with a "results" key containing the list of results.
        results = raw.get("results", [])

        # Trim to the requested number of results and normalise field names.
        # SearXNG uses "content" for the snippet, but we rename it to "snippet"
        # for consistency with what the AI and the rest of Tinker expect.
        trimmed = []
        for r in results[:num_results]:
            trimmed.append(
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get(
                        "content", ""
                    ),  # SearXNG calls it "content"; we call it "snippet"
                    "engine": r.get(
                        "engine", ""
                    ),  # which underlying search engine returned this
                    "score": round(
                        r.get("score", 0.0), 4
                    ),  # relevance score, rounded for readability
                }
            )
        return trimmed
