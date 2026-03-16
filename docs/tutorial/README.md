# Tinker — Build From Scratch Tutorial

A step-by-step guide for junior developers to understand and rebuild the
entire Tinker system from zero.

---

## What You Will Build

**Tinker** is an autonomous AI system that continuously designs software
architectures. You feed it a problem statement ("design a distributed task
queue"), then it runs forever — picking tasks, asking an AI to think about
them, criticising the output, summarising discoveries, and committing
design documents to a Git repository.

It is not a chatbot. It is a loop.

---

## How This Tutorial Is Structured

Each chapter follows the same pattern:

1. **The Problem** — what challenge this module solves
2. **The Architecture Decision** — what we chose and why
3. **Step-by-step Implementation** — writing the code, explained line by line
4. **Integration** — connecting this module to the previous ones
5. **Try It** — a quick way to verify it works before moving on

Build the chapters in order. Each one depends on the previous.

---

## Chapters

| # | Chapter | What You Build |
|---|---------|---------------|
| [00](./00-introduction.md) | Introduction & Big Picture | Architecture overview, key concepts |
| [01](./01-python-prerequisites.md) | Python Prerequisites | `async/await`, dataclasses, type hints, env vars |
| [02](./02-model-client.md) | The Model Client | HTTP calls to Ollama, routing between two models |
| [03](./03-memory-manager.md) | The Memory Manager | Redis, DuckDB, ChromaDB, SQLite — four memory tiers |
| [04](./04-tool-layer.md) | The Tool Layer | Web search, web scraping, artifact writing |
| [05](./05-agent-prompts.md) | Agent Prompts | Structured AI output, prompt engineering, JSON schemas |
| [06](./06-task-engine.md) | The Task Engine | Task database, priority scoring, task generation |
| [07](./07-context-assembler.md) | The Context Assembler | Token budgets, assembling context for the AI |
| [08](./08-orchestrator.md) | The Orchestrator | The three reasoning loops, state machine, shutdown |
| [09](./09-resilience.md) | Resilience | Circuit breakers, dead letter queues, rate limiting |
| [10](./10-stagnation.md) | Anti-Stagnation | Detecting and escaping stuck reasoning patterns |
| [11](./11-observability.md) | Observability | Health endpoints, metrics, audit log, TUI dashboard |
| [12](./12-web-ui.md) | The Web UI | FastAPI backend, React dashboard, Gradio, Streamlit |
| [13](./13-integration.md) | Full Integration | `main.py`, wiring everything together, first run |
| [14](./14-code-review.md) | Code Review Walkthrough | Real bugs we found and fixed, and how to find them |

---

## Prerequisites

You need:
- Python 3.11 installed
- A code editor (VS Code recommended)
- Git installed
- Basic familiarity with Python (variables, functions, `if/for`, importing modules)

You do **not** need to know asyncio, databases, Docker, or AI APIs yet.
This tutorial teaches all of that.

---

## How to Read Code Blocks

Throughout this tutorial, code blocks look like this:

```python
# tinker/llm/client.py          ← the file you are editing
# Lines 1-10                    ← approximate position in the file

class ModelClient:               # ← we explain every new concept
    """Talks to one Ollama instance."""
    pass
```

When you see `# ...existing code...` it means lines you already wrote
earlier in the same file — do not delete them, just keep reading down.
