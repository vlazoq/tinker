# Chapter 19 — Claude Code-Inspired Features

This chapter covers five features added to Tinker that are directly inspired by
Claude Code (Anthropic's official AI CLI). If you've used Claude Code, these
features will feel familiar — the ideas are the same, adapted for Tinker's
24/7 autonomous operation mode.

| Feature | Claude Code equivalent |
|---|---|
| **TINKER.md** | `CLAUDE.md` — persistent project instructions |
| **Confirmation gates** | interactive approval before risky actions |
| **Pause / resume** | `Ctrl-Z` / `fg` for long-running jobs |
| **Context summarization** | smart truncation of large context windows |
| **MCP support** | Model Context Protocol — tools & servers |

---

## 1. TINKER.md — Persistent Project Instructions

### What it is

`TINKER.md` is a Markdown file you write once and place in the root of your
Tinker directory. Tinker reads it at startup and injects its contents into the
Architect AI's system prompt on **every single micro loop**.

This is identical in concept to `CLAUDE.md` in Claude Code:

| | Claude Code | Tinker |
|---|---|---|
| File | `CLAUDE.md` | `TINKER.md` |
| Location | Project root | Project root |
| Read at | Session start | Process startup |
| Injected into | Claude's context | Architect system prompt |
| Purpose | Project conventions + constraints | Project conventions + constraints |

### Why it matters

Without `TINKER.md`, the Architect AI only knows what's in its base system
prompt and the current task's context. It doesn't know:

- Your project's technology stack ("use Redis, not Memcached")
- Decisions already made ("we chose gRPC over REST — do not revisit this")
- Conventions to follow ("every function must have a docstring and type hints")
- What to avoid ("never use pickle — security requirement")
- What's most important right now ("focus on the auth module this week")

With `TINKER.md`, all of that persists on every loop. The Architect can't
forget it because it's re-injected every time.

### How to use it

The `TINKER.md` file at the root of this repo is a comprehensive template with
instructions and examples. Fill in the sections:

```markdown
## Project Overview
We are building a distributed task queue for our home lab...

## Technology Stack (LOCKED)
- Language: Python 3.12
- Broker: Redis 7.x
- Web: FastAPI + HTMX

## Architecture Decisions Already Made
1. Tasks serialized as JSON (not pickle) — security requirement
2. Workers use asyncio — no threads

## Forbidden Patterns
- Do NOT use pickle or shelve
- Do NOT use threading.Thread
```

Then Tinker picks it up automatically on the next run.

### Configuration

```bash
# Default: ./TINKER.md (next to main.py)
# Override with:
TINKER_INSTRUCTIONS_PATH=/path/to/my-project-instructions.md
```

### How it works in code

1. `main.py` reads the file at startup via `Path(config.project_instructions_path).read_text()`
2. Calls `PromptBuilder.set_global_project_instructions(content)` (a class-level setter)
3. Every new `PromptBuilder` instance — including those created by the factory
   classmethods in `agents.py` — picks up the class-level default in its `__init__`
4. `_assemble_system()` inserts the instructions between the base system prompt
   and the build metadata on every call

If the file doesn't exist: a friendly INFO log, Tinker continues normally.

---

## 2. Confirmation Gates — Human Approval Before Risky Actions

### What it is

A confirmation gate pauses Tinker before an irreversible action and asks you
(the operator) to approve or deny it. Without the gate, Tinker acts immediately.
With the gate, it waits for a human decision.

```
Tinker about to push to main branch
       │
       ▼
  ┌────────────────────────────────────────┐
  │  TINKER CONFIRMATION REQUIRED          │
  │  id: abc12345                          │
  │  Action: git_push                      │
  │  Details: branch=main, remote=origin   │
  │                                        │
  │  Auto-approves in 300s                 │
  │  Approve? [y/N]                        │
  └────────────────────────────────────────┘
       │
    y ─┤─ n
       │    │
   Push     Push
   runs     cancelled
```

### Configuring gates

Add action names to `TINKER_CONFIRM_BEFORE` in `.env`:

```bash
# Gate these specific actions:
TINKER_CONFIRM_BEFORE=git_push,artifact_delete

# Wait up to 5 minutes for a response (then auto-approve)
TINKER_CONFIRM_TIMEOUT=300
```

Available action names:
- `git_push` — before Fritz pushes to any remote
- `artifact_delete` — before overwriting an existing artifact file
- `macro_snapshot` — before committing an architecture snapshot to git

Leave `TINKER_CONFIRM_BEFORE` empty (the default) to disable all gates.

### Two modes

**CLI mode** (when Tinker is running in your terminal):
Tinker prints the prompt to stdout and reads `y/N` from stdin.

**API / Dashboard mode** (when no terminal is available):
Tinker writes the pending request to the state snapshot and waits for the
Dashboard to call the confirm API:

```bash
# List pending confirmations
curl http://localhost:8082/api/confirmations

# Approve request abc12345
curl -X POST http://localhost:8082/api/confirm/abc12345 \
     -H "Content-Type: application/json" \
     -d '{"approved": true}'

# Deny
curl -X POST http://localhost:8082/api/confirm/abc12345 \
     -d '{"approved": false}'
```

### Timeout behaviour

`TINKER_CONFIRM_TIMEOUT` sets how long Tinker waits before auto-approving.

- `0` — wait forever (requires active monitoring; not safe for overnight runs)
- `300` — wait 5 minutes, then auto-approve (default; safe for overnight runs)
- `60` — 1 minute; good for semi-interactive sessions

If you want Tinker to be fully autonomous (auto-approve everything), just
don't set `TINKER_CONFIRM_BEFORE` at all.

### How it works in code

The `ConfirmationGate` class lives in `orchestrator/confirmation.py`.

The gate is created in `Orchestrator.__init__()` and stored as
`self.confirmation_gate`. Components that need gating receive a reference:

```python
# In main.py, after Orchestrator is created:
fritz.git_ops.confirmation_gate = orchestrator.confirmation_gate
```

Then in `fritz/git_ops.py`:
```python
async def push(self, branch, remote="origin", ...):
    if self.confirmation_gate is not None:
        allowed = await self.confirmation_gate.request(
            "git_push",
            details={"branch": branch, "remote": remote},
        )
        if not allowed:
            return FritzGitResult(ok=False, stderr="Cancelled by operator")
    # ...proceed with push
```

---

## 3. Pause / Resume — Stop Without Losing Work

### What it is

Pause and resume let you stop Tinker's main loop and restart it exactly where
it left off — without losing the micro loop that was in progress.

This is different from shutdown:
- **Shutdown** (`Ctrl-C` or `SIGTERM`): process exits; next `python main.py`
  starts from the top of the next task.
- **Pause**: orchestrator stops between micro loops; process stays alive.
  Call `resume()` to continue.
- **Checkpoint** (used by both): saves the in-progress state to disk so that
  even if the process is killed while paused, the next run can resume.

### Pausing via the Dashboard API

```bash
# Pause (orchestrator finishes current micro loop, then waits)
curl -X POST http://localhost:8082/api/pause

# Resume (orchestrator continues from where it stopped)
curl -X POST http://localhost:8082/api/resume
```

### Checking if Tinker is paused

```bash
curl http://localhost:8082/api/state | python3 -m json.tool | grep paused
# "paused": true
```

### What gets checkpointed

The checkpoint file (`tinker_checkpoint.json` by default) saves:

```json
{
  "version": 1,
  "created_at": "2025-01-15T14:32:10+00:00",
  "micro_iteration": 42,
  "current_task_id": "abc12345-...",
  "current_subsystem": "api_gateway",
  "subsystem_counts": {"api_gateway": 3, "auth_service": 1},
  "micro_history_tail": [...]
}
```

This is enough to resume without repeating the Architect/Critic calls —
the most expensive part of each micro loop.

**Not checkpointed** (because they're in durable stores that survive restarts):
- Task queue (SQLite)
- Memory / artifacts (DuckDB + ChromaDB)
- Architecture state (git)

### Crash recovery

If Tinker is killed mid-loop (power failure, OOM), the checkpoint file
stays on disk. On the next `python main.py`, this appears in the log:

```
INFO  Found checkpoint from micro iteration 42 — will resume from this point
```

The orchestrator restores the subsystem counts and current task ID, effectively
resuming without repeating work.

### Configuration

```bash
TINKER_CHECKPOINT_ENABLED=true         # default; set false to disable
TINKER_CHECKPOINT_PATH=./tinker_checkpoint.json
```

---

## 4. Grub Context Summarization

### What changed

Old behaviour (before this feature):
```python
# In ReviewerMinion.run():
design_excerpt = design_text[:3000]
if len(design_text) > 3000:
    design_excerpt += "\n\n[... design document truncated ...]"
```

New behaviour:
```python
# In ReviewerMinion.run():
design_excerpt = await self.compress_context(design_text, "design document")
```

### Why this matters

Hard truncation at 3000 characters deletes the tail of the document. The tail
often contains the most important decisions — the ones that were written last,
after extensive discussion. A design document that says:

```
... [2800 chars of context] ...
## Key Decision
After considering all options, we chose gRPC over REST because...
```

...gets its key decision deleted by hard truncation.

LLM-based compression reads the full document and produces a condensed version
that keeps the key decisions, function signatures, error messages, and
constraints while dropping verbose explanations and repetition.

### How it works

Every `BaseMinion` has a `_summarizer` attribute (a `MinionContextSummarizer`
instance). The public method `compress_context(text, label)` checks:

1. Is `text` already <= `GRUB_CONTEXT_MAX_CHARS` (6000)? → return unchanged
2. Is this a cached text (SHA-256 seen before this session)? → return cached result
3. Otherwise: call a small LLM model, cache the result, return the compressed text

If the LLM call fails (Ollama not reachable): log a warning, fall back to
hard truncation. The fallback only fires when the compression itself breaks,
not as the normal path.

### Configuration

```bash
GRUB_CONTEXT_SUMMARIZATION=true    # enable (default)
GRUB_CONTEXT_MAX_CHARS=6000        # trigger threshold
GRUB_CONTEXT_TARGET_CHARS=3000     # aim for this length after compression
GRUB_SUMMARIZER_MODEL=             # blank = use reviewer's model (qwen3:7b)
#GRUB_SUMMARIZER_MODEL=qwen3:1.7b  # set this for faster/cheaper compression
```

Disable to revert to the old behaviour:
```bash
GRUB_CONTEXT_SUMMARIZATION=false
```

---

## 5. MCP — Model Context Protocol

See `mcp/README.md` for the full reference. Here's the short version.

### What it is

MCP is an open standard by Anthropic for connecting AI models to external tools.
Claude Code uses it internally for everything. Tinker now supports it in both
directions:

**As a server**: Tinker's ToolRegistry tools become MCP tools that Claude Code
(or any other MCP client) can call directly.

**As a client**: Tinker connects to external MCP servers (filesystem, database,
another Tinker instance) and imports their tools into its ToolRegistry.

### Enable MCP

```bash
# In .env:
TINKER_MCP_ENABLED=true
```

This adds two routes to the webui:
- `GET  http://localhost:8082/mcp/sse`       — SSE stream
- `POST http://localhost:8082/mcp/messages`  — JSON-RPC messages

### Add Tinker to Claude Code

```json
// ~/.claude/mcp.json  (or your project's .mcp.json)
{
  "mcpServers": {
    "tinker": {
      "type": "http",
      "url": "http://localhost:8082/mcp/sse"
    }
  }
}
```

Now in a Claude Code session you can call:
```
/mcp tinker web_search "distributed systems patterns"
```

### Connect to external MCP servers

```bash
# In .env:
TINKER_MCP_SERVERS=http://nas:9000/mcp/sse
```

At startup, Tinker connects to the NAS server, imports all its tools (e.g.
`filesystem/read_file`, `filesystem/write_file`), and registers them in the
ToolRegistry. The Architect AI can then use those tools in research loops.

---

## Putting It All Together

Here is a `.env` that enables all five features:

```bash
# TINKER.md — project instructions
TINKER_INSTRUCTIONS_PATH=./TINKER.md

# Confirmation gates — approve git pushes manually
TINKER_CONFIRM_BEFORE=git_push
TINKER_CONFIRM_TIMEOUT=300

# Checkpointing — crash-safe pause/resume
TINKER_CHECKPOINT_ENABLED=true
TINKER_CHECKPOINT_PATH=./tinker_checkpoint.json

# Grub context summarization — smarter than truncation
GRUB_CONTEXT_SUMMARIZATION=true
GRUB_CONTEXT_MAX_CHARS=6000
GRUB_CONTEXT_TARGET_CHARS=3000

# MCP — expose tools to Claude Code and import external tools
TINKER_MCP_ENABLED=true
TINKER_MCP_SERVERS=http://nas:9000/mcp/sse
```

And a corresponding `TINKER.md` to guide the Architect:

```markdown
## Project: Home Lab Automation System

### Technology Stack (LOCKED)
- Python 3.12, Redis, FastAPI + HTMX
- Ollama for AI inference (no cloud APIs)

### Forbidden
- No pickle, no threading.Thread, no requests library

### Current Priority
Complete the task scheduling engine before starting the web interface.
```

With these in place, Tinker:
1. Reads your project instructions on every loop (**TINKER.md**)
2. Asks before pushing code you haven't reviewed (**confirmation gate**)
3. Survives power failures without losing work (**checkpoint**)
4. Reads long design docs without losing the important parts (**summarization**)
5. Shares its tools with Claude Code in the same project (**MCP**)

---

## Reference: New API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/api/confirmations` | GET | List pending confirmation requests |
| `/api/confirm/{id}` | POST | `{"approved": true/false}` — respond to a gate |
| `/api/pause` | POST | Ask orchestrator to pause after current micro loop |
| `/api/resume` | POST | Resume a paused orchestrator |
| `/api/mcp/status` | GET | MCP server info + connected clients + imported tools |

## Reference: New Environment Variables

| Variable | Default | Feature |
|---|---|---|
| `TINKER_INSTRUCTIONS_PATH` | `./TINKER.md` | TINKER.md |
| `TINKER_CONFIRM_BEFORE` | *(empty)* | Confirmation gates |
| `TINKER_CONFIRM_TIMEOUT` | `300` | Confirmation gates |
| `TINKER_CONFIRM_DIR` | `./tinker_confirmations` | Confirmation gates |
| `TINKER_CHECKPOINT_ENABLED` | `true` | Checkpoint |
| `TINKER_CHECKPOINT_PATH` | `./tinker_checkpoint.json` | Checkpoint |
| `TINKER_CONTROL_DIR` | `./tinker_control` | Pause/resume |
| `TINKER_MCP_ENABLED` | `false` | MCP |
| `TINKER_MCP_SERVER_PATH` | `/mcp` | MCP |
| `TINKER_MCP_SERVER_NAME` | `tinker` | MCP |
| `TINKER_MCP_SERVER_VERSION` | `1.0.0` | MCP |
| `TINKER_MCP_SERVERS` | *(empty)* | MCP client |
| `TINKER_MCP_CONNECT_TIMEOUT` | `10` | MCP client |
| `GRUB_CONTEXT_SUMMARIZATION` | `true` | Summarization |
| `GRUB_CONTEXT_MAX_CHARS` | `6000` | Summarization |
| `GRUB_CONTEXT_TARGET_CHARS` | `3000` | Summarization |
| `GRUB_SUMMARIZER_MODEL` | *(reviewer's model)* | Summarization |
