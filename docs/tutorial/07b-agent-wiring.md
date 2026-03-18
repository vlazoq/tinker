# Chapter 07b — Agent Wiring: PromptBuilder, Retry, and Context Role Unification

## The Problem

After building the Context Assembler (Chapter 07) and the agent skeletons
(Chapter 05), we have a gap: **the agents do not actually use the production
prompt templates**.  Three concrete issues:

1. `ArchitectAgent`, `CriticAgent`, and `SynthesizerAgent` build prompts inline
   using ad-hoc f-strings, ignoring the carefully engineered `PromptBuilder`
   from `prompts/builder.py`.
2. `AgentRole` is defined **twice** — once in `llm/types.py` and once
   (redundantly) in `context/assembler.py`.  Any module that imports from the
   wrong place ends up with a *different class object*, breaking `is` checks and
   `isinstance` guards silently.
3. `MemoryAdaptor.semantic_search_session` always falls back to "most recent
   N artifacts" regardless of the query — it never exploits the task-ID
   structure that gives us *exact* relevance when we know which task we are
   running.

---

## The Architecture Decision

### PromptBuilder wiring
`PromptBuilder` exposes static factory methods (`for_architect_micro`,
`for_critic_micro`, `for_synthesizer_meso`).  Each agent calls the appropriate
factory, passing the assembled context string from `ContextAssembler` as the
`architecture_state` argument.  On import failure (minimal test environments)
the agents fall back to the inline f-string prompts gracefully.

### Single `AgentRole` source of truth
`llm/types.py` is the canonical home.  `context/assembler.py` re-exports it
with `from llm.types import AgentRole  # noqa: F401` so existing callers are
not broken.  The local enum definition is removed.

### Two-phase session memory retrieval
`MemoryAdaptor.semantic_search_session` now uses:

1. **Task-ID lookup** — if the query looks like a UUID v4 (or starts with
   `"task:"`), call `get_artifacts_by_task(task_id)` and return the results
   with `score=1.0` (exact match).
2. **Recency fallback** — for general queries, return the most recent
   `top_k * 2` artifacts scored at `0.8`.  This is fast and almost always
   gives the right context for Tinker's usage pattern.

### Retry wiring
Each agent wraps its `router.complete(request)` call with `retry_async` from
`resilience/retry.py` when that module is importable.  The `CONSERVATIVE`
preset (3 attempts, 2 s base, 60 s max, full jitter) protects against
transient LLM-backend failures without blocking for too long.

---

## Step 1 — Understand the PromptBuilder API

```python
# prompts/builder.py

class PromptBuilder:
    @classmethod
    def for_architect_micro(
        cls,
        architecture_state: str,  # assembled context from ContextAssembler
        task_description:   str,
        constraints:        str = "None specified.",
        context:            str = "None.",
        variants: list[str] | None = None,
    ) -> tuple[str, str]:         # (system_prompt, user_prompt)
        ...

    @classmethod
    def for_critic_micro(
        cls,
        target_artifact:    dict,
        architecture_state: str,
        focus_areas:        str = "General design quality.",
        variants: list[str] | None = None,
    ) -> tuple[str, str]:
        ...

    @classmethod
    def for_synthesizer_meso(
        cls,
        source_artifacts:    list[dict],
        synthesis_directive: str,
        prior_meso_synthesis: str = "None — first meso synthesis.",
    ) -> tuple[str, str]:
        ...
```

All factory methods return a `(system_prompt, user_prompt)` tuple.
The LLM router expects a `CompletionRequest` with both fields.

---

## Step 2 — Lazy Imports for Portability

Agents must work in minimal test environments where `prompts/` or
`resilience/` may not be fully installed.  Use helper functions that return
`None` on `ImportError`:

