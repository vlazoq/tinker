# Tinker User Guide

A hands-on guide to running, monitoring, configuring, and getting results from
Tinker. If you haven't installed Tinker yet, start with [SETUP.md](SETUP.md).

---

## Table of Contents

1. [Your First Run](#1-your-first-run)
2. [What Happens When Tinker Runs](#2-what-happens-when-tinker-runs)
3. [Monitoring a Live Run](#3-monitoring-a-live-run)
4. [Reading the Output](#4-reading-the-output)
5. [Configuration Guide](#5-configuration-guide)
6. [The Web UI](#6-the-web-ui)
7. [The TUI Dashboard](#7-the-tui-dashboard)
8. [Using Fritz (Git Integration)](#8-using-fritz-git-integration)
9. [Using Grub (Code Generation)](#9-using-grub-code-generation)
10. [Makefile Targets](#10-makefile-targets)
11. [Troubleshooting](#11-troubleshooting)
12. [FAQ](#12-faq)

---

## 1. Your First Run

### Minimal start (no AI needed)

Test that everything works with stub models — no Ollama required:

```bash
python main.py --problem "Design a URL shortener" --stubs
```

You should see log output like:

```
14:23:01  INFO     tinker.main  Starting Tinker (stubs mode)
14:23:01  INFO     tinker.orchestrator  Micro loop #1 — task: "Identify core components"
14:23:01  INFO     tinker.orchestrator  Micro loop #1 complete (score: 0.75)
14:23:02  INFO     tinker.orchestrator  Micro loop #2 — task: ...
```

Press **Ctrl+C** to stop.

### Real run with AI models

Make sure Ollama is running and you've pulled the models:

```bash
ollama serve                          # start Ollama (skip if already running)
ollama pull qwen3:7b                  # primary model (Architect + Synthesizer)
ollama pull phi3:mini                 # secondary model (Critic)
```

Then start Tinker:

```bash
python main.py --problem "Design a distributed job queue system"
```

Each micro loop takes 30–120 seconds depending on your hardware. You'll see
architecture designs appearing in real time.

### With the terminal dashboard

```bash
python main.py --problem "Design a distributed cache layer" --dashboard
```

Or run the dashboard in a separate terminal while Tinker runs:

```bash
# Terminal 1
python main.py --problem "Design a distributed cache layer"

# Terminal 2
python -m ui.tui
```

---

## 2. What Happens When Tinker Runs

Tinker runs three nested loops automatically:

```
MICRO LOOP (every 30-120 seconds)
├── Pick highest-priority task from queue
├── Assemble context from memory (past work, research, arch state)
├── Architect AI designs a solution
├── Critic AI reviews and scores it
├── Store result as an artifact
└── Generate follow-up tasks

MESO LOOP (every 5 micro loops per subsystem)
├── Gather all recent artifacts for one subsystem
├── Synthesizer AI writes a cohesive design document
└── Update architecture state

MACRO LOOP (every 4 hours)
├── Synthesizer produces a full architecture snapshot
├── Commit snapshot to git (if auto-git enabled)
└── Version bumps (v1.0 → v1.1 → v2.0 ...)
```

Tinker runs **indefinitely** until you press Ctrl+C. It's designed for
long-running sessions — leave it running overnight and come back to a
complete architecture document.

---

## 3. Monitoring a Live Run

### Option A: Log output (simplest)

By default, Tinker logs to the terminal. Increase verbosity with:

```bash
python main.py --problem "..." --log-level DEBUG
```

### Option B: State file (non-intrusive)

Tinker writes its state to `./tinker_state.json` after every loop. You can
watch it in real time:

```bash
watch -n 2 cat tinker_state.json | python -m json.tool
```

The state file contains:
- `micro_loop_count` — total iterations completed
- `current_task` — what Tinker is working on right now
- `last_score` — Critic's score for the most recent design
- `subsystem_counts` — how many loops per subsystem
- `is_paused` — whether the orchestrator is paused
- `uptime_seconds` — how long this session has been running

### Option C: Web UI (full dashboard)

```bash
python -m ui.web                      # starts on http://localhost:8082
```

### Option D: Gradio or Streamlit

```bash
python -m ui.gradio                   # starts on http://localhost:7860
python -m ui.streamlit                # starts on http://localhost:8501
```

### Option E: TUI dashboard

```bash
python main.py --problem "..." --dashboard   # in-process
# or
python -m ui.tui                              # separate terminal
```

---

## 4. Reading the Output

### Where files go

| Location | What's in it |
|----------|-------------|
| `./tinker_state.json` | Live orchestrator state (loop counts, current task) |
| `./tinker_workspace/` | Architecture state JSON files (versioned snapshots) |
| `./tinker_artifacts/` | Design artifacts written to disk |
| `./tinker_diagrams/` | Generated architecture diagrams (Graphviz/Mermaid) |
| `./tinker_backups/` | Periodic database backups |
| `tinker_session.duckdb` | Session database (all artifacts from this run) |
| `./chroma_db/` | Research archive (semantic search index) |
| `tinker_tasks.sqlite` | Complete task history |
| `tinker_audit.sqlite` | Audit log of every action |

### Understanding artifacts

Each micro loop produces an **artifact** — a structured design document stored
in DuckDB. Artifacts contain:

```json
{
  "artifact_id": "550e8400-e29b...",
  "task_id": "...",
  "content": "## Component Design: Message Broker\n\n...",
  "knowledge_gaps": ["How does partition rebalancing work?"],
  "decisions": ["Use consistent hashing for partition assignment"],
  "open_questions": ["Should we support exactly-once delivery?"],
  "score": 0.82,
  "subsystem": "messaging",
  "loop_level": "micro",
  "created_at": "2025-03-28T14:23:01Z"
}
```

### Understanding the architecture state

The architecture state (`tinker_workspace/architecture_state.json`) is Tinker's
evolving understanding of the target system. It contains:

- **Components** — identified parts of the system (name, responsibility, dependencies)
- **Decisions** — design choices with rationale and confidence scores
- **Open questions** — things Tinker hasn't figured out yet
- **Interfaces** — how components talk to each other
- **Confidence scores** — 0.0 to 1.0; low confidence = needs more investigation

### Understanding scores

The Critic rates every design on a 0–1 scale:

| Score | Meaning |
|-------|---------|
| 0.0–0.3 | Poor — major gaps or flaws, will be sent back for revision |
| 0.3–0.5 | Below average — stored but generates improvement tasks |
| 0.5–0.7 | Decent — stored, generates follow-up refinement tasks |
| 0.7–0.9 | Good — stored, confidence in this area increases |
| 0.9–1.0 | Excellent — rare; Tinker moves focus elsewhere |

---

## 5. Configuration Guide

### The 10 most important environment variables

Copy `.env.example` to `.env` and adjust these first:

```bash
# Which models to use
TINKER_SERVER_MODEL=qwen3:7b          # Architect (bigger = better designs)
TINKER_SECONDARY_MODEL=phi3:mini      # Critic (smaller is fine)

# Where are the models?
TINKER_SERVER_URL=http://localhost:11434       # Primary Ollama server
# TINKER_SECONDARY_URL=http://192.168.1.50:11434  # Uncomment for 2-machine setup

# How fast should loops run?
TINKER_ARCHITECT_TIMEOUT=120          # Seconds to wait for Architect response
TINKER_CRITIC_TIMEOUT=60              # Seconds to wait for Critic response
TINKER_MESO_TRIGGER=5                 # Micro loops before meso synthesis
TINKER_MACRO_INTERVAL=14400           # Seconds between macro snapshots (4 hours)

# Logging
TINKER_LOG_LEVEL=INFO                 # DEBUG for maximum detail
TINKER_JSON_LOGS=false                # true for machine-readable logs
```

### One machine vs. two machines

**One machine** (default): Both models run on the same Ollama instance.
Set `TINKER_SERVER_URL` only; the secondary defaults to the same server.

**Two machines**: Run Ollama on both, set `TINKER_SECONDARY_URL` to the
second machine. The Critic runs on the secondary, freeing the primary
for the Architect.

```bash
# Machine A (your workstation — has the GPU)
TINKER_SERVER_URL=http://localhost:11434

# Machine B (a second box or Raspberry Pi with enough RAM)
TINKER_SECONDARY_URL=http://192.168.1.50:11434
```

### Making runs faster

```bash
TINKER_ARCHITECT_TIMEOUT=60           # Lower timeout (faster fail)
TINKER_MESO_TRIGGER=3                 # Synthesize more often
TINKER_SERVER_MAX_OUT=1024            # Shorter model responses
TINKER_SERVER_CTX=4096                # Smaller context window
```

### Making runs deeper (higher quality)

```bash
TINKER_ARCHITECT_TIMEOUT=300          # Give the model more time
TINKER_MESO_TRIGGER=10               # More micro loops per synthesis
TINKER_SERVER_MAX_OUT=4096            # Longer model responses
TINKER_SERVER_CTX=16384               # More context (needs more VRAM)
TINKER_TEMPERATURE=0.5                # More focused (less creative)
```

### Model presets

Tinker supports model presets — named configurations you can switch between:

```bash
# Via the web UI:
POST /api/models/presets
{
  "name": "fast-iteration",
  "primary_model": "qwen3:4b",
  "secondary_model": "phi3:mini",
  "temperature": 0.7
}

POST /api/models/presets/fast-iteration/activate
```

Presets are stored in `tinker_presets.json` and hot-reload at the next loop.

---

## 6. The Web UI

Start the web UI:

```bash
python -m ui.web                      # http://localhost:8082
```

### API endpoints reference

#### Health & Status
| Method | Path | What it does |
|--------|------|-------------|
| GET | `/api/health` | System health check |
| GET | `/api/state` | Current orchestrator state (loop counts, active task) |
| GET | `/api/version` | API version and schema version |
| GET | `/api/grub/status` | Grub pipeline status and queue stats |

#### Tasks
| Method | Path | What it does |
|--------|------|-------------|
| GET | `/api/tasks` | List all tasks with queue stats |
| POST | `/api/tasks/inject` | Inject a custom task into the queue |
| GET | `/api/dlq` | View Dead Letter Queue (failed operations) |
| POST | `/api/dlq/{id}/resolve` | Mark a DLQ item as resolved |

#### Configuration
| Method | Path | What it does |
|--------|------|-------------|
| GET | `/api/config` | Get current orchestrator settings |
| POST | `/api/config` | Update settings (hot-reload) |
| GET | `/api/flags` | Get feature flags |
| POST | `/api/flags/{name}` | Toggle a feature flag |

#### Orchestrator Control
| Method | Path | What it does |
|--------|------|-------------|
| POST | `/api/pause` | Pause the orchestrator between loops |
| POST | `/api/resume` | Resume a paused orchestrator |
| GET | `/api/confirmations` | View pending confirmation requests |
| POST | `/api/confirm/{id}` | Approve or deny a confirmation |

#### Models & Presets
| Method | Path | What it does |
|--------|------|-------------|
| GET | `/api/models/library` | List all known models |
| GET | `/api/models/presets` | List saved presets |
| POST | `/api/models/presets` | Create/update a preset |
| POST | `/api/models/presets/{name}/activate` | Switch to a preset |
| GET | `/api/models/active` | Current active preset |
| GET | `/api/models/ollama/available` | Query Ollama for installed models |

#### Fritz (Git)
| Method | Path | What it does |
|--------|------|-------------|
| GET | `/api/fritz/status` | Git branch, SHA, dirty files, remotes |
| POST | `/api/fritz/ship` | Commit and push (Fritz pipeline) |
| POST | `/api/fritz/push` | Push current branch |
| POST | `/api/fritz/pr` | Create a pull request |
| GET | `/api/fritz/verify` | Test GitHub/Gitea credentials |
| GET | `/api/fritz/recent-diffs` | Last N commits with diffs |

#### Observability
| Method | Path | What it does |
|--------|------|-------------|
| GET | `/api/audit` | Query audit log (filter by event, actor, trace) |
| GET | `/api/errors/recent` | Recent errors from DLQ |
| GET | `/api/errors/{trace_id}` | Detailed error by trace ID |
| GET | `/api/backups` | List available backups |
| POST | `/api/backups/trigger` | Trigger a manual backup |
| GET | `/api/logs/stream` | SSE stream of real-time state updates |
| GET | `/api/mcp/status` | MCP server and client status |

---

## 7. The TUI Dashboard

The terminal dashboard shows a live view of Tinker's operation:

```bash
python main.py --problem "..." --dashboard   # in-process
python -m ui.tui                              # separate terminal
```

Dashboard panels:
- **Loop Status** — micro/meso/macro counters, uptime, current phase
- **Active Task** — what Tinker is working on right now
- **Architect Output** — latest design proposal (scrollable)
- **Critic Output** — latest review with score
- **Task Queue** — upcoming tasks ranked by priority
- **Health** — connection status for Ollama, Redis, etc.
- **Architecture State** — current version, confidence map
- **Log Stream** — live log output

---

## 8. Using Fritz (Git Integration)

Fritz is Tinker's git agent. It can commit architecture snapshots, push to
remotes, and create pull requests.

### Enable auto-git

```bash
TINKER_AUTO_GIT=true                  # auto-commit after macro loops
```

### Manual git operations via web UI

```bash
# Check git status
curl http://localhost:8082/api/fritz/status

# Commit and push
curl -X POST http://localhost:8082/api/fritz/ship \
  -H "Content-Type: application/json" \
  -d '{"message": "Architecture snapshot v3.0"}'

# Create a PR
curl -X POST http://localhost:8082/api/fritz/pr \
  -H "Content-Type: application/json" \
  -d '{"title": "Architecture update", "body": "New snapshot from Tinker"}'
```

### Configure remotes

Fritz supports GitHub and Gitea. Configure via environment variables or
the Fritz config file:

```bash
FRITZ_CONFIG_FILE=./fritz_config.json
```

See [tutorial/18-fritz.md](tutorial/18-fritz.md) for the full Fritz guide.

---

## 9. Using Grub (Code Generation)

Grub turns Tinker's architecture designs into working code. It runs a
**minion pipeline**: Coder → Tester → Reviewer → Debugger → Refactorer.

### Configure Grub

```bash
GRUB_CODER_MODEL=qwen2.5-coder:32b   # Needs a coding-focused model
GRUB_REVIEWER_MODEL=qwen3:7b
GRUB_TESTER_MODEL=qwen3:7b
GRUB_EXEC_MODE=sequential             # or "parallel"
GRUB_MAX_ITERATIONS=5                 # refinement cycles per task
GRUB_QUALITY_THRESHOLD=0.75           # minimum score to accept code
```

### The pipeline

```
Tinker designs architecture
        │
        ▼
Grub receives design artifacts
        │
        ▼
Coder generates implementation
        │
        ▼
Tester writes and runs tests
        │
        ▼
Reviewer checks code quality
        │
        ├── Score too low → Debugger fixes issues → loop back
        │
        └── Score OK → Refactorer polishes → done
```

### Check Grub status

```bash
curl http://localhost:8082/api/grub/status
```

See [tutorial/15-grub-overview.md](tutorial/15-grub-overview.md) for the full
Grub guide.

---

## 10. Makefile Targets

```bash
make install          # Install production dependencies
make install-dev      # Install dev dependencies + tooling
make test             # Run full test suite (pytest)
make test-fast        # Run tests excluding slow/integration markers
make lint             # Check code style (ruff check + format check)
make format           # Auto-format code (ruff format)
make typecheck        # Run mypy type checker
make check            # lint + typecheck + test-fast (CI-friendly)
make clean            # Remove __pycache__, .pytest_cache, .mypy_cache
make audit            # Scan for CVEs in dependencies
make audit-fix        # Auto-fix CVE issues
```

---

## 11. Troubleshooting

### "Connection refused" to Ollama

```bash
# Check Ollama is running
curl http://localhost:11434/api/tags

# If not, start it
ollama serve
```

### "Model not found"

```bash
# List installed models
ollama list

# Pull the model Tinker expects
ollama pull qwen3:7b
ollama pull phi3:mini
```

### Micro loops are very slow

- Check your GPU memory: `nvidia-smi` — if VRAM is full, use a smaller model
- Lower context: `TINKER_SERVER_CTX=4096`
- Lower output length: `TINKER_SERVER_MAX_OUT=1024`
- Use a faster model: `TINKER_SERVER_MODEL=qwen3:4b`

### Tinker keeps working on the same subsystem

This is **subsystem fixation** — the anti-stagnation system should catch it.
If it persists:

- Lower `TINKER_MESO_TRIGGER` to force synthesis sooner
- Check the stagnation log: `sqlite3 tinker_stagnation.sqlite "SELECT * FROM events ORDER BY created_at DESC LIMIT 10"`
- Inject a task for a different subsystem via the web UI:
  ```bash
  curl -X POST http://localhost:8082/api/tasks/inject \
    -H "Content-Type: application/json" \
    -d '{"description": "Design the authentication subsystem", "subsystem": "auth"}'
  ```

### Critic gives the same score every time

- Try a different critic model: `TINKER_SECONDARY_MODEL=gemma2:2b`
- Increase temperature: `TINKER_TEMPERATURE=0.8`
- Check critic prompts: look at `core/prompts/templates.py` for the critic
  system prompt

### Web UI won't start

```bash
# Check if port is already in use
lsof -i :8082

# Use a different port
TINKER_WEBUI_PORT=9000 python -m ui.web
```

### Redis connection errors

Redis is optional — Tinker falls back to in-process memory. To suppress
the warnings:

```bash
# Either start Redis:
docker compose up -d redis

# Or disable Redis usage by not setting TINKER_REDIS_URL
unset TINKER_REDIS_URL
```

### How to reset and start fresh

```bash
# Remove all generated data (keeps config and code)
rm -f tinker_session.duckdb tinker_tasks.sqlite tinker_audit.sqlite
rm -f tinker_state.json tinker_checkpoint.json
rm -rf tinker_workspace/ tinker_artifacts/ tinker_diagrams/ chroma_db/
```

---

## 12. FAQ

**Q: How long should I let Tinker run?**
A: For a simple system (URL shortener, todo app), 30–60 minutes produces a
solid architecture. For complex systems (distributed database, real-time
analytics), leave it running for 4–12 hours to get through several macro
cycles.

**Q: Can I change the problem statement mid-run?**
A: Not directly. Stop with Ctrl+C, optionally clear the databases (see
"reset" above), and start with a new `--problem`.

**Q: Does Tinker use the internet?**
A: Only if SearXNG is configured (`TINKER_SEARXNG_URL`). The Researcher
agent uses it to look up technical references. Without SearXNG, Tinker
works entirely from the model's training knowledge.

**Q: Can I use OpenAI/Anthropic models instead of Ollama?**
A: Tinker is designed for local models via Ollama. The `TINKER_LLM_BACKEND`
variable supports `ollama` (default). An Azure OpenAI backend exists
experimentally (`AZURE_OPENAI_ENDPOINT`), but the primary path is Ollama.

**Q: Can I resume a previous session?**
A: Yes. If `TINKER_CHECKPOINT_ENABLED=true` (default), Tinker writes a
checkpoint file. Re-running with the same `--problem` will resume from
the checkpoint. Tasks, artifacts, and research from the previous run
persist in their databases.

**Q: What hardware do I need?**
A: Minimum: 16 GB RAM, any GPU with 6+ GB VRAM (for 7B model).
Recommended: 32–64 GB RAM, RTX 3090 or better (24 GB VRAM). The Critic
model (2-3B) can run on CPU if needed.

**Q: How do I see what Tinker has learned?**
A: Several ways:
- Read `tinker_workspace/architecture_state.json` for the latest snapshot
- Query artifacts: `python -c "import duckdb; db = duckdb.connect('tinker_session.duckdb'); print(db.sql('SELECT task_id, score, subsystem FROM artifacts ORDER BY created_at DESC LIMIT 10').fetchall())"`
- Use the web UI at `http://localhost:8082`
- Check the audit log: `sqlite3 tinker_audit.sqlite "SELECT event_type, created_at FROM events ORDER BY created_at DESC LIMIT 20"`
