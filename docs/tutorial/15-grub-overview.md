# Chapter 15 — Grub: The Implementation Agent

## The Problem

Tinker (Chapters 0–14) is a design thinking machine.  It produces
architecture documents — Markdown files describing what should be built.
But it never writes code.

You need Phase 2: an agent that reads those design documents and
**actually implements the software**.

That agent is **Grub**.

---

## Naming

| Name | Role |
|------|------|
| **Grub** | The orchestrator — reads Tinker's designs, delegates to Minions, judges results |
| **Minion** | A specialized sub-agent focused on one type of coding task |
| **Skill** | A plain text file injected into a Minion's prompt to give it extra expertise |

---

## The Full Two-Phase System

```
┌─────────────────────────────────────────────────────────────────┐
│  TINKER (Phase 1 — Design)                                       │
│                                                                  │
│  Problem → Tasks → Architect → Critic → Synthesize → Artifacts  │
│                                                          │        │
│                                 ┌────────────────────────┘        │
│                                 │  implementation tasks           │
└─────────────────────────────────┼───────────────────────────────┘
                                  │
┌─────────────────────────────────▼───────────────────────────────┐
│  GRUB (Phase 2 — Implementation)                                 │
│                                                                  │
│  Coder → Reviewer → Tester → Debugger → Refactorer → Code       │
│                                                  │               │
│                          ┌───────────────────────┘               │
│                          │  review/improvement tasks             │
└──────────────────────────┼──────────────────────────────────────┘
                           │
                    back to Tinker
```

The feedback loop:
1. Tinker creates an `implementation` task with a link to a design artifact
2. Grub picks it up, implements it through the Minion pipeline
3. Grub writes results back to Tinker as a `review` task
4. Tinker sees what was built, decides if the design needs updating
5. Loop repeats

---

## The Three Execution Modes

Grub supports three modes.  You switch between them in `grub_config.json`
without changing any code.

### Mode A — Sequential (Default)

```
Task 1 → [Coder → Reviewer → Tester → Debugger → Refactorer] → done
Task 2 → [Coder → Reviewer → Tester → Debugger → Refactorer] → done
...
```

**Use when**: Single PC, limited VRAM, getting started.
**Config**: `"execution_mode": "sequential"`

### Mode B — Parallel

```
Task 1 → pipeline ──────────────────────────────────► done
Task 2 → pipeline ──────────────────────────────────► done
Task 3 → pipeline ──────────────────────────────────► done
         (all running concurrently via asyncio)
```

**Use when**: Multiple independent tasks, different models on different GPUs.
**Config**: `"execution_mode": "parallel"`

⚠️ Warning: if all tasks use the same large model (e.g. `qwen2.5-coder:32b`)
they will compete for VRAM.  Only use parallel mode when different Minions
use different machines or the tasks are I/O-bound.

### Mode C — Queue

```
grub_queue.sqlite ──► Worker 1 (3090 PC)  ──► results
                  ──► Worker 2 (daily PC) ──► results
```

**Use when**: Multi-machine setup, want to add/remove workers dynamically.
**Config**: `"execution_mode": "queue"`
**Extra step**: run `python -m grub --mode worker` on each machine.

---

## Directory Structure

```
grub/
  __init__.py          ← public API
  __main__.py          ← CLI entry point (python -m grub)
  agent.py             ← GrubAgent: main orchestrator
  config.py            ← GrubConfig: all settings
  registry.py          ← MinionRegistry: discover minions + skills
  loop.py              ← Execution modes A, B, C + PipelineRunner
  feedback.py          ← TinkerBridge: Tinker ↔ Grub integration

  contracts/
    task.py            ← GrubTask: what Grub hands to a Minion
    result.py          ← MinionResult: what a Minion returns

  minions/
    base.py            ← BaseMinion: abstract base class
    coder.py           ← CoderMinion: writes implementation code
    reviewer.py        ← ReviewerMinion: scores code quality
    tester.py          ← TesterMinion: writes and runs pytest tests
    debugger.py        ← DebuggerMinion: fixes failing tests
    refactorer.py      ← RefactorerMinion: cleans up working code

  tools/
    file_ops.py        ← read/write/list files
    shell.py           ← run commands, run tests
    git_ops.py         ← git helpers (optional)
    code_analysis.py   ← count lines, extract functions/imports

  skills/
    python_expert.md       ← Python coding standards
    testing_patterns.md    ← pytest patterns
    clean_code.md          ← readability principles
    security_review.md     ← OWASP security checklist
    software_architecture.md ← SOLID, DI, common patterns
```

---

## How Skills Work

A skill is a plain Markdown text file.  When a Minion is instantiated,
the registry loads its assigned skills and injects them into its system prompt.

```
CoderMinion system prompt =
    [BASE_SYSTEM_PROMPT]         (the Coder's core instructions)
    ---
    [python_expert.md content]   (injected skill #1)
    ---
    [clean_code.md content]      (injected skill #2)
    ---
    [software_architecture.md]   (injected skill #3)
```

**To add a new skill:**
1. Create `grub/skills/my_skill.md` — just a text file
2. Add `"my_skill.md"` to the relevant minion's skill list in `grub_config.json`
3. Done.  No code changes needed.

**To use a skill from the internet:**
1. Copy the prompt text into `grub/skills/downloaded_skill.md`
2. Add it to the config
3. Done.

---

## How to Add a New Minion

1. Create `grub/minions/my_minion.py` — subclass `BaseMinion`
2. Override `MINION_NAME`, `BASE_SYSTEM_PROMPT`, and `run()`
3. Register it in `registry.py`:
   ```python
   from .minions.my_minion import MyMinion
   self.register_minion("my_minion", MyMinion)
   ```
4. Optionally add it to the pipeline in `loop.py`
5. Done.  Grub can now delegate tasks to `"my_minion"`.

---

## Quick Start

```bash
# 1. Start Ollama and pull a coding model
ollama pull qwen2.5-coder:7b   # fast, fits most GPUs

# 2. Run Grub (auto-creates grub_config.json on first run)
cd tinker/
python -m grub

# 3. Or test with a single task (no Tinker needed)
python -m grub --run-task "Implement a simple HTTP router" --artifact ./design.md
```

---

## Key Concepts

| Term | What it is |
|------|-----------|
| GrubTask | The unit of work (title + description + artifact path) |
| MinionResult | What a Minion returns (status + score + files + notes) |
| PipelineRunner | Chains Coder → Reviewer → Tester → Debugger → Refactorer |
| TinkerBridge | Reads tasks from Tinker's DB, writes results back |
| GrubConfig | All settings in one JSON file |
| MinionRegistry | Lookup table for Minion classes and Skill texts |

---

→ Next: [Chapter 16 — Minions in Detail](./16-grub-minions.md)
