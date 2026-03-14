"""
test_tool_layer.py
------------------
Exercises every tool in the Tinker Tool Layer.
Run with:
    python -m pytest tests/test_tool_layer.py -v
  or directly:
    python tests/test_tool_layer.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Stub httpx and trafilatura if not installed (sandbox/CI without pip)
try:
    import httpx
except ImportError:
    from unittest.mock import MagicMock
    import sys, types
    _httpx = types.ModuleType("httpx")
    _httpx.AsyncClient = MagicMock
    _httpx.HTTPStatusError = Exception
    _httpx.TimeoutException = Exception
    sys.modules["httpx"] = _httpx

try:
    import trafilatura
except ImportError:
    import sys, types
    _tra = types.ModuleType("trafilatura")
    def _extract(html, **kw): return "Extracted text from article about architecture."
    def _meta(html, **kw):
        class M: title = "Test Title"
        return M()
    _tra.extract = _extract
    _tra.extract_metadata = _meta
    sys.modules["trafilatura"] = _tra
    import tool_layer.tools.web_scraper as _ws
    _ws._trafilatura = _tra


# Make the package importable when run from the repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from tool_layer.tools.base import BaseTool, ToolResult, ToolSchema
from tool_layer.tools.web_search import WebSearchTool
from tool_layer.tools.web_scraper import WebScraperTool
from tool_layer.tools.artifact_writer import ArtifactWriterTool
from tool_layer.tools.diagram_generator import DiagramGeneratorTool
from tool_layer.tools.memory_query import MemoryQueryTool, _StubMemoryManager
from tool_layer.registry import ToolRegistry, build_default_registry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    """Run a coroutine in the test's event loop."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Base / ToolResult tests
# ---------------------------------------------------------------------------

class TestToolResult(unittest.TestCase):

    def test_to_dict_success(self):
        r = ToolResult(success=True, tool_name="foo", data={"key": "val"}, duration_ms=42.1)
        d = r.to_dict()
        self.assertTrue(d["success"])
        self.assertEqual(d["tool_name"], "foo")
        self.assertEqual(d["data"], {"key": "val"})
        self.assertIsNone(d["error"])

    def test_to_dict_failure(self):
        r = ToolResult(success=False, tool_name="bar", data=None, error="Boom")
        d = r.to_dict()
        self.assertFalse(d["success"])
        self.assertEqual(d["error"], "Boom")


# ---------------------------------------------------------------------------
# Web Search Tool
# ---------------------------------------------------------------------------

class TestWebSearchTool(unittest.TestCase):

    def _make_tool(self) -> WebSearchTool:
        return WebSearchTool(searxng_url="http://localhost:8080")

    def test_schema(self):
        tool = self._make_tool()
        schema = tool.schema
        self.assertEqual(schema.name, "web_search")
        self.assertIn("query", schema.parameters["properties"])
        self.assertIn("query", schema.parameters["required"])

    def test_success_response(self):
        tool = self._make_tool()

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "results": [
                {
                    "title": "Event Sourcing Explained",
                    "url": "https://example.com/es",
                    "content": "Event sourcing stores each change as an event...",
                    "engine": "google",
                    "score": 0.95,
                },
                {
                    "title": "CQRS and Event Sourcing",
                    "url": "https://example.com/cqrs",
                    "content": "CQRS separates reads from writes...",
                    "engine": "bing",
                    "score": 0.87,
                },
            ]
        }

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = run(tool.execute(query="event sourcing patterns", num_results=2))

        self.assertTrue(result.success, result.error)
        self.assertEqual(len(result.data), 2)
        self.assertEqual(result.data[0]["title"], "Event Sourcing Explained")
        self.assertIn("url", result.data[0])
        self.assertIn("snippet", result.data[0])

    def test_http_error_returns_failure(self):
        tool = self._make_tool()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(side_effect=Exception("Connection refused"))
            mock_client_cls.return_value = mock_client

            result = run(tool.execute(query="anything"))

        self.assertFalse(result.success)
        self.assertIn("Connection refused", result.error)

    def test_result_count_capped_by_num_results(self):
        tool = self._make_tool()

        many_results = [
            {"title": f"Result {i}", "url": f"https://example.com/{i}",
             "content": f"Content {i}", "engine": "google", "score": 0.9 - i * 0.01}
            for i in range(20)
        ]

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"results": many_results}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = run(tool.execute(query="test", num_results=5))

        self.assertEqual(len(result.data), 5)


# ---------------------------------------------------------------------------
# Web Scraper Tool
# ---------------------------------------------------------------------------

