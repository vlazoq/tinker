"""
_session_memory.py — SessionMemoryMixin: DuckDB-backed session artifact storage.

Split from manager.py to keep each memory layer in its own focused module.
"""

from __future__ import annotations

import logging

from .compression import _cosine_similarity
from .schemas import Artifact, ArtifactType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Semantic deduplication settings
#
# Before storing a new artifact, MemoryManager checks cosine similarity
# against the N most recent artifacts.  If any existing artifact is very
# similar (above the threshold), the new content is merged into the existing
# one instead of creating a near-duplicate entry.  This keeps the session
# memory lean and avoids redundant compression work later.
# ---------------------------------------------------------------------------

# How many recent artifacts to check for duplicates
_DEDUP_RECENT_WINDOW = 5

# Cosine similarity above which a new artifact is considered a duplicate
# of an existing one.  0.92 is deliberately high — we only merge when the
# two pieces of content are nearly identical.
_DEDUP_SIMILARITY_THRESHOLD = 0.92


def _parse_row_metadata(row: dict) -> dict:
    """Parse the ``metadata`` field of a DB row from JSON string to dict in-place."""
    import json

    if isinstance(row.get("metadata"), str):
        try:
            row["metadata"] = json.loads(row["metadata"])
        except Exception:
            row["metadata"] = {}
    elif row.get("metadata") is None:
        row["metadata"] = {}
    return row


