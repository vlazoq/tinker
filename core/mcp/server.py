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
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, TYPE_CHECKING

from infra.resilience.rate_limiter import TokenBucketRateLimiter

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from fastapi import FastAPI
    from core.mcp.config import MCPConfig
    from core.tools.registry import ToolRegistry

# ── Per-IP MCP rate limiting ─────────────────────────────────────────────────
# Default: 60 requests per minute per IP = 1 request per second steady state,
# with a burst capacity of 60 (so a client can fire 60 calls instantly, then
# must slow to 1/sec).  Override via environment variables.
_MCP_RATE_PER_SEC: float = float(os.getenv("TINKER_MCP_RATE_PER_SEC", "1.0"))
_MCP_RATE_BURST: float = float(os.getenv("TINKER_MCP_RATE_BURST", "60.0"))

# Registry of per-IP token-bucket rate limiters, lazily created on first
# request from each IP.  For a homelab the number of distinct IPs is tiny,
# so unbounded growth is fine.
_mcp_ip_limiters: dict[str, TokenBucketRateLimiter] = {}
_mcp_ip_limiters_lock = asyncio.Lock()


async def _mcp_limiter_for_ip(ip: str) -> TokenBucketRateLimiter:
    """Return (lazily creating) the MCP token-bucket rate limiter for *ip*."""
    async with _mcp_ip_limiters_lock:
        if ip not in _mcp_ip_limiters:
            _mcp_ip_limiters[ip] = TokenBucketRateLimiter(
                name=f"mcp_ip:{ip}",
                rate=_MCP_RATE_PER_SEC,
                burst=_MCP_RATE_BURST,
            )
        return _mcp_ip_limiters[ip]


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

        # ── Bearer token authentication ──────────────────────────────────────
        # Read the TINKER_MCP_TOKEN env var at init.  If set to a non-empty
        # string, every POST to /mcp/messages must include an
        # ``Authorization: Bearer <token>`` header whose value matches this
        # token.  If the env var is empty or unset, auth is disabled and all
        # requests are allowed (backwards-compatible with existing setups).
        self._auth_token: str = os.getenv("TINKER_MCP_TOKEN", "")

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
            # ── Bearer token authentication (same check as /messages) ────
            if self._auth_token:
                auth_header = request.headers.get("authorization", "")
                parts = auth_header.split(" ", 1)
                if len(parts) != 2 or parts[0] != "Bearer" or parts[1] != self._auth_token:
                    return JSONResponse(
                        {"error": "Unauthorized", "detail": "Missing or invalid Bearer token."},
                        status_code=401,
                    )

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
                        # We use a 30-second timeout: if no real event arrives
                        # within that window, we send a structured ping event
                        # to keep the connection alive and let clients track
                        # server health.
                        try:
                            event = await asyncio.wait_for(queue.get(), timeout=30.0)
                            yield f"event: message\ndata: {json.dumps(event)}\n\n"
                        except asyncio.TimeoutError:
                            # ── Heartbeat ping ───────────────────────────────
                            # Send a proper SSE "ping" event with a JSON body
                            # containing the current UTC timestamp.  MCP clients
                            # (including Tinker's own MCPClient) can use this to
                            # detect server liveness — if no ping arrives within
                            # 90 seconds, the server is likely down.
                            ping_data = json.dumps({
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            })
                            yield f"event: ping\ndata: {ping_data}\n\n"
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
            """
            JSON-RPC message endpoint.

            This handler performs three checks before processing a request:

            1. **Bearer token authentication** — if TINKER_MCP_TOKEN is set,
               the request must include a matching ``Authorization: Bearer <token>``
               header.  Missing or wrong tokens get HTTP 401.

            2. **Per-IP rate limiting** — each client IP gets a token-bucket
               rate limiter (default: 60 req/min).  If the bucket is empty the
               server returns a JSON-RPC error with code -32000 and standard
               rate-limit headers so the client knows when to retry.

            3. **JSON-RPC dispatch** — the body is parsed and routed to the
               appropriate handler (initialize, tools/list, tools/call, ping).
            """
            # ── Step 1: Bearer token authentication ──────────────────────────
            # If the operator configured a token via TINKER_MCP_TOKEN, we
            # enforce it here.  The auth flow is:
            #   1. Read the Authorization header from the incoming request.
            #   2. Expect the format "Bearer <token>".
            #   3. Compare the token value to our stored _auth_token.
            #   4. If it doesn't match (or is missing), reject with HTTP 401.
            # When _auth_token is empty, we skip this check entirely so that
            # existing deployments without a token continue to work.
            if self._auth_token:
                auth_header = request.headers.get("authorization", "")
                # Split "Bearer <token>" into parts.  A well-formed header has
                # exactly two parts: ["Bearer", "<actual-token>"].
                parts = auth_header.split(" ", 1)
                if len(parts) != 2 or parts[0] != "Bearer" or parts[1] != self._auth_token:
                    return JSONResponse(
                        {
                            "error": "Unauthorized",
                            "detail": "Missing or invalid Bearer token in Authorization header.",
                        },
                        status_code=401,
                    )

            # ── Step 2: Per-IP rate limiting ─────────────────────────────────
            # Get the client's IP address and look up (or create) their
            # per-IP token bucket.  We use try_acquire() which is non-blocking:
            # it returns immediately with (False, retry_after) if the bucket
            # is empty, rather than sleeping the event loop.
            ip = request.client.host if request.client else "127.0.0.1"
            limiter = await _mcp_limiter_for_ip(ip)
            acquired, retry_after = await limiter.try_acquire()

            if not acquired:
                # The client has exceeded the rate limit.  Return a JSON-RPC
                # error (code -32000 is the standard "server error" range) with
                # standard HTTP rate-limit headers so the client knows when to
                # retry.
                retry_secs = max(1, int(retry_after) + 1)
                reset_at = int(time.time()) + retry_secs
                return JSONResponse(
                    self._error_response(None, -32000, "Rate limit exceeded"),
                    status_code=200,
                    headers={
                        "X-RateLimit-Limit": str(int(_MCP_RATE_BURST)),
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": str(reset_at),
                    },
                )

            # ── Step 3: Parse and dispatch the JSON-RPC request ──────────────
            try:
                body = await request.json()
            except Exception:
                return JSONResponse(
                    self._error_response(None, -32700, "Parse error"), status_code=200
                )

            client_id = request.query_params.get("client_id")
            result = await self._handle_request(body, ip=ip)

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

    async def _handle_request(self, body: dict, *, ip: str = "unknown") -> dict:
        """
        Route a JSON-RPC 2.0 request to the appropriate handler.

        Parameters
        ----------
        body : The parsed JSON-RPC request body.
        ip   : The caller's IP address (used for audit logging on tools/call).
        """
        rpc_id = body.get("id")
        method = body.get("method", "")
        params = body.get("params", {})

        try:
            if method == "initialize":
                result = self._handle_initialize(params)
            elif method == "tools/list":
                result = self._handle_tools_list()
            elif method == "tools/call":
                result = await self._handle_tools_call(params, ip=ip)
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

    async def _handle_tools_call(self, params: dict, *, ip: str = "unknown") -> dict:
        """
        Execute a tool call and return the result.

        Every invocation is audit-logged at INFO level with a structured
        format so operators can grep logs for ``mcp_tool_call`` to see who
        called what, how long it took, and whether it succeeded.

        Parameters
        ----------
        params : The JSON-RPC ``params`` dict containing ``name`` and ``arguments``.
        ip     : The caller's IP address (for audit logging).
        """
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if not tool_name:
            raise ValueError("tools/call requires 'name' parameter")

        t0 = time.monotonic()
        result = await self._registry.execute(tool_name, **arguments)
        elapsed_ms = (time.monotonic() - t0) * 1000

        # ── Audit log ────────────────────────────────────────────────────────
        # Log every tool invocation at INFO level with a structured,
        # grep-friendly format.  This makes it easy for operators to monitor
        # tool usage, detect abuse, and debug failures:
        #   mcp_tool_call | tool=web_search ip=192.168.1.5 duration=342ms success=True
        logger.info(
            "mcp_tool_call | tool=%s ip=%s duration=%.0fms success=%s",
            tool_name,
            ip,
            elapsed_ms,
            result.success,
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
