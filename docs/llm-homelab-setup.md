# Homelab LLM Setup — Recommendations & Architecture

## Context

This document captures the recommended LLM configuration for running Tinker on a
two-machine homelab over a local network. It covers model selection rationale,
hardware constraints, how the existing Tinker client/server architecture maps to
this setup, and the three planned improvements needed to get full value from it.

---

## Hardware

| Machine | GPU | VRAM | Role |
|---|---|---|---|
| Asus ROG Strix (main) | RTX 3090 | 24 GB | Main LLM — Architect, Researcher, Synthesizer |
| Secondary PC | Quadro P2200 | 5 GB | Judge/Critic LLM |

Both machines run **Ollama** and are reachable over the local network.

---

## Model Recommendations

### Main LLM (Strix — RTX 3090)

**Recommended: `qwen2.5:32b-instruct-q4_K_M`**

| Model | Quantization | VRAM | Fits 24 GB? |
|---|---|---|---|
| 7B | Q4 | ~4.5 GB | easily |
| 13B | Q4 | ~8 GB | easily |
| 14B | Q4 | ~9 GB | easily |
| **32B** | **Q4** | **~20 GB** | **yes — recommended** |
| 32B | Q6 | ~26 GB | no |
| 70B | Q4 | ~40 GB | no |

**Why 32B:**
- Reliably produces Tinker's complex nested JSON schemas (9–12 required fields) without
  simplification
- Follows instruction constraints (enums, minimum step counts, string length minimums)
  consistently
- Produces genuine reasoning chains — steps are distinct and non-repetitive
- Understands abstract schema descriptions without needing few-shot examples injected
- Leaves ~4 GB headroom for context and Ollama overhead

**Why `qwen2.5-instruct` specifically:**
- Strong structured output / JSON compliance among open-source models at this size
- Good instruction following for complex multi-field schemas
- Alternative: `deepseek-r1:32b` — has trained reasoning built in, stacks well with
  Tinker's `reasoning_chain` scaffolding

---

### Judge / Critic LLM (Secondary — Quadro P2200)

**Recommended: `qwen2.5:3b` or `phi3.5-mini`**

| Model | VRAM | Fits P2200 (5 GB)? |
|---|---|---|
| 3B Q4 | ~2.5 GB | comfortably |
| **4B Q4** | **~3 GB** | **yes** |
| 7B Q4 | ~4.5 GB | tight, may spill to RAM |

**Why 3B–4B:**
- P2200 has 5 GB VRAM — 7B is risky, 3B–4B fits cleanly
- The Critic role does not require deep reasoning — it evaluates a structured output
  against known criteria, which lighter models handle well
- Keeps the GPU fully free on the Strix for the main model
- `qwen2.5:3b` is preferred: same model family as the main LLM, consistent behavior
  and output style across the pipeline

---

## How This Maps to Tinker's Architecture

### Agent Roles → Physical Machines

Tinker routes each `AgentRole` to a `Machine` via `ROLE_MACHINE_MAP` in `llm/types.py:92`:

```
AgentRole.ARCHITECT   → Machine.SERVER     → Strix (32B)
AgentRole.RESEARCHER  → Machine.SERVER     → Strix (32B)
AgentRole.SYNTHESIZER → Machine.SERVER     → Strix (32B)
AgentRole.CRITIC      → Machine.SECONDARY  → P2200 (3B–4B)
```

Despite being 4 roles, this is only 2 physical models and 2 machines.
Architect, Researcher and Synthesizer all share the same model on the same machine.

### Key Files

| File | What it controls |
|---|---|
| `llm/types.py` | `Machine` enum, `ROLE_MACHINE_MAP`, `MachineConfig` dataclass |
| `llm/types.py:106–107` | Default model names (`DEFAULT_SERVER_MODEL`, `DEFAULT_SECONDARY_MODEL`) |
| `llm/types.py:151–192` | `server_defaults()` and `secondary_defaults()` — read from env vars |

### Configuration via Environment Variables

No code changes are needed to point Tinker at the homelab machines.
Set these environment variables before running:

