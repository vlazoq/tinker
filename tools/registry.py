"""
ToolRegistry — tools/registry.py
==================================

What this file does
--------------------
This file defines the ``ToolRegistry`` class, which acts as a phone book and
switchboard for every tool Tinker can use.  You register tools with a name,
and later you (or the AI) can call them by that name.  The registry finds the
right tool, runs it, and returns the result.

Why it exists
-------------
Without a registry, the Orchestrator would need to know about every individual
tool class (WebSearchTool, WebScraperTool, etc.) and import them all directly.
That would tightly couple the Orchestrator to the tools, making it hard to add
or remove tools later.

Instead, the Orchestrator only ever talks to the ToolRegistry.  The registry
knows about the tools; the Orchestrator only knows about the registry.  This is
the classic "service locator" design pattern.

How it fits into Tinker
-----------------------
The Orchestrator calls ``build_default_registry()`` at startup to get a
fully-wired registry.  During each research loop the Orchestrator calls
``registry.execute("web_search", query="...")`` (or similar) and the registry
handles everything: looking up the tool, running it asynchronously, timing it,
catching exceptions, and returning a uniform ``ToolResult``.

The AI model itself can also trigger tool calls.  When the model returns a
structured "function call" dict (as used by OpenAI/Ollama APIs),
``execute_from_model_call()`` parses that dict and dispatches it automatically.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .base import BaseTool, ToolResult, ToolSchema
from exceptions import ToolNotFoundError

# Standard Python logging.  Log messages from this file will appear under the
# logger name "tools.registry", making it easy to filter in log output.
logger = logging.getLogger(__name__)


class ToolRegistry:
    """
    A named collection of tools with a unified execute interface.

    Think of this like a kitchen with labelled drawers.  Each drawer (tool) has
    a name.  The chef (Orchestrator) just says "get me the whisk" — it doesn't
    need to know where the whisk is kept or how it was made.

    Typical usage
    -------------
        registry = ToolRegistry()
        registry.register(WebSearchTool())
        registry.register(WebScraperTool())

        result = await registry.execute("web_search", query="microservices")
        if result.success:
            print(result.data)

    Or, using the factory shortcut that creates everything at once:

        registry = build_default_registry()
    """

    def __init__(self) -> None:
        # Internal dictionary: tool name (string) → tool instance (BaseTool).
        # We use a dict so we can look tools up by name in O(1) time.
        self._tools: dict[str, BaseTool] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, tool: BaseTool) -> "ToolRegistry":
        """
        Add a tool to the registry.

        The tool's name comes from ``tool.schema.name`` — each tool declares
        its own name in its schema, so we don't need to pass it separately.

        If a tool with the same name was already registered, we overwrite it
        and log a warning (this is usually a mistake, but sometimes intentional
        during testing when you want to swap in a fake tool).

        Returns ``self`` so you can chain calls:
            registry.register(ToolA()).register(ToolB()).register(ToolC())
        """
        name = tool.schema.name
        if name in self._tools:
            # Overwriting is allowed but suspicious — warn the developer.
            logger.warning("Overwriting existing tool: %s", name)
        self._tools[name] = tool
        logger.debug("Registered tool: %s", name)
        return self  # return self so callers can chain .register() calls

    def register_many(self, *tools: BaseTool) -> "ToolRegistry":
        """
        Register several tools at once.

        This is just a convenience wrapper around ``register()``.
        Instead of:
            registry.register(a)
            registry.register(b)
            registry.register(c)

        You can write:
            registry.register_many(a, b, c)
        """
        for tool in tools:
            self.register(tool)
        return self

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    @property
    def tool_names(self) -> list[str]:
        """
        Return a sorted list of all registered tool names.

        Sorted alphabetically so the output is predictable (easier to read in
        logs or debug sessions).
        """
        return sorted(self._tools.keys())

    def get_tool(self, name: str) -> BaseTool:
        """
        Retrieve a registered tool by name.

        Raises ``ToolNotFoundError`` if the name isn't found.
        """
        if name not in self._tools:
            raise ToolNotFoundError(
                f"No tool registered with name '{name}'.",
                context={"requested": name, "available": sorted(self._tools)},
            )
        return self._tools[name]

    def schemas(self) -> list[ToolSchema]:
        """
        Return the schema (metadata) for every registered tool.

        Schemas describe each tool's name, what it does, what parameters it
        takes, and what it returns.  The Orchestrator uses this list to build
        the system prompt that tells the AI model which tools are available.
        """
        return [t.schema for t in self._tools.values()]

    def schemas_as_json(self, indent: int = 2) -> str:
        """
        Serialise all tool schemas into a single JSON string.

        This is useful when you want to embed the tool descriptions directly
        into a prompt as text, e.g.:

            "Available tools:\n" + registry.schemas_as_json()

        Each tool becomes a dict with keys: name, description, parameters, returns.

        Args:
            indent: Number of spaces for JSON pretty-printing. Default 2.
        """
        payload = []
        for schema in self.schemas():
            # Build a plain dict for each schema so json.dumps can serialise it.
            payload.append(
                {
                    "name": schema.name,
                    "description": schema.description,
                    "parameters": schema.parameters,
                    "returns": schema.returns,
                }
            )
        # ensure_ascii=False preserves non-ASCII characters (e.g. quotes in descriptions).
        return json.dumps(payload, indent=indent, ensure_ascii=False)

    def schemas_as_tool_list(self) -> list[dict]:
        """
        Return schemas formatted for the OpenAI / Ollama function-calling API.

        The Orchestrator passes this list directly to the model when it wants
        the model to be able to call tools.  The format is the standard
        "function calling" shape used by OpenAI and compatible APIs:

            [
              {
                "type": "function",
                "function": {
                  "name": "web_search",
                  "description": "...",
                  "parameters": { ... }   ← JSON Schema object
                }
              },
              ...
            ]

        The model reads these and decides which tool (if any) to call.
        """
        tools = []
        for schema in self.schemas():
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": schema.name,
                        "description": schema.description,
                        # parameters is a JSON Schema dict that the model reads
                        # to understand what arguments to pass.
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
        Run a tool by name and return a ``ToolResult``.

        This is the primary method called by the Orchestrator.  It:
          1. Looks up the tool by name (returns an error result if not found).
          2. Calls ``tool.execute(**kwargs)`` — which runs the tool's logic,
             measures how long it took, and catches any exceptions.
          3. Logs success or failure at the appropriate log level.
          4. Returns the ToolResult to the caller.

        Crucially, this method NEVER raises an exception.  All errors are
        captured inside the ToolResult (``result.success=False``,
        ``result.error="..."``) so the caller always gets a usable object back
        and can decide how to handle failures gracefully.

        Args:
            tool_name: The string name of a registered tool (e.g. "web_search").
            **kwargs:  The keyword arguments to pass to the tool's execute method.

        Returns:
            ToolResult with success=True and data filled in, or success=False
            and an error message.
        """
        # Step 1: look up the tool. If it's missing, return a "not found" error.
        try:
            tool = self.get_tool(tool_name)
        except KeyError as exc:
            # We return an error ToolResult rather than raising, so the caller
            # never has to wrap this call in a try/except.
            return ToolResult(
                success=False,
                tool_name=tool_name,
                data=None,
                error=str(exc),
            )

        # Log what we're about to do (helpful when debugging a long run).
        # We log only the argument *names* here, not the values, to keep logs tidy.
        logger.info("Executing tool '%s' with args: %s", tool_name, list(kwargs.keys()))

        # Step 2: actually run the tool. BaseTool.execute() wraps the real logic
        # in timing and error handling, so we always get a ToolResult back.
        result = await tool.execute(**kwargs)

        # Step 3: log the outcome at different levels depending on success/failure.
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
        Execute a tool from an OpenAI/Ollama-style model tool_call dict.

        When the AI model decides to call a tool, it returns a dict like:
            {
              "name": "web_search",
              "arguments": {"query": "event sourcing patterns"}
            }

        This method parses that dict and dispatches to ``execute()``, so the
        Orchestrator doesn't have to write its own parsing logic.

        The ``arguments`` value can be either a Python dict (ideal) or a JSON
        string (some models return it this way).  We handle both cases.

        Args:
            tool_call: Dict in OpenAI/Ollama function-call format.

        Returns:
            ToolResult from the dispatched tool.
        """
        name = tool_call.get("name", "")
        arguments = tool_call.get("arguments", {})

        # Some models (e.g. older Ollama versions) serialize arguments as a
        # JSON string rather than a Python dict. Detect and decode that case.
        if isinstance(arguments, str):
            # Some models return arguments as a JSON string
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                # If parsing fails, fall back to empty dict — the tool will
                # likely return an error for missing required arguments, which
                # is more helpful than crashing here.
                arguments = {}

        # Unpack the arguments dict as keyword arguments to execute().
        return await self.execute(name, **arguments)

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        """
        Return a short string representation of the registry.

        Useful when printing the registry object in a REPL or log line.
        Example output: ToolRegistry(tools=['artifact_writer', 'memory_query', ...])
        """
        return f"ToolRegistry(tools={self.tool_names})"

    # ------------------------------------------------------------------
    # Orchestrator convenience method
    # ------------------------------------------------------------------

    async def research(self, query: str) -> dict:
        """
        High-level research helper expected by the Orchestrator.

        This method bundles two tool calls into one convenient operation:
          1. Search the web for the query using "web_search".
          2. If results are found, scrape the top result for its full text
             using "web_scraper" (provides richer content than a snippet).

        The result is a unified dict the micro loop can inject into the AI's
        context so it knows what research was found.

        Why combine two tools here?
        ---------------------------
        A web search returns short snippets (a sentence or two per result).
        Scraping the top result gives the full article text, which is much more
        useful for architecture analysis.  Doing both in one call saves the
        Orchestrator from orchestrating this pattern itself every time.

        If the web search is unavailable (e.g. SearXNG not running), the method
        returns a stub dict with a friendly message rather than raising.

        Args:
            query: A natural-language search query string.

        Returns:
            A dict with keys:
              - "query":      the original query string
              - "result":     the scraped text (up to 2000 chars) or search data
              - "sources":    list of URLs found in search results
              - "raw_search": the full raw search result data (optional)
        """
        # Step 1: run the web search.
        result = await self.execute("web_search", query=query, max_results=5)

        search_data: dict = {}
        top_url: str = ""

        # Step 2: extract search results — handle both list and dict formats
        # because WebSearchTool can return either depending on its version.
        if result.success and isinstance(result.data, list) and result.data:
            # Most common case: data is a list of result dicts.
            search_data = {"results": result.data}
            top_url = result.data[0].get("url", "") if result.data else ""
        elif result.success and isinstance(result.data, dict):
            # Alternative case: data is a dict with a "results" key.
            search_data = result.data
            items = result.data.get("results", [])
            top_url = items[0].get("url", "") if items else ""
        else:
            # Web search unavailable — return a minimal stub
            # so callers always get a usable dict even when tools are down.
            return {
                "query": query,
                "result": f"Web search unavailable for '{query}'.",
                "sources": [],
            }

        # Step 3: optionally scrape the top result for richer content.
        # We only try this if we have a URL and the scraper tool is registered.
        scraped_text = ""
        if top_url and "web_scraper" in self._tools:
            scrape_result = await self.execute("web_scraper", url=top_url)
            if scrape_result.success:
                data = scrape_result.data or {}
                # The scraper returns a dict; pull the "text" field.
                scraped_text = data.get("text", "") if isinstance(data, dict) else str(data)

        # Step 4: build the list of source URLs from all search results.
        sources = []
        for r in (search_data.get("results") or []):
            url = r.get("url", "")
            if url:
                sources.append(url)

        # Truncate the content so it doesn't overwhelm the AI's context window.
        # If we got scraped text, use up to 2000 chars; otherwise fall back to
        # the raw search data summary (capped at 1000 chars).
        summary = scraped_text[:2000] if scraped_text else str(search_data)[:1000]

        return {
            "query": query,
            "result": summary,
            "sources": sources,
            "raw_search": search_data,  # include full search data for callers that want it
        }


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
    Create and return a ToolRegistry pre-loaded with all built-in Tinker tools.

    This is the one-liner you call at Tinker startup.  It creates each tool,
    configures it, and registers it — you don't need to touch the individual
    tool classes at all.

    All parameters are optional.  When omitted, each tool reads its defaults
    from environment variables (e.g. TINKER_SEARXNG_URL for the search URL).

    Args:
        searxng_url:
            Override the SearXNG search engine URL.
            Defaults to the TINKER_SEARXNG_URL environment variable, or
            "http://localhost:8080" if that's not set.

        artifact_output_dir:
            Directory where ArtifactWriterTool saves files.
            Defaults to the ARTIFACT_OUTPUT_DIR env var, or "./artifacts".

        diagram_output_dir:
            Directory where DiagramGeneratorTool saves .dot and .png files.
            Defaults to DIAGRAM_OUTPUT_DIR env var, or "./artifacts/diagrams".

        memory_manager:
            A MemoryManager instance for the MemoryQueryTool.
            If None, a stub is used that returns empty results and logs a warning.
            You must pass a real MemoryManager for production use.

    Returns:
        A fully configured ToolRegistry with these tools registered:
          - web_search      (WebSearchTool)
          - web_scraper     (WebScraperTool)
          - artifact_writer (ArtifactWriterTool)
          - diagram_generator (DiagramGeneratorTool)
          - memory_query    (MemoryQueryTool)

    Example:
        registry = build_default_registry(
            searxng_url="http://searxng:8080",
            artifact_output_dir="/data/artifacts",
        )
        result = await registry.execute("web_search", query="CQRS patterns")
    """
    # Import tool classes here (inside the function) rather than at the top of
    # the file.  This is called "lazy importing" — it avoids circular imports
    # and means the registry module doesn't fail to load if one tool's
    # dependencies (e.g. playwright) aren't installed.
    from .web_search import WebSearchTool
    from .web_scraper import WebScraperTool
    from .artifact_writer import ArtifactWriterTool
    from .diagram_generator import DiagramGeneratorTool
    from .memory_query import MemoryQueryTool

    # Build keyword args for WebSearchTool — only pass searxng_url if provided,
    # otherwise let the tool use its own default from the environment.
    kwargs: dict[str, Any] = {}
    if searxng_url:
        kwargs["searxng_url"] = searxng_url

    # Create each tool instance.
    search_tool = WebSearchTool(**kwargs)
    scraper_tool = WebScraperTool()

    # For tools that write to disk, pass the output directory if given.
    # Otherwise, let the tool use its env-var default.
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

    # MemoryQueryTool wraps the MemoryManager. If none is provided,
    # it falls back to a stub that warns but doesn't crash.
    memory_tool = MemoryQueryTool(memory_manager=memory_manager)

    # Create the registry and register all tools in one shot.
    registry = ToolRegistry()
    registry.register_many(
        search_tool,
        scraper_tool,
        writer_tool,
        diagram_tool,
        memory_tool,
    )
    return registry
