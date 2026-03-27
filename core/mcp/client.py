"""
core/mcp/client.py
=============
MCP client — connects to external MCP servers and imports their tools.

What this does
--------------
This module connects to external MCP servers (via SSE transport) and fetches
their tool lists.  Each remote tool is wrapped in a RemoteMCPTool — a
BaseTool subclass that proxies calls to the remote server.  The wrapped tools
are then registered in Tinker's ToolRegistry and become available to the
Architect AI exactly like native tools.

Example external MCP servers that Tinker could connect to:
  * A filesystem server that reads/writes files on the NAS.
  * A database server that queries PostgreSQL or SQLite.
  * Another Tinker instance (two Tinker processes sharing tools).
  * Any Claude Code MCP server configured in your ~/.claude/mcp.json.

Transport
---------
We connect using HTTP (not stdio).  The SSE stream is opened for receiving
server-to-client events, and we POST JSON-RPC messages to the messages
endpoint.  This is the standard "remote" MCP transport.

Error handling
--------------
Connection failures are non-fatal: a warning is logged, the server is skipped,
and Tinker starts without those tools.  This is the right behaviour for a
home lab where servers may be offline (e.g. the NAS is sleeping).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, TYPE_CHECKING

import httpx

from core.tools.base import BaseTool, ToolResult, ToolSchema

if TYPE_CHECKING:
    from core.mcp.config import MCPConfig

logger = logging.getLogger(__name__)

# ── Heartbeat health threshold ───────────────────────────────────────────────
# The MCP server sends a ``ping`` SSE event every 30 seconds.  If no ping is
# received within this many seconds, is_healthy() returns False — the server
# is likely down or the network path is broken.
_HEARTBEAT_TIMEOUT_SECONDS: float = 90.0


class RemoteMCPTool(BaseTool):
    """
    A BaseTool that proxies calls to a remote MCP server.

    When Tinker calls execute() on this tool, it sends a JSON-RPC
    ``tools/call`` request to the remote server and returns the result.

    Parameters
    ----------
    tool_name   : The tool's name on the remote server.
    description : Tool description (from the remote server's tools/list).
    parameters  : JSON Schema for the tool's parameters (from tools/list).
    messages_url: The remote server's ``/mcp/messages`` endpoint URL.
    server_name : Human-readable name of the remote server (for logging).
    timeout     : HTTP timeout for tool calls.
    """

    def __init__(
        self,
        tool_name: str,
        description: str,
        parameters: dict,
        messages_url: str,
        server_name: str = "remote",
        timeout: float = 30.0,
    ) -> None:
        self._tool_name = tool_name
        self._description = description
        self._parameters = parameters
        self._messages_url = messages_url
        self._server_name = server_name
        self._timeout = timeout

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self._tool_name,
            description=f"[{self._server_name}] {self._description}",
            parameters=self._parameters,
            returns="dict",
        )

    async def _execute(self, **kwargs: Any) -> Any:
        """Send a tools/call request to the remote server and return the result."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": self._tool_name,
                "arguments": kwargs,
            },
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(self._messages_url, json=payload)
            r.raise_for_status()
            data = r.json()

        if "error" in data:
            raise RuntimeError(
                f"Remote tool error: {data['error'].get('message', data['error'])}"
            )

        result = data.get("result", {})
        # MCP tools/call returns {"content": [{"type": "text", "text": "..."}], "isError": bool}
        if result.get("isError"):
            content = result.get("content", [{}])
            text = content[0].get("text", "Unknown tool error") if content else "Unknown tool error"
            raise RuntimeError(text)

        content = result.get("content", [{}])
        if content and content[0].get("type") == "text":
            text = content[0]["text"]
            # Try to parse JSON; fall back to raw string.
            try:
                return json.loads(text)
            except (json.JSONDecodeError, ValueError):
                return text

        return result


