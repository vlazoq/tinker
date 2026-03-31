"""
agents/_shared.py
=================

Shared internals for all three Tinker AI agents (Architect, Critic, Synthesizer).

Why this file exists
--------------------
The three agent classes share a significant amount of infrastructure:

  - A distributed trace-ID ContextVar so every log message inside an agent
    call carries the same correlation identifier.
  - Lazy importers that load optional packages (PromptBuilder, retry_async,
    RateLimiterRegistry) at call-time instead of module-import time, keeping
    the agents loadable in minimal test environments.
  - Response-schema constants and validators that enforce a minimum field set
    on LLM JSON responses.
  - Regex-based fallback helpers that extract structured data from plain-text
    responses when the model doesn't return JSON.
  - Prompt-builder adapters that delegate to PromptBuilder when available and
    fall back to inline prompts when it isn't.

None of the symbols here are public API — they are implementation details used
by agents/architect.py, agents/critic.py, and agents/synthesizer.py.  Callers
outside the agents package should import the agent classes directly.
"""

from __future__ import annotations

import json
import logging
import os
import re
from contextvars import ContextVar
from pathlib import Path
from typing import Any

logger = logging.getLogger("tinker.agents")


# ---------------------------------------------------------------------------
# System mode reader
# ---------------------------------------------------------------------------
# Reads the current system mode from the control file written by the web UI.
# This is the same file-based control mechanism used by pause/resume.


def _read_system_mode() -> tuple[str, str]:
    """Return ``(system_mode, research_topic)`` from the control file.

    Falls back to ``("architect", "")`` if the file doesn't exist or is
    unreadable.  This function is called on every prompt-build so the mode
    switch takes effect on the very next micro loop iteration.
    """
    control_dir = Path(os.getenv("TINKER_CONTROL_DIR", "./tinker_control"))
    mode_path = control_dir / "mode.json"
    if mode_path.exists():
        try:
            data = json.loads(mode_path.read_text())
            return data.get("system_mode", "architect"), data.get("research_topic", "")
        except Exception:
            pass
    return "architect", ""


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
# the agents remain loadable in minimal test environments without the full
# prompts/ and resilience/ packages installed.


def _get_prompt_builder_cls():
    """Return the PromptBuilder class, or None if not available."""
    try:
        from core.prompts.builder import PromptBuilder

        return PromptBuilder
    except Exception as exc:
        logger.debug("agents: PromptBuilder not available — using inline prompts: %s", exc)
        return None


def _get_retry_async():
    """Return (retry_async, CONSERVATIVE) or (None, None) if unavailable."""
    try:
        from infra.resilience.retry import CONSERVATIVE, retry_async

        return retry_async, CONSERVATIVE
    except Exception as exc:
        logger.debug("agents: resilience.retry not available — calls are not retried: %s", exc)
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
        logger.debug("agents: rate_limiter not available — token tracking disabled: %s", exc)
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

_ARCHITECT_REQUIRED_KEYS: frozenset[str] = frozenset({"content"})
_ARCHITECT_PROMPTBUILDER_REQUIRED_KEYS: frozenset[str] = frozenset(
    {"artifact_type", "title", "design"}
)
_CRITIC_REQUIRED_KEYS: frozenset[str] = frozenset({"content", "score"})


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
# Internal helper functions
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
        line = line.strip("- •*").strip()
        if (
            any(
                kw in line.lower()
                for kw in ("gap", "unknown", "unclear", "need to research", "investigate")
            )
            and 10 < len(line) < 300
        ):
            gaps.append(line)
    return gaps[:5]


def _extract_candidate_tasks(text: str) -> list[dict]:
    """
    When the AI doesn't return structured JSON, try to find a JSON object
    embedded somewhere in the free-form text and extract 'candidate_tasks'.

    Candidate tasks are follow-up items the Architect wants to investigate
    next. Returns an empty list if no tasks can be extracted.
    """
    try:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            return parsed.get("candidate_tasks", [])
    except Exception:
        pass
    return []


def _extract_score(text: str) -> float:
    """
    When the AI returns plain text instead of JSON, try to find a numeric
    score from patterns like "Score: 7.5/10" or "I give it a 0.8".

    Normalises everything to 0.0–1.0 scale.  Returns 0.7 (neutral default)
    if no score can be found.
    """
    for pattern in [
        r"score[:\s]+([0-9]+(?:\.[0-9]+)?)\s*/\s*10",
        r"score[:\s]+([0-9]+(?:\.[0-9]+)?)",
        r"([0-9]+(?:\.[0-9]+)?)\s*/\s*10",
        r"([0-9]\.[0-9]+)\b",
    ]:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1))
                return val / 10.0 if val > 1.0 else val
            except ValueError:
                pass
    return 0.7


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
        components_txt = ""
        for comp in design.get("components", [])[:5]:
            components_txt += f"\n- {comp.get('name', '?')}: {comp.get('responsibility', '')}"
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
        open_qs = d.get("open_questions", [])
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


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


