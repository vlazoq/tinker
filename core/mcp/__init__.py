"""
core/mcp/ — Model Context Protocol support for Tinker.

What is MCP?
------------
The Model Context Protocol (MCP) is an open standard created by Anthropic that
defines how AI models connect to external tools, data sources, and services.

Think of it like USB for AI: instead of every AI tool having its own custom
API, MCP provides one standard plug.  An MCP *server* exposes resources and
tools.  An MCP *client* connects to servers and uses their tools.  Any client
can work with any server, regardless of who built them.

Claude Code (Anthropic's official CLI) uses MCP extensively.  When you add an
MCP server to Claude Code, Claude can suddenly call tools on that server —
querying databases, reading files, fetching APIs — without any changes to
Claude itself.

Why does Tinker need MCP?
--------------------------
Two reasons:

1. **As a server**: Tinker's existing tools (web_search, artifact_writer,
   memory_query, etc.) become available to any MCP client — including Claude
   Code.  This means a Claude Code session on the same machine can call
   "artifact_writer" and the result shows up in Tinker's artifact store.
   Two AI systems, one shared tool layer.

2. **As a client**: Tinker can connect to external MCP servers and import
   their tools into its ToolRegistry.  A database MCP server, a file system
   MCP server, or any other MCP server becomes instantly available to the
   Architect AI, without writing custom tool code.

Transport
---------
We use HTTP + Server-Sent Events (SSE) transport.  This is the standard
"remote" transport for MCP servers that run as long-lived processes.

  * SSE endpoint: ``GET /mcp/sse``  (client connects here first)
  * Message endpoint: ``POST /mcp/messages`` (JSON-RPC 2.0 requests)

Local stdio transport (where the client spawns the server as a child process
communicating over stdin/stdout) is not implemented here because Tinker already
runs as a long-lived server process.

Protocol
---------
MCP uses JSON-RPC 2.0.  The key methods Tinker implements:

  * ``initialize``     — client identifies itself, server responds with capabilities
  * ``tools/list``     — list all available tools
  * ``tools/call``     — call a tool by name with arguments

Public API
----------
    from core.mcp.bridge import MCPBridge
    from core.mcp.config import MCPConfig

    config = MCPConfig.from_env()
    bridge = MCPBridge(config, tool_registry=registry)

    # Start the MCP server (adds routes to the existing FastAPI app)
    bridge.mount_server(fastapi_app)

    # Connect to external MCP servers and import their tools
    await bridge.connect_clients()
"""