class MCPClient:
    """
    Connects to external MCP servers and returns their tools as RemoteMCPTool instances.

    Parameters
    ----------
    config : MCPConfig with client_server_urls and connect_timeout.

    Heartbeat tracking
    ------------------
    When the client receives ``ping`` events on the SSE stream, it records the
    timestamp.  Call ``is_healthy()`` to check whether the server has sent a
    ping recently (within the last 90 seconds).  This is useful for health
    dashboards and automatic reconnection logic.
    """

    def __init__(self, config: "MCPConfig") -> None:
        self._config = config

        # ── Heartbeat tracking ───────────────────────────────────────────────
        # Stores the monotonic timestamp of the last ping event received from
        # each connected MCP server.  Keyed by SSE URL.  Updated by the SSE
        # listener when a ``ping`` event arrives.
        self._last_ping: dict[str, float] = {}

    def record_ping(self, server_url: str) -> None:
        """
        Record that a ping was received from *server_url*.

        Called internally when the SSE stream delivers a ``ping`` event.
        External callers can also use this for testing.

        Parameters
        ----------
        server_url : The SSE URL of the server that sent the ping.
        """
        self._last_ping[server_url] = time.monotonic()

    def is_healthy(self, server_url: str | None = None) -> bool:
        """
        Check whether a connected MCP server is still alive.

        The server sends ``ping`` events every 30 seconds.  If no ping has
        been received within the last 90 seconds (3 missed pings), the
        server is considered unhealthy.

        Parameters
        ----------
        server_url : The SSE URL to check.  If None, checks all configured
                     servers — returns True only if *all* are healthy.

        Returns
        -------
        bool : True if the server(s) sent a ping within the threshold.
               Returns False if no ping has ever been received (e.g. the
               client hasn't connected yet).
        """
        now = time.monotonic()

        if server_url is not None:
            last = self._last_ping.get(server_url)
            if last is None:
                # No ping ever received from this server.
                return False
            return (now - last) < _HEARTBEAT_TIMEOUT_SECONDS

        # Check all configured servers — healthy only if all are healthy.
        if not self._config.client_server_urls:
            # No servers configured; vacuously healthy (nothing to fail).
            return True

        return all(
            self.is_healthy(url) for url in self._config.client_server_urls
        )

    async def fetch_all_tools(self) -> list[RemoteMCPTool]:
        """
        Connect to all configured MCP servers and fetch their tool lists.

        Connection failures are logged and skipped — never raises.

        Returns
        -------
        list[RemoteMCPTool] : All tools from all successfully connected servers.
        """
        all_tools: list[RemoteMCPTool] = []
        for url in self._config.client_server_urls:
            try:
                tools = await self._fetch_tools_from(url)
                logger.info(
                    "MCP client: connected to %s, imported %d tool(s)",
                    url,
                    len(tools),
                )
                all_tools.extend(tools)
            except Exception as exc:
                logger.warning(
                    "MCP client: could not connect to %s (%s) — skipping",
                    url,
                    exc,
                )
        return all_tools

    async def _fetch_tools_from(self, sse_url: str) -> list[RemoteMCPTool]:
        """
        Connect to one MCP server, initialize, fetch tool list, return tools.

        Derives the messages URL from the SSE URL:
          http://host:port/mcp/sse  →  http://host:port/mcp/messages
        """
        messages_url = sse_url.replace("/sse", "/messages")
        # Extract a human-readable server name from the URL.
        from urllib.parse import urlparse
        parsed = urlparse(sse_url)
        server_name = parsed.netloc or sse_url

        timeout = self._config.connect_timeout

        # Step 1: initialize handshake.
        init_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "tinker", "version": "1.0.0"},
                "capabilities": {},
            },
        }
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(messages_url, json=init_payload)
            r.raise_for_status()
            init_data = r.json()

        server_info = init_data.get("result", {}).get("serverInfo", {})
        remote_name = server_info.get("name", server_name)
        remote_version = server_info.get("version", "?")
        logger.debug(
            "MCP initialize: %s v%s at %s",
            remote_name,
            remote_version,
            sse_url,
        )

        # Step 2: fetch tool list.
        list_payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        }
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(messages_url, json=list_payload)
            r.raise_for_status()
            list_data = r.json()

        remote_tools = list_data.get("result", {}).get("tools", [])

        # Step 3: wrap each remote tool.
        wrapped = []
        for rt in remote_tools:
            name = rt.get("name", "")
            description = rt.get("description", "")
            input_schema = rt.get("inputSchema", {})
            parameters = {
                "type": "object",
                "properties": input_schema.get("properties", {}),
                "required": input_schema.get("required", []),
            }
            wrapped.append(
                RemoteMCPTool(
                    tool_name=f"{remote_name}/{name}",
                    description=description,
                    parameters=parameters,
                    messages_url=messages_url,
                    server_name=remote_name,
                    timeout=30.0,
                )
            )

        return wrapped
