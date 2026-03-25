"""
grub/context_summarizer.py
==========================
MinionContextSummarizer — compresses large context blocks using an LLM call
instead of hard-truncating them.

Why this exists
---------------
Grub minions deal with growing amounts of context: design documents, existing
code, prior output from other minions, stack traces, etc.  When this context
grows beyond what a model can comfortably handle, the old approach was to chop
it off at a fixed character limit (e.g. 3000 chars) with a marker like
"[... truncated ...]".

Hard truncation has a serious problem: it often cuts off the most important
part.  A design document that is 4000 characters long might have its key
architectural decision on line 80, which gets truncated in the middle of a
sentence.

LLM-based summarization fixes this: instead of deleting the tail, we ask a
small model to read the full text and produce a compressed version that
preserves the key information.  The result is shorter *and* more informative
than a truncated excerpt.

Performance
-----------
Summarization adds one LLM call per unique context block.  To avoid redundant
calls, results are cached by SHA-256 hash of the input text.  If the same
design document appears in two different tasks, it is only summarized once per
process lifetime.

The cache is in-memory (not persisted to disk) so it is cleared when Grub
restarts.  This is intentional: cached summaries may become stale if the
source file changes between runs.

Configuration
-------------
All behaviour is controlled by GrubConfig fields added alongside this module:

    context_summarization_enabled : bool  (default True)
    context_max_chars             : int   (default 6000)
    context_target_chars          : int   (default 3000)

These can be set in grub_config.json or via environment variables.

How to use
----------
This class is instantiated in BaseMinion and exposed as
``self.compress_context(text, label)``.  Minions call it like this::

    design_text = await self.compress_context(design_text, "design document")
    test_output = await self.compress_context(test_output, "test output")

If the text is already under ``context_max_chars``, it is returned unchanged
with zero LLM calls made — so there is no overhead for short contexts.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class MinionContextSummarizer:
    """
    Compresses context text using a small LLM model.

    Parameters
    ----------
    model      : Ollama model name to use for summarization.
                 Should be a small, fast model (e.g. phi3:mini, qwen3:1.7b).
                 Defaults to the minion's own model if not set.
    ollama_url : Base URL of the Ollama instance.
    max_chars  : Text longer than this will be compressed.
    target_chars: Aim for this many characters in the compressed output.
    timeout    : HTTP timeout for the summarization call.
    enabled    : If False, the class is a no-op (returns text unchanged).
    """

    # Simple prompt that tells the model what to keep vs drop.
    _PROMPT_TEMPLATE = """Compress the following {label} to approximately {target} characters.

KEEP: key decisions, identified issues, function signatures, error messages,
      class names, important data structures, constraints, warnings.
DROP: verbose explanations, repeated content, boilerplate, unrelated examples.

Output ONLY the compressed text — no preamble, no "here is the summary".

--- BEGIN {label_upper} ---
{text}
--- END {label_upper} ---"""

    def __init__(
        self,
        model: str,
        ollama_url: str,
        max_chars: int = 6000,
        target_chars: int = 3000,
        timeout: float = 45.0,
        enabled: bool = True,
    ) -> None:
        self._model = model
        self._ollama_url = ollama_url
        self._max_chars = max_chars
        self._target_chars = target_chars
        self._timeout = timeout
        self._enabled = enabled
        # SHA-256 hash → compressed text
        self._cache: dict[str, str] = {}

    async def compress(self, text: str, label: str = "context") -> str:
        """
        Return a compressed version of ``text`` if it exceeds ``max_chars``.

        If ``text`` is already short enough, returns it unchanged with no LLM
        call.  If summarization is disabled, also returns unchanged.

        Parameters
        ----------
        text  : The text to (possibly) compress.
        label : Human-readable name for the context type, used in the prompt
                (e.g. "design document", "test output", "stack trace").

        Returns
        -------
        str : Original text (if short enough or disabled) or compressed text.
        """
        if not self._enabled or len(text) <= self._max_chars:
            return text

        # Check cache first — avoid re-summarizing identical content.
        key = hashlib.sha256(text.encode()).hexdigest()
        if key in self._cache:
            logger.debug(
                "compress_context: cache hit for %s (input=%d chars)",
                label,
                len(text),
            )
            return self._cache[key]

        logger.info(
            "compress_context: %s is %d chars (limit %d) — summarizing with %s",
            label,
            len(text),
            self._max_chars,
            self._model,
        )

        compressed = await self._llm_compress(text, label)

        # Store in cache even if the LLM call failed (we return original text
        # in that case so future calls also short-circuit cleanly).
        self._cache[key] = compressed
        logger.info(
            "compress_context: compressed %s from %d → %d chars",
            label,
            len(text),
            len(compressed),
        )
        return compressed

    async def _llm_compress(self, text: str, label: str) -> str:
        """
        Make a single Ollama call to compress ``text``.

        On any error, logs a warning and returns the original text truncated
        to ``target_chars`` with an explanatory marker — the same as the old
        hard-truncation behaviour, but only as a last-resort fallback.
        """
        prompt = self._PROMPT_TEMPLATE.format(
            label=label,
            label_upper=label.upper(),
            target=self._target_chars,
            text=text,
        )

        payload = {
            "model": self._model,
            "stream": False,
            "options": {
                "temperature": 0.1,  # low temperature = deterministic, factual
                "num_predict": max(512, self._target_chars // 3),
            },
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a precise text compressor. "
                        "Compress the given text to the requested length, "
                        "preserving all technically important information."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(
                    f"{self._ollama_url}/api/chat",
                    json=payload,
                )
                r.raise_for_status()
                data = r.json()
                return data.get("message", {}).get("content", "").strip()

        except httpx.ConnectError:
            logger.warning(
                "compress_context: cannot connect to Ollama at %s — "
                "falling back to hard truncation for %s",
                self._ollama_url,
                label,
            )
        except httpx.TimeoutException:
            logger.warning(
                "compress_context: Ollama timed out after %.0fs — "
                "falling back to hard truncation for %s",
                self._timeout,
                label,
            )
        except Exception as exc:
            logger.warning(
                "compress_context: unexpected error (%s) — "
                "falling back to hard truncation for %s",
                exc,
                label,
            )

        # Fallback: hard truncate (same as the old behaviour, but only triggered
        # when the LLM call itself fails — not the happy path).
        return text[: self._target_chars] + f"\n\n[{label} truncated — LLM compression failed]"

    def clear_cache(self) -> None:
        """Clear the in-memory cache (useful for tests or long-running processes)."""
        self._cache.clear()

    @property
    def cache_size(self) -> int:
        """Number of cached summaries."""
        return len(self._cache)
