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

Prompt construction
-------------------
Agents use ``PromptBuilder`` (prompts/builder.py) to build system and user
prompts from version-controlled templates.  This is the production code path:

    system, user = PromptBuilder.for_architect_micro(
        architecture_state = context_str,
        task_description   = task_desc,
        constraints        = constraints_str,
        context            = "(see architecture state above)",
    )

If ``PromptBuilder`` is unavailable (e.g. templates directory missing during
testing), the agents fall back to concise inline prompts so the orchestrator
can always make progress.

Retry policy
------------
Every model call is wrapped with ``resilience.retry.retry_async`` using the
``CONSERVATIVE`` preset: 3 attempts, 2-second base delay, 60-second cap, with
full jitter.  Only ``TinkerError`` subclasses where ``retryable=True`` trigger
a retry — connection errors, timeouts, and rate-limit errors are retried;
response parse errors and configuration errors propagate immediately.

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
import uuid
from contextvars import ContextVar
from typing import Any

# Standard Python logging — any message from this module will appear with
# the prefix "tinker.agents" in the log output.
logger = logging.getLogger("tinker.agents")

# ---------------------------------------------------------------------------
# Lightweight distributed tracing via contextvars
# ---------------------------------------------------------------------------
# Each agent call runs in an async context.  We store a trace_id in a
# ContextVar so that every log message emitted from within that call
# automatically carries the same correlation ID.  This makes it possible to
# find all log lines for a single Architect→Critic→Synthesizer chain in any
# log aggregator (Loki, CloudWatch, Datadog) by filtering on trace_id.
#
# Usage:
#   _current_trace_id.set("abc-123")
#   logger.info("msg [trace_id=%s]", _current_trace_id.get())
#
# For full OpenTelemetry support, replace this with:
#   from opentelemetry import trace
#   tracer = trace.get_tracer(__name__)
#   with tracer.start_as_current_span("architect.call") as span:
#       span.set_attribute("task.id", task_id)
_current_trace_id: ContextVar[str] = ContextVar("trace_id", default="")


# ---------------------------------------------------------------------------
# Lazy infrastructure imports
# ---------------------------------------------------------------------------
# Both PromptBuilder and retry_async are optional at import time so that
# agents.py remains loadable in minimal test environments without the full
# prompts/ and resilience/ packages installed.


def _get_prompt_builder_cls():
    """Return the PromptBuilder class, or None if not available."""
    try:
        from core.prompts.builder import PromptBuilder

        return PromptBuilder
    except Exception as exc:
        logger.debug(
            "agents: PromptBuilder not available — using inline prompts: %s", exc
        )
        return None


def _get_retry_async():
    """Return (retry_async, CONSERVATIVE) or (None, None) if unavailable."""
    try:
        from infra.resilience.retry import retry_async, CONSERVATIVE

        return retry_async, CONSERVATIVE
    except Exception as exc:
        logger.debug(
            "agents: resilience.retry not available — calls are not retried: %s", exc
        )
        return None, None


# Module-level singleton — built once and reused across all agent calls.
# Recreating the registry on every call would give each call its own fresh
# token bucket, making the rate limiter a no-op.
_rate_limiter_registry = None
_rate_limiter_registry_initialized = False


def _get_rate_limiter_registry():
    """Return the process-wide RateLimiterRegistry singleton, or None if unavailable."""
    global _rate_limiter_registry, _rate_limiter_registry_initialized
    if _rate_limiter_registry_initialized:
        return _rate_limiter_registry
    _rate_limiter_registry_initialized = True
    try:
        from infra.resilience.rate_limiter import build_default_rate_limiters

        _rate_limiter_registry = build_default_rate_limiters()
    except Exception as exc:
        logger.debug(
            "agents: rate_limiter not available — token tracking disabled: %s", exc
        )
        _rate_limiter_registry = None
    return _rate_limiter_registry


# ---------------------------------------------------------------------------
# Response schema validation
# ---------------------------------------------------------------------------
# Each agent defines the keys it requires in the LLM's JSON response.
# _validate_agent_response() raises ResponseParseError when required keys are
# missing so the retry decorator can handle the failure rather than silently
# producing empty/garbage artifacts.
#
# This is deliberately a strict key-presence check rather than a full JSON
# Schema validation.  For stronger enforcement, replace with:
#   import jsonschema; jsonschema.validate(d, ARCHITECT_SCHEMA)

