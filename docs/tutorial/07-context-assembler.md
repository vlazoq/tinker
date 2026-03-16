# Chapter 07 — The Context Assembler

## The Problem

Before the Architect can think about a task, it needs context — relevant
information from memory.  But AI models have a limited *context window*
(how much text they can read at once).  For `qwen3:7b` this is ~8,000
tokens.  If we just dump everything in memory into the prompt, we'll:

1. Exceed the context limit and get an error
2. Drown the relevant information in noise

We need to **assemble** a context that:
- Fits within a token budget
- Contains the most relevant information first
- Includes recent artifacts, research results, and system prompts

---

## The Architecture Decision

The `ContextAssembler` builds a context string with a hard token budget.
It fills the budget greedily, most-relevant-first:

```
Token budget: 4000 tokens

Priority order:
  1. Task description (always included)         ~  200 tokens
  2. Most recent artifacts for this subsystem   ~ 1500 tokens (top 3)
  3. Relevant research from ChromaDB            ~ 1000 tokens (top 5 results)
  4. Recent micro history across all subsystems ~  800 tokens (last 5)
  5. Stop — budget reached
```

This guarantees we never exceed the budget, and the most important
information is always included.

---

## Step 1 — Directory Structure

```
tinker/
  context/
    __init__.py
    assembler.py
```

---

## Step 2 — Token Counting

We need to estimate how many tokens a piece of text uses.  The exact count
depends on the model's tokeniser, but a good approximation is:

> **~4 characters per token** (for English text)

For our purposes, this approximation is close enough.  We are not trying
to pack the context window to the last token — we are trying to avoid
overflowing it.

```python
# tinker/context/assembler.py

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def estimate_tokens(text: str) -> int:
    """Rough token count estimate: ~4 characters per token."""
    return max(1, len(text) // 4)
```

---

## Step 3 — The Context Assembler

```python
# tinker/context/assembler.py (continued)

from memory.manager import MemoryManager


class ContextAssembler:
    """
    Assembles token-budgeted context for AI prompts.

    Usage:
        assembler = ContextAssembler(memory_manager, token_budget=4000)
        context = await assembler.assemble(
            session_id="sess-1",
            task=current_task,
            subsystem="api_gateway",
        )
        # context is a string ready to inject into the Architect's prompt
    """

    def __init__(
        self,
        memory:       MemoryManager,
        token_budget: int = 4000,
    ) -> None:
        self._memory = memory
        self._budget = token_budget

    async def assemble(
        self,
        session_id:  str,
        task_title:  str,
        task_description: str,
        subsystem:   str,
        extra_query: Optional[str] = None,
    ) -> str:
        """
        Build a context string that fits within the token budget.

        Returns a formatted string ready to use as the 'context' parameter
        in build_architect_prompt().
        """
        budget_remaining = self._budget
        sections: list[str] = []

        # ── Section 1: Recent artifacts for this subsystem ──────────────────
        # These are the most directly relevant pieces of prior work.
        artifacts = await self._memory.get_recent_artifacts(
            session_id   = session_id,
            artifact_type= "design",
            limit        = 5,
        )
        # Filter to this subsystem first
        subsystem_artifacts = [
            a for a in artifacts
            if a.get("metadata") and
               (a["metadata"].get("subsystem") == subsystem
                if isinstance(a.get("metadata"), dict)
                else False)
        ] or artifacts[:3]   # fall back to recent artifacts if none for this subsystem

        for artifact in subsystem_artifacts[:3]:
            text = f"### Prior Design Work ({artifact.get('created_at', '')[:10]})\n{artifact['content']}"
            tokens = estimate_tokens(text)
            if tokens > budget_remaining:
                # Truncate to fit
                max_chars = budget_remaining * 4
                text = text[:max_chars] + "\n...[truncated]"
                tokens = budget_remaining

            sections.append(text)
            budget_remaining -= tokens
            if budget_remaining <= 0:
                break

        # ── Section 2: Semantic search results from research archive ────────
        if budget_remaining > 200:
            search_query = extra_query or f"{subsystem} {task_title}"
            research     = await self._memory.search_research(
                query    = search_query,
                n        = 5,
            )
            for item in research:
                text   = f"### Research: {item.get('id', '')[:20]}\n{item['text']}"
                tokens = estimate_tokens(text)
                if tokens > budget_remaining:
                    break
                sections.append(text)
                budget_remaining -= tokens

        if not sections:
            return "No relevant prior context found."

        header = f"## Context for {subsystem}\n\n"
        return header + "\n\n---\n\n".join(sections)
```

