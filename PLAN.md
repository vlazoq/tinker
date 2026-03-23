# Tinker Enhancement Plan

## Branch: `claude/fix-tinker-integration-J2ZPQ`

Five features, implemented in order of dependency (least-coupled first).

---

## Item 1 — Human-in-the-Loop Confirmation Gates

### Goal
Before destructive/irreversible actions, Tinker pauses and asks for human
approval. Configurable list of action names. Works in both CLI mode (stdin/stdout)
and Dashboard mode (new API endpoint).

### New files
- `orchestrator/confirmation.py` — `ConfirmationGate` class

### Modified files
- `orchestrator/config.py` — add `confirm_before: list[str]` field (default `[]`)
  and `confirm_timeout_seconds: float` (default `300.0`, auto-approve after 5 min)
- `orchestrator/state.py` — add `pending_confirmations: dict[str, dict]` to
  `OrchestratorState.to_dict()` so Dashboard can display them
- `fritz/git_ops.py` — call gate before any push operation
- `tools/artifact_writer.py` — call gate before delete/overwrite operations
- `orchestrator/orchestrator.py` — expose `confirmation_gate` attribute, wire
  into `__init__`
- `ui/web/app.py` — add `POST /api/confirm/{request_id}` endpoint (approve/deny)
  and `GET /api/confirmations` (list pending)

### How it works
```
action triggers → ConfirmationGate.request("git_push", details) →
  if "git_push" not in confirm_before: return approved immediately
  else: create pending request (UUID) → write to state → pause with asyncio.Event
  → Dashboard polls state, sees pending → user clicks Approve/Deny →
    POST /api/confirm/{id} → event set → gate returns True/False
  OR: CLI prints to stdout, reads stdin (y/n), returns immediately
  OR: timeout expires → auto-approve (logged as WARNING)
```

### Config example
```python
OrchestratorConfig(
    confirm_before=["git_push", "artifact_delete", "macro_snapshot"],
    confirm_timeout_seconds=300.0,
)
# or via env: TINKER_CONFIRM_BEFORE="git_push,artifact_delete"
```

---

## Item 2 — Mid-Run Pause / Resume (Checkpointing)

### Goal
1. **Pause** — Operator can pause Tinker between micro loop steps; state is
   serialized to disk. Distinct from shutdown (process stays alive, or can be
   restarted and resumed).
2. **Resume** — On startup, if a checkpoint file exists, restore state and
   continue from the saved point rather than re-running completed steps.
3. **Crash recovery** — Even if process is killed, checkpoint lets the next
   run skip already-completed work.

### New files
- `orchestrator/checkpoint.py` — `CheckpointManager` class

### Modified files
- `orchestrator/config.py` — add `checkpoint_path: str` (env `TINKER_CHECKPOINT_PATH`,
  default `"./tinker_checkpoint.json"`) and `checkpoint_enabled: bool` (default `True`)
- `orchestrator/state.py` — add `paused: bool` flag to `OrchestratorState`
- `orchestrator/orchestrator.py`:
  - `pause()` method — sets `_pause_event`, serializes checkpoint, marks state
  - `resume()` method — clears `_pause_event`, deletes checkpoint file
  - `_main_loop()` — check `_pause_event` between micro loops (interruptible wait)
  - `__init__` — accept optional `CheckpointManager`, restore from checkpoint if found
- `orchestrator/micro_loop.py` — accept `checkpoint_manager` kwarg; write
  checkpoint after Architect step (so resume skips re-calling Architect) and
  after Critic step
- `main.py` — create `CheckpointManager`, check for existing checkpoint, pass
  to Orchestrator
- `ui/web/app.py` — add `POST /api/pause`, `POST /api/resume` endpoints

### Checkpoint file format
```json
{
  "version": 1,
  "created_at": "2025-01-01T00:00:00Z",
  "micro_iteration": 42,
  "current_task": { "id": "...", "description": "..." },
  "assembled_context_hash": "sha256:...",
  "architect_result": { "content": "...", "tokens_used": 1234 },
  "critic_iterations_done": 0,
  "subsystem_counts": { "auth_service": 3, "api_gateway": 2 },
  "micro_history_tail": [ ... ]
}
```