_ARCHITECT_REQUIRED_KEYS: frozenset[str] = frozenset(
    {
        "content",
    }
)
_ARCHITECT_PROMPTBUILDER_REQUIRED_KEYS: frozenset[str] = frozenset(
    {
        "artifact_type",
        "title",
        "design",
    }
)
_CRITIC_REQUIRED_KEYS: frozenset[str] = frozenset(
    {
        "content",
        "score",
    }
)


def _validate_agent_response(
    d: dict,
    required_keys: frozenset[str],
    agent_name: str,
) -> None:
    """Raise ResponseParseError if any required key is absent from *d*.

    Parameters
    ----------
    d             : The parsed JSON dict from the LLM.
    required_keys : Keys that MUST be present.
    agent_name    : Human label for error messages (e.g. "Architect").

    Raises
    ------
    ResponseParseError
        When one or more required keys are missing.  The exception has
        ``retryable=False`` because a retry would likely produce the same
        malformed response — the caller should log the issue and use the
        fallback path instead.
    """
    from exceptions import ResponseParseError

    missing = required_keys - d.keys()
    if missing:
        trace_id = _current_trace_id.get() or "?"
        raise ResponseParseError(
            f"{agent_name} response missing required keys: {sorted(missing)} "
            f"(got keys: {sorted(d.keys())}) [trace_id={trace_id}]",
        )


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
        if any(
            kw in line.lower()
            for kw in ("gap", "unknown", "unclear", "need to research", "investigate")
        ):
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
        r"score[:\s]+([0-9]+(?:\.[0-9]+)?)",  # "score: 0.75"
        r"([0-9]+(?:\.[0-9]+)?)\s*/\s*10",  # "7.5/10" anywhere
        r"([0-9]\.[0-9]+)\b",  # any decimal like "0.8"
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


def _parse_architect_structured(d: dict) -> tuple[str, list, list, list, list]:
    """
    Parse a structured (JSON) Architect response into the canonical tuple.

    Handles BOTH the PromptBuilder schema (artifact_type="design_proposal",
    fields: design.summary, open_questions, candidate_next_tasks) AND the
    legacy schema (fields: content, knowledge_gaps, decisions, candidate_tasks).

    Validates that the required keys for each schema are present and raises
    ResponseParseError if not.

    Returns (content, knowledge_gaps, decisions, open_questions, candidate_tasks).
    """
    # ── PromptBuilder schema (design_proposal) ──────────────────────────────
    if d.get("artifact_type") == "design_proposal":
        _validate_agent_response(
            d, _ARCHITECT_PROMPTBUILDER_REQUIRED_KEYS, "Architect(PromptBuilder)"
        )
        design = d.get("design", {})
        summary = design.get("summary", "")
        # Reconstruct a readable narrative from structured fields
        components_txt = ""
        for comp in design.get("components", [])[:5]:
            components_txt += (
                f"\n- {comp.get('name', '?')}: {comp.get('responsibility', '')}"
            )
        trade_offs = design.get("trade_offs", {})
        trade_offs_txt = (
            f"\nGains: {trade_offs.get('gains', [])}"
            f"\nCosts: {trade_offs.get('costs', [])}"
            f"\nRisks: {trade_offs.get('risks', [])}"
        )
        content = (
            f"## {d.get('title', 'Design Proposal')}\n"
            f"{summary}"
            f"\n### Components{components_txt}"
            f"\n### Trade-offs{trade_offs_txt}"
        )
        # Extract knowledge gaps from open_questions (same concept)
        open_qs = d.get("open_questions", [])
        # candidate_next_tasks → candidate_tasks with remapping
        raw_next = d.get("candidate_next_tasks", [])
        candidate_tasks = [
            {
                "title": t.get("task", "")[:120],
                "description": t.get("task", ""),
                "type": "design",
                "subsystem": "unknown",
                "confidence_gap": 0.5,
                "tags": [],
            }
            for t in raw_next
            if isinstance(t, dict)
        ]
        # reasoning_chain steps → decisions
        decisions = [
            step.get("thought", "")
            for step in d.get("reasoning_chain", [])[:3]
            if isinstance(step, dict)
        ]
        return content, open_qs, decisions, open_qs, candidate_tasks

    # ── Legacy schema ────────────────────────────────────────────────────────
    _validate_agent_response(d, _ARCHITECT_REQUIRED_KEYS, "Architect(legacy)")
    content = d.get("content", "")
    gaps = d.get("knowledge_gaps", [])
    decisions = d.get("decisions", [])
    questions = d.get("open_questions", [])
    candidates = d.get("candidate_tasks", [])
    return content, gaps, decisions, questions, candidates


