Let me build this systematically across multiple files.27/27. Now copy everything to outputs.**27/27 tests passing.** Here's a full map of what was built:

---

## `prompts/` — File guide

| File | What it does |
|------|-------------|
| `templates.py` | All 10 prompt templates (4 roles × up to 3 loop levels). Each is a `(system, user)` pair with `{placeholder}` slots. Tuned for Qwen3-7B / Phi-3-mini: imperative language, inline schema in system prompt, explicit field-by-field instructions. |
| `schemas.py` | 10 jsonschema-compatible output schemas in a `SCHEMA_REGISTRY` dict. Covers every role × loop combination. Critic weaknesses require `id`, `severity`, `category`, `statement`, `evidence`, `impact`. Architect requires `reasoning_chain`, `trade_offs`, `candidate_next_tasks` with priority enum. |
| `variants.py` | 8 prompt variants as injectable blocks: `harder_critic`, `alternative_forcing`, `contradiction_injection`, `devil_advocate_critic`, `socratic_architect`, `paranoid_security`, `minimum_viable_design`, `scalability_stress`. Incompatibility graph enforced at build time. |
| `builder.py` | `PromptBuilder` class — validates context completeness, resolves variant conflicts, serializes dict context values to JSON, stamps build metadata. Factory methods for common invocations (`for_architect_micro`, `for_critic_micro`, etc.) |
| `validator.py` | `OutputValidator` with 3-pass validation: JSON extraction (handles markdown fences, mixed prose), auto-repair (UUID injection, confidence clamping, weakness ID renumbering), then schema + semantic checks. Semantic rules catch things schema can't: confidence/weakness severity disagreements, missing high-priority tasks, generic weakness language. |
| `examples.py` | Full realistic Architect → Critic exchange on the Memory Manager design. Architect produces a 5-step reasoning chain + full SQLite-backed design. Critic finds 5 specific weaknesses (token approximation flaw, no write versioning, no metrics, hardcoded ranking policy, FTS5 noise) plus 3 sharp objections. Researcher note also included. |
| `__init__.py` | Clean public API — one import gives you everything. |

---

**Key design decisions for small models:**

- The schema is embedded verbatim in the system prompt — Qwen3/Phi-3 hallucinate field names less when they can see the exact shape they're targeting
- `artifact_id` is auto-repaired if the model emits `"<uuid4>"` as a literal (common failure mode)
- Critic prompts open with the mandate statement ("You NEVER propose alternatives") in bold imperative — small models drift toward helpfulness and start suggesting fixes without this
- Variant injections are appended to system (not user) so they survive context window pressure on long runs