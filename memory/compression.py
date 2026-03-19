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

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Awaitable, Optional

from .schemas import Artifact, ArtifactType, ResearchNote, MemoryConfig
from .storage import DuckDBAdapter, ChromaAdapter
from .embeddings import EmbeddingPipeline

logger = logging.getLogger(__name__)

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
        summariser: Optional[SummariserFn] = None,
        config: Optional[MemoryConfig] = None,
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
            total_archived += await self._compress_chunk(
                session_id, aged_out, "age-based"
            )

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
        cutoff = datetime.now(timezone.utc) - timedelta(
            hours=self.config.compression_max_age_hours
        )
        return await self.duckdb.get_old_artifacts(
            session_id,
            older_than=cutoff,
            limit=self.config.compression_summary_chunk * 5,
        )

    async def _compress_chunk(
        self, session_id: str, artifacts: list[dict], reason: str
    ) -> int:
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

    async def _summarise(self, artifacts: list[dict], reason: str) -> str:
        """Call the summariser with a structured prompt."""
        lines = []
        for a in artifacts:
            ts = a.get("created_at", "?")
            atype = a.get("artifact_type", "?")
            content = str(a.get("content", ""))[:500]  # truncate for context window
            lines.append(f"[{ts}] ({atype})\n{content}")

        joined = "\n\n---\n\n".join(lines)
        prompt = (
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
