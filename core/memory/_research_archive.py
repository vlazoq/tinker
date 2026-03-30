"""
_research_archive.py — ResearchArchiveMixin: ChromaDB-backed semantic research storage.

Split from manager.py to keep each memory layer in its own focused module.
"""

from __future__ import annotations

import logging

from .schemas import ResearchNote

logger = logging.getLogger(__name__)


class ResearchArchiveMixin:
    """Methods for research note storage and semantic search (ChromaDB)."""

    async def store_research(
        self,
        content: str,
        topic: str,
        tags: list[str] | None = None,
        source: str = "tinker-internal",
        task_id: str | None = None,
        metadata: dict | None = None,
        session_id: str | None = None,
    ) -> ResearchNote:
        """
        Embed and store a research note in the Research Archive (ChromaDB).

        Notes are semantically searchable across all sessions.
        Returns the stored ResearchNote (with its assigned id).
        """
        sid = session_id or self.session_id
        note = ResearchNote(
            content=content,
            topic=topic,
            source=source,
            tags=tags or [],
            session_id=sid,
            task_id=task_id,
            metadata=metadata or {},
        )
        embedding = await self._embeddings.embed(content)
        await self._chroma.upsert(
            doc_id=note.id,
            document=note.content,
            embedding=embedding,
            metadata=note.to_chroma_metadata(),
        )
        logger.debug("Stored research note %s (topic=%s)", note.id, topic)
        return note

    async def search_research(
        self,
        query: str,
        n_results: int = 5,
        filter_topic: str | None = None,
        filter_session: str | None = None,
    ) -> list[ResearchNote]:
        """
        Semantic search over the Research Archive.

        Parameters
        ----------
        query          : natural-language search string
        n_results      : max number of results
        filter_topic   : restrict to a specific topic (exact match)
        filter_session : restrict to notes from a specific session
        """
        embedding = await self._embeddings.embed(query)

        where: dict = {}
        if filter_topic:
            where["topic"] = {"$eq": filter_topic}
        if filter_session:
            where["session_id"] = {"$eq": filter_session}

        results = await self._chroma.query(
            embedding=embedding,
            n_results=n_results,
            where=where if where else None,
        )
        return [ResearchNote.from_chroma(r["id"], r["document"], r["metadata"]) for r in results]

    async def get_research(self, note_id: str) -> ResearchNote | None:
        """Retrieve a specific research note by its ID."""
        result = await self._chroma.get_by_id(note_id)
        if not result:
            return None
        return ResearchNote.from_chroma(result["id"], result["document"], result["metadata"])

    async def count_research_notes(self) -> int:
        return await self._chroma.count()
