"""
agents.py — The three AI agent wrappers for Tinker.
====================================================

What this file does
--------------------
This file defines the three "agents" that Tinker uses to think about architecture:

1. **ArchitectAgent** — The main thinker. Given a task (e.g. "Design the caching
   layer"), it produces a detailed design proposal with decisions, knowledge gaps,
   and follow-up tasks to investigate.

2. **CriticAgent** — The reviewer. Given the Architect's proposal, it scores it,
   identifies weaknesses (called "flags"), and lists strengths.

3. **SynthesizerAgent** — The summariser. After many micro loops, it reads all
   the results so far and distills them into a coherent document. Used in both
   the meso loop (per subsystem) and the macro loop (system-wide snapshot).

Why they're in one file
-----------------------
All three agents have the same basic pattern:
  build a prompt → send to AI model → parse the response → return a dict

Keeping them together makes it easy to see how they differ (different system
prompts, different models, different output fields) without jumping between files.

How agents fit into the system
-------------------------------
The Orchestrator (orchestrator/orchestrator.py) is wired up with all three agents
at startup and calls them in sequence during each loop:

    micro loop: architect.call() → critic.call() → store result
    meso loop:  synthesizer.call(level="meso", ...)
    macro loop: synthesizer.call(level="macro", ...)

All three agents talk to the AI models through the `ModelRouter` (llm/router.py),
which decides whether to use the primary server (big 7B model) or the secondary
machine (smaller, faster model).

Error handling
--------------
If the AI returns a JSON response, great — we parse the fields directly.
If the AI returns plain text (no JSON), we fall back to regex helpers that try
to extract structured information from the free text. This makes the system
resilient to model variability.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

# Standard Python logging — any message from this module will appear with
# the prefix "tinker.agents" in the log output.
logger = logging.getLogger("tinker.agents")


# ---------------------------------------------------------------------------
# Internal helper functions (not part of the public API)
# ---------------------------------------------------------------------------

def _json_block(obj: Any) -> str:
    """
    Convert any Python object into a nicely-formatted JSON string.

    We use this to embed Python dicts (like task info or context) into
    the prompt text we send to the AI model.

    If the object can't be serialised to JSON (e.g. it contains a custom
    object), we fall back to Python's str() representation.
    """
    try:
        return json.dumps(obj, indent=2, ensure_ascii=False)
    except Exception:
        return str(obj)


def _extract_knowledge_gaps(text: str) -> list[str]:
    """
    When the AI doesn't return structured JSON, try to pull 'knowledge gaps'
    out of free-form text by looking for keywords like "unknown", "unclear",
    "need to research", etc.

    A 'knowledge gap' is something the Architect doesn't know yet and wants
    the Researcher tool to look up. For example: "Unknown: how does DynamoDB
    handle write amplification at scale?"

    Returns up to 5 gaps found in the text.
    """
    gaps = []
    for line in text.splitlines():
        # Strip common bullet-point characters and surrounding whitespace
        line = line.strip("- •*").strip()
        # Only keep lines that contain keywords suggesting a knowledge gap
        if any(kw in line.lower() for kw in ("gap", "unknown", "unclear", "need to research", "investigate")):
            # Filter out very short lines (not useful) and very long ones (probably paragraphs)
            if 10 < len(line) < 300:
                gaps.append(line)
    return gaps[:5]  # Return at most 5, to keep prompts manageable


def _extract_candidate_tasks(text: str) -> list[dict]:
    """
    When the AI doesn't return structured JSON, try to find a JSON object
    embedded somewhere in the free-form text and extract 'candidate_tasks'.

    Candidate tasks are follow-up items the Architect wants to investigate
    next (e.g. "Investigate: what caching library works best for this use case?").

    Returns an empty list if no tasks can be extracted.
    """
    try:
        # Search for any { ... } block in the text using a greedy regex.
        # re.DOTALL makes '.' match newlines, so we can find multi-line JSON.
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            return parsed.get("candidate_tasks", [])
    except Exception:
        pass  # If parsing fails for any reason, return empty list
    return []


def _extract_score(text: str) -> float:
    """
    When the AI returns plain text instead of JSON, try to find a numeric
    score from patterns like "Score: 7.5/10" or "I give it a 0.8".

    We normalise everything to a 0.0–1.0 scale:
      - If the score looks like it's on a 0–10 scale (value > 1), divide by 10
      - If it's already 0–1, use it as-is

    Returns 0.7 (neutral default) if no score can be found.
    """
    for pattern in [
        r"score[:\s]+([0-9]+(?:\.[0-9]+)?)\s*/\s*10",  # "score: 7.5/10"
        r"score[:\s]+([0-9]+(?:\.[0-9]+)?)",            # "score: 0.75"
        r"([0-9]+(?:\.[0-9]+)?)\s*/\s*10",             # "7.5/10" anywhere
        r"([0-9]\.[0-9]+)\b",                           # any decimal like "0.8"
    ]:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1))
                # Normalise: if the score is greater than 1, assume it's on a 0-10 scale
                return val / 10.0 if val > 1.0 else val
            except ValueError:
                pass
    return 0.7  # Return a neutral score if we can't find any number


# ---------------------------------------------------------------------------
# ArchitectAgent
# ---------------------------------------------------------------------------

class ArchitectAgent:
    """
    The Architect thinks creatively about architecture problems.

    When called, it receives:
      - A task dict (what to design)
      - A context dict (relevant background info assembled from memory)

    It sends a carefully-crafted prompt to the large 7B model (qwen3:7b)
    and asks it to return a structured JSON response with:
      - 'content': the actual design narrative
      - 'knowledge_gaps': things it doesn't know yet
      - 'candidate_tasks': follow-up tasks to investigate
      - 'decisions': key design decisions made
      - 'open_questions': unresolved questions

    If the model returns plain text instead of JSON, we use regex fallbacks
    to extract as much structure as possible.

    Analogy: Think of the Architect as the senior engineer who goes to a
    whiteboard and designs the solution. The Critic then reviews what they drew.
    """

    def __init__(self, router) -> None:
        # Store the ModelRouter so we can use it to send requests to Ollama.
        # The router knows which machine runs which model.
        self._router = router

    async def call(self, task: dict, context: dict) -> dict:
        """
        Run one Architect turn: build a prompt, call the AI, parse the response.

        Parameters:
            task    — dict with 'description', 'subsystem', 'id', etc.
                      (comes from TaskEngine.select_task())
            context — dict with 'prompt' key containing assembled background info
                      (comes from ContextAssembler.build())

        Returns a dict with:
            'content'         — the design narrative (string)
            'tokens_used'     — how many tokens this call consumed (for monitoring)
            'knowledge_gaps'  — list of strings: things to research
            'decisions'       — list of strings: design choices made
            'open_questions'  — list of strings: unresolved questions
            'candidate_tasks' — list of dicts: follow-up tasks to create
        """
        # Import inside the function to avoid circular imports at module load time.
        # These are only needed when we actually call the agent.
        from llm.types import AgentRole, Message
        from llm.router import ModelRouter  # noqa: F401 (type hint only)

        # Pull the most useful fields out of the task dict.
        # .get() with a default means we won't crash if a field is missing.
        task_desc   = task.get("description", task.get("title", "architecture task"))
        subsystem   = task.get("subsystem", "unknown")

        # The context can be large; truncate to 4000 chars to avoid exceeding
        # the model's context window. We prefer the pre-assembled 'prompt' string,
        # but fall back to serialising the whole context dict.
        context_str = context.get("prompt", _json_block(context))[:4000]

        # The system prompt sets the AI's "persona" and tells it exactly
        # what format we expect in the response.
        system_prompt = (
            "You are a senior software architect participating in an autonomous "
            "architecture-design engine called Tinker.  Your role is to analyse "
            "the current task and produce a structured design proposal.\n\n"
            "IMPORTANT: Respond with a JSON object containing:\n"
            "- 'content': string — your full design narrative\n"
            "- 'knowledge_gaps': array of strings — topics you need more info on\n"
            "- 'candidate_tasks': array of objects with 'title','description','type',"
            "'subsystem','confidence_gap','tags'\n"
            "- 'decisions': array of strings — key design decisions made\n"
            "- 'open_questions': array of strings\n"
        )

        # The user prompt is the actual question we're asking on this turn.
        user_prompt = (
            f"## Task\nSubsystem: {subsystem}\nDescription: {task_desc}\n\n"
            f"## Context\n{context_str}\n\n"
            "Produce your JSON design proposal now."
        )

        # Build the request object and send it to the AI via ModelRouter.
        # AgentRole.ARCHITECT tells the router to use the large, capable model
        # (qwen3:7b on the primary server) rather than the smaller critic model.
        from llm.types import ModelRequest
        req = ModelRequest(
            agent_role=AgentRole.ARCHITECT,
            messages=[
                Message("system", system_prompt),
                Message("user",   user_prompt),
            ],
            expect_json=True,   # Tell the router we want JSON back
            temperature=0.7,    # 0=deterministic, 1=very creative; 0.7 is a good balance
        )
        resp = await self._router.complete(req)

        # Parse the response: try structured JSON first, fall back to regex extraction
        if resp.structured and isinstance(resp.structured, dict):
            # Happy path: the AI returned valid JSON and the router parsed it for us
            content   = resp.structured.get("content", resp.raw_text)
            gaps      = resp.structured.get("knowledge_gaps", [])
            decisions = resp.structured.get("decisions", [])
            questions = resp.structured.get("open_questions", [])
            candidates = resp.structured.get("candidate_tasks", [])
        else:
            # Fallback: the AI returned plain text — extract what we can
            content   = resp.raw_text
            gaps      = _extract_knowledge_gaps(content)
            decisions = []
            questions = []
            candidates = _extract_candidate_tasks(content)

        # Return a normalised dict that the Orchestrator's micro loop expects.
        # Every field has a safe default so callers don't need to handle None.
        return {
            "content":        content,
            "tokens_used":    resp.total_tokens,
            "knowledge_gaps": gaps,
            "decisions":      decisions,
            "open_questions": questions,
            "candidate_tasks": candidates,  # TaskGenerator reads this field
        }


# ---------------------------------------------------------------------------
# CriticAgent
# ---------------------------------------------------------------------------

class CriticAgent:
    """
    The Critic reviews the Architect's design and gives it a score.

    It receives the same task and the Architect's output, then returns:
      - 'content': the critique narrative (what's good, what's bad, what's risky)
      - 'score': a float from 0.0 (terrible) to 1.0 (excellent)
      - 'flags': specific problems that should be addressed

    The score is used by the stagnation detector and task generator to decide
    how to proceed. A low score means the design needs more work on this
    subsystem; a high score means we can move on.

    Analogy: If the Architect is the engineer who draws the design, the Critic
    is the tech lead who reviews it and says "this won't scale" or "good idea".

    The Critic uses the smaller, faster model (phi3:mini) on the secondary machine.
    Speed is important here because critiques are done on every single micro loop.
    """

    def __init__(self, router) -> None:
        # Same router as the Architect, but we'll request the CRITIC role,
        # which routes to the smaller/faster model on the secondary machine.
        self._router = router

    async def call(self, task: dict, architect_result: dict) -> dict:
        """
        Run one Critic turn: evaluate the Architect's design proposal.

        Parameters:
            task             — the original task dict (for context)
            architect_result — the dict returned by ArchitectAgent.call()

        Returns a dict with:
            'content'     — the critique narrative (string)
            'tokens_used' — token consumption for monitoring
            'score'       — float 0.0–1.0; higher is better
            'flags'       — list of specific issues to fix
        """
        from llm.types import AgentRole, Message, ModelRequest

        # Truncate the design content to avoid exceeding the model's context window.
        # 3000 chars is usually enough to convey the key ideas.
        design_content = architect_result.get("content", "")[:3000]
        task_desc      = task.get("description", task.get("title", ""))

        system_prompt = (
            "You are a senior software architect acting as a critic in Tinker, "
            "an autonomous architecture engine.  Evaluate the design proposal "
            "and respond with a JSON object containing:\n"
            "- 'content': string — your critique narrative\n"
            "- 'score': float between 0 and 1 (1 = excellent)\n"
            "- 'flags': array of strings — specific issues to address\n"
            "- 'strengths': array of strings\n"
        )
        user_prompt = (
            f"## Original Task\n{task_desc}\n\n"
            f"## Design Proposal\n{design_content}\n\n"
            "Critique this proposal and return your JSON evaluation."
        )

        req = ModelRequest(
            agent_role=AgentRole.CRITIC,
            messages=[
                Message("system", system_prompt),
                Message("user",   user_prompt),
            ],
            expect_json=True,
            temperature=0.3,  # Lower temperature = more consistent, less creative scoring
        )
        resp = await self._router.complete(req)

        # Parse the response, with regex fallback
        if resp.structured and isinstance(resp.structured, dict):
            content = resp.structured.get("content", resp.raw_text)
            score   = float(resp.structured.get("score", 0.7))
            flags   = resp.structured.get("flags", [])
        else:
            content = resp.raw_text
            score   = _extract_score(content)
            flags   = []

        # Always clamp the score to [0, 1] even if the model returns something
        # outside that range (e.g. "1.2" or "-0.1").
        score = max(0.0, min(1.0, score))

        return {
            "content":     content,
            "tokens_used": resp.total_tokens,
            "score":       score,
            "flags":       flags,
        }


# ---------------------------------------------------------------------------
# SynthesizerAgent
# ---------------------------------------------------------------------------

class SynthesizerAgent:
    """
    The Synthesizer reads many past results and distills them into a summary.

    It's used in two situations:

    1. **Meso synthesis** (level="meso"): After several micro loops have worked
       on the same subsystem (e.g. "caching"), the Synthesizer reads all the
       artifacts produced for that subsystem and writes a coherent design document.
       Think of this as the weekly summary of all the whiteboard sessions.

    2. **Macro synthesis** (level="macro"): Every few hours, the Synthesizer reads
       all the meso-level documents and produces a global architectural snapshot.
       Think of this as the quarterly architecture review document.

    The Synthesizer uses the large model (qwen3:7b) because synthesis requires
    deep understanding and coherent writing across large amounts of text.
    """

    def __init__(self, router) -> None:
        self._router = router

    async def call(self, level: str, **kwargs) -> dict:
        """
        Run one synthesis pass at either meso (subsystem) or macro (global) level.

        Parameters:
            level  — "meso" or "macro"
            **kwargs — depends on level:
                For meso:
                    subsystem (str)     — which subsystem is being synthesised
                    artifacts (list)    — list of artifact dicts to synthesise
                For macro:
                    documents (list)    — list of meso-level documents
                    snapshot_version (int) — version counter for this snapshot
                    total_micro_loops (int) — how many micro loops have run so far

        Returns a dict with:
            'content'     — the synthesis document (string)
            'tokens_used' — token consumption
            'level'       — echoes back the level ("meso" or "macro")
        """
        from llm.types import AgentRole, Message, ModelRequest

        if level == "meso":
            # Build a meso synthesis prompt: summarise all artifacts for one subsystem
            subsystem = kwargs.get("subsystem", "unknown")
            artifacts = kwargs.get("artifacts", [])

            # Join up to 10 artifacts, each truncated to 500 chars, with a divider
            artifacts_text = "\n---\n".join(
                (a.get("content", str(a)) if isinstance(a, dict) else str(a))[:500]
                for a in artifacts[:10]
            )
            system_prompt = (
                "You are a senior software architect. Synthesise the provided "
                "design artifacts for a subsystem into a coherent design document."
            )
            user_prompt = (
                f"## Subsystem: {subsystem}\n\n"
                f"## Artifacts to synthesise ({len(artifacts)} items)\n{artifacts_text}\n\n"
                "Produce a synthesis document covering architecture decisions, patterns, "
                "open issues, and recommended next steps."
            )
        else:
            # level == "macro": build a global snapshot from all meso documents
            documents   = kwargs.get("documents", [])
            version     = kwargs.get("snapshot_version", 0)
            micro_count = kwargs.get("total_micro_loops", 0)

            # Join up to 20 documents, each truncated to 300 chars
            docs_text = "\n---\n".join(
                (d.get("content", str(d)) if isinstance(d, dict) else str(d))[:300]
                for d in documents[:20]
            )
            system_prompt = (
                "You are a chief architect. Produce a high-level architectural snapshot "
                "from the subsystem design documents provided."
            )
            user_prompt = (
                f"## Global Snapshot v{version} (after {micro_count} micro loops)\n\n"
                f"## Subsystem Documents ({len(documents)} total)\n{docs_text}\n\n"
                "Produce a macro-level architecture snapshot covering system-wide patterns, "
                "cross-cutting concerns, major decisions, and open risks."
            )

        # Use the SYNTHESIZER role — typically routed to the large model
        req = ModelRequest(
            agent_role=AgentRole.SYNTHESIZER,
            messages=[
                Message("system", system_prompt),
                Message("user",   user_prompt),
            ],
            expect_json=False,  # Synthesis output is prose, not JSON
            temperature=0.5,    # Moderate temperature: creative but coherent
        )
        resp = await self._router.complete(req)

        return {
            "content":     resp.raw_text,
            "tokens_used": resp.total_tokens,
            "level":       level,
        }