class TestWebScraperTool(unittest.TestCase):

    def _make_tool(self) -> WebScraperTool:
        return WebScraperTool(timeout_ms=5000)

    def test_schema(self):
        tool = self._make_tool()
        schema = tool.schema
        self.assertEqual(schema.name, "web_scraper")
        self.assertIn("url", schema.parameters["required"])

    def test_httpx_fallback_success(self):
        tool = self._make_tool()

        html = """
        <html><head><title>Test Article</title></head>
        <body>
          <article>
            <p>Event sourcing is a pattern where state changes are stored as events.</p>
            <p>Each event is immutable and represents a fact about what happened.</p>
          </article>
        </body></html>
        """

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.text = html

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            # Force httpx path by patching Playwright as unavailable
            with patch("tool_layer.tools.web_scraper._PLAYWRIGHT_AVAILABLE", False):
                result = run(tool.execute(url="https://example.com/article"))

        self.assertTrue(result.success, result.error)
        self.assertIn("url", result.data)
        self.assertEqual(result.data["fetch_method"], "httpx")
        self.assertIsInstance(result.data["word_count"], int)

    def test_url_scheme_prepended(self):
        """If URL is missing scheme, https:// should be added."""
        tool = self._make_tool()

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.text = "<html><body>Test content about architecture</body></html>"

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            with patch("tool_layer.tools.web_scraper._PLAYWRIGHT_AVAILABLE", False):
                result = run(tool.execute(url="example.com"))

        self.assertTrue(result.success, result.error)
        self.assertTrue(result.data["url"].startswith("https://"))


# ---------------------------------------------------------------------------
# Artifact Writer Tool
# ---------------------------------------------------------------------------