---

## Step 4 — Try It

```python
# test_context.py
import asyncio
from memory.storage  import RedisAdapter, DuckDBAdapter, ChromaAdapter, SQLiteAdapter
from memory.manager  import MemoryManager
from context.assembler import ContextAssembler

async def main():
    mm = MemoryManager(
        redis  = RedisAdapter("redis://localhost:6379"),
        duckdb = DuckDBAdapter("test_session.duckdb"),
        chroma = ChromaAdapter("./test_chroma"),
        sqlite = SQLiteAdapter("test_tasks.sqlite"),
    )
    await mm.connect()

    # Store some fake prior work
    await mm.store_artifact(
        session_id    = "sess-1",
        task_id       = "task-001",
        artifact_type = "design",
        content       = "The API gateway should use JWT tokens with short TTL (15 min).",
        metadata      = {"subsystem": "api_gateway"},
    )

    # Assemble context
    assembler = ContextAssembler(mm, token_budget=2000)
    context = await assembler.assemble(
        session_id       = "sess-1",
        task_title       = "Rate limiting strategy",
        task_description = "Choose a rate limiting approach for the API gateway",
        subsystem        = "api_gateway",
    )

    print("Assembled context:")
    print(context)
    print(f"\nEstimated tokens: {len(context) // 4}")

    await mm.close()

asyncio.run(main())
```

---

## The Full Pipeline So Far

At this point, if you run the components together, you can see the
complete AI reasoning pipeline in action:

```python
# Conceptual pipeline (not yet wired into the orchestrator)

async def one_micro_loop(task, memory, tools, llm, assembler):

    # 1. Assemble context from memory
    context = await assembler.assemble(
        session_id=session_id,
        task_title=task.title,
        task_description=task.description,
        subsystem=task.subsystem,
    )

    # 2. Build the Architect's prompt
    from prompts.architect import build_architect_prompt, parse_architect_response
    prompt = build_architect_prompt(
        task_title=task.title,
        task_description=task.description,
        subsystem=task.subsystem,
        context=context,
    )

    # 3. Ask the Architect
    text, pt, ct = await llm.complete(prompt, role="architect")
    arch_result = parse_architect_response(text)

    # 4. If Architect wants to search, do it and ask again
    if arch_result.tool_call and arch_result.tool_call.startswith("web_search:"):
        query = arch_result.tool_call[len("web_search:"):]
        research = await tools.web_search(query)
        prompt2 = build_architect_prompt(..., research_results=research)
        text2, _, _ = await llm.complete(prompt2, role="architect")
        arch_result = parse_architect_response(text2)

    # 5. Ask the Critic to review
    from prompts.critic import build_critic_prompt, parse_critic_response
    critic_prompt = build_critic_prompt(task.title, arch_result.content, task.subsystem)
    crit_text, _, _ = await llm.complete(critic_prompt, role="critic")
    crit_result = parse_critic_response(crit_text)

    # 6. Store the artifact
    artifact_id = await memory.store_artifact(
        session_id=session_id,
        task_id=task.id,
        artifact_type="design",
        content=arch_result.content,
        metadata={"score": crit_result.score, "subsystem": task.subsystem},
    )

    return arch_result, crit_result, artifact_id
```

This is essentially the micro loop.  We will formalise it in Chapter 08.

---

## What We Have So Far

```
tinker/
  llm/         ✅  model client + router
  memory/      ✅  four adapters + unified manager
  tools/       ✅  search + scraper + writer + layer
  prompts/     ✅  architect + critic + synthesizer + parser
  tasks/       ✅  registry + engine + generator
  context/     ✅  token-budgeted context assembly
```

All the building blocks exist.  Chapter 08 assembles them into the
orchestrator — the beating heart of Tinker.

---

→ Next: [Chapter 08 — The Orchestrator](./08-orchestrator.md)