### Resume logic
```
startup → CheckpointManager.load() →
  if no file: start fresh
  if file found:
    - restore subsystem_counts, micro_history_tail into OrchestratorState
    - if architect_result present: skip Architect call, go straight to Critic
    - if critic_iterations_done > 0: inject previous feedback, continue loop
    - log: "Resuming from checkpoint at micro iteration 42"
```

---

## Item 3 — Grub Context Summarization

### Goal
Replace hard truncation in Grub minions with LLM-based compression. When
context (code files, design docs, prior output) exceeds a configurable limit,
a small/fast model summarizes it rather than cutting it off mid-sentence.

### New files
- `grub/context_summarizer.py` — `MinionContextSummarizer` class

### Modified files
- `grub/config.py` — add:
  - `context_summarization_enabled: bool` (default `True`)
  - `context_max_chars: int` (default `6000`)
  - `context_target_chars: int` (default `3000`, target after compression)
  - `summarizer_model: str` (default `""`, falls back to Grub's own `model`)
- `grub/minions/base.py` — instantiate `MinionContextSummarizer`; expose
  `self.compress_context(text)` helper that minions can call
- `grub/minions/reviewer.py` — replace 2000-char design truncation with
  `self.compress_context(design_content)`
- `grub/minions/coder.py` — compress prior artifact context before injection
- `grub/minions/debugger.py` — compress stack traces / prior output
- `grub/contracts/result.py` — replace 2000-char `to_dict()` truncation:
  summarizer is too heavy here (no LLM access), so keep truncation but raise
  to 4000 chars and add `[TRUNCATED — use result.output directly]` marker

### How summarization works
```python
class MinionContextSummarizer:
    def __init__(self, llm_client, model, max_chars, target_chars):
        self._cache = {}  # hash(text) → summary

    async def compress(self, text: str, label: str = "context") -> str:
        if len(text) <= self.max_chars:
            return text  # no compression needed
        key = hashlib.sha256(text.encode()).hexdigest()
        if key in self._cache:
            return self._cache[key]
        summary = await self._llm_summarize(text, label)
        self._cache[key] = summary
        return summary

    async def _llm_summarize(self, text, label):
        prompt = f"""Compress the following {label} to ~{target_chars} chars.
Preserve: key decisions, identified issues, function signatures, error messages.
Drop: verbose explanations, repeated content, boilerplate.

{text}"""
        # Single LLM call, no retry needed for summarization
        return await self._client.complete(prompt, max_tokens=800)
```

---

## Item 4 — MCP Support (Model Context Protocol)

### Goal
- **Server side**: Expose Tinker's existing tools (web_search, artifact_writer,
  etc.) as an MCP server so external clients (other Claude instances, Claude Code)
  can call them.
- **Client side**: Connect to external MCP servers and import their tools into
  Tinker's ToolRegistry, so the Architect can use them transparently.

### New files
- `mcp/__init__.py`
- `mcp/config.py` — `MCPConfig` dataclass (server port, client server URLs, auth)
- `mcp/server.py` — MCP server (HTTP/SSE transport, JSON-RPC 2.0)
  - Exposes each tool in ToolRegistry as an MCP tool
  - Implements `tools/list` and `tools/call` MCP methods
- `mcp/client.py` — MCP client
  - Connects to external MCP server URLs
  - Fetches their tool list
  - Wraps each remote tool as a `BaseTool` subclass
- `mcp/bridge.py` — `MCPBridge` class
  - Starts server (optional)
  - Connects to configured client servers
  - Calls `registry.register_many(*remote_tools)` to add them

### Modified files
- `tools/registry.py` — add `register_from_mcp(bridge)` convenience method
- `main.py` — if `TINKER_MCP_ENABLED=true`, create `MCPBridge`, call
  `bridge.start_server()` and `bridge.connect_clients()`, then register tools
