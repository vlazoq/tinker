"""
Web Scraper Tool — tools/web_scraper.py
=========================================

What this file does
--------------------
This file defines ``WebScraperTool``, which downloads a web page and extracts
its readable text content — stripping out navigation menus, cookie banners,
advertisements, and other "boilerplate" that would waste the AI's context
window.

The tool uses two different strategies to fetch pages:

  1. **Playwright** (primary): A real browser (Chromium) launched in headless
     (invisible) mode.  It can execute JavaScript, wait for the page to fully
     render, and then read the resulting HTML.  This is essential for modern
     "single-page applications" (SPAs) built with React, Vue, etc., which show
     only a loading spinner until JavaScript runs.

  2. **httpx** (fallback): A fast, lightweight HTTP client that fetches the raw
     HTML directly without running JavaScript.  Used when Playwright isn't
     installed or when Playwright fails (e.g. network timeout).

Once we have the raw HTML, we use the **trafilatura** library to extract the
main article text.  Trafilatura is purpose-built for this: it identifies the
"main content" of a page and discards everything else.

Why it exists
-------------
The web search tool returns only short snippets (a sentence or two).  For
deep research, Tinker needs the full text of the most relevant pages.  The
scraper provides that full text.

How it fits into Tinker
-----------------------
Registered as "web_scraper" in the ToolRegistry.  Typically called by the
``registry.research()`` method after a web search, to scrape the top result.
The Researcher agent can also call it directly if it wants the full text of a
specific URL.

Optional dependencies
---------------------
Both playwright and trafilatura are optional (not hard requirements).  If
playwright is not installed, we fall back to httpx.  If trafilatura is not
installed, ``_extract()`` will raise an AttributeError — you'll need to install
it with ``pip install trafilatura`` to use this tool.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

from .base import BaseTool, ToolSchema

# Try to import Playwright.  Playwright is an optional dependency — not everyone
# needs JavaScript-rendered page fetching.  If it's not installed, we set a flag
# so the rest of the code knows to skip the Playwright code path.
# Playwright is optional — fall back gracefully if not installed.
try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout

    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    # Playwright is not installed.  Set _PLAYWRIGHT_AVAILABLE = False so _execute()
    # knows to use the httpx fallback path instead.
    _PLAYWRIGHT_AVAILABLE = False
    # We alias PWTimeout to Exception so that the "except PWTimeout" in _execute()
    # is still syntactically valid even when playwright isn't installed.
    PWTimeout = Exception  # type: ignore[misc,assignment]

# Try to import trafilatura, the library that extracts readable text from HTML.
# It's imported lazily here (at module level) to avoid hard-failing at import time.
# trafilatura / httpx are imported lazily so the module loads without them
try:
    import trafilatura as _trafilatura
except ImportError:
    # If trafilatura isn't installed, _extract() will fail at call time —
    # but at least the module can be imported without error.
    _trafilatura = None  # type: ignore[assignment]

# How long (in milliseconds) to wait for a page to load before giving up.
# Read from an environment variable so operators can tune this without code changes.
# Default is 20,000 ms (20 seconds) which is generous for slow sites.
DEFAULT_TIMEOUT_MS = int(os.getenv("SCRAPER_TIMEOUT_MS", "20000"))  # 20 s


class WebScraperTool(BaseTool):
    """
    Fetch a web page and return its main text content, clean of boilerplate.

    This tool tries Playwright first (for JavaScript-heavy pages), then falls
    back to httpx (for simpler pages or when Playwright isn't available).
    Either way, the raw HTML is processed by trafilatura to extract just the
    article text.

    Example output (the ``data`` field of the returned ToolResult):

        {
          "url": "https://example.com/article",
          "title": "Understanding Event Sourcing",
          "text": "Event sourcing is a pattern where...",
          "word_count": 1842,
          "links": None,          # or a list of URLs if include_links=True
          "fetch_method": "playwright"  # or "httpx" or "httpx_fallback"
        }
    """

    def __init__(
        self,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        include_links: bool = False,
    ) -> None:
        """
        Initialise the scraper.

        Args:
            timeout_ms:
                How many milliseconds to wait for the browser/HTTP client
                before giving up.  Used for both the page load and any
                ``wait_for_selector`` call.

            include_links:
                If True, the tool will also extract hyperlinks from the page
                and include them in the result.  This is a global default;
                individual calls can also pass include_links=True.
        """
        self._timeout_ms = timeout_ms
        self._include_links = include_links

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    @property
    def schema(self) -> ToolSchema:
        """
        Describe this tool to the ToolRegistry and the AI model.

        The ``wait_for_selector`` parameter is particularly useful for
        single-page applications (SPAs) where the content doesn't exist in
        the initial HTML but is added by JavaScript after the page loads.
        For example, a React dashboard might render its data in a ``<div id="content">``
        element — passing ``wait_for_selector="#content"`` tells Playwright
        to wait until that element appears before reading the page.
        """
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
        """
        Ensure the URL has a scheme (http:// or https://).

        Some callers might pass a URL like "example.com/article" without the
        leading "https://".  Both Playwright and httpx need the scheme to
        work correctly, so we add it here if it's missing.

        Args:
            url: The URL as provided by the caller.

        Returns:
            The URL with "https://" prepended if no scheme was present.
        """
        parsed = urlparse(url)
        if not parsed.scheme:
            # No scheme found — default to https for safety.
            url = "https://" + url
        return url

    def _extract(self, html: str, url: str, include_links: bool) -> dict:
        """
        Run trafilatura on raw HTML and return a structured result dict.

        Trafilatura does the heavy lifting:
          - It figures out which part of the page is the "main content"
            (the article, blog post, documentation section, etc.).
          - It strips out navigation, footers, sidebars, ads, cookie banners.
          - It optionally extracts the page title from the metadata.

        If ``include_links`` is True, we also run a quick regex scan over the
        raw HTML to pull out hyperlinks.  We limit to the first 50 links to
        avoid overwhelming the result.

        Args:
            html:          Raw HTML string fetched from the page.
            url:           The URL the HTML came from (used by trafilatura
                           for relative link resolution and metadata).
            include_links: Whether to also extract hyperlinks.

        Returns:
            A dict with keys: url, title, text, word_count, links.
        """
        # Run trafilatura extraction.
        # favor_precision=True tells trafilatura to err on the side of
        # extracting less content rather than including boilerplate.
        extracted = _trafilatura.extract(
            html,
            url=url,
            include_links=include_links,
            output_format="txt",
            favor_precision=True,
        )
        # Separately extract metadata (like the page title) from the HTML.
        metadata = _trafilatura.extract_metadata(html, default_url=url)
        title = metadata.title if metadata else ""
        # If trafilatura found no content (e.g. a very JS-heavy page), use empty string.
        text = extracted or ""
        links: list[str] = []

        if include_links:
            # Simple link extraction as a bonus alongside trafilatura text.
            # We use a regex to find href attributes in the HTML.
            # This is deliberately simple — we only want http/https links,
            # and we cap at 50 to keep the result manageable.
            import re

            links = re.findall(r'href=["\']([^"\']+)["\']', html)
            # Filter to only absolute URLs (skip relative paths like "/about")
            # and limit to the first 50.
            links = [lnk for lnk in links if lnk.startswith("http")][:50]

        return {
            "url": url,
            "title": title,
            "text": text,
            # Word count is a quick proxy for content length — useful for
            # the Orchestrator to know if a scrape returned meaningful content.
            "word_count": len(text.split()),
            # Only include the "links" key if the caller asked for links;
            # otherwise set to None to keep the output clean.
            "links": links if include_links else None,
        }

    async def _fetch_with_playwright(
        self, url: str, wait_for_selector: str | None
    ) -> str:
        """
        Fetch a page using a real Chromium browser via Playwright.

        Why Playwright?
        ---------------
        Many modern websites (dashboards, documentation sites, news sites)
        render their content with JavaScript.  A plain HTTP request gets back
        only the initial HTML skeleton — without the JavaScript running, the
        content div is empty.  Playwright launches a real browser that
        executes the JavaScript and waits for the page to finish loading.

        What this does step by step:
          1. Start a Playwright session (manages the browser process lifecycle).
          2. Launch a headless Chromium browser (no visible window).
          3. Open a new browser tab (page).
          4. Navigate to the URL and wait until the DOM is ready.
          5. Optionally wait for a specific CSS selector to appear.
          6. Read the full rendered HTML.
          7. Close the browser (cleanup — important to free resources).

        Args:
            url:                The URL to visit.
            wait_for_selector:  CSS selector to wait for, or None.

        Returns:
            The full rendered HTML of the page as a string.

        Raises:
            Any exception from Playwright if the page can't be loaded.
            BaseTool.execute() catches these and turns them into ToolResult errors.
        """
        async with async_playwright() as p:
            # Launch headless Chromium — "headless" means no visible window.
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                # Navigate to the URL. "domcontentloaded" means we wait until
                # the HTML is parsed and the DOM is built, but we don't wait
                # for all images/fonts to finish downloading (faster).
                await page.goto(
                    url, timeout=self._timeout_ms, wait_until="domcontentloaded"
                )
                if wait_for_selector:
                    # Wait for a specific element to appear — useful for SPAs
                    # where the main content is loaded by JavaScript after the DOM.
                    await page.wait_for_selector(
                        wait_for_selector, timeout=self._timeout_ms
                    )
                # Read the fully rendered HTML from the browser.
                html = await page.content()
            finally:
                # Always close the browser, even if an error occurred.
                # Leaving browsers open would be a resource leak.
                await browser.close()
        return html

    async def _fetch_with_httpx(self, url: str) -> str:
        """
        Fetch a page using a plain async HTTP request (no JavaScript execution).

        This is simpler and faster than Playwright, and works fine for pages
        that render their content server-side (plain HTML, most documentation,
        Wikipedia, etc.).

        We send a User-Agent header so the server knows we're a bot.  Politely
        identifying ourselves is good practice and avoids some bot-detection
        measures that block requests with no User-Agent.

        Args:
            url: The URL to fetch.

        Returns:
            The raw HTML response body as a string.

        Raises:
            httpx.HTTPStatusError if the server returns 4xx or 5xx.
        """
        import httpx

        headers = {
            # Identify ourselves as "Tinker-Researcher" — a polite, honest User-Agent.
            "User-Agent": (
                "Mozilla/5.0 (compatible; Tinker-Researcher/1.0; "
                "+https://github.com/tinker)"
            )
        }
        async with httpx.AsyncClient(
            # Convert ms to seconds for httpx (httpx uses seconds for timeouts).
            timeout=self._timeout_ms / 1000,
            # follow_redirects=True automatically follows HTTP 301/302 redirects.
            follow_redirects=True,
        ) as client:
            resp = await client.get(url, headers=headers)
            # Raise an exception for 4xx/5xx responses so BaseTool catches it.
            resp.raise_for_status()
            return resp.text

    # ------------------------------------------------------------------
    # Implementation
    # ------------------------------------------------------------------

    async def _execute(  # type: ignore[override]
        self,
        url: str,
        include_links: bool = False,
        wait_for_selector: str | None = None,
        **_: Any,  # absorb any unexpected kwargs the caller might pass
    ) -> dict:
        """
        Fetch and extract the content of a web page.

        Strategy:
          1. Ensure the URL has a scheme (https://).
          2. Try Playwright first (supports JavaScript-rendered pages).
             If Playwright fails (timeout, crash, not installed), fall back to httpx.
          3. Run trafilatura on the fetched HTML to extract clean text.
          4. Attach the "fetch_method" to the result so callers know which
             path was taken (useful for debugging).

        Args:
            url:                Full URL to scrape.
            include_links:      If True, also extract hyperlinks from the page.
            wait_for_selector:  Optional CSS selector to wait for (Playwright only).
            **_:                Ignored extra arguments.

        Returns:
            A dict with keys: url, title, text, word_count, links, fetch_method.
        """
        url = self._parse_url(url)
        fetch_method = "playwright"  # assume we'll use Playwright; may be updated below
        html = ""

        if _PLAYWRIGHT_AVAILABLE:
            try:
                html = await self._fetch_with_playwright(url, wait_for_selector)
            except (PWTimeout, Exception):  # noqa: BLE001
                # Playwright failed (timeout, browser crash, network error, etc.).
                # Fall back to the simpler httpx approach.
                # Fall back to httpx
                html = await self._fetch_with_httpx(url)
                fetch_method = "httpx_fallback"  # record that we had to fall back
        else:
            # Playwright isn't installed at all — go straight to httpx.
            html = await self._fetch_with_httpx(url)
            fetch_method = "httpx"

        # Extract the main text from the raw HTML, using the instance-level
        # include_links flag OR the per-call override (whichever is True).
        result = self._extract(html, url, include_links or self._include_links)
        # Record which fetch method was used so callers can see it in the result.
        result["fetch_method"] = fetch_method
        return result