class SessionMemoryMixin:
    """Methods for session artifact storage (DuckDB)."""

    async def store_artifact(
        self,
        content: str,
        artifact_type: ArtifactType = ArtifactType.RAW,
        task_id: str | None = None,
        metadata: dict | None = None,
        session_id: str | None = None,
        auto_compress: bool = True,
    ) -> Artifact:
        """
        Persist an output artifact to Session Memory (DuckDB).

        Before writing, runs semantic deduplication: if the new content is
        near-identical (cosine similarity > 0.92) to a recent artifact, the
        new content is merged into the existing artifact instead of creating
        a duplicate.

        After writing, optionally triggers compression if the session
        has grown beyond the configured threshold.

        Returns the stored Artifact (with its assigned id).  When dedup merges
        into an existing artifact, the *existing* artifact is returned.
        """
        if not self._connected:
            logger.warning("store_artifact called before connect() — call connect() first")
        sid = session_id or self.session_id

        # ── Semantic deduplication ────────────────────────────────────
        # Check the last N artifacts for near-duplicates before inserting.
        # This prevents the session from accumulating redundant entries
        # (e.g. repeated micro-loop outputs for the same sub-problem).
        existing = await self._check_semantic_duplicate(content, sid)
        if existing is not None:
            return existing

        artifact = Artifact(
            content=content,
            artifact_type=artifact_type,
            session_id=sid,
            task_id=task_id,
            metadata=metadata or {},
        )
        await self._duckdb.insert_artifact(artifact)
        logger.debug("Stored artifact %s (type=%s)", artifact.id, artifact_type.value)

        if auto_compress:
            await self._compressor.maybe_compress(sid)

        return artifact

    async def _check_semantic_duplicate(
        self,
        new_content: str,
        session_id: str,
    ) -> Artifact | None:
        """
        Check if *new_content* is semantically near-identical to any of the
        last ``_DEDUP_RECENT_WINDOW`` artifacts in the session.

        How it works
        ------------
        1. Fetch the N most recent (non-archived) artifacts from DuckDB.
        2. Embed the new content and each recent artifact's content.
        3. Compute cosine similarity between the new embedding and each
           existing embedding.
        4. If any similarity exceeds ``_DEDUP_SIMILARITY_THRESHOLD``, merge
           the new content into the existing artifact (append a separator
           and the new text) and return the updated Artifact.
        5. Otherwise return ``None`` to signal "no duplicate found".

        Returns
        -------
        Artifact or None
            The merged artifact if dedup triggered, otherwise None.
        """
        try:
            recent_rows = await self._duckdb.get_recent(
                session_id, limit=_DEDUP_RECENT_WINDOW, include_archived=False
            )
            if not recent_rows:
                return None

            # Embed the new content
            new_emb = await self._embeddings.embed(new_content)

            for row in recent_rows:
                existing_content = str(row.get("content", ""))
                if not existing_content:
                    continue

                existing_emb = await self._embeddings.embed(existing_content)
                similarity = _cosine_similarity(new_emb, existing_emb)

                if similarity > _DEDUP_SIMILARITY_THRESHOLD:
                    # Merge: append the new content to the existing artifact
                    merged_content = (
                        f"{existing_content}\n\n"
                        f"--- [merged duplicate, similarity={similarity:.3f}] ---\n\n"
                        f"{new_content}"
                    )
                    row_id = row["id"]
                    logger.info(
                        "[dedup] Merging new artifact into existing %s "
                        "(cosine_similarity=%.3f > %.3f). "
                        "Duplicate content appended instead of creating new entry.",
                        row_id,
                        similarity,
                        _DEDUP_SIMILARITY_THRESHOLD,
                    )

                    # Update the existing artifact in DuckDB with merged content.
                    # We reconstruct an Artifact with the same ID and upsert it.
                    _parse_row_metadata(row)
                    merged_artifact = Artifact(
                        content=merged_content,
                        artifact_type=ArtifactType(row.get("artifact_type", "raw")),
                        session_id=session_id,
                        task_id=row.get("task_id"),
                        metadata=row.get("metadata", {}),
                    )
                    # Preserve the original artifact's ID so the upsert
                    # replaces it rather than creating a new row.
                    merged_artifact.id = row_id
                    await self._duckdb.insert_artifact(merged_artifact)
                    return merged_artifact

        except Exception as exc:
            # Dedup is best-effort — if embeddings fail or anything goes wrong,
            # fall through and store the artifact normally.
            logger.debug("[dedup] Semantic dedup check failed (non-fatal): %s", exc)

        return None

    async def get_artifact(self, artifact_id: str) -> Artifact | None:
        """Retrieve an artifact by its UUID. Returns None if not found."""
        row = await self._duckdb.get_artifact(artifact_id)
        if not row:
            return None
        return Artifact.from_dict(_parse_row_metadata(row))

    async def get_recent_artifacts(
        self,
        artifact_type: ArtifactType | None = None,
        limit: int = 20,
        include_archived: bool = False,
        session_id: str | None = None,
    ) -> list[Artifact]:
        """
        Return the most recent artifacts for the current (or specified) session.

        Optionally filter by ArtifactType.
        """
        sid = session_id or self.session_id
        type_val = artifact_type.value if artifact_type else None
        rows = await self._duckdb.get_recent(
            sid, artifact_type=type_val, limit=limit, include_archived=include_archived
        )
        return [Artifact.from_dict(_parse_row_metadata(row)) for row in rows]

    async def get_artifacts_by_task_ids(
        self,
        task_ids: list,
        limit_each: int = 2,
    ) -> list[dict]:
        """
        Return raw artifact dicts for a batch of task_ids in one DB round-trip.

        Used by the meso loop to supplement the subsystem-tag search with
        a targeted lookup of artifacts from known micro-loop task IDs.  This
        catches any artifacts whose subsystem metadata was missing or mismatched
        but whose task_id correctly identifies them as part of the batch.

        Returns plain dicts (not Artifact objects) so the meso loop can merge
        them directly with the dicts returned by ``get_artifacts()``.

        Parameters
        ----------
        task_ids   : List of task UUID strings from recent MicroLoopRecords.
        limit_each : Max artifacts per task_id (default 2).
        """
        if not task_ids:
            return []
        rows = await self._duckdb.get_by_task_ids(task_ids, limit_each=limit_each)
        return [_parse_row_metadata(row) for row in rows]

    async def get_artifacts_by_task(
        self,
        task_id: str,
        limit: int = 5,
    ) -> list[Artifact]:
        """Return artifacts stored under a specific task_id, newest first."""
        rows = await self._duckdb.get_by_task_id(task_id, limit=limit)
        return [Artifact.from_dict(_parse_row_metadata(row)) for row in rows]

    async def count_artifacts(
        self, session_id: str | None = None, include_archived: bool = False
    ) -> int:
        sid = session_id or self.session_id
        return await self._duckdb.count_session_artifacts(sid, include_archived)
