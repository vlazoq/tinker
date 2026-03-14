"""
ToolRegistry
Central registry and dispatcher for all Tinker Tool Layer tools.
The Orchestrator uses this to list available tools, retrieve schemas,
and invoke tools by name.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .tools.base import BaseTool, ToolResult, ToolSchema

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Maintains a named collection of tools and dispatches calls."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, tool: BaseTool) -> "ToolRegistry":
        """Register a tool instance. Returns self for chaining."""
        name = tool.schema.name
        if name in self._tools:
            logger.warning("Overwriting existing tool: %s", name)
        self._tools[name] = tool
        logger.debug("Registered tool: %s", name)
        return self

    def register_many(self, *tools: BaseTool) -> "ToolRegistry":
        for tool in tools:
            self.register(tool)
        return self

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    @property
    def tool_names(self) -> list[str]:
        return sorted(self._tools.keys())

    def get_tool(self, name: str) -> BaseTool:
        if name not in self._tools:
            raise KeyError(f"No tool registered with name '{name}'.")
        return self._tools[name]

    def schemas(self) -> list[ToolSchema]:
        """Return all tool schemas (useful for building the model's system prompt)."""
        return [t.schema for t in self._tools.values()]

    def schemas_as_json(self, indent: int = 2) -> str:
        """Serialise all schemas to a JSON string for injection into prompts."""
        payload = []
        for schema in self.schemas():
            payload.append(
                {
                    "name": schema.name,
                    "description": schema.description,
                    "parameters": schema.parameters,
                    "returns": schema.returns,
                }
            )
        return json.dumps(payload, indent=indent, ensure_ascii=False)

    def schemas_as_tool_list(self) -> list[dict]:
        """
        Return schemas in Ollama / OpenAI function-calling format.
        The Orchestrator can pass this directly to the model.
        """
        tools = []
        for schema in self.schemas():
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": schema.name,
                        "description": schema.description,
                        "parameters": schema.parameters,
                    },
                }
            )
        return tools

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(self, tool_name: str, **kwargs: Any) -> ToolResult:
        """
        Execute a tool by name and return a structured ToolResult.
        Never raises — all errors are captured inside the ToolResult.
        """
        try:
            tool = self.get_tool(tool_name)
        except KeyError as exc:
            return ToolResult(
                success=False,
                tool_name=tool_name,
                data=None,
                error=str(exc),
            )

        logger.info("Executing tool '%s' with args: %s", tool_name, list(kwargs.keys()))
        result = await tool.execute(**kwargs)

        if result.success:
            logger.debug("Tool '%s' succeeded in %.1f ms", tool_name, result.duration_ms)
        else:
            logger.warning(
                "Tool '%s' failed in %.1f ms: %s",
                tool_name,
                result.duration_ms,
                result.error,
            )
        return result

    async def execute_from_model_call(self, tool_call: dict) -> ToolResult:
        """
        Convenience method: accepts a model tool_call dict in OpenAI/Ollama format:
        {
          "name": "web_search",
          "arguments": {"query": "event sourcing patterns"}
        }
        Parses and dispatches automatically.
        """
        name = tool_call.get("name", "")
        arguments = tool_call.get("arguments", {})

        if isinstance(arguments, str):
            # Some models return arguments as a JSON string
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}

        return await self.execute(name, **arguments)

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"ToolRegistry(tools={self.tool_names})"


# ---------------------------------------------------------------------------
# Factory — build the default registry with all standard tools
# ---------------------------------------------------------------------------

def build_default_registry(
    searxng_url: str | None = None,
    artifact_output_dir: str | None = None,
    diagram_output_dir: str | None = None,
    memory_manager=None,          # MemoryManagerProtocol | None
) -> ToolRegistry:
    """
    Instantiate and register all built-in Tinker tools.

    Parameters override env-var defaults when provided.
    """
    from .tools.web_search import WebSearchTool
    from .tools.web_scraper import WebScraperTool
    from .tools.artifact_writer import ArtifactWriterTool
    from .tools.diagram_generator import DiagramGeneratorTool
    from .tools.memory_query import MemoryQueryTool

    kwargs: dict[str, Any] = {}
    if searxng_url:
        kwargs["searxng_url"] = searxng_url

    search_tool = WebSearchTool(**kwargs)
    scraper_tool = WebScraperTool()
    writer_tool = (
        ArtifactWriterTool(output_dir=artifact_output_dir)
        if artifact_output_dir
        else ArtifactWriterTool()
    )
    diagram_tool = (
        DiagramGeneratorTool(output_dir=diagram_output_dir)
        if diagram_output_dir
        else DiagramGeneratorTool()
    )
    memory_tool = MemoryQueryTool(memory_manager=memory_manager)

    registry = ToolRegistry()
    registry.register_many(
        search_tool,
        scraper_tool,
        writer_tool,
        diagram_tool,
        memory_tool,
    )
    return registry
