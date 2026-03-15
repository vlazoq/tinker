All 18 pass. Now copy everything to outputs:All 18 unit tests pass. Here's a map of everything built:

---

## Module Structure

```
tinker/
└── model_client/
    ├── __init__.py      ← Public API surface
    ├── types.py         ← Dataclasses: AgentRole, Machine, MachineConfig,
    │                      ModelRequest, ModelResponse, RetryConfig, Message
    ├── client.py        ← OllamaClient — raw async HTTP, retry, timeouts
    ├── router.py        ← ModelRouter  — the single entry point for Tinker
    ├── parsing.py       ← JSON extraction (4 fallback strategies)
    ├── context.py       ← Token counting + context-window enforcement
    └── tests.py         ← Unit + mock + live integration harness
```

---

## Key Design Decisions

**`ModelRouter` is the only thing you import elsewhere.** Nothing in the rest of Tinker needs to know which machine or model handles a given agent. You call `router.complete(request)` and get a `ModelResponse` back.

**Routing table is data, not code.** `ROLE_MACHINE_MAP` in `types.py` is a plain dict — swapping which machine handles a role is a one-line change.

**JSON extraction has 4 progressively looser strategies:** direct parse → fenced markdown → first `{`/`[` → strip preamble and retry. Models almost never produce clean JSON; this handles the common failure modes.

**Context enforcement is surgical.** It preserves the system message and the final user turn unconditionally, then evicts from the *middle* of the history oldest-first. Tiktoken is used if installed, character heuristic otherwise.

**Retry uses exponential backoff** with jitter-ready config. Connection errors and 5xx retry; 4xx (except 429) propagate immediately.

---

## Usage at a Glance

```python
from tinker.model_client import ModelRouter, AgentRole

async with ModelRouter() as router:
    # plain text
    resp = await router.complete_text(AgentRole.ARCHITECT, "Design a cache layer.")

    # structured JSON
    resp = await router.complete_json(
        AgentRole.CRITIC,
        "Score this design.",
        schema_hint='{"score": int, "issues": [str], "verdict": str}',
    )
    print(resp.json)  # raises if no JSON found
```

**Environment variables** to configure without code changes:
`TINKER_SERVER_URL`, `TINKER_SECONDARY_URL`, `TINKER_SERVER_MODEL`, `TINKER_SECONDARY_MODEL`, `TINKER_SERVER_CTX`, `TINKER_SECONDARY_CTX`

**Running the tests:**
```bash
# unit only (no Ollama needed)
python -m tinker.model_client.tests --unit-only

# with live Ollama
python -m tinker.model_client.tests --integration \
  --server-url http://192.168.1.10:11434 \
  --secondary-url http://192.168.1.20:11434
```