# Grub — The Implementation Agent

Grub is Phase 2 of the Tinker system. Tinker thinks and designs; Grub reads
those designs and writes actual working code.

```
Your problem: "Build a billing API"
        │
        ▼
  Tinker (Phase 1)   →  architecture documents in tinker_artifacts/
        │                "The BillingService should have an InvoiceRepository
        │                 with create(), find_by_id(), and list_by_customer()..."
        │
        ▼
  Grub (Phase 2)     →  working, tested code in grub_output/
                         billing/invoice_repository.py   ← CoderMinion wrote this
                         tests/test_invoice_repository.py ← TesterMinion wrote this
                         (score: 0.83 / review passed)   ← ReviewerMinion checked
```

---

## What Grub Does

Grub reads Tinker's design artifacts and runs a team of specialized sub-agents
("Minions") to implement them:

| Minion | Job | Model |
|---|---|---|
| **CoderMinion** | Reads design doc, writes implementation code | `qwen2.5-coder:32b` |
| **ReviewerMinion** | Scores code quality and design alignment | `qwen3:7b` |
| **TesterMinion** | Writes pytest test files | `qwen3:7b` |
| **DebuggerMinion** | Reads failing test output, fixes the bug | `qwen2.5-coder:32b` |
| **RefactorerMinion** | Cleans up structure, naming, and duplication | `qwen2.5-coder:7b` |

Each Minion runs until the quality score reaches the configured threshold
(`quality_threshold`, default `0.75`) or the maximum retry count is hit.

---

## Execution Modes

Grub supports three execution modes. Change `execution_mode` in
`grub_config.json` or set `GRUB_EXEC_MODE`:

### Mode A: Sequential (default)

```
Task 1 → [all Minions] → result
Task 2 → [all Minions] → result
Task 3 → [all Minions] → result
```

