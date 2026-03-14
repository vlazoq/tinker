"""
Memory Query Tool
Wraps the Tinker MemoryManager's semantic search interface.
Allows the Researcher to retrieve relevant past research from the archive.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .base import BaseTool, ToolSchema


# ---------------------------------------------------------------------------
# Protocol — matches whatever MemoryManager exposes
# ---------------------------------------------------------------------------

@runtime_checkable
class MemoryManagerProtocol(Protocol):
    """Minimal interface the MemoryManager must implement."""

    async def search(
        self,
        query: str,
        top_k: int = 10,
        filters: dict | None = None,
    ) -> list[dict]:
        """Return top-k memory records relevant to query."""
        ...

    async def store(self, record: dict) -> str:
        """Persist a record and return its memory ID."""
        ...


# ---------------------------------------------------------------------------
# Stub used when no real MemoryManager is wired in (e.g., during testing)
# ---------------------------------------------------------------------------

class _StubMemoryManager:
    """Placeholder that returns empty results and logs a warning."""

    async def search(
        self,
        query: str,
        top_k: int = 10,
        filters: dict | None = None,
    ) -> list[dict]:
        import warnings
        warnings.warn(
            "MemoryQueryTool is using the stub MemoryManager — no results returned. "
            "Pass a real MemoryManager instance to MemoryQueryTool().",
            stacklevel=2,
        )
        return []

    async def store(self, record: dict) -> str:
        return "stub-id"


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

class MemoryQueryTool(BaseTool):
    """Semantic search over Tinker's Research Archive via the MemoryManager."""

    def __init__(self, memory_manager: MemoryManagerProtocol | None = None) -> None:
        self._mm: MemoryManagerProtocol = memory_manager or _StubMemoryManager()  # type: ignore[assignment]

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="memory_query",
            description=(
                "Perform a semantic search over the Tinker Research Archive to retrieve "
                "past research notes, architecture analyses, and source summaries that are "
                "semantically similar to the given query. Returns ranked memory records."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Natural-language query describing what you're looking for. "
                            "Example: 'event sourcing patterns in distributed systems'."
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of results to return (1-50). Default 10.",
                        "default": 10,
                        "minimum": 1,
                        "maximum": 50,
                    },
                    "filters": {
                        "type": "object",
                        "description": (
                            "Optional metadata filters. Supported keys: "
                            "artifact_type, task_id, tags (list), date_after (ISO-8601)."
                        ),
                        "properties": {
                            "artifact_type": {"type": "string"},
                            "task_id": {"type": "string"},
                            "tags": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "date_after": {
                                "type": "string",
                                "description": "ISO-8601 date string. Only return records after this date.",
                            },
                        },
                        "additionalProperties": False,
                    },
                },
                "required": ["query"],
            },
            returns=(
                "List of dicts: [{memory_id, score, title, artifact_type, task_id, "
                "created_at, tags, snippet}] sorted by relevance."
            ),
        )

    # ------------------------------------------------------------------
    # Implementation
    # ------------------------------------------------------------------

    async def _execute(           # type: ignore[override]
        self,
        query: str,
        top_k: int = 10,
        filters: dict | None = None,
        **_: Any,
    ) -> list[dict]:
        results = await self._mm.search(
            query=query,
            top_k=top_k,
            filters=filters,
        )

        # Normalise results — ensure expected keys are present
        normalised = []
        for r in results:
            normalised.append(
                {
                    "memory_id": r.get("memory_id", r.get("id", "")),
                    "score": round(float(r.get("score", 0.0)), 4),
                    "title": r.get("title", ""),
                    "artifact_type": r.get("artifact_type", ""),
                    "task_id": r.get("task_id", ""),
                    "created_at": r.get("created_at", ""),
                    "tags": r.get("tags", []),
                    "snippet": r.get("snippet", r.get("text", "")[:300]),
                }
            )
        return normalised