class TestArtifactWriterTool(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.tool = ArtifactWriterTool(output_dir=self._tmpdir)

    def test_schema(self):
        schema = self.tool.schema
        self.assertEqual(schema.name, "artifact_writer")
        self.assertIn("title", schema.parameters["required"])
        self.assertIn("content", schema.parameters["required"])

    def test_write_markdown_artifact(self):
        result = run(self.tool.execute(
            title="Event Sourcing Research",
            content="## Summary\n\nEvent sourcing stores state as a sequence of events.",
            artifact_type="research_note",
            task_id="task-001",
            tags=["event-sourcing", "distributed-systems"],
        ))

        self.assertTrue(result.success, result.error)
        self.assertIn("artifact_id", result.data)
        self.assertIn("file_path", result.data)

        file_path = Path(result.data["file_path"])
        self.assertTrue(file_path.exists())
        self.assertTrue(file_path.suffix == ".md")

        content = file_path.read_text()
        self.assertIn("artifact_id:", content)
        self.assertIn("Event Sourcing Research", content)
        self.assertIn("event-sourcing", content)
        self.assertIn("## Summary", content)

    def test_write_json_artifact(self):
        data = json.dumps({"components": ["A", "B"], "pattern": "CQRS"})
        result = run(self.tool.execute(
            title="Architecture Spec",
            content=data,
            artifact_type="architecture_analysis",
            task_id="task-002",
            format="json",
        ))

        self.assertTrue(result.success, result.error)
        file_path = Path(result.data["file_path"])
        self.assertEqual(file_path.suffix, ".json")

        parsed = json.loads(file_path.read_text())
        self.assertEqual(parsed["title"], "Architecture Spec")
        self.assertIn("components", parsed["content"])

    def test_artifacts_organised_by_task(self):
        for i in range(3):
            run(self.tool.execute(
                title=f"Note {i}",
                content=f"Content {i}",
                artifact_type="research_note",
                task_id="task-xyz",
            ))

        task_dir = Path(self._tmpdir) / "task_xyz"
        self.assertTrue(task_dir.exists())
        md_files = list(task_dir.glob("*.md"))
        self.assertEqual(len(md_files), 3)

    def test_size_bytes_returned(self):
        result = run(self.tool.execute(
            title="Size Test",
            content="Hello world",
            artifact_type="raw_data",
            task_id="task-size",
        ))
        self.assertGreater(result.data["size_bytes"], 0)


# ---------------------------------------------------------------------------
# Diagram Generator Tool
# ---------------------------------------------------------------------------

class TestDiagramGeneratorTool(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.tool = DiagramGeneratorTool(output_dir=self._tmpdir)

    SAMPLE_COMPONENTS = [
        {"id": "client", "label": "Client App", "shape": "ellipse", "color": "#AED6F1"},
        {"id": "api_gw", "label": "API Gateway", "shape": "box", "color": "#A9DFBF"},
        {"id": "auth", "label": "Auth Service", "group": "Backend"},
        {"id": "orders", "label": "Orders Service", "group": "Backend"},
        {"id": "db", "label": "PostgreSQL", "shape": "cylinder", "color": "#F9E79F"},
    ]

    SAMPLE_RELATIONSHIPS = [
        {"from": "client", "to": "api_gw", "label": "HTTPS"},
        {"from": "api_gw", "to": "auth", "label": "gRPC"},
        {"from": "api_gw", "to": "orders", "label": "gRPC"},
        {"from": "orders", "to": "db", "label": "SQL"},
        {"from": "auth", "to": "db", "label": "SQL", "style": "dashed"},
    ]

    def test_schema(self):
        schema = self.tool.schema
        self.assertEqual(schema.name, "diagram_generator")
        self.assertIn("components", schema.parameters["required"])
        self.assertIn("relationships", schema.parameters["required"])

    def test_dot_file_generated(self):
        result = run(self.tool.execute(
            diagram_name="test_arch",
            title="Test Architecture",
            components=self.SAMPLE_COMPONENTS,
            relationships=self.SAMPLE_RELATIONSHIPS,
            direction="LR",
        ))

        self.assertTrue(result.success, result.error)
        self.assertIn("dot_path", result.data)
        self.assertIn("dot_source", result.data)

        dot_path = Path(result.data["dot_path"])
        self.assertTrue(dot_path.exists())

        dot_src = dot_path.read_text()
        self.assertIn("digraph G", dot_src)
        self.assertIn("client", dot_src)
        self.assertIn("rankdir=LR", dot_src)

    def test_node_and_edge_counts(self):
        result = run(self.tool.execute(
            diagram_name="count_test",
            components=self.SAMPLE_COMPONENTS,
            relationships=self.SAMPLE_RELATIONSHIPS,
        ))
        self.assertEqual(result.data["node_count"], 5)
        self.assertEqual(result.data["edge_count"], 5)

    def test_cluster_subgraphs(self):
        result = run(self.tool.execute(
            diagram_name="cluster_test",
            components=self.SAMPLE_COMPONENTS,
            relationships=self.SAMPLE_RELATIONSHIPS,
        ))
        dot_src = result.data["dot_source"]
        self.assertIn("subgraph cluster_", dot_src)
        self.assertIn("Backend", dot_src)

    def test_graphviz_not_installed_handled_gracefully(self):
        """Even if Graphviz is not on PATH, the tool should succeed for the .dot file."""
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("graphviz not found")):
            result = run(self.tool.execute(
                diagram_name="no_graphviz",
                components=self.SAMPLE_COMPONENTS[:2],
                relationships=self.SAMPLE_RELATIONSHIPS[:1],
            ))

        # The tool must still succeed — .dot file is valuable even without PNG
        self.assertTrue(result.success, result.error)
        self.assertFalse(result.data["rendered"])
        self.assertIsNotNone(result.data["render_error"])


# ---------------------------------------------------------------------------
# Memory Query Tool
# ---------------------------------------------------------------------------

class TestMemoryQueryTool(unittest.TestCase):

    def test_schema(self):
        tool = MemoryQueryTool()
        schema = tool.schema
        self.assertEqual(schema.name, "memory_query")
        self.assertIn("query", schema.parameters["required"])

    def test_stub_returns_empty_with_warning(self):
        tool = MemoryQueryTool()  # Uses _StubMemoryManager
        import warnings
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = run(tool.execute(query="event sourcing"))

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.data, [])
        self.assertTrue(any("stub" in str(warning.message).lower() for warning in w))

    def test_with_real_memory_manager(self):
        mock_mm = MagicMock()
        mock_mm.search = AsyncMock(return_value=[
            {
                "memory_id": "mem-001",
                "score": 0.92,
                "title": "CQRS Pattern Analysis",
                "artifact_type": "architecture_analysis",
                "task_id": "task-003",
                "created_at": "2024-01-15T10:00:00Z",
                "tags": ["cqrs", "patterns"],
                "snippet": "CQRS separates read and write models...",
            }
        ])

        tool = MemoryQueryTool(memory_manager=mock_mm)
        result = run(tool.execute(
            query="CQRS read write separation",
            top_k=5,
            filters={"artifact_type": "architecture_analysis"},
        ))

        self.assertTrue(result.success, result.error)
        self.assertEqual(len(result.data), 1)
        self.assertEqual(result.data[0]["title"], "CQRS Pattern Analysis")
        self.assertEqual(result.data[0]["score"], 0.92)

        mock_mm.search.assert_called_once_with(
            query="CQRS read write separation",
            top_k=5,
            filters={"artifact_type": "architecture_analysis"},
        )

    def test_normalises_missing_fields(self):
        mock_mm = MagicMock()
        mock_mm.search = AsyncMock(return_value=[
            {"id": "mem-002", "score": 0.7, "text": "Some text"}
        ])
        tool = MemoryQueryTool(memory_manager=mock_mm)
        result = run(tool.execute(query="anything"))

        self.assertTrue(result.success)
        r = result.data[0]
        # All standard fields must be present
        for key in ("memory_id", "score", "title", "artifact_type", "task_id",
                    "created_at", "tags", "snippet"):
            self.assertIn(key, r, f"Missing key: {key}")


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------

