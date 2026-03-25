# TINKER.md — Project Instructions for Tinker's Architect AI

## What is this file?

**TINKER.md** is a persistent instruction file that Tinker reads at startup and
injects into the Architect AI's system prompt every single micro loop. Think of
it as a "brief" you hand to a new employee before they start work — it tells
them about the project, the rules, the decisions already made, and the
conventions to follow.

Without TINKER.md, the Architect AI starts every session with only its general
intelligence and whatever context appears in the memory store. It doesn't know
your team's preferences, your project's quirks, or the architectural decisions
you've already locked in. With TINKER.md, it does.

**This file is for you to edit.** Everything below the divider line is a
template. Replace the template sections with information about your actual
project. The more specific you are, the better Tinker's output will be.

---

## How Tinker Uses This File

When Tinker starts (`python main.py --problem "..."`), it:

1. Looks for this file at the path in `TINKER_INSTRUCTIONS_PATH` (default:
   `./TINKER.md` next to `main.py`).
2. Reads the entire file as plain text.
3. Injects it into every Architect AI call, between the base system prompt and
   the per-task context.

The injection point in the prompt looks like this:

```
[Base Architect system prompt — built in to Tinker]

## PROJECT INSTRUCTIONS (from TINKER.md)
[contents of this file]

## CURRENT TASK
[the specific task for this micro loop]

## CONTEXT
[relevant artifacts from memory]
```

This means the Architect sees your instructions on *every single reasoning
loop*, not just the first one. It can't forget them.

---

## How This Relates to CLAUDE.md in Claude Code

Claude Code (Anthropic's official CLI for Claude) uses the same pattern with a
file called `CLAUDE.md`. When you open a project in Claude Code, it
automatically reads `CLAUDE.md` and uses it to understand the project context,
coding conventions, and constraints.

TINKER.md serves exactly the same purpose for Tinker:

| | Claude Code | Tinker |
|---|---|---|
| File | `CLAUDE.md` | `TINKER.md` |
| Location | Project root | Project root (configurable) |
| Read at | Session start | Process startup |
| Injected into | Claude's context | Architect's system prompt |
| Purpose | Project context + conventions | Project context + constraints |

The key difference: Claude Code's Claude is interactive (you can correct it
mid-conversation), whereas Tinker's Architect runs autonomously 24/7. So
TINKER.md needs to be more explicit about *what not to do*, not just *what to
do*, because there's no human to catch mistakes in real time.

---

## How to Edit This File

1. **Replace the template sections below** with information about your project.
2. **Be specific, not vague.** "Use async/await for all I/O" is better than
   "write good code."
3. **List decisions already made** so the Architect doesn't waste loops
   re-debating them. Example: "We are using PostgreSQL, not MySQL. Do not
   suggest MySQL."
4. **List forbidden patterns** explicitly. The Architect will avoid them if you
   name them clearly.
5. **Save the file and restart Tinker.** Changes take effect on the next
   startup — there's no hot-reload.

You can point Tinker at a different file using the environment variable:
```bash
export TINKER_INSTRUCTIONS_PATH=/path/to/my-project-instructions.md
python main.py --problem "..."
```

---

## Example: What a Good TINKER.md Looks Like

Here is an example for a hypothetical project (a distributed task queue):

```markdown
## Project: Distributed Task Queue — "Celery replacement for Python 3.12+"

### What We Are Building
A lightweight distributed task queue that:
- Runs on a single machine (no Kubernetes required)
- Uses Redis as the broker
- Supports priority queues and task chaining
- Has a web dashboard built with FastAPI + HTMX

### Technology Stack (LOCKED — do not suggest alternatives)
- Language: Python 3.12 (use all 3.12 features freely)
- Broker: Redis 7.x via redis-py asyncio client
- Web: FastAPI + HTMX (no React, no Vue, no heavy JS frameworks)
- Database: SQLite for task metadata (no PostgreSQL needed — single machine)
- Testing: pytest + pytest-asyncio (no unittest)

### Architecture Decisions (Already Made — Do Not Revisit)
1. Tasks are serialized as JSON, not pickle. Security requirement.
2. Workers use asyncio, not threading or multiprocessing.
3. The web dashboard is read-only (no task management from UI — only CLI).
4. No external service discovery — workers find the broker via Redis URL.

### Coding Conventions
- All public functions must have type hints and a docstring.
- Use dataclasses, not dicts, for internal data structures.
- Use `asyncio.TaskGroup` (Python 3.11+) instead of `asyncio.gather()`.
- Max line length: 100 characters.
- All database operations must be wrapped in a retry decorator.

### Forbidden Patterns
- Do NOT use `pickle` or `shelve` anywhere in the codebase.
- Do NOT use `threading.Thread` — we are async-only.
- Do NOT import `requests` — use `httpx` with async client.
- Do NOT use global mutable state — pass dependencies explicitly.
- Do NOT write SQL with string formatting — always use parameterized queries.

### Current Priority
The task serialization and deserialization layer is not yet complete.
Focus on making the `TaskSerializer` class robust before moving to the
worker pool implementation.
```

Notice how specific that is. Compare it to a bad TINKER.md that says:
```markdown
Build a task queue. Use Python. Write good code.
```
The second version gives the Architect almost nothing to work with.

---

## Template — Fill In Your Project Below

Delete everything from the dashed line below and replace it with your project's
specific instructions. The sections are suggestions — add or remove as needed.

---

## Project Overview

<!-- REPLACE THIS: One paragraph describing what you are building and why. -->

Example: We are building a home lab orchestration system that automates the
deployment and monitoring of self-hosted services across three machines (a
primary server with RTX 3090, a secondary NAS, and a daily-driver laptop).
The system should work without any cloud dependencies.

## Technology Stack

<!-- REPLACE THIS: List every technology choice that is LOCKED. -->
<!-- "Locked" means: do not suggest alternatives, do not debate these choices. -->

- Language: Python 3.12
- AI inference: Ollama (local, no cloud APIs)
- Database: [your choice]
- Web framework: [your choice, if any]
- Testing: pytest

## Architecture Decisions Already Made

<!-- REPLACE THIS: List decisions that are final. -->
<!-- Format: numbered list with a brief justification for each. -->

1. [Decision] — [why it was made]
2. [Decision] — [why it was made]

## Coding Conventions

<!-- REPLACE THIS: List conventions the Architect must follow. -->
<!-- Be specific. "Write clean code" means nothing. -->

- [Convention 1]
- [Convention 2]

## Forbidden Patterns

<!-- REPLACE THIS: List patterns the Architect must NEVER use. -->
<!-- These are hard rules, not suggestions. -->

- Do NOT [pattern 1]
- Do NOT [pattern 2]

## Current Priority

<!-- REPLACE THIS: What should Tinker focus on RIGHT NOW? -->
<!-- This helps the Architect decide which follow-up tasks to generate. -->

[Describe the current phase of the project and what needs to be completed
before moving on.]

## Open Questions

<!-- OPTIONAL: List things that are not yet decided. -->
<!-- The Architect will try to resolve these through research and reasoning. -->

- [Question 1]
- [Question 2]
