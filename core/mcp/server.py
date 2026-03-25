"""
core/mcp/server.py
=============
MCP server — exposes Tinker's ToolRegistry as an MCP server over HTTP/SSE.

Protocol overview
-----------------
MCP uses JSON-RPC 2.0 over two HTTP endpoints:

  GET  /mcp/sse       — Client opens a persistent SSE stream.  The server
                         uses this to push events to the client.  The client's
                         first event is an "endpoint" event that tells it where
                         to POST its JSON-RPC messages.

  POST /mcp/messages  — Client sends JSON-RPC 2.0 method calls here.  The
                         server responds synchronously (for tools/list) or
                         sends the result back via the SSE stream (for tools/call).

Supported methods
-----------------
  initialize   : Client handshake.  Server responds with its name, version,
                 and the list of capabilities it supports.
  tools/list   : Return a list of all tools in the ToolRegistry.
  tools/call   : Call a specific tool by name and return the result.

Why SSE transport?
------------------
SSE (Server-Sent Events) is a standard HTTP mechanism for streaming data from
server to client.  It's simpler than WebSockets (unidirectional, plain HTTP,
no special handshake) and is the recommended transport for MCP servers that
run as long-lived HTTP processes.

For Tinker, this is a natural fit because the webui already runs a FastAPI HTTP
server.  We just add a couple of routes to the existing app.

Integration
-----------
The MCPServer class doesn't create its own HTTP server.  Instead, it adds
routes to the existing FastAPI app passed to mount()::

    server = MCPServer(config, registry)
    server.mount(fastapi_app)

This keeps the existing webui port and lets clients find Tinker's MCP server
at the same address as the web dashboard, on a different path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, AsyncIterator, TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from fastapi import FastAPI
    from core.mcp.config import MCPConfig
    from core.tools.registry import ToolRegistry


class MCPServer:
    """
    Exposes a ToolRegistry as an MCP server over HTTP/SSE.

    Parameters
    ----------
    config   : MCPConfig with server_name, server_version, server_path.
    registry : Tinker's ToolRegistry — all registered tools become MCP tools.
    """

    def __init__(self, config: "MCPConfig", registry: "ToolRegistry") -> None:
        self._config = config
        self._registry = registry
        # Connected SSE clients — keyed by client_id.
        # Each value is an asyncio.Queue that we put events into; the SSE
        # generator reads from this queue and yields to the HTTP response.
        self._clients: dict[str, asyncio.Queue] = {}

    def mount(self, app: "FastAPI") -> None:
        """
        Add MCP routes to an existing FastAPI application.

        Routes added:
          GET  {server_path}/sse       — SSE stream
          POST {server_path}/messages  — JSON-RPC messages

        Parameters
        ----------
        app : The FastAPI application to add routes to.
        """
        from fastapi import Request
        from fastapi.responses import StreamingResponse, JSONResponse

        path = self._config.server_path

        @app.get(f"{path}/sse")
        async def mcp_sse(request: Request):
            """SSE stream — clients connect here first."""
            client_id = str(uuid.uuid4())[:8]
            queue: asyncio.Queue = asyncio.Queue()
            self._clients[client_id] = queue

            # Tell the client which endpoint to use for messages.
            messages_url = f"{path}/messages?client_id={client_id}"

            async def event_stream() -> AsyncIterator[str]:
                # First event: the messages endpoint URL.
                yield f"event: endpoint\ndata: {json.dumps({'uri': messages_url})}\n\n"
                logger.info("MCP client connected: client_id=%s", client_id)
                try:
                    while True:
                        # Check if client disconnected.
                        if await request.is_disconnected():
                            break
                        # Wait for events from the queue (with a ping timeout).
                        try:
                            event = await asyncio.wait_for(queue.get(), timeout=30.0)
                            yield f"event: message\ndata: {json.dumps(event)}\n\n"
                        except asyncio.TimeoutError:
                            # Send a keep-alive ping.
                            yield ": ping\n\n"
                except asyncio.CancelledError:
                    pass
                finally:
                    self._clients.pop(client_id, None)
                    logger.info("MCP client disconnected: client_id=%s", client_id)

            return StreamingResponse(
                event_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )

        @app.post(f"{path}/messages")
        async def mcp_messages(request: Request):
            """JSON-RPC message endpoint."""
            try:
                body = await request.json()
            except Exception:
                return JSONResponse(
                    self._error_response(None, -32700, "Parse error"), status_code=200
                )

            client_id = request.query_params.get("client_id")
            result = await self._handle_request(body)

            # For tools/call, send the result via SSE (async) and return 202.
            # For other methods, return synchronously.
            if client_id and client_id in self._clients:
                await self._clients[client_id].put(result)
                return JSONResponse({"status": "accepted"}, status_code=202)

            return JSONResponse(result, status_code=200)

        logger.info(
            "MCP server mounted at %s/sse and %s/messages",
            path,
            path,
        )

    # ── JSON-RPC dispatch ─────────────────────────────────────────────────────

    async def _handle_request(self, body: dict) -> dict:
        """Route a JSON-RPC 2.0 request to the appropriate handler."""
        rpc_id = body.get("id")
        method = body.get("method", "")
        params = body.get("params", {})

        try:
            if method == "initialize":
                result = self._handle_initialize(params)
            elif method == "tools/list":
                result = self._handle_tools_list()
            elif method == "tools/call":
                result = await self._handle_tools_call(params)
            elif method == "ping":
                result = {}
            else:
                return self._error_response(rpc_id, -32601, f"Method not found: {method}")
        except Exception as exc:
            logger.exception("MCP handler error for method '%s': %s", method, exc)
            return self._error_response(rpc_id, -32603, f"Internal error: {exc}")

        return {"jsonrpc": "2.0", "id": rpc_id, "result": result}

    def _handle_initialize(self, params: dict) -> dict:
        """Respond to the initialize handshake."""
        client_name = params.get("clientInfo", {}).get("name", "unknown")
        logger.info("MCP initialize from client: %s", client_name)
        return {
            "protocolVersion": "2024-11-05",
            "serverInfo": {
                "name": self._config.server_name,
                "version": self._config.server_version,
            },
            "capabilities": {
                "tools": {"listChanged": False},
            },
        }

    def _handle_tools_list(self) -> dict:
        """Return all tools in the registry as MCP tool descriptors."""
        tools = []
        for name in self._registry.tool_names:
            try:
                tool = self._registry.get_tool(name)
                schema = tool.schema
                tools.append({
                    "name": schema.name,
                    "description": schema.description,
                    "inputSchema": {
                        "type": "object",
                        "properties": schema.parameters.get("properties", {}),
                        "required": schema.parameters.get("required", []),
                    },
                })
            except Exception as exc:
                logger.warning("MCP tools/list: skipping tool %s: %s", name, exc)
        return {"tools": tools}

    async def _handle_tools_call(self, params: dict) -> dict:
        """Execute a tool call and return the result."""
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if not tool_name:
            raise ValueError("tools/call requires 'name' parameter")

        t0 = time.monotonic()
        result = await self._registry.execute(tool_name, **arguments)
        elapsed_ms = (time.monotonic() - t0) * 1000

        logger.debug(
            "MCP tools/call: %s → success=%s (%.0fms)",
            tool_name,
            result.success,
            elapsed_ms,
        )

        if result.success:
            content_text = (
                json.dumps(result.data, indent=2)
                if isinstance(result.data, (dict, list))
                else str(result.data)
            )
            return {
                "content": [{"type": "text", "text": content_text}],
                "isError": False,
            }
        else:
            return {
                "content": [{"type": "text", "text": f"Tool error: {result.error}"}],
                "isError": True,
            }

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _error_response(rpc_id: Any, code: int, message: str) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": code, "message": message},
        }

    @property
    def connected_clients(self) -> int:
        """Number of currently connected SSE clients."""
        return len(self._clients)