def _build_architect_prompts(
    task_desc: str,
    subsystem: str,
    context_str: str,
    grub_section: str,
    constraints_str: str,
) -> tuple[str, str]:
    """
    Build (system, user) prompt pair using PromptBuilder when available.

    Falls back to concise inline prompts so the orchestrator can always
    make progress even without the prompts/ package.

    Parameters
    ----------
    task_desc     : Short description of the design task.
    subsystem     : Subsystem this task belongs to.
    context_str   : The assembled architecture state (from ContextAssembler).
    grub_section  : Optional section for review tasks; empty string otherwise.
    constraints_str : Formatted constraints string.
    """
    pb_cls = _get_prompt_builder_cls()
    if pb_cls is not None:
        try:
            system, user = pb_cls.for_architect_micro(
                # The assembled context IS the architecture state — it contains
                # all the relevant sections (arch state, recent artifacts,
                # research notes, prior critique) that ContextAssembler fetched.
                architecture_state=context_str[:3000],
                task_description=task_desc,
                constraints=constraints_str or "None specified.",
                # Additional context: the grub review section if present.
                context=grub_section.strip() or "(see architecture state above)",
            )
            return system, user
        except Exception as exc:
            logger.debug(
                "agents: PromptBuilder.for_architect_micro failed (%s) — "
                "using inline fallback prompt",
                exc,
            )

    # Inline fallback — matches the PromptBuilder output format closely so
    # downstream response parsing handles both paths identically.
    system = (
        "You are a senior software architect in Tinker, an autonomous "
        "architecture-design engine.  Analyse the task and produce a structured "
        "design proposal.\n\n"
        "IMPORTANT: Respond with a JSON object containing:\n"
        "- 'content': string — your full design narrative\n"
        "- 'knowledge_gaps': array of strings — topics you need more info on\n"
        "- 'candidate_tasks': array of objects with 'title','description','type',"
        "'subsystem','confidence_gap','tags'\n"
        "- 'decisions': array of strings — key design decisions made\n"
        "- 'open_questions': array of strings\n"
    )
    user = (
        f"## Task\nSubsystem: {subsystem}\nDescription: {task_desc}\n\n"
        f"## Architecture State\n{context_str[:3000]}\n\n"
        f"## Constraints\n{constraints_str or 'None specified.'}"
        f"{grub_section}\n\n"
        "Produce your JSON design proposal now."
    )
    return system, user


def _build_critic_prompts(
    task_desc: str,
    design_content: str,
) -> tuple[str, str]:
    """
    Build (system, user) prompt pair for the Critic using PromptBuilder.

    Falls back to inline prompts if PromptBuilder is unavailable.

    Parameters
    ----------
    task_desc      : The original task description (for context).
    design_content : The Architect's proposal content to critique.
    """
    pb_cls = _get_prompt_builder_cls()
    if pb_cls is not None:
        try:
            system, user = pb_cls.for_critic_micro(
                target_artifact={"content": design_content[:2000]},
                architecture_state=task_desc,
                focus_areas="Design quality, scalability, and completeness.",
            )
            return system, user
        except Exception as exc:
            logger.debug(
                "agents: PromptBuilder.for_critic_micro failed (%s) — "
                "using inline fallback prompt",
                exc,
            )

    # Inline fallback
    system = (
        "You are a senior software architect acting as a critic in Tinker, "
        "an autonomous architecture engine.  Evaluate the design proposal "
        "and respond with a JSON object containing:\n"
        "- 'content': string — your critique narrative\n"
        "- 'score': float between 0 and 1 (1 = excellent)\n"
        "- 'flags': array of strings — specific issues to address\n"
        "- 'strengths': array of strings\n"
    )
    user = (
        f"## Original Task\n{task_desc}\n\n"
        f"## Design Proposal\n{design_content}\n\n"
        "Critique this proposal and return your JSON evaluation."
    )
    return system, user


