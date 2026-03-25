"""
Tinker Tool Layer — tools/__init__.py
======================================

What this file does
--------------------
This is the "front door" of the tools package. When any other part of Tinker
writes ``from core.tools import ...``, Python runs this file first. Its job is
simple: collect the most important names from the sub-modules in this package
and make them available in one place, so callers don't have to remember which
sub-file each class lives in.

Why it exists
-------------
Without this file, a caller would have to write:

    from core.tools.registry import ToolRegistry, build_default_registry
    from core.tools.base import BaseTool, ToolResult, ToolSchema

With this file they can simply write:

    from core.tools import ToolRegistry, build_default_registry, BaseTool

That's a small convenience, but it also means that if we ever reorganise the
internals (e.g. move ToolRegistry to a different file), callers don't break —
we just update the import lines here.

How it fits into Tinker
-----------------------
The Orchestrator (the "brain" that coordinates all agents) creates a
ToolRegistry via ``build_default_registry()``.  It then passes the registry
to the Researcher agent, which uses it to search the web, scrape pages, query
memory, write artifacts, and generate diagrams — all through the same uniform
``registry.execute("tool_name", **kwargs)`` interface.

Quick start example
-------------------
    from core.tools import build_default_registry

    registry = build_default_registry()
    result = await registry.execute("web_search", query="event sourcing")
    # result is a ToolResult object: result.success, result.data, result.error
"""

# Pull in the registry class and its factory function.
# ToolRegistry is the central hub that holds all tools and dispatches calls.
# build_default_registry() is the one-liner that wires everything together.
from .registry import ToolRegistry, build_default_registry

# Pull in the base classes every tool is built on.
# BaseTool   — the abstract class all individual tools inherit from.
# ToolResult — the standard return type every tool call produces.
# ToolSchema — a small data object describing a tool's name, inputs, and outputs.
from .base import BaseTool, ToolResult, ToolSchema

# __all__ controls what gets exported when someone writes "from core.tools import *".
# Listing things here is also useful documentation: it says "these are the
# public names you're meant to use; everything else is an internal detail."
__all__ = [
    "ToolRegistry",  # The central registry and dispatcher
    "build_default_registry",  # Factory that creates a ready-to-use registry
    "BaseTool",  # Base class for every tool
    "ToolResult",  # Standardised return envelope for tool calls
    "ToolSchema",  # Metadata descriptor for a tool
]
