"""
agents/critic.py
================

CriticAgent — the adversarial reviewer in Tinker's micro loop.

What this agent does
--------------------
Given the Architect's design proposal and the original task, the Critic:

  - Scores the proposal on a 0.0–1.0 scale (1.0 = excellent)
  - Writes a critique narrative (``content``)
  - Lists specific issues to address (``flags``)
  - Lists strengths (implicitly, via positive framing in ``content``)

The score drives the refinement loop: if it's below ``cfg.min_critic_score``
and iterations remain, the orchestrator re-runs the Architect with the
Critic's feedback injected into its context.

The Critic uses the smaller, faster model (phi3:mini / Qwen3-1.7B) on the
secondary machine.  Speed matters here because critiques run on every single
micro loop — the Critic should be quick and consistent, not creative.

How it fits into the system
----------------------------
The Orchestrator (runtime/orchestrator/micro_loop.py) calls::

    critic_result = await critic_agent.call(task=task, architect_result=result)

after the Architect call completes.  A low score may trigger additional
Architect iterations before the artifact is committed to memory.

Protocol compliance
-------------------
CriticAgent satisfies the ``CriticStrategy`` protocol defined in
``agents/protocols.py``.
"""

from __future__ import annotations

import logging
import uuid

from agents._shared import (
    _CRITIC_REQUIRED_KEYS,
    _build_critic_prompts,
    _current_trace_id,
    _extract_score,
    _get_rate_limiter_registry,
    _get_retry_async,
    _read_system_mode,
    _validate_agent_response,
)

logger = logging.getLogger("tinker.agents")


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
        trace_id = architect_result.get("trace_id") or task.get("trace_id") or str(uuid.uuid4())
        _current_trace_id.set(trace_id)
        task_id = task.get("id", "?")

        logger.info("CriticAgent.call start [task=%s trace_id=%s]", task_id, trace_id)

        design_content = architect_result.get("content", "")[:3000]
        task_desc = task.get("description", task.get("title", ""))

        system_mode, _research_topic = _read_system_mode()
        system_prompt, user_prompt = _build_critic_prompts(
            task_desc=task_desc,
            design_content=design_content,
            system_mode=system_mode,
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

        if resp.structured and isinstance(resp.structured, dict):
            try:
                _validate_agent_response(resp.structured, _CRITIC_REQUIRED_KEYS, "Critic")
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