def _build_synthesizer_prompts(
    level: str,
    **kwargs: Any,
) -> tuple[str, str]:
    """
    Build (system, user) prompt pair for the Synthesizer using PromptBuilder.

    For meso synthesis: uses ``PromptBuilder.for_synthesizer_meso()``.
    For macro synthesis: falls back to inline (no factory method yet).

    Falls back to inline prompts if PromptBuilder is unavailable.
    """
    pb_cls = _get_prompt_builder_cls()

    if level == "meso" and pb_cls is not None:
        subsystem = kwargs.get("subsystem", "unknown")
        artifacts = kwargs.get("artifacts", [])
        try:
            system, user = pb_cls.for_synthesizer_meso(
                source_artifacts=artifacts[:10],
                synthesis_directive=(
                    f"Synthesise all micro-level design artifacts for the "
                    f"'{subsystem}' subsystem into a coherent design document."
                ),
                prior_meso_synthesis="None — first meso synthesis for this subsystem.",
            )
            return system, user
        except Exception as exc:
            logger.debug(
                "agents: PromptBuilder.for_synthesizer_meso failed (%s) — "
                "using inline fallback",
                exc,
            )

    # Inline fallbacks (macro always uses this path; meso uses it on PB failure)
    if level == "meso":
        subsystem = kwargs.get("subsystem", "unknown")
        artifacts = kwargs.get("artifacts", [])
        artifacts_text = "\n---\n".join(
            (a.get("content", str(a)) if isinstance(a, dict) else str(a))[:500]
            for a in artifacts[:10]
        )
        system = (
            "You are a senior software architect. Synthesise the provided "
            "design artifacts for a subsystem into a coherent design document."
        )
        user = (
            f"## Subsystem: {subsystem}\n\n"
            f"## Artifacts to synthesise ({len(artifacts)} items)\n{artifacts_text}\n\n"
            "Produce a synthesis document covering architecture decisions, patterns, "
            "open issues, and recommended next steps."
        )
    else:
        # level == "macro"
        documents = kwargs.get("documents", [])
        version = kwargs.get("snapshot_version", 0)
        micro_count = kwargs.get("total_micro_loops", 0)
        docs_text = "\n---\n".join(
            (d.get("content", str(d)) if isinstance(d, dict) else str(d))[:300]
            for d in documents[:20]
        )
        system = (
            "You are a chief architect. Produce a high-level architectural snapshot "
            "from the subsystem design documents provided."
        )
        user = (
            f"## Global Snapshot v{version} (after {micro_count} micro loops)\n\n"
            f"## Subsystem Documents ({len(documents)} total)\n{docs_text}\n\n"
            "Produce a macro-level architecture snapshot covering system-wide patterns, "
            "cross-cutting concerns, major decisions, and open risks."
        )

    return system, user


# ---------------------------------------------------------------------------
# ArchitectAgent
# ---------------------------------------------------------------------------


