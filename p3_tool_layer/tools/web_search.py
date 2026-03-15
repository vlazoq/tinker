"""
Web Search Tool
Queries a self-hosted SearXNG instance and returns structured results.
"""

from __future__ import annotations

import os
from typing import Any

from .base import BaseTool, ToolSchema


# Honour both the Tinker-prefixed name (canonical) and the bare name (legacy)
SEARXNG_URL = os.getenv("TINKER_SEARXNG_URL") or os.getenv("SEARXNG_URL", "http://localhost:8080")


class WebSearchTool(BaseTool):
    """Search the web via a local SearXNG instance."""

    def __init__(
        self,
        searxng_url: str = SEARXNG_URL,
        default_results: int = 10,
        timeout: float = 15.0,
    ) -> None:
        self._url = searxng_url.rstrip("/")
        self._default_results = default_results
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    @property
    def schema(self) -> ToolSchema:
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
                "required": ["query"],
            },
            returns=(
                "List of dicts: [{title, url, snippet, engine, score}] "
                "sorted by relevance."
            ),
        )

    # ------------------------------------------------------------------
    # Implementation
    # ------------------------------------------------------------------

    async def _execute(                     # type: ignore[override]
        self,
        query: str,
        num_results: int = 10,
        categories: list[str] | None = None,
        language: str = "en",
        **_: Any,
    ) -> list[dict]:
        if categories is None:
            categories = ["general"]

        payload = {
            "q": query,
            "format": "json",
            "language": language,
            "categories": ",".join(categories),
        }

        import httpx
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(f"{self._url}/search", data=payload)
            resp.raise_for_status()
            raw = resp.json()

        results = raw.get("results", [])
        trimmed = []
        for r in results[:num_results]:
            trimmed.append(
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get("content", ""),
                    "engine": r.get("engine", ""),
                    "score": round(r.get("score", 0.0), 4),
                }
            )
        return trimmed
