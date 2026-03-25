"""
Base Tool interface — tools/base.py
=====================================

What this file does
--------------------
This file defines the building blocks that every Tinker tool is made from.
There are three things here:

  1. ``ToolResult``  — a standardised "response envelope" that every tool
                       call returns, regardless of which tool was called.
  2. ``ToolSchema``  — a small metadata object that describes a tool to the
                       outside world (name, description, inputs, outputs).
  3. ``BaseTool``    — an abstract base class that every tool must inherit from.
                       It defines what a tool *is* and provides the timing +
                       error-handling wrapper for free.

Why it exists
-------------
Without a shared base, each tool could return data in a completely different
format — one might return a list, another a dict, another raise an exception on
failure.  The caller would need to know about every tool's quirks.

By defining ``ToolResult`` here, we guarantee that no matter which tool you
call, you always get back an object with the same fields: ``success``,
``tool_name``, ``data``, ``error``, ``duration_ms``, ``metadata``.

By defining ``BaseTool`` here, we guarantee that every tool:
  - Has a ``schema`` property that describes it.
  - Has an ``execute(**kwargs)`` method that always returns a ``ToolResult``.
  - Automatically measures how long it took to run.
  - Automatically catches any exception and wraps it in an error ``ToolResult``
    instead of crashing the whole program.

How it fits into Tinker
-----------------------
Every tool in this package (WebSearchTool, WebScraperTool, etc.) is a subclass
of ``BaseTool``.  The ToolRegistry works with ``BaseTool`` objects and returns
``ToolResult`` objects.  The Orchestrator inspects ``ToolSchema`` objects to
build the AI's system prompt.  All three classes here are fundamental.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass
class ToolResult:
    """
    The standardised return envelope for every tool call.

    Think of this like a delivery package.  Every tool wraps its result in
    the same box with the same labels.  The caller (the ToolRegistry or the
    Orchestrator) can always read the same fields without caring which tool
    produced the result.

    Fields
    ------
    success : bool
        True if the tool ran without errors; False otherwise.
        Always check this before using ``data``.

    tool_name : str
        The name of the tool that produced this result (e.g. "web_search").
        Useful for logging and debugging.

    data : Any
        The actual payload the tool produced.  What this contains depends on
        the tool:
          - WebSearchTool returns a list of dicts (title, url, snippet, ...).
          - WebScraperTool returns a dict (url, title, text, word_count, ...).
          - ArtifactWriterTool returns a dict (artifact_id, file_path, ...).
        If success=False, data is None.

    error : str | None
        If success=False, this contains a human-readable error message in the
        format "ExceptionType: message".  None if success=True.

    duration_ms : float
        How many milliseconds the tool took to run.  Useful for spotting slow
        tools (e.g. a web scrape that took 18 seconds).

    metadata : dict
        A flexible dict for any extra information a tool wants to attach.
        Currently unused by most tools, but available for future use.
    """

    success: bool
    tool_name: str
    data: Any  # The actual payload
    error: str | None = None
    duration_ms: float = 0.0
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """
        Convert this ToolResult to a plain Python dict.

        Useful when you need to serialise the result to JSON, log it, or pass
        it to code that expects a plain dict rather than a dataclass object.

        The duration_ms is rounded to 2 decimal places to keep logs tidy.
        """
        return {
            "success": self.success,
            "tool_name": self.tool_name,
            "data": self.data,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 2),
            "metadata": self.metadata,
        }


@dataclass
class ToolSchema:
    """
    A descriptor that explains what a tool is and how to use it.

    The Orchestrator reads these schemas to build the AI's system prompt —
    essentially telling the model "here are the tools you can call and what
    they need as input."

    Fields
    ------
    name : str
        The unique identifier for this tool (e.g. "web_search").
        This is the string the Orchestrator passes to ``registry.execute()``.

    description : str
        A plain-English explanation of what the tool does.
        The AI model reads this to decide WHEN to use the tool.
        Write it as if explaining to a smart person who has never seen the tool.

    parameters : dict
        A JSON Schema object (draft-07 style) describing the tool's inputs.
        Example:
            {
              "type": "object",
              "properties": {
                "query": {"type": "string", "description": "The search query"}
              },
              "required": ["query"]
            }
        The AI uses this to know which arguments to pass when calling the tool.

    returns : str
        A human-readable description of what the tool gives back.
        Example: "List of dicts: [{title, url, snippet}] sorted by relevance."
    """

    name: str
    description: str
    parameters: dict  # JSON-Schema object describing the input
    returns: str  # Human-readable description of the return value


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class BaseTool(ABC):
    """
    The abstract base class that every Tinker tool must inherit from.

    If you want to create a new tool, you subclass this and implement two things:

      1. The ``schema`` property — returns a ``ToolSchema`` describing your tool.
      2. The ``_execute(**kwargs)`` method — contains the actual tool logic.

    You do NOT override ``execute()`` — that's the public entry point that the
    ToolRegistry calls, and it already handles timing and error catching for you.

    Here is a minimal example of a custom tool:

        class GreetingTool(BaseTool):

            @property
            def schema(self) -> ToolSchema:
                return ToolSchema(
                    name="greet",
                    description="Say hello to a person.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Person's name"}
                        },
                        "required": ["name"],
                    },
                    returns="A greeting string.",
                )

            async def _execute(self, name: str, **_) -> str:
                return f"Hello, {name}!"

    Once registered, you can call it like any other tool:
        result = await registry.execute("greet", name="Alice")
        print(result.data)  # "Hello, Alice!"
    """

    @property
    @abstractmethod
    def schema(self) -> ToolSchema:
        """
        Return the static schema (metadata) for this tool.

        This is an "abstract property" — every subclass MUST implement it.
        If you forget to implement it, Python will raise a TypeError when you
        try to instantiate your class.

        The schema should be returned as a constant — it describes what the
        tool IS, not its current state, so it shouldn't change between calls.
        """
        ...

    @abstractmethod
    async def _execute(self, **kwargs) -> Any:
        """
        Implement the actual tool logic here.

        This is an "abstract method" — every subclass MUST implement it.

        Rules for implementing _execute:
          - It must be ``async`` (use ``await`` for any I/O operations like
            HTTP requests, file reads, subprocess calls).
          - Accept **kwargs to capture any arguments passed by the caller.
          - Return the raw payload — whatever data the tool produces.
            The return value becomes ``ToolResult.data``.
          - If something goes wrong, just raise an exception.
            The ``execute()`` wrapper (below) will catch it and turn it into
            a ToolResult with success=False — you don't need to handle errors
            here yourself.
        """
        ...

    async def execute(self, **kwargs) -> ToolResult:
        """
        Public entry point — run the tool and return a ``ToolResult``.

        This method is called by the ToolRegistry.  You should NOT override
        it in your subclass — override ``_execute()`` instead.

        What this method does automatically, for free:
          1. Records the start time.
          2. Calls ``_execute(**kwargs)`` — your actual tool logic.
          3. If _execute succeeds, wraps the return value in a success ToolResult.
          4. If _execute raises ANY exception, catches it and wraps it in a
             failure ToolResult with the exception type and message in ``error``.
          5. In both cases, calculates how many milliseconds elapsed and
             stores that in ``duration_ms``.

        This pattern is called a "template method" — the base class defines
        the structure of the operation, and subclasses fill in the details.

        Args:
            **kwargs: Passed through to ``_execute()``.

        Returns:
            ToolResult with success=True and data set, or success=False and
            error set.  Never raises.
        """
        # Record the time before we start, using monotonic() which is immune
        # to clock adjustments (safer than time.time() for measuring durations).
        t0 = time.monotonic()
        try:
            # Delegate to the subclass's actual implementation.
            data = await self._execute(**kwargs)
            duration = (time.monotonic() - t0) * 1000  # convert seconds → milliseconds
            return ToolResult(
                success=True,
                tool_name=self.schema.name,
                data=data,
                duration_ms=duration,
            )
        except Exception as exc:  # noqa: BLE001
            # BLE001 is a linting rule that discourages bare "except Exception".
            # We silence it here because catching everything is intentional —
            # we want the registry to NEVER crash, no matter what a tool does.
            duration = (time.monotonic() - t0) * 1000
            return ToolResult(
                success=False,
                tool_name=self.schema.name,
                data=None,
                # Include the exception class name so callers can tell what went wrong
                # without needing to re-raise and inspect a traceback.
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=duration,
            )