**Strix (main server):**
```
TINKER_SERVER_URL=http://<strix-ip>:11434
TINKER_SERVER_MODEL=qwen2.5:32b-instruct-q4_K_M
TINKER_SERVER_CTX=32768
TINKER_SERVER_MAX_OUT=4096
TINKER_SERVER_TIMEOUT=180
```

**P2200 (secondary / judge):**
```
TINKER_SECONDARY_URL=http://<p2200-ip>:11434
TINKER_SECONDARY_MODEL=qwen2.5:3b
TINKER_SECONDARY_CTX=4096
TINKER_SECONDARY_MAX_OUT=1024
TINKER_SECONDARY_TIMEOUT=60
```

---

## Component Impact vs Current Default Config

| Component | Current default | Homelab setup | Change |
|---|---|---|---|
| Architect LLM | qwen3:7b | qwen2.5:32b | major upgrade |
| Researcher LLM | qwen3:7b | qwen2.5:32b | major upgrade |
| Synthesizer LLM | qwen3:7b | qwen2.5:32b | major upgrade |
| Critic / Judge LLM | phi3:mini | qwen2.5:3b | slight upgrade |
| `prompts/templates.py` | unchanged | unchanged | no change |
| `prompts/validator.py` | unchanged | unchanged | no change |
| `prompts/variants.py` | unchanged | unchanged | no change |
| `context/assembler.py` | 8192 token window | 32k+ (env var) | expanded |
| Anti-stagnation system | unchanged | unchanged | no change |
| Meso / Macro loops | unchanged | unchanged | no change |
| Task engine / queue | unchanged | unchanged | no change |
| Observability / OTLP | unchanged | unchanged | no change |
| Security / encryption | unchanged | unchanged | no change |

**Nothing is degraded.** The 32B main model handles the existing schemas, validators,
and variants without any simplification. All Tinker components operate at full fidelity.

---

## Three Planned Code Changes

These are the only code changes needed. They are all additive — they add new behavior
without modifying or weakening any existing component.

### 1. Judge loop with threshold (`orchestrator/micro_loop.py`, `orchestrator/config.py`)

Currently the Critic runs once and its score is stored but nothing acts on it.

**Change:** Add a configurable `min_critic_score` threshold. After the Critic scores
the Architect output:
- Score ≥ threshold → accept, store artifact, move on
- Score < threshold → inject Critic feedback into a new Architect prompt, re-run
  Architect, re-run Critic
- Cap at N iterations (configurable, e.g. 3) to prevent infinite loops

This implements the "don't stop refining until a desirable level is reached" requirement.

### 2. Expand context window config (`context/assembler.py`, `orchestrator/config.py`, `llm/types.py`)

Currently hardcoded to 8192 tokens. `qwen2.5:32b` supports 32k+ context.

**Change:** Replace the hardcoded `8192` with a config value driven by the
`TINKER_SERVER_CTX` env var (already read by `MachineConfig.server_defaults()`).
The assembler needs to read this value at runtime instead of using a constant.

This lets the Architect see significantly more artifact history per cycle, improving
architectural reasoning quality with no other changes.

### 3. Retry-on-validation-failure (`orchestrator/micro_loop.py`, `prompts/templates.py`)

Currently validation failures fall back to raw text silently — the model is never
re-prompted.

**Change:** If `ValidationResult.valid = False`, re-prompt the model with the specific
validation errors listed ("your output was missing `candidate_next_tasks`..."),
retry up to 2 times before falling back. The model gets a chance to self-correct
before the output is accepted or stored.

---

## Latency Considerations

The Critic now runs on a separate machine over the local network. Each micro loop cycle:

1. Architect call — Strix (fast, 32B on dedicated GPU)
2. Critic call — P2200, over LAN (adds network round-trip + inference time)
3. Possibly 2–3 iterations of above if judge loop threshold not met

**Network overhead:** local network round trips are 0.5–2ms, negligible.
**Bottleneck:** P2200 inference time for the 3B critic (~2–5 seconds per call).
This is acceptable for a homelab / async pipeline. The Strix GPU remains fully
dedicated to the main model at all times.
