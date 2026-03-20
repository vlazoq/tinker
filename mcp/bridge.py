"""
mcp/bridge.py
=============
MCPBridge — the entry point for all MCP functionality.

This class wires together the MCP server (Tinker → outside world) and the MCP
client (outside world → Tinker) into a single object that main.py constructs
and passes around.

Usage in main.py
----------------
    from mcp.bridge import MCPBridge
    from mcp.config import MCPConfig

    mcp_config = MCPConfig.from_env()
    if mcp_config.enabled:
        bridge = MCPBridge(mcp_config, tool_registry=registry)

        # Add MCP routes to the FastAPI webui app.
        bridge.mount_server(webui_app)

        # Connect to external MCP servers and import their tools.
        await bridge.connect_clients()

        # At this point, registry contains both native Tinker tools and any
        # tools imported from external MCP servers.

Status reporting
----------------
bridge.status() returns a dict suitable for the /api/mcp/status endpoint::

    {
      "enabled": true,
      "server": {
        "path": "/mcp",
        "connected_clients": 2
      },
      "clients": {
        "configured_servers": ["http://nas:9000/mcp/sse"],
        "imported_tools": ["filesystem/read_file", "filesystem/write_file"]
      }
    }
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from fastapi import FastAPI
    from mcp.config import MCPConfig
    from mcp.server import MCPServer
    from mcp.client import MCPClient
    from tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class MCPBridge:
    """
    Coordinates MCP server and client for Tinker.

    Parameters
    ----------
    config   : MCPConfig.
    registry : Tinker's ToolRegistry.  Remote tools are registered here.
    """

    def __init__(self, config: "MCPConfig", registry: "ToolRegistry") -> None:
        self._config = config
        self._registry = registry
        self._server: Optional["MCPServer"] = None
        self._imported_tool_names: list[str] = []

    def mount_server(self, app: "FastAPI") -> None:
        """
        Add MCP server routes to an existing FastAPI app.

        This exposes Tinker's tools as an MCP server.  After calling this,
        external clients (e.g. Claude Code) can connect to:
          GET  /mcp/sse
          POST /mcp/messages

        Parameters
        ----------
        app : The FastAPI app to add routes to.
        """
        from mcp.server import MCPServer

        self._server = MCPServer(self._config, self._registry)
        self._server.mount(app)
        logger.info(
            "MCP server mounted (path=%s, tools=%d)",
            self._config.server_path,
            len(self._registry.tool_names),
        )

    async def connect_clients(self) -> None:
        """
        Connect to all configured external MCP servers and import their tools.

        Failures are non-fatal: a warning is logged and the server is skipped.
        After this call, any successfully imported tools are available in
        self._registry.
        """
        if not self._config.client_server_urls:
            logger.debug("MCP client: no external servers configured — skipping")
            return

        from mcp.client import MCPClient

        client = MCPClient(self._config)
        remote_tools = await client.fetch_all_tools()

        if remote_tools:
            self._registry.register_many(*remote_tools)
            self._imported_tool_names = [t.schema.name for t in remote_tools]
            logger.info(
                "MCP client: imported %d tool(s) from external servers: %s",
                len(remote_tools),
                ", ".join(self._imported_tool_names[:10]),
            )
        else:
            logger.debug("MCP client: no tools imported from external servers")

    def status(self) -> dict:
        """Return a status dict for the /api/mcp/status endpoint."""
        return {
            "enabled": self._config.enabled,
            "server": {
                "path": self._config.server_path,
                "name": self._config.server_name,
                "version": self._config.server_version,
                "connected_clients": self._server.connected_clients if self._server else 0,
                "tools_exposed": len(self._registry.tool_names),
            },
            "clients": {
                "configured_servers": self._config.client_server_urls,
                "imported_tools": self._imported_tool_names,
            },
        }