class TestToolRegistry(unittest.TestCase):

    def _make_registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(WebSearchTool())
        registry.register(ArtifactWriterTool(output_dir=tempfile.mkdtemp()))
        return registry

    def test_tool_names(self):
        registry = self._make_registry()
        names = registry.tool_names
        self.assertIn("web_search", names)
        self.assertIn("artifact_writer", names)

    def test_schemas_as_json(self):
        registry = self._make_registry()
        json_str = registry.schemas_as_json()
        parsed = json.loads(json_str)
        names = [s["name"] for s in parsed]
        self.assertIn("web_search", names)

    def test_schemas_as_tool_list(self):
        registry = self._make_registry()
        tools = registry.schemas_as_tool_list()
        self.assertIsInstance(tools, list)
        for t in tools:
            self.assertEqual(t["type"], "function")
            self.assertIn("name", t["function"])

    def test_get_tool_raises_for_unknown(self):
        registry = self._make_registry()
        with self.assertRaises(KeyError):
            registry.get_tool("nonexistent_tool")

    def test_execute_unknown_tool_returns_failure(self):
        registry = self._make_registry()
        result = run(registry.execute("does_not_exist", foo="bar"))
        self.assertFalse(result.success)
        self.assertIn("does_not_exist", result.error)

    def test_execute_from_model_call(self):
        registry = ToolRegistry()
        writer = ArtifactWriterTool(output_dir=tempfile.mkdtemp())
        registry.register(writer)

        model_call = {
            "name": "artifact_writer",
            "arguments": {
                "title": "Model Call Test",
                "content": "Testing model call dispatch",
                "artifact_type": "research_note",
                "task_id": "task-model",
            },
        }
        result = run(registry.execute_from_model_call(model_call))
        self.assertTrue(result.success, result.error)

    def test_execute_from_model_call_with_json_string_args(self):
        registry = ToolRegistry()
        writer = ArtifactWriterTool(output_dir=tempfile.mkdtemp())
        registry.register(writer)

        model_call = {
            "name": "artifact_writer",
            "arguments": json.dumps({
                "title": "JSON String Args",
                "content": "Testing JSON string arguments",
                "artifact_type": "raw_data",
                "task_id": "task-json",
            }),
        }
        result = run(registry.execute_from_model_call(model_call))
        self.assertTrue(result.success, result.error)

    def test_build_default_registry(self):
        registry = build_default_registry(
            artifact_output_dir=tempfile.mkdtemp(),
            diagram_output_dir=tempfile.mkdtemp(),
        )
        for name in ("web_search", "web_scraper", "artifact_writer",
                     "diagram_generator", "memory_query"):
            self.assertIn(name, registry.tool_names)


# ---------------------------------------------------------------------------
# Integration smoke tests (requires live services)
# Skip these in CI — they're opt-in
# ---------------------------------------------------------------------------

class IntegrationTests(unittest.TestCase):
    """Marked skip by default — remove the skip decorator to run against live services."""

    @unittest.skip("Requires live SearXNG on localhost:8080")
    def test_live_web_search(self):
        tool = WebSearchTool()
        result = run(tool.execute(query="Python asyncio best practices", num_results=3))
        self.assertTrue(result.success)
        self.assertGreater(len(result.data), 0)

    @unittest.skip("Requires network access and trafilatura")
    def test_live_web_scraper(self):
        tool = WebScraperTool()
        result = run(tool.execute(url="https://httpbin.org/html"))
        self.assertTrue(result.success)
        self.assertGreater(result.data["word_count"], 0)

    @unittest.skip("Requires Graphviz on PATH")
    def test_live_diagram_render(self):
        tool = DiagramGeneratorTool(output_dir=tempfile.mkdtemp())
        result = run(tool.execute(
            diagram_name="live_test",
            components=[
                {"id": "a", "label": "Service A"},
                {"id": "b", "label": "Service B"},
            ],
            relationships=[{"from": "a", "to": "b", "label": "calls"}],
        ))
        self.assertTrue(result.success)
        self.assertTrue(result.data["rendered"])


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("Tinker Tool Layer — Test Suite")
    print("=" * 60)
    unittest.main(verbosity=2)