```python
# agents.py

def _get_prompt_builder_cls():
    """
    Return the PromptBuilder class, or None if prompts package is unavailable.

    This lazy import lets agents.py load in minimal test environments
    (e.g. without the full prompts/ package tree).  Callers check for None
    and fall back to inline f-string prompts.
    """
    try:
        from prompts.builder import PromptBuilder
        return PromptBuilder
    except ImportError:
        return None


def _get_retry_async():
    """
    Return (retry_async, CONSERVATIVE) from resilience.retry, or (None, None).

    Agents use retry_async to wrap LLM router calls.  When the resilience
    package is unavailable the agents proceed without retry (acceptable in
    tests and lightweight deployments).
    """
    try:
        from resilience.retry import retry_async, CONSERVATIVE
        return retry_async, CONSERVATIVE
    except ImportError:
        return None, None
```

---

## Step 3 — Wire ArchitectAgent to PromptBuilder

The key change is in the `_build_architect_prompts` helper.  Previously it
used an inline f-string.  Now it calls `PromptBuilder.for_architect_micro`:

```python
# agents.py

def _build_architect_prompts(
    task_desc: str,
    subsystem: str,
    context_str: str,       # from ContextAssembler.build() → ctx["prompt"]
    grub_section: str = "",
    constraints_str: str = "None.",
) -> tuple[str, str]:
    """
    Build (system_prompt, user_prompt) for an Architect micro call.

    Uses PromptBuilder when available; degrades gracefully to an inline
    f-string so agents work in minimal test environments too.
    """
    PromptBuilder = _get_prompt_builder_cls()
    if PromptBuilder is not None:
        try:
            full_context = context_str
            if grub_section:
                full_context = f"{context_str}\n\n{grub_section}".strip()
            return PromptBuilder.for_architect_micro(
                architecture_state = full_context[:4000],
                task_description   = task_desc,
                constraints        = constraints_str,
                context            = f"Subsystem: {subsystem}",
            )
        except Exception:
            pass  # fall through to inline prompts

    # Inline fallback (always available, no external dependencies)
    system = (
        "You are an expert software architect. Your task is to analyze the "
        "given problem and produce a detailed architectural design.\n\n"
        f"Context:\n{context_str[:2000]}"
    )
    user = (
        f"Design task: {task_desc}\n"
        f"Subsystem: {subsystem}\n"
        f"Constraints: {constraints_str}\n"
        f"{grub_section}"
    )
    return system, user
```

### ArchitectAgent.call() — key changes

```python
async def call(self, task: dict, context: dict) -> dict:
    task_desc = task.get("description", str(task))
    subsystem  = task.get("subsystem", "core")

    # Map assembled context from ContextAssembler to architecture_state
    context_str = context.get("prompt", "") or context.get("architecture_state", "")

    # Extract constraints list from task metadata
    constraints_list = task.get("constraints", [])
    constraints_str  = (
        "; ".join(constraints_list) if constraints_list else "None specified."
    )

    # Grub integration section (populated by Orchestrator when grub is active)
    grub_section = ""
    if context.get("grub_plan"):
        grub_section = f"[Grub plan available]\n{context['grub_plan']}"

    system_prompt, user_prompt = _build_architect_prompts(
        task_desc, subsystem, context_str, grub_section, constraints_str
    )

    req = CompletionRequest(
        system=system_prompt,
        user=user_prompt,
        temperature=0.7,
        max_tokens=2048,
    )

    # Wrap with retry when resilience package is available
    retry_async, CONSERVATIVE = _get_retry_async()
    if retry_async is not None:
        raw = await retry_async(
            lambda: self._router.complete(req), CONSERVATIVE
        )
    else:
        raw = await self._router.complete(req)

    return _parse_architect_structured(raw)
```

---

## Step 4 — Handle Both Response Schemas

`PromptBuilder` templates instruct the model to return a JSON payload with
`artifact_type: "design_proposal"` and nested `design.summary`.  The legacy
inline prompts produce a flat schema (`content`, `knowledge_gaps`).  The
parser must handle both:

```python
def _parse_architect_structured(d: dict | str) -> dict:
    """
    Parse architect response.  Handles two schemas:

    Legacy (inline prompt):
        {"content": "...", "knowledge_gaps": [...], "candidate_tasks": [...]}

    PromptBuilder schema:
        {"artifact_type": "design_proposal",
         "design": {"summary": "...", ...},
         "reasoning_chain": [...],
         "candidate_next_tasks": [...]}

    Both are normalised to the canonical return format:
        {"content": str, "knowledge_gaps": list, "candidate_tasks": list,
         "score": float, "tool_call": dict | None}
    """
    if isinstance(d, str):
        return {"content": d, "knowledge_gaps": [], "candidate_tasks": [],
                "score": 0.5, "tool_call": None}

    if d.get("artifact_type") == "design_proposal":
        design = d.get("design", {})
        content = (
            design.get("summary", "")
            or design.get("full_specification", "")
            or str(design)
        )
        return {
            "content":        content,
            "knowledge_gaps": _extract_knowledge_gaps(d),
            "candidate_tasks": _extract_candidate_tasks(d),
            "score":          d.get("confidence_score", 0.7),
            "tool_call":      d.get("tool_request"),
        }

    # Legacy schema
    return {
        "content":        d.get("content", ""),
        "knowledge_gaps": _extract_knowledge_gaps(d),
        "candidate_tasks": _extract_candidate_tasks(d),
        "score":          _extract_score(d),
        "tool_call":      d.get("tool_call"),
    }
```

---

## Step 5 — Fix the Duplicate AgentRole

Before this fix, `context/assembler.py` defined its own enum:

```python
# WRONG — creates a second, incompatible AgentRole class object
from enum import Enum
class AgentRole(str, Enum):
    ARCHITECT   = "architect"
    CRITIC      = "critic"
    SYNTHESIZER = "synthesizer"
    RESEARCHER  = "researcher"
```

**The fix** — replace the definition with a re-export:

```python
# context/assembler.py (correct)

# AgentRole is the single source of truth for role names throughout Tinker.
# It lives in llm.types because the model router uses it to decide which
# machine / model to target.  We import it here so ContextAssembler callers
# can use the same enum without a separate import.  Previously this module
# defined its own duplicate AgentRole — that redundancy is now eliminated.
from llm.types import AgentRole  # noqa: F401  (re-exported for callers)
```

**Why it matters** — Python's `enum` creates a new class object for every
`class AgentRole(Enum)` definition.  Two definitions of the "same" enum with
identical members are still *different types*:

```python
>>> from context.assembler import AgentRole as A
>>> from llm.types import AgentRole as B
>>> A.ARCHITECT is B.ARCHITECT   # False — different class objects!
```

A downstream `isinstance(role, AgentRole)` check would silently fail depending
on which import path the caller used.  With a single source of truth the
classes are identical:

```python
>>> import context.assembler, llm.types
>>> context.assembler.AgentRole is llm.types.AgentRole  # True
```

---

## Step 6 — Two-Phase Session Memory Retrieval

The naive implementation of `MemoryAdaptor.semantic_search_session` always
fetches the N most recent artifacts.  But when we *know* the task ID (which
we always do inside `ContextAssembler.assemble()`), we can get exact results:

```python
# context/memory_adapter.py

async def semantic_search_session(
    self, query: str, top_k: int = 5
) -> list[MemoryItem]:
    import re as _re

    # UUID v4 pattern: 8-4-4-4-12 hex digits with version 4 and variant bits.
    uuid_pattern = _re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        _re.IGNORECASE,
    )

    try:
        # ── Phase 1: task-ID based lookup ──────────────────────────────────
        # ContextAssembler passes query = f"{task.goal} {task.description}".
        # When the task ID itself is the query (or prefixed with "task:"),
        # we can retrieve artifacts with exact relevance (score=1.0).
        task_id: str | None = None
        stripped = query.strip()
        if uuid_pattern.match(stripped):
            task_id = stripped
        elif stripped.lower().startswith("task:"):
            task_id = stripped[5:].strip()

        if task_id:
            try:
                artifacts = await self._mm.get_artifacts_by_task(
                    task_id, limit=top_k
                )
                if artifacts:
                    return [
                        MemoryItem(
                            id      = a.id,
                            content = a.content[: self._limit],
                            score   = 1.0,   # exact task match
                            source  = "session",
                        )
                        for a in artifacts[:top_k]
                    ]
            except Exception:
                pass  # fall through to recency retrieval

        # ── Phase 2: recency fallback ──────────────────────────────────────
        # For general queries, fetch top_k * 2 recent artifacts and return
        # the first top_k.  True semantic search would require DuckDB +
        # embeddings; recency is a fast, correct approximation for Tinker's
        # usage pattern where recent context is almost always the most
        # relevant context.
        artifacts = await self._mm.get_recent_artifacts(limit=top_k * 2)
        return [
            MemoryItem(
                id      = a.id,
                content = a.content[: self._limit],
                score   = 0.8,   # approximate relevance (recency-based)
                source  = "session",
            )
            for a in artifacts[:top_k]
        ]

    except Exception as exc:
        logger.warning("MemoryAdaptor.semantic_search_session: %s", exc)
        return []
```

