"""
agents/architect.py
===================

ArchitectAgent — the main reasoning agent in Tinker's micro loop.

What this agent does
--------------------
Given a task (e.g. "Design the caching layer") and an assembled context
(prior artifacts, architecture state, research notes), the Architect produces
a detailed design proposal with:

  - A design narrative (``content``)
  - Topics it needs more information on (``knowledge_gaps``)
  - Key design decisions made (``decisions``)
  - Unresolved questions (``open_questions``)
  - Follow-up tasks to explore next (``candidate_tasks``)

The Architect uses the large primary model (qwen3:7b on the homelab server)
because design reasoning benefits from a capable, creative model.

How it fits into the system
----------------------------
The Orchestrator (runtime/orchestrator/micro_loop.py) calls::

    result = await architect_agent.call(task=task, context=context)

after assembling context via ContextAssembler.  The result feeds into the
CriticAgent and is eventually stored as an artifact in MemoryManager.

Protocol compliance
-------------------
ArchitectAgent satisfies the ``ArchitectStrategy`` protocol defined in
``agents/protocols.py``.  To substitute a different implementation (e.g. a
domain-specialised architect or a test double), register it via
``agents.agent_factory.register_agent(AgentRole.ARCHITECT, MyClass)``.
"""

from __future__ import annotations

import logging
import uuid

from agents._shared import (
    _current_trace_id,
    _get_rate_limiter_registry,
    _get_retry_async,
    _json_block,
    _extract_knowledge_gaps,
    _extract_candidate_tasks,
    _parse_architect_structured,
    _build_architect_prompts,
)

logger = logging.getLogger("tinker.agents")


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

        trace_id = task.get("trace_id") or str(uuid.uuid4())
        _current_trace_id.set(trace_id)
        task_id = task.get("id", "?")

        logger.info(
            "ArchitectAgent.call start [task=%s trace_id=%s]", task_id, trace_id
        )

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

        # Use the assembler's token budget to derive a character limit.
        # chars_per_token ≈ 3.8 (LLaMA/GPT average).
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
        _chars_limit = int(_tokens_budget * 3.8)
        raw_prompt = context.get("prompt", _json_block(context))
        context_str = (
            raw_prompt[:_chars_limit] if len(raw_prompt) > _chars_limit else raw_prompt
        )

        # ── Grub implementation section (review tasks only) ─────────────────
        # When Tinker is processing a 'review' task, the micro loop adds a
        # 'grub_implementation' key to the context dict so the Architect can
        # see what Grub actually built before making a design decision.
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

        system_prompt, user_prompt = _build_architect_prompts(
            task_desc=task_desc,
            subsystem=subsystem,
            context_str=context_str,
            grub_section=grub_section,
            constraints_str=constraints_str,
        )

        req = ModelRequest(
            agent_role=AgentRole.ARCHITECT,
            messages=[
                Message("system", system_prompt),
                Message("user", user_prompt),
            ],
            expect_json=True,
            temperature=0.7,
        )

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

        if resp.structured and isinstance(resp.structured, dict):
            try:
                content, gaps, decisions, questions, candidates = (
                    _parse_architect_structured(resp.structured)
                )
            except Exception as parse_exc:
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

        return {
            "content": content,
            "tokens_used": resp.total_tokens,
            "knowledge_gaps": gaps,
            "decisions": decisions,
            "open_questions": questions,
            "candidate_tasks": candidates,
            "trace_id": trace_id,
        }