- `ui/web/app.py` — add `GET /api/mcp/status` (connected servers, available tools)
- `orchestrator/config.py` — no changes needed (MCP config is separate)

### Protocol notes
MCP uses JSON-RPC 2.0. Transport options:
- `stdio` — for local processes (Claude Code spawns Tinker as subprocess)
- `sse` — HTTP Server-Sent Events for remote (Tinker runs as HTTP server)

We implement SSE transport (simpler for a server that's already running HTTP).
Use `httpx` for the client (already likely in the project) — no external MCP
SDK required, keeping the dependency footprint small.

### MCP server endpoints (SSE transport)
```
GET  /mcp/sse              — SSE stream (client connects here first)
POST /mcp/messages         — JSON-RPC messages from client
```

### Example: external usage
```json
// In Claude Code's mcp_servers config:
{
  "tinker": {
    "transport": "sse",
    "url": "http://localhost:8765/mcp/sse"
  }
}
```

---

## Item 5 — TINKER.md (Persistent Instruction File)

### Goal
A human-editable markdown file at the project root that Tinker reads at startup
and injects into the Architect's system prompt. Lets teams encode project-specific
constraints, conventions, and context without touching Python config files.

Heavily documented for people who have never seen this pattern before.

### New files
- `TINKER.md` — the instruction file itself (very detailed, with examples)
  Sections:
  1. What is TINKER.md? (beginner explanation)
  2. How does Tinker use this file? (runtime injection)
  3. How is this similar to CLAUDE.md in Claude Code? (comparison)
  4. Project context (fill in: what are we building?)
  5. Architecture constraints (forbidden patterns, required conventions)
  6. Preferred libraries and tools
  7. Design decisions already made (don't revisit)
  8. Output format requirements
  9. Full worked examples

### Modified files
- `prompts/builder.py` — add `with_project_instructions(content: str)` method
  that prepends the TINKER.md content to the Architect's system prompt (after
  the base system prompt, before the task-specific section)
- `main.py` — at startup, try to read `TINKER.md` (or `TINKER_INSTRUCTIONS_PATH`
  env var path); if found, pass content to `PromptBuilder`; log warning if not found
- `orchestrator/config.py` — add `project_instructions_path: str` (env
  `TINKER_INSTRUCTIONS_PATH`, default `"./TINKER.md"`)

### TINKER.md injection point
```
System prompt structure with TINKER.md:

[Base Architect system prompt]
[TINKER.md content — project-specific constraints]
[Task-specific context — assembled per micro loop]
```

---

## Implementation Order

1. Item 5 (TINKER.md) — pure additions, zero risk, no existing code broken
2. Item 3 (Grub summarization) — self-contained in grub/
3. Item 1 (Confirmation gates) — adds pause points, needs care in orchestrator
4. Item 2 (Checkpoint/resume) — most complex, touches orchestrator core
5. Item 4 (MCP) — new subsystem, largely additive

---

## Files Created (new)
- `orchestrator/confirmation.py`
- `orchestrator/checkpoint.py`
- `grub/context_summarizer.py`
- `mcp/__init__.py`
- `mcp/config.py`
- `mcp/server.py`
- `mcp/client.py`
- `mcp/bridge.py`
- `TINKER.md`

## Files Modified
- `orchestrator/config.py` (items 1, 2, 5)
- `orchestrator/state.py` (items 1, 2)
- `orchestrator/orchestrator.py` (items 1, 2)
- `orchestrator/micro_loop.py` (item 2)
- `grub/config.py` (item 3)
- `grub/minions/base.py` (item 3)
- `grub/minions/reviewer.py` (item 3)
- `grub/minions/coder.py` (item 3)
- `grub/minions/debugger.py` (item 3)
- `grub/contracts/result.py` (item 3)
- `fritz/git_ops.py` (item 1)
- `tools/artifact_writer.py` (item 1)
- `tools/registry.py` (item 4)
- `prompts/builder.py` (item 5)
- `main.py` (items 2, 3, 4, 5)
- `ui/web/app.py` (items 1, 2, 4)