**Score semantics**:

| Score | Meaning |
|-------|---------|
| `1.0` | Exact task-ID match — these artifacts are directly related to the task being processed right now |
| `0.8` | Recency-based approximation — recent context is probably relevant but not guaranteed |
| `0.75` | ChromaDB cosine similarity from archive search (see `semantic_search_archive`) |

---

## Step 7 — Testing

The `tests/test_agents.py` file covers all the changes above.

### Running the tests

```bash
# From the tinker/ root
python -m pytest tests/test_agents.py -v
```

### Key test classes

| Class | What it tests |
|-------|--------------|
| `TestParseArchitectStructured` | Legacy schema, PromptBuilder schema, raw string fallback |
| `TestBuildArchitectPrompts` | PromptBuilder called with correct args; graceful fallback when module absent or raises |
| `TestBuildCriticPrompts` | Same pattern for critic |
| `TestBuildSynthesizerPrompts` | Meso uses PromptBuilder; macro uses inline template |
| `TestArchitectAgentCall` | Async end-to-end: mock router → structured response → dict with expected keys |
| `TestCriticAgentCall` | Score clamping, retry wiring |
| `TestContextRoleUnification` | `context.assembler.AgentRole is llm.types.AgentRole` (must be `True`) |
| `TestMemoryAdaptorSemanticSearch` | UUID triggers task-ID lookup, fallback to recency, exception isolation |

### Example: verifying AgentRole unification

```python
import context.assembler
import llm.types

def test_agent_role_is_single_class():
    assert context.assembler.AgentRole is llm.types.AgentRole, (
        "AgentRole must be imported from llm.types — "
        "if you see False here, assembler.py defines its own copy again"
    )
```

---

## Integration Checklist

After applying all the changes in this chapter, verify the following:

- [ ] `python -m pytest tests/test_agents.py` — 47 tests pass
- [ ] `python -m pytest context/tests/` — 22 context assembler tests still pass
- [ ] `python -m pytest tasks/tests/` — all task registry tests pass
- [ ] `python -c "from context.assembler import AgentRole; from llm.types import AgentRole as B; assert AgentRole is B"` — no assertion error
- [ ] `python -c "from agents import ArchitectAgent, CriticAgent, SynthesizerAgent"` — no import error

---

## Summary

| Change | File | Why |
|--------|------|-----|
| Wire `PromptBuilder.for_architect_micro` | `agents.py` | Production prompts, consistent JSON schemas |
| Wire `PromptBuilder.for_critic_micro` | `agents.py` | Structured critique output |
| Wire `PromptBuilder.for_synthesizer_meso` | `agents.py` | Consistent meso synthesis |
| Lazy `_get_prompt_builder_cls()` | `agents.py` | Portability — works without full `prompts/` package |
| Lazy `_get_retry_async()` | `agents.py` | Resilience — retries transient LLM failures |
| Dual-schema `_parse_architect_structured` | `agents.py` | Handles both legacy and PromptBuilder output |
| Remove duplicate `AgentRole` | `context/assembler.py` | Single source of truth in `llm/types.py` |
| Two-phase `semantic_search_session` | `context/memory_adapter.py` | Exact results when task-ID is known; fast recency fallback otherwise |
