**22/22 tests passing, full demo output above.** Here's a map of every design decision:

---

## Architecture Overview

### `context_assembler.py` — Core module

**`TokenBudgetManager`**
Owns the math of converting a token ceiling into per-section character limits. Default allocation (tunable via `allocation_overrides`):

| Section | % | Role |
|---|---|---|
| `system_identity` | 5% | Always present, small |
| `task` | 10% | The north star — never drop |
| `arch_state` | 25% | Core working memory |
| `recent_artifacts` | 25% | Semantic session retrieval |
| `prior_critique` | 15% | Loop-over-loop learning |
| `research_notes` | 15% | External knowledge injection |
| `output_format` | 5% | Instruction tail |

`truncate()` is word-boundary–aware (no mid-word cuts), and uses a "…[truncated]" suffix so the model always knows when it's seeing partial content.

**`ContextAssembler.assemble(task, role, loop_level)`**

Three-phase pipeline:
1. **Concurrent fetch** — all four memory retrievals fire in parallel via `asyncio.gather`. Each is wrapped in `_safe_fetch` which enforces a per-retrieval timeout and catches any exception independently. A failed section becomes empty, never a crash.
2. **Priority-ordered assembly** — sections are consumed in `SECTION_PRIORITY` order. Each section gets `min(section_budget, remaining_budget)` — so high-priority sections protect their allocation, but low-priority sections still get anything left over.
3. **Metadata annotation** — every assembled context carries `sections_included`, `sections_dropped`, `tokens_used`, `assembly_time_ms`, and `warnings` for the Observability Dashboard (Component 9).

### `stubs.py` — Integration shims

Drop-in `StubMemoryManager` and `StubPromptBuilder` with realistic fake data matching the Tinker architecture narrative. Configurable artificial latency so you can stress-test timeouts during development.

### Graceful degradation ladder

| Failure mode | What happens |
|---|---|
| Memory retrieval timeout | Section silently dropped, warning logged |
| Memory retrieval exception | Same — warning includes the exception message |
| Budget fully exhausted | Remaining sections dropped, all logged |
| Completely empty memory | Prompt still has `system_identity + task + output_format` — always a valid call |

---

## Integration Wiring (when you have Components 2 & 4)

```python
# Replace stubs with real implementations
assembler = ContextAssembler(
    memory_manager=MemoryManager(session_store, vector_db),  # Component 2
    prompt_builder=PromptBuilder(template_dir="prompts/"),   # Component 4
    budget_manager=TokenBudgetManager(
        total_tokens=16384,  # match your Ollama model's context window
        allocation_overrides={"arch_state": 0.35}  # tune per model
    ),
)

# Called by the Orchestrator (Component 7) on every loop tick
ctx = await assembler.assemble(task, role=AgentRole.ARCHITECT, loop_level=loop)
response = await model_client.complete(ctx.prompt)
```