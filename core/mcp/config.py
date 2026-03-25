"""
mcp/config.py
=============
Configuration for Tinker's MCP server and client.

Environment variables
---------------------
TINKER_MCP_ENABLED       : "true" to enable MCP (default: "false")
TINKER_MCP_SERVER_PORT   : Port for the MCP HTTP server (default: same as webui, via router)
TINKER_MCP_SERVER_PATH   : URL path prefix for MCP endpoints (default: "/mcp")
TINKER_MCP_SERVERS       : Comma-separated list of external MCP server SSE URLs to connect to
                           Example: "http://localhost:9000/mcp/sse,http://nas:9001/mcp/sse"
TINKER_MCP_SERVER_NAME   : Name advertised in initialize response (default: "tinker")
TINKER_MCP_SERVER_VERSION: Version advertised (default: "1.0.0")
TINKER_MCP_CONNECT_TIMEOUT: Seconds to wait when connecting to external servers (default: 10)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class MCPConfig:
    """
    Configuration for MCP server and client behaviour.

    All fields have env-var-backed defaults so you can configure via
    environment without changing code.
    """

    # ── Server settings ───────────────────────────────────────────────────────

    # Enable/disable the entire MCP subsystem.  Set to False to skip all
    # MCP initialisation even if the package is installed.
    enabled: bool = field(
        default_factory=lambda: os.getenv("TINKER_MCP_ENABLED", "false").lower() == "true"
    )

    # URL path prefix for the MCP HTTP endpoints added to the FastAPI app.
    # The SSE endpoint becomes: {server_path}/sse
    # The message endpoint becomes: {server_path}/messages
    server_path: str = field(
        default_factory=lambda: os.getenv("TINKER_MCP_SERVER_PATH", "/mcp")
    )

    # Name that Tinker advertises in the MCP initialize response.
    server_name: str = field(
        default_factory=lambda: os.getenv("TINKER_MCP_SERVER_NAME", "tinker")
    )

    # Version string advertised in the MCP initialize response.
    server_version: str = field(
        default_factory=lambda: os.getenv("TINKER_MCP_SERVER_VERSION", "1.0.0")
    )

    # ── Client settings ───────────────────────────────────────────────────────

    # Comma-separated list of external MCP server SSE URLs.
    # Example: "http://localhost:9000/mcp/sse,http://nas:9001/mcp/sse"
    # Each URL is connected to on startup and their tools imported into
    # Tinker's ToolRegistry.
    client_server_urls: list[str] = field(default_factory=list)

    # How long (seconds) to wait when connecting to an external MCP server.
    # Failures are logged and skipped — a bad server URL won't prevent Tinker
    # from starting.
    connect_timeout: float = field(
        default_factory=lambda: float(os.getenv("TINKER_MCP_CONNECT_TIMEOUT", "10"))
    )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @classmethod
    def from_env(cls) -> "MCPConfig":
        """
        Build MCPConfig from environment variables.

        Parses TINKER_MCP_SERVERS into the client_server_urls list.
        """
        raw_servers = os.getenv("TINKER_MCP_SERVERS", "")
        urls = [u.strip() for u in raw_servers.split(",") if u.strip()]
        return cls(client_server_urls=urls)

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "server_path": self.server_path,
            "server_name": self.server_name,
            "server_version": self.server_version,
            "client_server_urls": self.client_server_urls,
            "connect_timeout": self.connect_timeout,
        }
