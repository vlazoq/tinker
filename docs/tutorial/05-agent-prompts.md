# Chapter 05 — Agent Prompts

## The Problem

We have a model client that can send text to an AI.  But what text?

A raw prompt like "think about this design task" produces inconsistent,
hard-to-parse output.  We need:

1. **Role-specific prompts** — the Architect and the Critic need different
   personalities, different instructions, and different output formats
2. **Structured output** — we need to parse the AI's response reliably.
   Free-form text is hard to extract fields from.
3. **Schema validation** — we need to know when the AI returned garbage

---

## The Architecture Decision

Each agent gets a prompt *builder* that takes parameters (task, context,
previous output) and returns a fully assembled prompt string.  The AI
is instructed to return JSON.  We parse and validate that JSON.

```
architect_prompt(task, context) → prompt string
                                        ↓
                               model.complete(prompt)
                                        ↓
                               raw text (hopefully JSON)
                                        ↓
                    parse_architect_response(text) → ArchitectResult
```

**Why JSON?**  JSON has a rigid structure.  If the AI returns well-formed
JSON, we can reliably extract the design content, the knowledge gaps it
identified, and whether it requests a tool call.  We can also detect
when the AI is hallucinating (produces invalid JSON and we fall back to
a default).

---

## Step 1 — Directory Structure

```
tinker/
  prompts/
    __init__.py
    architect.py
    critic.py
    synthesizer.py
    parser.py     ← shared JSON parsing + fallback logic
```

---

## Step 2 — The Prompt Parser (Shared Utility)

Before building prompts, let's build the parser so we can test our
prompt outputs:

```python
# tinker/prompts/parser.py

"""
Robust JSON parser for AI responses.

AI models sometimes:
  - Wrap JSON in markdown code fences (```json ... ```)
  - Add commentary before or after the JSON
  - Return malformed JSON

This module handles all of those cases.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def extract_json(text: str) -> dict | list | None:
    """
    Try to extract the first valid JSON object or array from text.

    Handles:
      - Pure JSON: {"key": "value"}
      - Markdown fences: ```json\n{"key": "value"}\n```
      - JSON embedded in prose: "Here is my answer: {"key": "value"} done."
    """
    text = text.strip()

    # 1. Try parsing the whole text first (most common case)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. Try stripping markdown code fences
    fenced = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass

    # 3. Try finding a JSON object or array anywhere in the text
    for pattern in [r"(\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})", r"(\[[^\[\]]*\])"]:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                continue

    return None   # nothing worked


def safe_parse(text: str, default: dict) -> dict:
    """
    Parse JSON from text, returning `default` if parsing fails.
    Always logs a warning when falling back to the default.
    """
    result = extract_json(text)
    if isinstance(result, dict):
        return result
    logger.warning(
        "AI response did not contain valid JSON — using default. "
        "Response preview: %s",
        text[:200],
    )
    return default
```

---

## Step 3 — The Architect Prompt

