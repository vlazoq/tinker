"""
agents/synthesizer.py
=====================

SynthesizerAgent — the distillation agent in Tinker's meso and macro loops.

What this agent does
--------------------
The Synthesizer reads many past results and distills them into a coherent
summary document.  It operates at two levels:

  1. **Meso synthesis** (``level="meso"``):
     After several micro loops have worked on the same subsystem (e.g. "caching"),
     the Synthesizer reads all the artifacts produced for that subsystem and
     writes a coherent design document.  Think of this as the weekly summary
     of all the whiteboard sessions for one area.

  2. **Macro synthesis** (``level="macro"``):
     Every few hours, the Synthesizer reads all the meso-level documents and
     produces a global architectural snapshot — the quarterly architecture
     review document.

The Synthesizer uses the large model (qwen3:7b) because synthesis requires
deep understanding and coherent writing across large amounts of text.

How it fits into the system
----------------------------
``meso_loop.py`` calls::

    result = await synthesizer_agent.call(level="meso", subsystem=..., artifacts=[...])

``macro_loop.py`` calls::

    result = await synthesizer_agent.call(
        level="macro", documents=[...], snapshot_version=n, total_micro_loops=n
    )

Protocol compliance
-------------------
SynthesizerAgent satisfies the ``SynthesizerStrategy`` protocol defined in
``agents/protocols.py``.
"""

from __future__ import annotations

import logging
import uuid

from agents._shared import (
    _current_trace_id,
    _get_rate_limiter_registry,
    _get_retry_async,
    _build_synthesizer_prompts,
)

logger = logging.getLogger("tinker.agents")


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

        system_prompt, user_prompt = _build_synthesizer_prompts(level, **kwargs)

        req = ModelRequest(
            agent_role=AgentRole.SYNTHESIZER,
            messages=[
                Message("system", system_prompt),
                Message("user", user_prompt),
            ],
            expect_json=False,  # Synthesis output is prose, not JSON
            temperature=0.5,
        )

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
