"""
Memory Query Tool — tools/memory_query.py
==========================================

What this file does
--------------------
This file defines ``MemoryQueryTool``, which lets the Researcher agent search
Tinker's **Research Archive** — a store of everything Tinker has learned in
previous research loops.

The Research Archive contains things like:
  - Research notes from earlier Researcher loops
  - Architecture design proposals from the Architect agent
  - Critique artifacts from the Critic agent
  - Synthesis documents from the Synthesizer agent

Searching the archive is like asking "have we already looked into this?" before
going to the web.  If a relevant artifact is in memory, the Researcher can
reuse it instead of repeating a web search.

Why it exists
-------------
Without memory, each Tinker loop starts from scratch and re-discovers things it
already knows.  The memory system gives Tinker continuity: insights from one
loop can inform the next.

How it fits into Tinker
-----------------------
The MemoryManager (a separate component, not in this file) is responsible for
actually storing and indexing artifacts.  This tool is a thin wrapper around
the MemoryManager's search interface, exposing it as a callable "tool" that
the Researcher agent can use through the ToolRegistry.

The search is "semantic" — it uses vector embeddings (numerical representations
of meaning) to find artifacts that are *conceptually similar* to the query,
even if they don't use the same words.  For example, searching for "event
streaming" might find an artifact about "Kafka and pub-sub patterns" because
they're semantically close.

Dependency injection
--------------------
The MemoryManager is passed into this tool at construction time via the
``memory_manager`` parameter.  If none is provided, a stub is used instead.
This is called "dependency injection" — instead of the tool creating its own
MemoryManager, it accepts one from outside.  This makes testing easy (you can
pass a fake MemoryManager that returns predetermined results).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .base import BaseTool, ToolSchema

# ---------------------------------------------------------------------------
# Protocol — matches whatever MemoryManager exposes
# ---------------------------------------------------------------------------


@runtime_checkable
class MemoryManagerProtocol(Protocol):
    """
    A "Protocol" that defines the interface any MemoryManager must implement.

    What is a Protocol?
    -------------------
    A Python Protocol is like an interface in other languages (Java, TypeScript).
    It says: "any class that has these methods can be used here, regardless of
    what class it actually is."

    We use a Protocol here instead of a concrete class because the real
    MemoryManager lives in a separate part of Tinker and we don't want to
    create a hard import dependency.  Any object with ``search()`` and
    ``store()`` methods will satisfy this Protocol.

    ``@runtime_checkable`` means we can use ``isinstance(obj, MemoryManagerProtocol)``
    at runtime to check if an object satisfies the Protocol.

    The real MemoryManager (p2) exposes ``search_research()`` with slightly
    different parameters; the ``search()`` adapter shim added to manager.py
    satisfies this Protocol.
    """

    async def search(
        self,
        query: str,
        top_k: int = 10,
        filters: dict | None = None,
    ) -> list[dict]:
        """
        Perform a semantic search over stored artifacts.

        Args:
            query:   Natural-language description of what you're looking for.
            top_k:   Maximum number of results to return.
            filters: Optional metadata filters (e.g. only return artifacts
                     of a certain type, or from a specific task).

        Returns:
            A list of dicts, each representing a matching artifact,
            sorted from most to least relevant.
        """
        ...

    async def store(self, record: dict) -> str:
        """
        Persist an artifact in the memory archive.

        Args:
            record: The artifact dict to store.

        Returns:
            A unique memory ID string for the stored record.
        """
        ...


# ---------------------------------------------------------------------------
# Stub used when no real MemoryManager is wired in (e.g., during testing)
# ---------------------------------------------------------------------------


class _StubMemoryManager:
    """
    A placeholder MemoryManager used when no real one is provided.

    This class exists so that MemoryQueryTool can be instantiated without
    crashing, even in environments where the full MemoryManager isn't set up
    (e.g. during unit tests, or when running a minimal Tinker configuration).

    Instead of raising an error, it returns empty results and emits a Python
    warning to alert the developer that they probably need to wire up a real
    MemoryManager for production use.

    The underscore prefix in ``_StubMemoryManager`` is a Python convention
    meaning "this is an internal detail — don't use it directly from outside
    this module."
    """

    async def search(
        self,
        query: str,
        top_k: int = 10,
        filters: dict | None = None,
    ) -> list[dict]:
        """Return empty results and warn that the stub is being used."""
        import warnings

        warnings.warn(
            "MemoryQueryTool is using the stub MemoryManager — no results returned. "
            "Pass a real MemoryManager instance to MemoryQueryTool().",
            stacklevel=2,
        )
        return []  # always empty — the stub can't actually search anything

    async def store(self, record: dict) -> str:
        """Accept a record but don't actually store it anywhere."""
        return "stub-id"  # return a fake ID to satisfy callers that expect a string


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class MemoryQueryTool(BaseTool):
    """
    Semantic search over Tinker's Research Archive via the MemoryManager.

    This tool wraps the MemoryManager's search interface and normalises the
    results into a consistent format.  It also handles the case where different
    MemoryManager implementations might use slightly different field names.

    Typical usage by the Researcher agent:
        # Find past research relevant to a question
        result = await registry.execute(
            "memory_query",
            query="event sourcing patterns in distributed systems",
            top_k=5,
            filters={"artifact_type": "research_note"},
        )
        for item in result.data:
            print(item["title"], item["score"])
    """

    def __init__(self, memory_manager: MemoryManagerProtocol | None = None) -> None:
        """
        Initialise the memory query tool.

        Args:
            memory_manager:
                The MemoryManager instance to search.  Must have a ``search()``
                async method.  If None, a stub is used that returns empty
                results and logs a warning.
        """
        # Use the provided memory_manager, or fall back to the stub if None.
        # The "# type: ignore" comment suppresses a mypy type-checker warning —
        # the stub satisfies the protocol at runtime even if mypy doesn't see it.
        self._mm: MemoryManagerProtocol = memory_manager or _StubMemoryManager()  # type: ignore[assignment]

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    @property
    def schema(self) -> ToolSchema:
        """
        Describe this tool to the ToolRegistry and the AI model.

        The ``filters`` parameter is particularly useful for narrowing searches.
        For example, if the Researcher only wants to find past research notes
        (not architect proposals), it can pass ``filters={"artifact_type": "research_note"}``.
        """
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

    async def _execute(  # type: ignore[override]
        self,
        query: str,
        top_k: int = 10,
        filters: dict | None = None,
        **_: Any,  # absorb any unexpected kwargs
    ) -> list[dict]:
        """
        Search the memory archive and return normalised results.

        The search is delegated entirely to the MemoryManager.  This method's
        main job is normalisation: different MemoryManager implementations may
        use slightly different field names (e.g. "id" vs "memory_id", or "text"
        vs "snippet").  We standardise them here so the Researcher agent always
        sees the same structure.

        Args:
            query:   Natural-language search query.
            top_k:   Maximum number of results to return.
            filters: Optional metadata filters passed through to the MemoryManager.
            **_:     Ignored extra arguments.

        Returns:
            A list of normalised dicts with guaranteed keys:
            memory_id, score, title, artifact_type, task_id, created_at, tags, snippet.
        """
        # Delegate the actual search to the MemoryManager.
        results = await self._mm.search(
            query=query,
            top_k=top_k,
            filters=filters,
        )

        # Normalise results — ensure expected keys are present
        # regardless of which MemoryManager implementation returned the data.
        normalised = []
        for r in results:
            normalised.append(
                {
                    # Some implementations use "id", others use "memory_id" — handle both.
                    "memory_id": r.get("memory_id", r.get("id", "")),
                    # Round the relevance score to 4 decimal places for readability.
                    "score": round(float(r.get("score", 0.0)), 4),
                    "title": r.get("title", ""),
                    "artifact_type": r.get("artifact_type", ""),
                    "task_id": r.get("task_id", ""),
                    "created_at": r.get("created_at", ""),
                    "tags": r.get("tags", []),
                    # Some implementations use "snippet", others use "text".
                    # We take whichever exists, and cap text at 300 chars to keep
                    # the result compact (the full artifact can be fetched separately
                    # if needed).
                    "snippet": r.get("snippet", r.get("text", "")[:300]),
                }
            )
        return normalised
