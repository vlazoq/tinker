"""
agents.py — Architect, Critic, and Synthesizer agent wrappers.

Each agent class wraps a ModelRouter call and normalises the response into
the flat dict that the Orchestrator's micro/meso/macro loops expect.

Interface (matching the stubs in p7_orchestrator/stubs.py):
    architect  .call(task: dict, context: dict) -> dict
    critic     .call(task: dict, architect_result: dict) -> dict
    synthesizer.call(level: str, **kwargs) -> dict
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger("tinker.agents")


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------

def _json_block(obj: Any) -> str:
    try:
        return json.dumps(obj, indent=2, ensure_ascii=False)
    except Exception:
        return str(obj)


def _extract_knowledge_gaps(text: str) -> list[str]:
    """Best-effort extraction of knowledge gaps from free-text architect output."""
    gaps = []
    for line in text.splitlines():
        line = line.strip("- •*").strip()
        if any(kw in line.lower() for kw in ("gap", "unknown", "unclear", "need to research", "investigate")):
            if 10 < len(line) < 300:
                gaps.append(line)
    return gaps[:5]


def _extract_candidate_tasks(text: str) -> list[dict]:
    """Try to parse candidate_tasks from a JSON block in the architect's response."""
    try:
        # Look for a JSON object in the text
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            return parsed.get("candidate_tasks", [])
    except Exception:
        pass
    return []


def _extract_score(text: str) -> float:
    """Best-effort score extraction from critic free-text output."""
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
                # Normalise 0-10 scale to 0-1
                return val / 10.0 if val > 1.0 else val
            except ValueError:
                pass
    return 0.7  # neutral default


# ---------------------------------------------------------------------------
# ArchitectAgent
# ---------------------------------------------------------------------------

class ArchitectAgent:
    """
    Calls the 7B model on the server machine (via ModelRouter) to produce
    a design proposal for the current task.
    """

    def __init__(self, router) -> None:
        self._router = router

    async def call(self, task: dict, context: dict) -> dict:
        from p1_model_client_n_ollama.types import AgentRole, Message
        from p1_model_client_n_ollama.router import ModelRouter  # noqa: F401 (type hint)

        task_desc   = task.get("description", task.get("title", "architecture task"))
        subsystem   = task.get("subsystem", "unknown")
        context_str = context.get("prompt", _json_block(context))[:4000]

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
        user_prompt = (
            f"## Task\nSubsystem: {subsystem}\nDescription: {task_desc}\n\n"
            f"## Context\n{context_str}\n\n"
            "Produce your JSON design proposal now."
        )

        from p1_model_client_n_ollama.types import ModelRequest
        req = ModelRequest(
            agent_role=AgentRole.ARCHITECT,
            messages=[
                Message("system", system_prompt),
                Message("user",   user_prompt),
            ],
            expect_json=True,
            temperature=0.7,
        )
        resp = await self._router.complete(req)

        # Parse structured output or fall back to raw text
        if resp.structured and isinstance(resp.structured, dict):
            content   = resp.structured.get("content", resp.raw_text)
            gaps      = resp.structured.get("knowledge_gaps", [])
            decisions = resp.structured.get("decisions", [])
            questions = resp.structured.get("open_questions", [])
            candidates = resp.structured.get("candidate_tasks", [])
        else:
            content   = resp.raw_text
            gaps      = _extract_knowledge_gaps(content)
            decisions = []
            questions = []
            candidates = _extract_candidate_tasks(content)

        return {
            "content":        content,
            "tokens_used":    resp.total_tokens,
            "knowledge_gaps": gaps,
            "decisions":      decisions,
            "open_questions": questions,
            "candidate_tasks": candidates,
            # TaskGenerator reads "candidate_tasks" from this dict
        }


# ---------------------------------------------------------------------------
# CriticAgent
# ---------------------------------------------------------------------------

class CriticAgent:
    """
    Calls the smaller critic model (phi3:mini on secondary machine) to
    evaluate the Architect's design proposal.
    """

    def __init__(self, router) -> None:
        self._router = router

    async def call(self, task: dict, architect_result: dict) -> dict:
        from p1_model_client_n_ollama.types import AgentRole, Message, ModelRequest

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
            temperature=0.3,
        )
        resp = await self._router.complete(req)

        if resp.structured and isinstance(resp.structured, dict):
            content = resp.structured.get("content", resp.raw_text)
            score   = float(resp.structured.get("score", 0.7))
            flags   = resp.structured.get("flags", [])
        else:
            content = resp.raw_text
            score   = _extract_score(content)
            flags   = []

        # Clamp score to [0, 1]
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
    Calls the 7B model to produce meso (subsystem) or macro (global) synthesis.
    """

    def __init__(self, router) -> None:
        self._router = router

    async def call(self, level: str, **kwargs) -> dict:
        from p1_model_client_n_ollama.types import AgentRole, Message, ModelRequest

        if level == "meso":
            subsystem = kwargs.get("subsystem", "unknown")
            artifacts = kwargs.get("artifacts", [])
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
        else:  # macro
            documents  = kwargs.get("documents", [])
            version    = kwargs.get("snapshot_version", 0)
            micro_count = kwargs.get("total_micro_loops", 0)
            docs_text   = "\n---\n".join(
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

        req = ModelRequest(
            agent_role=AgentRole.SYNTHESIZER,
            messages=[
                Message("system", system_prompt),
                Message("user",   user_prompt),
            ],
            expect_json=False,
            temperature=0.5,
        )
        resp = await self._router.complete(req)

        return {
            "content":     resp.raw_text,
            "tokens_used": resp.total_tokens,
            "level":       level,
        }
