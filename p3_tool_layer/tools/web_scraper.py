"""
Web Scraper Tool
Fetches a URL with Playwright (JavaScript-rendered pages), then extracts
clean article text via trafilatura.  Falls back to raw httpx if Playwright
is unavailable.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

from .base import BaseTool, ToolSchema

# Playwright is optional — fall back gracefully if not installed.
try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False
    PWTimeout = Exception  # type: ignore[misc,assignment]

# trafilatura / httpx are imported lazily so the module loads without them
try:
    import trafilatura as _trafilatura
except ImportError:
    _trafilatura = None  # type: ignore[assignment]

DEFAULT_TIMEOUT_MS = int(os.getenv("SCRAPER_TIMEOUT_MS", "20000"))   # 20 s


class WebScraperTool(BaseTool):
    """Fetch a web page and return clean extracted text."""

    def __init__(
        self,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        include_links: bool = False,
    ) -> None:
        self._timeout_ms = timeout_ms
        self._include_links = include_links

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="web_scraper",
            description=(
                "Fetch a web page at the given URL and return the main text content, "
                "stripped of ads and boilerplate. Handles JavaScript-heavy pages via "
                "Playwright. Returns title, main text, and optional links."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full URL to scrape (must include scheme).",
                    },
                    "include_links": {
                        "type": "boolean",
                        "description": "Also return hyperlinks found on the page.",
                        "default": False,
                    },
                    "wait_for_selector": {
                        "type": "string",
                        "description": (
                            "Optional CSS selector to wait for before extracting "
                            "(useful for SPA pages). Example: '#content'."
                        ),
                    },
                },
                "required": ["url"],
            },
            returns=(
                "Dict: {url, title, text, word_count, links?, fetch_method, success}"
            ),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _parse_url(self, url: str) -> str:
        parsed = urlparse(url)
        if not parsed.scheme:
            url = "https://" + url
        return url

    def _extract(self, html: str, url: str, include_links: bool) -> dict:
        """Run trafilatura on raw HTML and return structured result."""
        extracted = _trafilatura.extract(
            html,
            url=url,
            include_links=include_links,
            output_format="txt",
            favor_precision=True,
        )
        metadata = _trafilatura.extract_metadata(html, default_url=url)
        title = metadata.title if metadata else ""
        text = extracted or ""
        links: list[str] = []

        if include_links:
            # Simple link extraction as a bonus alongside trafilatura text.
            import re
            links = re.findall(r'href=["\']([^"\']+)["\']', html)
            links = [l for l in links if l.startswith("http")][:50]

        return {
            "url": url,
            "title": title,
            "text": text,
            "word_count": len(text.split()),
            "links": links if include_links else None,
        }

    async def _fetch_with_playwright(
        self, url: str, wait_for_selector: str | None
    ) -> str:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(url, timeout=self._timeout_ms, wait_until="domcontentloaded")
                if wait_for_selector:
                    await page.wait_for_selector(
                        wait_for_selector, timeout=self._timeout_ms
                    )
                html = await page.content()
            finally:
                await browser.close()
        return html

    async def _fetch_with_httpx(self, url: str) -> str:
        import httpx
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (compatible; Tinker-Researcher/1.0; "
                "+https://github.com/tinker)"
            )
        }
        async with httpx.AsyncClient(
            timeout=self._timeout_ms / 1000, follow_redirects=True
        ) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.text

    # ------------------------------------------------------------------
    # Implementation
    # ------------------------------------------------------------------

    async def _execute(           # type: ignore[override]
        self,
        url: str,
        include_links: bool = False,
        wait_for_selector: str | None = None,
        **_: Any,
    ) -> dict:
        url = self._parse_url(url)
        fetch_method = "playwright"
        html = ""

        if _PLAYWRIGHT_AVAILABLE:
            try:
                html = await self._fetch_with_playwright(url, wait_for_selector)
            except (PWTimeout, Exception):           # noqa: BLE001
                # Fall back to httpx
                html = await self._fetch_with_httpx(url)
                fetch_method = "httpx_fallback"
        else:
            html = await self._fetch_with_httpx(url)
            fetch_method = "httpx"

        result = self._extract(html, url, include_links or self._include_links)
        result["fetch_method"] = fetch_method
        return result
