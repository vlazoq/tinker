# MCP — Model Context Protocol Support for Tinker

MCP (Model Context Protocol) is an open standard by Anthropic that defines how
AI models connect to external tools and data sources — think of it as USB for
AI agents. Any MCP client can use any MCP server without custom glue code.

Claude Code (Anthropic's CLI) is built entirely on MCP. Tinker's MCP support
means that Claude Code sessions and Tinker instances running on the same machine
can share tools and coordinate through a common protocol layer.

---

## Two Directions

Tinker's MCP implementation works in both directions:

### 1. Tinker as an MCP Server → Claude Code uses Tinker's tools

Every tool in Tinker's `ToolRegistry` (web search, artifact writer, memory
query, diagram generator, etc.) becomes available as an MCP tool. Once you
configure Claude Code to point at Tinker's MCP endpoint, you can type something
like `@tinker search for distributed systems patterns` in a Claude Code session
and Claude will call Tinker's `web_search` tool directly.

```
Claude Code (MCP client)
        │  JSON-RPC 2.0 over HTTP/SSE
        ▼
  Tinker webui
  ├── GET  /mcp/sse        ← SSE stream (keep-alive)
  └── POST /mcp/messages   ← JSON-RPC requests
        │
        ▼
  ToolRegistry
  ├── web_search
  ├── artifact_writer
  ├── memory_query
  └── ...
```

### 2. Tinker as an MCP Client → Tinker uses tools from external servers

External MCP servers (a filesystem server on the NAS, a database server, another
Tinker instance) expose tools that Tinker's Architect AI can call during research
loops. At startup, Tinker connects to each configured server, imports its tools,
and registers them alongside the native tools.

```
External MCP server (filesystem on NAS)
  ├── read_file
  ├── write_file
  └── list_directory
        │  JSON-RPC 2.0 over HTTP
        ▼
  Tinker
  └── ToolRegistry
      ├── web_search           (native)
      ├── filesystem/read_file  (imported from NAS MCP server)
      └── filesystem/write_file (imported from NAS MCP server)
```

---

## Quick Start

### Enable MCP in `.env`

```bash
TINKER_MCP_ENABLED=true
```

That's it to enable the server. Tinker's webui will now serve:
- `GET  http://localhost:8082/mcp/sse`
- `POST http://localhost:8082/mcp/messages`

### Add Tinker to Claude Code

In your Claude Code project's `.mcp.json` (or `~/.claude/mcp.json`):

```json
{
  "mcpServers": {
    "tinker": {
      "type": "http",
      "url": "http://localhost:8082/mcp/sse"
    }
  }
}
```

Then in Claude Code, you can call Tinker's tools directly:
```
/mcp tinker tools/list
```

### Connect Tinker to external MCP servers

```bash
# In .env:
TINKER_MCP_SERVERS=http://nas:9000/mcp/sse,http://desktop:9001/mcp/sse
```

On startup, Tinker will connect to both servers and import their tools.

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `TINKER_MCP_ENABLED` | `false` | Set `true` to enable MCP |
| `TINKER_MCP_SERVER_PATH` | `/mcp` | URL path prefix on the webui |
| `TINKER_MCP_SERVER_NAME` | `tinker` | Name in initialize handshake |
| `TINKER_MCP_SERVER_VERSION` | `1.0.0` | Version in initialize handshake |
| `TINKER_MCP_SERVERS` | *(empty)* | Comma-separated external server SSE URLs |
| `TINKER_MCP_CONNECT_TIMEOUT` | `10` | Seconds to wait when connecting to an external server |

---

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/mcp/sse` | GET | SSE stream — clients connect here first |
| `/mcp/messages` | POST | JSON-RPC 2.0 endpoint for method calls |
| `/api/mcp/status` | GET | Dashboard: server info + connected clients + imported tools |

### Example: call a tool from curl

```bash
# First, open the SSE stream (in one terminal):
curl -N http://localhost:8082/mcp/sse

# In the second line of output you'll see the messages URL, e.g.:
# event: endpoint
# data: {"uri":"/mcp/messages?client_id=abc12345"}

# Then POST a tools/call to that URL:
curl -X POST "http://localhost:8082/mcp/messages?client_id=abc12345" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
      "name": "web_search",
      "arguments": {"query": "distributed systems patterns"}
    }
  }'
```

### Example: list available tools

```bash
curl -X POST http://localhost:8082/mcp/messages \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

### Check MCP status

```bash
curl http://localhost:8082/api/mcp/status
# Returns:
# {
#   "enabled": true,
#   "server": {
#     "path": "/mcp",
#     "name": "tinker",
#     "version": "1.0.0",
#     "connected_clients": 1,
#     "tools_exposed": 8
#   },
#   "clients": {
#     "configured_servers": ["http://nas:9000/mcp/sse"],
#     "imported_tools": ["filesystem/read_file", "filesystem/write_file"]
#   }
# }
```

---

## Protocol Reference

Tinker implements the MCP 2024-11-05 protocol spec.

### Supported JSON-RPC methods

| Method | Description |
|---|---|
| `initialize` | Handshake: client sends capabilities, server responds with its name/version/capabilities |
| `tools/list` | Returns list of all available tools with their input schemas |
| `tools/call` | Executes a tool and returns the result |
| `ping` | Keep-alive check (always returns `{}`) |

### Tool result format

```json
{
  "content": [{"type": "text", "text": "...result..."}],
  "isError": false
}
```

On error: `"isError": true` and `"text"` contains the error message.

---

## Module Layout

```
mcp/
├── __init__.py   # Module docstring explaining MCP and its role in Tinker
├── config.py     # MCPConfig dataclass (all settings + from_env() factory)
├── server.py     # MCPServer (SSE transport + JSON-RPC dispatch)
├── client.py     # MCPClient + RemoteMCPTool (connects to external servers)
└── bridge.py     # MCPBridge (wires server + client, status reporting)
```

Integration point in `main.py`:
```python
from mcp.bridge import MCPBridge
from mcp.config import MCPConfig

mcp_config = MCPConfig.from_env()
if mcp_config.enabled:
    bridge = MCPBridge(mcp_config, tool_registry=tool_layer)
    bridge.mount_server(webui_app)        # adds /mcp/sse + /mcp/messages routes
    await bridge.connect_clients()        # imports remote tools
```

---

## Common Questions

**Does MCP work without the webui running?**
No — MCP shares the webui's HTTP port (default 8082). If the webui is not
running, MCP is not reachable either.

**Can I use MCP with Claude Code in the same terminal as Tinker?**
Yes. Start Tinker with `python main.py --problem "..."`, enable MCP in `.env`,
then add the server to your `~/.claude/mcp.json`. Claude Code can then call
Tinker's tools in any session.

**What happens if an external MCP server is offline at startup?**
It is logged as a warning and skipped. Tinker starts normally without those
tools. Reconnection is not automatic — restart Tinker to re-attempt the
connection.

**Can I write my own MCP server for Tinker to consume?**
Yes. Any server that implements the MCP 2024-11-05 spec over HTTP/SSE transport
will work. The simplest starting point is the reference implementation from
Anthropic: https://github.com/modelcontextprotocol/python-sdk