class ArchitectAgent:
    """
    The Architect thinks creatively about architecture problems.

    When called, it receives:
      - A task dict (what to design)
      - A context dict (assembled by ContextAssembler — contains the
        architecture state, recent artifacts, research notes, etc.)

    It uses ``PromptBuilder.for_architect_micro()`` to build a
    production-grade (system, user) prompt pair from version-controlled
    templates, then sends it to the large 7B model (qwen3:7b).

    The model call is automatically retried on transient failures
    (connection errors, timeouts, rate limits) using
    ``resilience.retry.retry_async`` with the ``CONSERVATIVE`` preset
    (3 attempts, 2 s base delay, full jitter).

    Response schema (what this method returns)
    ------------------------------------------
    - 'content'         — the design narrative (string)
    - 'tokens_used'     — how many tokens this call consumed (for monitoring)
    - 'knowledge_gaps'  — list of strings: things to research
    - 'decisions'       — list of strings: design choices made
    - 'open_questions'  — list of strings: unresolved questions
    - 'candidate_tasks' — list of dicts: follow-up tasks to create

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
            context — assembled context dict from ContextAssembler.build().
                      The 'prompt' key contains the full assembled context string.

        Returns a dict with:
            'content'         — the design narrative (string)
            'tokens_used'     — how many tokens this call consumed (for monitoring)
            'knowledge_gaps'  — list of strings: things to research
            'decisions'       — list of strings: design choices made
            'open_questions'  — list of strings: unresolved questions
            'candidate_tasks' — list of dicts: follow-up tasks to create
            'trace_id'        — correlation ID for this agent call
        """
        from core.llm.types import AgentRole, Message, ModelRequest

        # Assign a trace ID for this call.  If the task carries a trace_id
        # (e.g. from an upstream HTTP request) propagate it; otherwise
        # generate a fresh one.  Stored in a ContextVar so helper functions
        # and log messages can reference it without being threaded through
        # every call site.
        trace_id = task.get("trace_id") or str(uuid.uuid4())
        _current_trace_id.set(trace_id)
        task_id = task.get("id", "?")

        logger.info(
            "ArchitectAgent.call start [task=%s trace_id=%s]", task_id, trace_id
        )

        # Pull the most useful fields out of the task dict.
        # .get() with a default means we won't crash if a field is missing.
        task_desc = task.get("description", task.get("title", "architecture task"))
        subsystem = task.get("subsystem", "unknown")
        constraints_list = task.get("constraints", [])
        constraints_str = (
            ", ".join(constraints_list)
            if isinstance(constraints_list, list) and constraints_list
            else str(constraints_list)
            if constraints_list
            else "None specified."
        )

        # The context dict from ContextAssembler.build() has a 'prompt' key
        # containing the full assembled context (architecture state + recent
        # artifacts + research notes + prior critique), pre-truncated to the
        # token budget.  This becomes the 'architecture_state' for PromptBuilder.
        #
        # Token budget guard: warn if the assembler already hit or exceeded its
        # budget (should not normally happen, but surfaces misconfigurations).
        _tokens_used = context.get("tokens_used", 0)
        _tokens_budget = context.get("tokens_budget", 8192)
        if _tokens_used and _tokens_budget and _tokens_used > _tokens_budget:
            logger.warning(
                "ArchitectAgent: context tokens_used (%d) exceeds budget (%d) "
                "[task=%s trace_id=%s] — prompt will be truncated",
                _tokens_used,
                _tokens_budget,
                task_id,
                trace_id,
            )
        # Use the assembler's token budget to derive a character limit instead
        # of a hardcoded slice.  chars_per_token ≈ 3.8 (LLaMA/GPT average).
        _chars_limit = int(_tokens_budget * 3.8)
        raw_prompt = context.get("prompt", _json_block(context))
        context_str = (
            raw_prompt[:_chars_limit] if len(raw_prompt) > _chars_limit else raw_prompt
        )

        # ── Grub implementation section (review tasks only) ─────────────────
        # When Tinker is processing a 'review' task, _enrich_review_context()
        # in the micro loop adds a 'grub_implementation' key to the context
        # dict.  This contains what Grub actually built (score, files, tests,
        # summary) so the Architect can make an informed decision about
        # whether the design needs refining.
        grub_section = ""
        grub_impl = context.get("grub_implementation")
        if grub_impl:
            score = grub_impl.get("score", "?")
            summary = grub_impl.get("summary", "")[:600]
            files = grub_impl.get("files_written", [])[:5]
            tests = grub_impl.get("test_results", {})
            status = grub_impl.get("status", "unknown")
            tests_str = (
                f"Passed: {tests.get('passed', '?')}, "
                f"Failed: {tests.get('failed', '?')}"
                if tests
                else "Not available"
            )
            files_str = ", ".join(files) if files else "(none recorded)"
            grub_section = (
                f"\n\n## Grub Implementation Report\n"
                f"**Status**: {status}  |  **Quality score**: {score}\n"
                f"**Files produced**: {files_str}\n"
                f"**Tests**: {tests_str}\n"
                f"**Summary**: {summary}\n\n"
                f"Based on this report, decide whether the design needs "
                f"refining or can be accepted as-is."
            )

        # Build (system, user) prompts using PromptBuilder, falling back to
        # inline prompts when PromptBuilder is unavailable.
        system_prompt, user_prompt = _build_architect_prompts(
            task_desc=task_desc,
            subsystem=subsystem,
            context_str=context_str,
            grub_section=grub_section,
            constraints_str=constraints_str,
        )

        # Build the request and send it to the AI via ModelRouter.
        # AgentRole.ARCHITECT tells the router to use the large, capable model
        # (qwen3:7b on the primary server) rather than the smaller critic model.
        req = ModelRequest(
            agent_role=AgentRole.ARCHITECT,
            messages=[
                Message("system", system_prompt),
                Message("user", user_prompt),
            ],
            expect_json=True,  # Tell the router we want JSON back
            temperature=0.7,  # 0=deterministic, 1=very creative; 0.7 is a good balance
        )

        # Wrap the model call with retry for transient failures.
        # CONSERVATIVE = 3 attempts, 2 s base delay, 60 s max, full jitter.
        # Only TinkerError subclasses with retryable=True are retried.
        retry_async, CONSERVATIVE = _get_retry_async()
        if retry_async is not None:
            resp = await retry_async(
                lambda: self._router.complete(req),
                config=CONSERVATIVE,
            )
        else:
            resp = await self._router.complete(req)

        # Record token cost against the architect rate limiter.
        try:
            _rl = _get_rate_limiter_registry()
            if _rl is not None:
                lim = _rl.get("architect")
                if lim is not None:
                    lim.record_tokens(resp.total_tokens)
        except Exception:
            pass  # cost tracking must never crash the agent

        # Parse the response: try structured JSON first, fall back to regex
        if resp.structured and isinstance(resp.structured, dict):
            # Happy path: the AI returned valid JSON and the router parsed it.
            # _parse_architect_structured handles both the PromptBuilder schema
            # (artifact_type="design_proposal") and the legacy schema.
            try:
                content, gaps, decisions, questions, candidates = (
                    _parse_architect_structured(resp.structured)
                )
            except Exception as parse_exc:
                # Schema validation failed — fall back to raw text so the loop
                # can make progress, but log the failure for diagnosis.
                logger.warning(
                    "ArchitectAgent: schema validation failed (%s) — "
                    "falling back to raw text [trace_id=%s]",
                    parse_exc,
                    trace_id,
                )
                content = resp.raw_text
                gaps = _extract_knowledge_gaps(content)
                decisions = []
                questions = []
                candidates = _extract_candidate_tasks(content)
        else:
            # Fallback: the AI returned plain text — extract what we can.
            content = resp.raw_text
            gaps = _extract_knowledge_gaps(content)
            decisions = []
            questions = []
            candidates = _extract_candidate_tasks(content)

        logger.info(
            "ArchitectAgent.call complete [task=%s tokens=%d trace_id=%s]",
            task_id,
            resp.total_tokens,
            trace_id,
        )

        # Return a normalised dict that the Orchestrator's micro loop expects.
        # Every field has a safe default so callers don't need to handle None.
        return {
            "content": content,
            "tokens_used": resp.total_tokens,
            "knowledge_gaps": gaps,
            "decisions": decisions,
            "open_questions": questions,
            "candidate_tasks": candidates,  # TaskGenerator reads this field
            "trace_id": trace_id,
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

    The Critic uses ``PromptBuilder.for_critic_micro()`` to build production
    prompts from templates, and ``resilience.retry`` for transient error recovery.

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
            'trace_id'    — correlation ID for this agent call
        """
        from core.llm.types import AgentRole, Message, ModelRequest

        # Propagate trace_id from the Architect's result so all three agents
        # in a single micro loop share the same correlation ID.
        trace_id = (
            architect_result.get("trace_id")
            or task.get("trace_id")
            or str(uuid.uuid4())
        )
        _current_trace_id.set(trace_id)
        task_id = task.get("id", "?")

        logger.info("CriticAgent.call start [task=%s trace_id=%s]", task_id, trace_id)

        # Truncate the design content to avoid exceeding the model's context window.
        # 3000 chars is usually enough to convey the key ideas.
        design_content = architect_result.get("content", "")[:3000]
        task_desc = task.get("description", task.get("title", ""))

        # Build (system, user) prompts using PromptBuilder, falling back to
        # inline prompts when PromptBuilder is unavailable.
        system_prompt, user_prompt = _build_critic_prompts(
            task_desc=task_desc,
            design_content=design_content,
        )

        req = ModelRequest(
            agent_role=AgentRole.CRITIC,
            messages=[
                Message("system", system_prompt),
                Message("user", user_prompt),
            ],
            expect_json=True,
            temperature=0.3,  # Lower temperature = more consistent, less creative scoring
        )

        # Retry on transient failures.
        retry_async, CONSERVATIVE = _get_retry_async()
        if retry_async is not None:
            resp = await retry_async(
                lambda: self._router.complete(req),
                config=CONSERVATIVE,
            )
        else:
            resp = await self._router.complete(req)

        # Record token cost against the critic rate limiter.
        try:
            _rl = _get_rate_limiter_registry()
            if _rl is not None:
                lim = _rl.get("critic")
                if lim is not None:
                    lim.record_tokens(resp.total_tokens)
        except Exception:
            pass

        # Parse the response, with schema validation and regex fallback
        if resp.structured and isinstance(resp.structured, dict):
            try:
                _validate_agent_response(
                    resp.structured, _CRITIC_REQUIRED_KEYS, "Critic"
                )
                content = resp.structured.get("content", resp.raw_text)
                score = float(resp.structured.get("score", 0.7))
                flags = resp.structured.get("flags", [])
            except Exception as parse_exc:
                logger.warning(
                    "CriticAgent: schema validation failed (%s) — "
                    "falling back to raw text [trace_id=%s]",
                    parse_exc,
                    trace_id,
                )
                content = resp.raw_text
                score = _extract_score(content)
                flags = []
        else:
            content = resp.raw_text
            score = _extract_score(content)
            flags = []

        # Always clamp the score to [0, 1] even if the model returns something
        # outside that range (e.g. "1.2" or "-0.1").
        score = max(0.0, min(1.0, score))

        logger.info(
            "CriticAgent.call complete [task=%s score=%.2f tokens=%d trace_id=%s]",
            task_id,
            score,
            resp.total_tokens,
            trace_id,
        )

        return {
            "content": content,
            "tokens_used": resp.total_tokens,
            "score": score,
            "flags": flags,
            "trace_id": trace_id,
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
       Uses ``PromptBuilder.for_synthesizer_meso()`` for production-grade prompts.

    2. **Macro synthesis** (level="macro"): Every few hours, the Synthesizer reads
       all the meso-level documents and produces a global architectural snapshot.
       Think of this as the quarterly architecture review document.

    Both levels use ``resilience.retry`` for transient error recovery.
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
        from core.llm.types import AgentRole, Message, ModelRequest

        trace_id = kwargs.pop("trace_id", None) or str(uuid.uuid4())
        _current_trace_id.set(trace_id)

        logger.info(
            "SynthesizerAgent.call start [level=%s trace_id=%s]", level, trace_id
        )

        # Build (system, user) prompts using PromptBuilder when available.
        system_prompt, user_prompt = _build_synthesizer_prompts(level, **kwargs)

        # Use the SYNTHESIZER role — typically routed to the large model
        req = ModelRequest(
            agent_role=AgentRole.SYNTHESIZER,
            messages=[
                Message("system", system_prompt),
                Message("user", user_prompt),
            ],
            expect_json=False,  # Synthesis output is prose, not JSON
            temperature=0.5,  # Moderate temperature: creative but coherent
        )

        # Retry on transient failures.
        retry_async, CONSERVATIVE = _get_retry_async()
        if retry_async is not None:
            resp = await retry_async(
                lambda: self._router.complete(req),
                config=CONSERVATIVE,
            )
        else:
            resp = await self._router.complete(req)

        # Record token cost against the synthesizer rate limiter.
        try:
            _rl = _get_rate_limiter_registry()
            if _rl is not None:
                lim = _rl.get("synthesizer")
                if lim is not None:
                    lim.record_tokens(resp.total_tokens)
        except Exception:
            pass

        logger.info(
            "SynthesizerAgent.call complete [level=%s tokens=%d trace_id=%s]",
            level,
            resp.total_tokens,
            trace_id,
        )

        return {
            "content": resp.raw_text,
            "tokens_used": resp.total_tokens,
            "level": level,
            "trace_id": trace_id,
        }
