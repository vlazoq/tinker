"""
compression.py — Memory compression and archival for Session Memory.

Strategy
--------
When the number of un-archived artifacts in a session exceeds
`MemoryConfig.compression_artifact_threshold`, or when artifacts are
older than `compression_max_age_hours`, the compressor:

  1. Fetches a chunk of old / excess artifacts from DuckDB.
  2. Summarises them using the model client (passed in as a callable)
     so the compressor stays decoupled from the model layer.
  3. Stores the summary as a new SUMMARY artifact in DuckDB.
  4. Stores the summary as a ResearchNote in ChromaDB so knowledge
     survives beyond the current session.
  5. Marks the original artifacts as archived (not deleted — audit trail
     is preserved).

The compressor is async and designed to be called periodically by the
Orchestrator (e.g. every N minutes or after every K new artifacts).
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from .embeddings import EmbeddingPipeline
from .schemas import Artifact, ArtifactType, MemoryConfig, ResearchNote
from .storage import ChromaAdapter, DuckDBAdapter

logger = logging.getLogger(__name__)

# Minimum cosine similarity between the original chunk's centroid embedding
# and the summary embedding.  Below this, a WARNING is emitted so operators
# know the summariser may be producing low-quality or hallucinated output.
_MIN_SUMMARY_SIMILARITY = 0.4

# Maximum number of retry attempts when a summary's cosine similarity is
# below _MIN_SUMMARY_SIMILARITY.  Each retry uses a more explicit prompt
# to force the summariser to preserve technical details.
_MAX_QUALITY_RETRIES = 2

# The retry prompt prefix, prepended to the original prompt when the
# quality gate fails.  This nudges the model to be more faithful.
_QUALITY_RETRY_PREFIX = (
    "IMPORTANT: Preserve ALL technical decisions, trade-offs, and specific "
    "details. The previous summary lost too much information.\n\n"
)


class OllamaSummarizer:
    """
    A production-grade summariser that calls a local Ollama model via HTTP.

    How it works
    ------------
    1. Receives a text prompt (from ``MemoryCompressor._summarise``).
    2. Sends an HTTP POST to the Ollama ``/api/generate`` endpoint.
    3. Returns the model's response text.
    4. On transient failures (network errors, timeouts), retries once
       (2 total attempts) before falling back to simple truncation.

    Parameters
    ----------
    base_url : str
        The base URL where Ollama is running, e.g. ``"http://localhost:11434"``.
    model : str
        The Ollama model name to use for summarisation.  Typically a small,
        fast Judge model (2-3B params) like ``"phi3:mini"`` — the same model
        used for the Critic role.
    timeout : float
        HTTP request timeout in seconds.  Defaults to 60 seconds, which is
        generous enough for the 2-3B Judge model on most hardware.

    Usage
    -----
    ::

        summariser = OllamaSummarizer(
            base_url="http://localhost:11434",
            model="phi3:mini",
        )

        # Pass it directly to MemoryCompressor or MemoryManager:
        manager = MemoryManager(summariser=summariser)

    The class implements ``__call__`` so it can be used anywhere a
    ``SummariserFn`` (i.e. ``Callable[[str], Awaitable[str]]``) is expected.
    """

    # The system prompt instructs the model to focus on preserving the
    # information that matters most in an architecture research context.
    _SYSTEM_PROMPT = (
        "Summarize the following text, preserving all technical decisions, "
        "architecture choices, trade-offs, function signatures, and error "
        "details. Be concise but complete."
    )

    # Number of total attempts before falling back to truncation.
    # 2 means: try once, and if that fails, retry once more.
    _MAX_ATTEMPTS = 2

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "phi3:mini",
        timeout: float = 60.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    async def __call__(self, text: str) -> str:
        """
        Summarise *text* using the Ollama model.

        This method is the ``SummariserFn`` interface that ``MemoryCompressor``
        calls.  It tries up to ``_MAX_ATTEMPTS`` times to get a response from
        the Ollama API.  If all attempts fail, it falls back to returning a
        truncated version of the input so that compression can still proceed
        (data loss from truncation beats a total failure).

        Parameters
        ----------
        text : str
            The prompt text to send to the model (typically a structured
            prompt built by ``MemoryCompressor._summarise``).

        Returns
        -------
        str
            The model's summary, or a truncated fallback on failure.
        """
        import aiohttp  # type: ignore  — lazy import to match codebase pattern

        url = f"{self.base_url}/api/generate"
        payload = {
            "model": self.model,
            "system": self._SYSTEM_PROMPT,
            "prompt": text,
            "stream": False,  # We want the full response at once
        }

        last_error: Exception | None = None

        for attempt in range(1, self._MAX_ATTEMPTS + 1):
            try:
                async with (
                    aiohttp.ClientSession() as session,
                    session.post(
                        url,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=self.timeout),
                    ) as resp,
                ):
                    resp.raise_for_status()
                    data = await resp.json()
                    # Ollama returns the generated text in the "response" field
                    response_text = data.get("response", "").strip()
                    if response_text:
                        return response_text
                    # Empty response — treat as a failure and retry
                    last_error = ValueError("Ollama returned empty response")
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "[OllamaSummarizer] Attempt %d/%d failed: %s",
                    attempt,
                    self._MAX_ATTEMPTS,
                    exc,
                )
                if attempt < self._MAX_ATTEMPTS:
                    # Brief pause before retry to allow transient issues to clear
                    await asyncio.sleep(1.0)

        # All attempts exhausted — fall back to truncation so compression
        # doesn't stall.  We truncate to ~500 chars which is enough to
        # preserve the most important leading content.
        logger.warning(
            "[OllamaSummarizer] All %d attempts failed (last error: %s). "
            "Falling back to truncation.",
            self._MAX_ATTEMPTS,
            last_error,
        )
        return self._truncation_fallback(text)

    @staticmethod
    def _truncation_fallback(text: str, max_chars: int = 500) -> str:
        """
        Emergency fallback: return the first *max_chars* characters of the
        input when the LLM is unreachable.

        We strip the prompt scaffolding (everything before "ARTIFACTS:")
        so the truncated text contains actual content rather than the
        instruction prefix.
        """
        # Try to skip past the prompt prefix to get at the raw artifacts
        marker = "ARTIFACTS:\n"
        idx = text.find(marker)
        body = text[idx + len(marker) :] if idx != -1 else text
        if len(body) <= max_chars:
            return body
        return body[:max_chars] + "… [truncated — LLM unavailable]"


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two equal-length vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


# Type alias for "any async function that takes a prompt string and returns text"
SummariserFn = Callable[[str], Awaitable[str]]


class MemoryCompressor:
    """
    Compresses old session artifacts into summaries.

    Parameters
    ----------
    duckdb      : DuckDB session memory adapter
    chroma      : ChromaDB research archive adapter
    embeddings  : embedding pipeline for archiving summaries
    summariser  : async callable(prompt: str) -> str
                  Typically wraps the Tinker model client. A no-op stub is
                  used when no summariser is supplied (useful for tests).
    config      : MemoryConfig with compression thresholds
    """

    def __init__(
        self,
        duckdb: DuckDBAdapter,
        chroma: ChromaAdapter,
        embeddings: EmbeddingPipeline,
        summariser: SummariserFn | None = None,
        config: MemoryConfig | None = None,
    ):
        self.duckdb = duckdb
        self.chroma = chroma
        self.embeddings = embeddings
        self.summariser = summariser or self._stub_summariser
        self.config = config or MemoryConfig()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def maybe_compress(self, session_id: str) -> int:
        """
        Check whether compression is needed and run it if so.
        Returns the number of artifacts archived this call.
        """
        count = await self.duckdb.count_session_artifacts(session_id)
        aged_out = await self._get_aged_out_artifacts(session_id)
        excess = max(0, count - self.config.compression_artifact_threshold)

        if not aged_out and excess <= 0:
            logger.debug(
                "[compression] session %s: %d artifacts, no compression needed.",
                session_id,
                count,
            )
            return 0

        total_archived = 0

        # Archive aged-out artifacts first
        if aged_out:
            logger.info(
                "[compression] Archiving %d aged-out artifacts for session %s",
                len(aged_out),
                session_id,
            )
            total_archived += await self._compress_chunk(session_id, aged_out, "age-based")

        # Archive excess artifacts (oldest first)
        if excess > 0:
            excess_artifacts = await self.duckdb.get_recent(
                session_id,
                limit=min(excess, self.config.compression_summary_chunk * 5),
                include_archived=False,
            )
            # get_recent returns newest-first; reverse for oldest-first archival
            excess_artifacts = list(reversed(excess_artifacts))
            if excess_artifacts:
                logger.info(
                    "[compression] Archiving %d excess artifacts for session %s",
                    len(excess_artifacts),
                    session_id,
                )
                total_archived += await self._compress_chunk(
                    session_id, excess_artifacts, "threshold-based"
                )

        logger.info(
            "[compression] Session %s: archived %d artifacts total.",
            session_id,
            total_archived,
        )
        return total_archived

    async def force_compress_all(self, session_id: str) -> int:
        """
        Archive every non-archived artifact in a session — e.g. at session end.
        """
        all_artifacts = await self.duckdb.get_recent(
            session_id, limit=10_000, include_archived=False
        )
        if not all_artifacts:
            return 0
        return await self._compress_chunk(session_id, all_artifacts, "end-of-session")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _get_aged_out_artifacts(self, session_id: str) -> list[dict]:
        cutoff = datetime.now(UTC) - timedelta(hours=self.config.compression_max_age_hours)
        return await self.duckdb.get_old_artifacts(
            session_id,
            older_than=cutoff,
            limit=self.config.compression_summary_chunk * 5,
        )

    async def _compress_chunk(self, session_id: str, artifacts: list[dict], reason: str) -> int:
        """
        Break artifacts into sub-chunks, summarise each, store summaries,
        and mark originals archived.
        """
        chunk_size = self.config.compression_summary_chunk
        total_archived = 0

        for i in range(0, len(artifacts), chunk_size):
            chunk = artifacts[i : i + chunk_size]
            summary_text = await self._summarise(chunk, reason)
            artifact_ids = [a["id"] for a in chunk]

            # ── Quality validation with retry ─────────────────────────────────
            # Embed the concatenated original content and the generated summary,
            # then measure cosine similarity.  A very low similarity suggests
            # the summariser drifted from the source material (hallucination or
            # stub output).
            #
            # If the similarity is below _MIN_SUMMARY_SIMILARITY, we retry with
            # a more explicit prompt up to _MAX_QUALITY_RETRIES times.  This
            # gives the model a second (and third) chance to produce a faithful
            # summary.  If it still fails, we log a WARNING and proceed rather
            # than blocking the compression pipeline.
            try:
                original_text = " ".join(str(a.get("content", ""))[:500] for a in chunk)
                orig_emb = await self.embeddings.embed(original_text)
                summ_emb = await self.embeddings.embed(summary_text)
                similarity = _cosine_similarity(orig_emb, summ_emb)

                # Retry loop: re-summarise with a stronger prompt if quality
                # is too low.  Each retry prepends _QUALITY_RETRY_PREFIX to
                # nudge the model toward higher fidelity.
                quality_retries = 0
                while (
                    similarity < _MIN_SUMMARY_SIMILARITY and quality_retries < _MAX_QUALITY_RETRIES
                ):
                    quality_retries += 1
                    logger.info(
                        "[compression] Low summary quality (cosine=%.3f < %.3f). "
                        "Retrying with explicit prompt (attempt %d/%d) for "
                        "chunk starting at artifact %s.",
                        similarity,
                        _MIN_SUMMARY_SIMILARITY,
                        quality_retries,
                        _MAX_QUALITY_RETRIES,
                        artifact_ids[0] if artifact_ids else "unknown",
                    )
                    # Re-summarise with the retry prefix for emphasis
                    summary_text = await self._summarise(
                        chunk, reason, prompt_prefix=_QUALITY_RETRY_PREFIX
                    )
                    summ_emb = await self.embeddings.embed(summary_text)
                    similarity = _cosine_similarity(orig_emb, summ_emb)

                if similarity < _MIN_SUMMARY_SIMILARITY:
                    # All retries exhausted — warn but do NOT block compression
                    logger.warning(
                        "[compression] Low summary quality persists after %d "
                        "retries: cosine_similarity=%.3f < threshold=%.3f for "
                        "chunk starting at artifact %s. "
                        "Proceeding with best-effort summary.",
                        _MAX_QUALITY_RETRIES,
                        similarity,
                        _MIN_SUMMARY_SIMILARITY,
                        artifact_ids[0] if artifact_ids else "unknown",
                    )
                else:
                    logger.debug(
                        "[compression] Summary quality OK: cosine_similarity=%.3f%s",
                        similarity,
                        f" (after {quality_retries} retry/retries)" if quality_retries > 0 else "",
                    )
            except Exception as exc:
                logger.debug("[compression] Quality check failed (non-fatal): %s", exc)

            # Store summary as a new DuckDB artifact
            summary_artifact = Artifact(
                content=summary_text,
                artifact_type=ArtifactType.SUMMARY,
                session_id=session_id,
                metadata={
                    "compression_reason": reason,
                    "source_ids": artifact_ids,
                    "source_count": len(chunk),
                },
            )
            await self.duckdb.insert_artifact(summary_artifact)

            # Archive summary in ChromaDB so it outlives the session
            embedding = await self.embeddings.embed(summary_text)
            note = ResearchNote(
                content=summary_text,
                topic="session-summary",
                source="tinker-compression",
                tags=["summary", reason, session_id],
                session_id=session_id,
                metadata={"source_artifact_count": len(chunk)},
            )
            await self.chroma.upsert(
                doc_id=note.id,
                document=note.content,
                embedding=embedding,
                metadata=note.to_chroma_metadata(),
            )

            # Mark originals archived
            await self.duckdb.mark_archived(artifact_ids)
            total_archived += len(chunk)
            logger.debug(
                "[compression] Summarised %d artifacts → summary %s",
                len(chunk),
                summary_artifact.id,
            )

        return total_archived

    async def _summarise(
        self,
        artifacts: list[dict],
        reason: str,
        prompt_prefix: str = "",
    ) -> str:
        """
        Call the summariser with a structured prompt.

        Parameters
        ----------
        artifacts    : The chunk of artifact dicts to summarise.
        reason       : Human-readable reason for compression (e.g. "age-based").
        prompt_prefix: Optional prefix prepended to the prompt on quality retries.
                       Used by the quality-gate retry loop to nudge the model
                       toward higher-fidelity output.  Empty string on first call.
        """
        lines = []
        for a in artifacts:
            ts = a.get("created_at", "?")
            atype = a.get("artifact_type", "?")
            content = str(a.get("content", ""))[:500]  # truncate for context window
            lines.append(f"[{ts}] ({atype})\n{content}")

        joined = "\n\n---\n\n".join(lines)
        prompt = (
            f"{prompt_prefix}"
            f"You are a technical memory compressor for an AI architecture engine.\n"
            f"Reason for compression: {reason}\n\n"
            f"Summarise the following {len(artifacts)} session artifacts into a single "
            f"dense technical paragraph. Preserve all architectural decisions, key "
            f"findings, and unresolved questions. Discard conversational filler.\n\n"
            f"ARTIFACTS:\n{joined}\n\nSUMMARY:"
        )
        return await self.summariser(prompt)

    @staticmethod
    async def _stub_summariser(prompt: str) -> str:
        """Fallback when no real model client is wired up (useful in tests)."""
        # Extract the artifact count from the prompt for a realistic stub
        count_str = ""
        for word in prompt.split():
            if word.isdigit():
                count_str = word
                break
        return (
            f"[Stub summary of {count_str or '?'} artifacts. "
            f"A real summariser should be injected via MemoryManager(summariser=...).]"
        )