The Architect's job: given a design task and some context, produce a
thoughtful design decision.  It should also flag knowledge gaps (things
it doesn't know and wants to research).

```python
# tinker/prompts/architect.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .parser import safe_parse


# ── Output schema ────────────────────────────────────────────────────────────
# This dataclass mirrors the JSON we expect the AI to return.

@dataclass
class ArchitectResult:
    """Parsed output from the Architect AI."""
    content: str              # the main design text
    confidence: float = 0.5   # self-assessed confidence 0.0–1.0
    knowledge_gaps: list[str] = field(default_factory=list)
    # If the AI wants to do a web search, it sets this:
    tool_call: Optional[str] = None   # e.g. "web_search:consistent hashing"
    raw: str = ""             # the unparsed AI response (for debugging)


# ── Prompt builder ───────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a senior software architect with deep expertise in
distributed systems, API design, and system reliability.

Your task is to produce a thoughtful architectural design decision for the
given problem. Be specific, opinionated, and justify your choices.

IMPORTANT: You must respond with ONLY a JSON object matching this schema:
{
    "content": "Your detailed architectural design here (markdown).",
    "confidence": 0.8,
    "knowledge_gaps": ["A specific thing you're unsure about", "..."],
    "tool_call": null
}

If you need to research something before answering, set tool_call to:
    "web_search:<your search query>"

Do NOT include any text outside the JSON object.
"""


def build_architect_prompt(
    task_title: str,
    task_description: str,
    subsystem: str,
    context: str = "",
    research_results: str = "",
) -> str:
    """
    Assemble the full prompt for the Architect AI.

    Parameters
    ----------
    task_title        : Short title of the design task
    task_description  : Full description of what needs to be designed
    subsystem         : Which part of the system this belongs to
    context           : Recent relevant artifacts from memory
    research_results  : Output from a previous web search (if any)
    """
    parts = [
        f"## Design Task: {task_title}",
        f"**Subsystem:** {subsystem}",
        f"\n{task_description}",
    ]

    if context:
        parts.append(f"\n## Relevant Context from Memory\n{context}")

    if research_results:
        parts.append(f"\n## Research Results\n{research_results}")

    parts.append(
        "\n## Your Task\n"
        "Produce a detailed architectural design decision for this task. "
        "Consider trade-offs, failure modes, and operational concerns. "
        "If you need web research to answer well, set tool_call."
    )

    return "\n".join(parts)


def parse_architect_response(text: str) -> ArchitectResult:
    """
    Parse the Architect's JSON response into an ArchitectResult.
    Falls back to a minimal result if the JSON is invalid.
    """
    default = {
        "content": text,    # if JSON fails, use the raw text as content
        "confidence": 0.3,  # low confidence because we couldn't parse it properly
        "knowledge_gaps": [],
        "tool_call": None,
    }
    data = safe_parse(text, default)

    return ArchitectResult(
        content       = str(data.get("content", text)),
        confidence    = float(data.get("confidence", 0.5)),
        knowledge_gaps= list(data.get("knowledge_gaps", [])),
        tool_call     = data.get("tool_call"),
        raw           = text,
    )
```

---

## Step 4 — The Critic Prompt

The Critic's job: read the Architect's output and evaluate it.  Is it
actually good?  Is it realistic?  Does it miss obvious failure modes?

```python
# tinker/prompts/critic.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .parser import safe_parse


@dataclass
class CriticResult:
    """Parsed output from the Critic AI."""
    score: float              # quality score 0.0–1.0 (0=terrible, 1=excellent)
    summary: str              # one-line verdict
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    raw: str = ""


CRITIC_SYSTEM_PROMPT = """You are a rigorous technical reviewer.
You evaluate architectural design decisions for correctness, completeness,
and practicality.

Respond with ONLY a JSON object:
{
    "score": 0.75,
    "summary": "Solid design with one significant gap",
    "strengths": ["Clear separation of concerns", "..."],
    "weaknesses": ["Doesn't address failover", "..."],
    "suggestions": ["Consider adding a circuit breaker", "..."]
}

Be honest. A score above 0.9 should be rare. Most first-pass designs have gaps.
Do NOT add text outside the JSON.
"""


def build_critic_prompt(
    task_title: str,
    architect_content: str,
    subsystem: str,
) -> str:
    return f"""## Review Task: {task_title}
**Subsystem:** {subsystem}

## Design to Review
{architect_content}

## Your Task
Critically evaluate this design. Identify gaps, unrealistic assumptions,
missing failure modes, and opportunities for improvement. Be specific.
"""


def parse_critic_response(text: str) -> CriticResult:
    default = {
        "score": 0.5,
        "summary": "Could not parse critic response",
        "strengths": [],
        "weaknesses": [],
        "suggestions": [],
    }
    data = safe_parse(text, default)

    return CriticResult(
        score      = max(0.0, min(1.0, float(data.get("score", 0.5)))),
        summary    = str(data.get("summary", "")),
        strengths  = list(data.get("strengths", [])),
        weaknesses = list(data.get("weaknesses", [])),
        suggestions= list(data.get("suggestions", [])),
        raw        = text,
    )
```

---

## Step 5 — The Synthesizer Prompt

The Synthesizer reads multiple Architect outputs for a subsystem and
produces a coherent summary document:

```python
# tinker/prompts/synthesizer.py

from __future__ import annotations

from dataclasses import dataclass

from .parser import safe_parse


@dataclass
class SynthesisResult:
    """Parsed output from the Synthesizer AI."""
    document: str    # full markdown design document for this subsystem
    summary: str     # one-paragraph executive summary
    raw: str = ""


SYNTHESIZER_SYSTEM_PROMPT = """You are a technical writer and architect.
You synthesise multiple design decisions into a coherent, well-structured
architectural document.

Respond with ONLY a JSON object:
{
    "document": "# Subsystem Design\\n\\nFull markdown document here...",
    "summary": "One paragraph executive summary."
}

The document should be in clean markdown, suitable for committing to a
design repository. Do NOT add text outside the JSON.
"""


def build_synthesizer_prompt(
    subsystem: str,
    artifacts: list[str],
) -> str:
    """
    Build a synthesis prompt from a list of artifact strings.

    artifacts: list of recent Architect outputs for this subsystem
    """
    artifact_text = "\n\n---\n\n".join(
        f"## Artifact {i+1}\n{a}" for i, a in enumerate(artifacts)
    )
    return f"""## Synthesis Task: {subsystem}

You have been given {len(artifacts)} architectural design artifact(s) for
the **{subsystem}** subsystem. Synthesise them into a single, coherent
design document.

{artifact_text}

## Your Task
Produce a comprehensive design document that:
- Integrates the key decisions from all artifacts
- Resolves any contradictions
- Adds structure and clarity
- Is ready to be committed as a design reference
"""


def parse_synthesizer_response(text: str) -> SynthesisResult:
    default = {"document": text, "summary": ""}
    data = safe_parse(text, default)
    return SynthesisResult(
        document = str(data.get("document", text)),
        summary  = str(data.get("summary", "")),
        raw      = text,
    )
```

---

## Step 6 — Try It

```python
# test_prompts.py
import asyncio
from llm import ModelClient, ModelRouter
from prompts.architect import build_architect_prompt, parse_architect_response
from prompts.critic import build_critic_prompt, parse_critic_response

async def main():
    client = ModelClient("http://localhost:11434", "qwen3:7b")
    await client.start()

    # Build the architect prompt
    prompt = build_architect_prompt(
        task_title="API Gateway Authentication",
        task_description="Choose an authentication strategy for the API gateway.",
        subsystem="api_gateway",
    )

    print("Sending to Architect...")
    text, pt, ct = await client.complete(
        prompt=prompt,
        system_prompt="You are a senior software architect. Respond in JSON only.",
    )
    print(f"Raw response ({ct} tokens):")
    print(text[:500])

    # Parse the response
    result = parse_architect_response(text)
    print(f"\nConfidence: {result.confidence}")
    print(f"Knowledge gaps: {result.knowledge_gaps}")
    print(f"Content preview: {result.content[:200]}")

    await client.close()

asyncio.run(main())
```

---

## The Critical Pattern: Structured Output with Fallback

The most important thing in this chapter is the pattern in `safe_parse`:

```python
def safe_parse(text: str, default: dict) -> dict:
    result = extract_json(text)
    if isinstance(result, dict):
        return result                # happy path — AI returned valid JSON
    logger.warning("Falling back to default for: %s", text[:200])
    return default                   # graceful fallback
```

AI models sometimes produce invalid JSON.  Instead of crashing the
orchestrator, we:
1. Try to extract JSON from the response
2. Try to find JSON embedded in surrounding text
3. Fall back to a sensible default and log a warning

The orchestrator keeps running.  A low-quality output is better than no
output.

---

## What We Have So Far

```
tinker/
  llm/         ✅  model client + router
  memory/      ✅  four adapters + unified manager
  tools/       ✅  search + scraper + writer + layer
  prompts/     ✅  architect + critic + synthesizer + parser
```

---

→ Next: [Chapter 06 — The Task Engine](./06-task-engine.md)
