"""
Tinker Tool Layer
=================
Provides the ToolRegistry and all built-in research tools.

Quick start:
    from tool_layer import build_default_registry

    registry = build_default_registry()
    result = await registry.execute("web_search", query="event sourcing")
"""

from .registry import ToolRegistry, build_default_registry
from .tools.base import BaseTool, ToolResult, ToolSchema

__all__ = [
    "ToolRegistry",
    "build_default_registry",
    "BaseTool",
    "ToolResult",
    "ToolSchema",
]