One task at a time. Best for a single GPU with limited VRAM (e.g. your RTX
3090 running the 32B coder model — you can't run two 32B models in parallel).

```json
{ "execution_mode": "sequential" }
```

### Mode B: Parallel (async)

```
Task 1 → [Minions] ─┐
Task 2 → [Minions]   ├── all running concurrently
Task 3 → [Minions] ─┘
```

Multiple tasks run as asyncio tasks simultaneously. Best when different Minions
use different machines (your 3090 for the Coder, a secondary machine for the
Reviewer) so there's no VRAM contention.

```json
{ "execution_mode": "parallel" }
```

### Mode C: Queue (SQLite-backed)

```
SQLite queue  →  Worker 1 (this machine)
              →  Worker 2 (NAS)
              →  Worker 3 (laptop, when awake)
```

Tasks sit in a SQLite database. Workers poll the queue, pick up tasks, and
submit results. Workers can be started and stopped independently. Best for
a multi-machine setup where you want to add more workers without restarting
the coordinator.

```json
{ "execution_mode": "queue", "queue_workers": 2 }
```

**Switching modes:**
1. Open `grub_config.json`
2. Change `"execution_mode"` to `"sequential"`, `"parallel"`, or `"queue"`
3. Restart Grub

Or via env var (no config file change needed):
```bash
GRUB_EXEC_MODE=parallel python -m grub --problem "..."
```

---

## Configuration

`grub_config.json` is auto-created on first run. Edit it directly or use env
vars to override any field.

### Key settings

```json
{
  "execution_mode": "sequential",
  "quality_threshold": 0.75,
  "max_iterations": 5,
  "output_dir": "./grub_output",
  "models": {
    "coder":      "qwen2.5-coder:32b",
    "reviewer":   "qwen3:7b",
    "tester":     "qwen3:7b",
    "debugger":   "qwen2.5-coder:32b",
    "refactorer": "qwen2.5-coder:7b"
  },
  "ollama_urls": {
    "coder":      "http://localhost:11434",
    "reviewer":   "http://secondary:11434",
    "tester":     "http://secondary:11434",
    "debugger":   "http://localhost:11434",
    "refactorer": "http://localhost:11434"
  }
}
```

The `ollama_urls` dict lets each Minion talk to a different Ollama instance —
point the heavy coders at your 3090 and the reviewers at a lighter machine.

### Full environment variable reference

```bash
# Execution
GRUB_EXEC_MODE=sequential      # sequential | parallel | queue

# Models
GRUB_CODER_MODEL=qwen2.5-coder:32b
GRUB_REVIEWER_MODEL=qwen3:7b
GRUB_TESTER_MODEL=qwen3:7b
GRUB_DEBUGGER_MODEL=qwen2.5-coder:32b
GRUB_REFACTORER_MODEL=qwen2.5-coder:7b

# All Minions use the same Ollama instance by default
GRUB_OLLAMA_URL=http://localhost:11434

# Quality & retries
GRUB_QUALITY_THRESHOLD=0.75    # 0.0–1.0; Reviewer score needed to accept output
GRUB_MAX_ITERATIONS=5          # max retries before giving up

# Paths
GRUB_OUTPUT_DIR=./grub_output
GRUB_ARTIFACTS_DIR=./grub_artifacts
TINKER_TASK_DB=tinker_tasks_engine.sqlite   # where Grub reads Tinker tasks from
TINKER_ARTIFACTS_DIR=./tinker_artifacts     # where Grub reads design documents from

# Queue mode
GRUB_QUEUE_DB=grub_queue.sqlite
GRUB_QUEUE_WORKERS=2

# Optional
GRUB_ENABLE_GIT=false          # true to auto-commit output via git
GRUB_REQUEST_TIMEOUT=120.0     # seconds to wait for Ollama

# Context summarization (see below)
GRUB_CONTEXT_SUMMARIZATION=true
GRUB_CONTEXT_MAX_CHARS=6000
GRUB_CONTEXT_TARGET_CHARS=3000
GRUB_SUMMARIZER_MODEL=          # blank = use reviewer's model
```

---

## Context Summarization

When design documents or stack traces grow large (> 6000 characters by
default), Grub's old behaviour was to hard-truncate them at 3000 characters
with a `[... truncated ...]` marker. This often cut off the most important
part — the architectural decisions buried at the end of a long document.

The new behaviour: use a small, fast LLM to **compress** the text to ~3000
characters while preserving all the technically important content. The
compressor is instructed to keep function signatures, class names, error
messages, and constraints, and to drop verbose explanations and repetition.

The result is shorter *and* more informative than a truncated excerpt.

**Compression is transparent** — the Minion calls `compress_context()` and
gets back a string. If the text was short enough, it comes back unchanged.
If the LLM compression call itself fails (Ollama not reachable), the fallback
is hard truncation with a clear marker.

To disable and revert to the old truncation:
```bash
GRUB_CONTEXT_SUMMARIZATION=false
```

---

## Skills

Each Minion gets a set of "skills" — plain text files injected at the end of
its system prompt to give it extra expertise.

Skills live in `grub/skills/` and are mapped to Minions in `grub_config.json`:

```json
{
  "minion_skills": {
    "coder":      ["python_expert.md", "clean_code.md", "software_architecture.md"],
    "reviewer":   ["clean_code.md", "security_review.md"],
    "tester":     ["python_expert.md", "testing_patterns.md"],
    "debugger":   ["python_expert.md", "clean_code.md"],
    "refactorer": ["python_expert.md", "clean_code.md"]
  }
}
```

To add a skill:
1. Create a `.md` file in `grub/skills/`
2. Add the filename to the relevant Minion's list in `grub_config.json`
3. Restart Grub

Skills are plain text — write them like a reference card or style guide that
an expert would hand to a junior developer before a task.

---

## Module Layout

```
grub/
├── __init__.py              # Public API + quick-start example
├── config.py                # GrubConfig dataclass (all settings, auto-save)
├── context_summarizer.py    # MinionContextSummarizer (LLM-based compression)
├── contracts/
│   ├── task.py              # GrubTask dataclass (input to every Minion)
│   └── result.py            # MinionResult dataclass (output from every Minion)
├── minions/
│   ├── base.py              # BaseMinion ABC (shared LLM call, compress_context)
│   ├── coder.py             # CoderMinion
│   ├── reviewer.py          # ReviewerMinion
│   ├── tester.py            # TesterMinion
│   ├── debugger.py          # DebuggerMinion
│   ├── refactorer.py        # RefactorerMinion
│   └── registry.py          # Minion factory — creates instances from config
├── skills/                  # Plain text skill files
│   ├── python_expert.md
│   ├── clean_code.md
│   ├── security_review.md
│   └── testing_patterns.md
├── tools/
│   ├── file_ops.py          # read_file(), write_file(), ensure_dir()
│   ├── shell.py             # run_tests(), check_syntax()
│   └── code_analysis.py     # summarise_file() (line count, imports, functions)
└── tinker_bridge.py         # reads Tinker tasks, reports results back
```

---

## Common Issues

**Reviewer score is always below threshold**
: Lower `quality_threshold` (e.g. `0.60`) or check if the design artifact
  exists at `task.artifact_path`.

**CoderMinion produces no code blocks**
: The 32B coder model sometimes wraps output in text instead of code fences.
  Check `grub_output/` — the files may have been written anyway via the
  filepath fallback. Also try increasing `max_iterations`.

**Debugger can't find the fix after 3 iterations**
: Check `task.context['test_file']` — if the test file path is wrong, the
  Debugger can't re-run tests after applying fixes.

**Context summarization calls are slow**
: Set `GRUB_SUMMARIZER_MODEL=qwen3:1.7b` to use the smallest available model.
  Or `GRUB_CONTEXT_SUMMARIZATION=false` to disable it entirely.

See also: `docs/tutorial/15-grub-overview.md`, `docs/tutorial/16-grub-minions.md`,
`docs/tutorial/17-grub-integration.md`
