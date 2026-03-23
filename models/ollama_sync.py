"""
models/ollama_sync.py
=====================
OllamaSync — discover and import models from one or more Ollama instances.

This module queries the Ollama HTTP API (``GET /api/tags``) on each configured
server and returns a list of ``ModelEntry`` objects ready to be added to the
ModelLibrary.  No model is added automatically — the user reviews the list
in the Dashboard and clicks "Add to Library" for the ones they want.

Ollama /api/tags response shape
---------------------------------
::

    {
      "models": [
        {
          "name": "qwen3:7b",
          "size": 4683075680,
          "modified_at": "2026-03-20T10:00:00Z",
          "details": {
            "format": "gguf",
            "family": "qwen3",
            "parameter_size": "7.6B",
            "quantization_level": "Q4_K_M"
          }
        }
      ]
    }

Usage
-----
::

    sync = OllamaSync(["http://localhost:11434", "http://192.168.1.10:11434"])
    models = await sync.discover_all()
    for m in models:
        print(m.display_name, m.ollama_url, m.model_tag)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .library import ModelEntry

logger = logging.getLogger(__name__)

# Seconds to wait for Ollama's /api/tags response before giving up.
_TIMEOUT = 8.0


class OllamaSync:
    """
    Discover AI models available on one or more local/LAN Ollama servers.

    Parameters
    ----------
    server_urls : List of Ollama base URLs to query.
                  Example: ``["http://localhost:11434", "http://nas:11434"]``
    timeout     : HTTP timeout in seconds (default: 8).
    """

    def __init__(
        self,
        server_urls: list[str] | None = None,
        timeout: float = _TIMEOUT,
    ) -> None:
        self._urls = server_urls or ["http://localhost:11434"]
        self._timeout = timeout

    async def discover_all(self) -> list[dict]:
        """
        Query all configured Ollama servers and return discovered models.

        Returns
        -------
        List of dicts, each with:
          - ``model_tag``    : Ollama model name (e.g. ``"qwen3:7b"``)
          - ``display_name`` : Suggested human-readable label
          - ``suggested_id`` : Suggested library ID (slug, not guaranteed unique)
          - ``ollama_url``   : The server this model lives on
          - ``size_gb``      : Model file size in GB (rounded to 2dp)
          - ``family``       : Model family from Ollama metadata (e.g. ``"qwen3"``)
          - ``parameter_size``: e.g. ``"7.6B"``
          - ``quantization`` : e.g. ``"Q4_K_M"``
          - ``in_library``   : bool — always False here (caller checks library)
        """
        tasks = [self._query_server(url) for url in self._urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_models: list[dict] = []
        for url, result in zip(self._urls, results):
            if isinstance(result, Exception):
                logger.warning("OllamaSync: could not reach %s — %s", url, result)
                continue
            all_models.extend(result)

        return all_models

    async def discover_as_entries(self) -> list[ModelEntry]:
        """
        Like ``discover_all()`` but returns ``ModelEntry`` objects directly.

        Each entry has a suggested id, display_name, and capabilities inferred
        from the model family name.  Use this to bulk-import into the library.
        """
        raw = await self.discover_all()
        return [self._to_entry(m) for m in raw]

    async def check_server(self, url: str) -> dict:
        """
        Check reachability of a single Ollama server.

        Returns
        -------
        dict with ``reachable: bool``, ``model_count: int``, ``url: str``.
        """
        try:
            models = await self._query_server(url)
            return {"reachable": True, "url": url, "model_count": len(models)}
        except Exception as exc:
            return {"reachable": False, "url": url, "model_count": 0, "error": str(exc)}

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _query_server(self, base_url: str) -> list[dict]:
        """Query one Ollama server's /api/tags endpoint."""
        try:
            import aiohttp
        except ImportError:
            # Fall back to urllib if aiohttp is not installed
            return await asyncio.to_thread(self._query_server_sync, base_url)

        url = base_url.rstrip("/") + "/api/tags"
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status} from {url}")
                data = await resp.json()
        return self._parse_tags(data, base_url)

    def _query_server_sync(self, base_url: str) -> list[dict]:
        """Sync fallback using urllib (no aiohttp dependency)."""
        import json
        import urllib.error
        import urllib.request

        url = base_url.rstrip("/") + "/api/tags"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.URLError as exc:
            raise RuntimeError(str(exc)) from exc
        return self._parse_tags(data, base_url)

    @staticmethod
    def _parse_tags(data: dict, base_url: str) -> list[dict]:
        """Parse Ollama /api/tags JSON into our internal dict format."""
        results = []
        for m in data.get("models", []):
            name = m.get("name", "")
            if not name:
                continue
            details = m.get("details", {})
            family = details.get("family", "")
            param_size = details.get("parameter_size", "")
            quant = details.get("quantization_level", "")
            size_bytes = m.get("size", 0)
            size_gb = round(size_bytes / 1_073_741_824, 2) if size_bytes else 0.0

            # Build a display name: "Qwen3 7.6B Q4_K_M"
            parts = [p for p in [family.title() or name.split(":")[0].title(), param_size, quant] if p]
            display = " ".join(parts) if parts else name

            # Suggested library id: "qwen3-7b-192-168-1-10"
            slug = name.replace(":", "-").replace(".", "").replace("_", "-").lower()
            host_slug = base_url.replace("http://", "").replace("https://", "").replace(":", "-").replace(".", "-")
            suggested_id = f"{slug}-{host_slug}"

            results.append(
                {
                    "model_tag": name,
                    "display_name": display,
                    "suggested_id": suggested_id,
                    "ollama_url": base_url,
                    "size_gb": size_gb,
                    "family": family,
                    "parameter_size": param_size,
                    "quantization": quant,
                    "in_library": False,  # caller sets this after checking library
                }
            )
        return results

    @staticmethod
    def _infer_capabilities(family: str, tag: str) -> list[str]:
        """Guess capability tags from model family/tag name."""
        caps: list[str] = []
        combined = (family + " " + tag).lower()
        if any(k in combined for k in ("coder", "code", "deepseek-coder", "starcoder")):
            caps.append("coding")
        if any(k in combined for k in ("mini", "small", "phi3", "phi-3", "tiny")):
            caps.append("fast")
        if any(k in combined for k in ("32b", "70b", "34b", "72b", "65b")):
            caps.append("large")
        if any(k in combined for k in ("instruct", "chat")):
            caps.append("chat")
        if "embed" in combined:
            caps.append("embedding")
        if not caps:
            caps.append("general")
        return caps

    def _to_entry(self, m: dict) -> ModelEntry:
        caps = self._infer_capabilities(m.get("family", ""), m.get("model_tag", ""))
        ctx = _infer_context_window(m.get("model_tag", ""), m.get("parameter_size", ""))
        return ModelEntry(
            id=m["suggested_id"],
            model_tag=m["model_tag"],
            display_name=m["display_name"],
            ollama_url=m["ollama_url"],
            context_window=ctx,
            notes=f"{m.get('size_gb', 0):.2f} GB — {m.get('quantization', '')}",
            capabilities=caps,
        )


def _infer_context_window(tag: str, param_size: str) -> int:
    """
    Guess a reasonable context window from model name / size.

    These are rough heuristics — the user can edit the value in the library.
    """
    tag_lower = tag.lower()
    if "qwen" in tag_lower:
        return 32768
    if "llama-3" in tag_lower or "llama3" in tag_lower:
        return 8192
    if "phi3" in tag_lower or "phi-3" in tag_lower:
        return 4096
    if "deepseek" in tag_lower:
        return 32768
    if "mistral" in tag_lower:
        return 32768
    if "gemma" in tag_lower:
        return 8192
    # Default: small models 4k, larger 8k
    size_str = param_size.upper()
    if any(s in size_str for s in ("70B", "34B", "32B", "65B", "72B")):
        return 8192
    return 4096