def _build_architect_prompts(
    task_desc: str,
    subsystem: str,
    context_str: str,
    grub_section: str,
    constraints_str: str,
    *,
    system_mode: str = "architect",
    research_topic: str = "",
) -> tuple[str, str]:
    """
    Build (system, user) prompt pair using PromptBuilder when available.

    Falls back to concise inline prompts so the orchestrator can always
    make progress even without the prompts/ package.

    When *system_mode* is ``"research"``, uses research-oriented prompts
    that focus on gathering information about a topic rather than designing
    software architecture.
    """
    if system_mode == "research":
        return _build_research_architect_prompts(
            task_desc=task_desc,
            topic=subsystem,
            context_str=context_str,
            constraints_str=constraints_str,
            research_topic=research_topic,
        )

    pb_cls = _get_prompt_builder_cls()
    if pb_cls is not None:
        try:
            system, user = pb_cls.for_architect_micro(
                architecture_state=context_str[:3000],
                task_description=task_desc,
                constraints=constraints_str or "None specified.",
                context=grub_section.strip() or "(see architecture state above)",
            )
            return system, user
        except Exception as exc:
            logger.debug(
                "agents: PromptBuilder.for_architect_micro failed (%s) — "
                "using inline fallback prompt",
                exc,
            )

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
    *,
    system_mode: str = "architect",
) -> tuple[str, str]:
    """
    Build (system, user) prompt pair for the Critic using PromptBuilder.

    Falls back to inline prompts if PromptBuilder is unavailable.
    When *system_mode* is ``"research"``, critiques research quality instead
    of design quality.
    """
    if system_mode == "research":
        return _build_research_critic_prompts(
            task_desc=task_desc,
            research_content=design_content,
        )

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
    *,
    system_mode: str = "architect",
    **kwargs: Any,
) -> tuple[str, str]:
    """
    Build (system, user) prompt pair for the Synthesizer using PromptBuilder.

    For meso synthesis: uses ``PromptBuilder.for_synthesizer_meso()``.
    For macro synthesis: falls back to inline (no factory method yet).

    Falls back to inline prompts if PromptBuilder is unavailable.
    When *system_mode* is ``"research"``, synthesises research findings
    instead of design artifacts.
    """
    if system_mode == "research":
        return _build_research_synthesizer_prompts(level=level, **kwargs)

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
                "agents: PromptBuilder.for_synthesizer_meso failed (%s) — using inline fallback",
                exc,
            )

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
# Research-mode prompt builders
# ---------------------------------------------------------------------------
# These produce prompts for general-purpose research instead of software
# architecture design.  They are called by the main prompt builders above
# when system_mode == "research".


def _build_research_architect_prompts(
    task_desc: str,
    topic: str,
    context_str: str,
    constraints_str: str,
    research_topic: str = "",
) -> tuple[str, str]:
    """Build (system, user) prompts for the Researcher role (research mode)."""
    topic_line = f"Overall research topic: {research_topic}\n" if research_topic else ""
    system = (
        "You are an autonomous research analyst in Tinker, a research engine "
        "that continuously gathers, analyses, and synthesises information on "
        "a given topic.  Your job is to investigate the task below thoroughly: "
        "search for evidence, compare sources, identify patterns, and produce "
        "a structured research report.\n\n"
        f"{topic_line}"
        "IMPORTANT: Respond with a JSON object containing:\n"
        "- 'content': string — your full research findings narrative\n"
        "- 'knowledge_gaps': array of strings — areas that need more investigation\n"
        "- 'candidate_tasks': array of objects with 'title','description','type',"
        "'subsystem','confidence_gap','tags' — follow-up research tasks\n"
        "- 'decisions': array of strings — key conclusions or findings\n"
        "- 'open_questions': array of strings — unanswered questions\n"
    )
    user = (
        f"## Research Task\nTopic area: {topic}\nTask: {task_desc}\n\n"
        f"## Prior Research Context\n{context_str[:3000]}\n\n"
        f"## Constraints\n{constraints_str or 'None specified.'}\n\n"
        "Investigate this topic and produce your JSON research report now."
    )
    return system, user


def _build_research_critic_prompts(
    task_desc: str,
    research_content: str,
) -> tuple[str, str]:
    """Build (system, user) prompts for reviewing research output."""
    system = (
        "You are a research quality reviewer in Tinker, an autonomous research "
        "engine.  Evaluate the research report for accuracy, completeness, "
        "source quality, and logical reasoning.  Respond with a JSON object "
        "containing:\n"
        "- 'content': string — your review narrative\n"
        "- 'score': float between 0 and 1 (1 = excellent research)\n"
        "- 'flags': array of strings — specific issues (unsupported claims, "
        "missing perspectives, logical gaps)\n"
        "- 'strengths': array of strings — what the research does well\n"
    )
    user = (
        f"## Original Research Task\n{task_desc}\n\n"
        f"## Research Report\n{research_content}\n\n"
        "Critique this research report and return your JSON evaluation."
    )
    return system, user


def _build_research_synthesizer_prompts(
    level: str,
    **kwargs: Any,
) -> tuple[str, str]:
    """Build (system, user) prompts for synthesising research findings."""
    if level == "meso":
        topic = kwargs.get("subsystem", "unknown")
        artifacts = kwargs.get("artifacts", [])
        artifacts_text = "\n---\n".join(
            (a.get("content", str(a)) if isinstance(a, dict) else str(a))[:500]
            for a in artifacts[:10]
        )
        system = (
            "You are a research synthesiser.  Combine the provided research "
            "findings on a topic into a coherent summary document that "
            "identifies key themes, agreements, contradictions, and gaps."
        )
        user = (
            f"## Topic: {topic}\n\n"
            f"## Research Findings to Synthesise ({len(artifacts)} items)\n"
            f"{artifacts_text}\n\n"
            "Produce a synthesis document covering key findings, common themes, "
            "contradictions between sources, confidence levels, and recommended "
            "follow-up research."
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
            "You are a chief research analyst.  Produce a high-level research "
            "overview from the topic-level summaries provided."
        )
        user = (
            f"## Research Overview v{version} (after {micro_count} research cycles)\n\n"
            f"## Topic Summaries ({len(documents)} total)\n{docs_text}\n\n"
            "Produce a macro-level research overview covering cross-topic patterns, "
            "key conclusions, confidence levels, and priority areas for deeper research."
        )

    return system, user
