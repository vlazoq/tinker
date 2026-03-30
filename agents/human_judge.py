"""
agents/human_judge.py
======================

Human Judge: a human-in-the-loop quality control agent.

The Human Judge lets a human operator step in as a Critic — scoring
Architect proposals, providing written feedback, and (optionally)
injecting steering directives that redirect the next Architect iteration.

Three judge modes
-----------------
``llm``       — Default.  The LLM Critic runs alone (current behaviour).
``human``     — The human replaces the LLM Critic entirely.  The orchestrator
                pauses at each refinement step and waits for human input.
``hybrid``    — The LLM Critic runs first, then the human reviews *if* the
                score is below a threshold or every N loops.  The final score
                is the human's (human overrides LLM).
``on_demand`` — LLM-only by default, but the human can request review of
                the current/next loop via the API.  When triggered, the
                loop pauses for human scoring.

Human review response
---------------------
The human provides three fields:

  score     : float 0.0–1.0 (same semantics as LLM critic score)
  feedback  : str           (structured review text)
  directive : str, optional (steering instruction for next Architect call)
  sticky    : bool, optional (if True, directive persists across loops)

The ``directive`` is the most powerful field — it's a natural-language
instruction injected directly into the Architect's context, allowing the
human to say things like "stop exploring Redis, use an in-process LRU cache."

Integration
-----------
The HumanJudge follows the same pattern as ConfirmationGate: it creates a
pending review in OrchestratorState and waits on an asyncio.Event for the
human's response (via web API or CLI).

Usage::

    judge = HumanJudge(config, state)

    # In micro_loop.py, at the refinement step:
    result = await judge.request_review(
        task=task,
        architect_result=architect_result,
        llm_critic_result=critic_result,  # None in human-only mode
    )
    # result is a dict: {score, content, tokens_used, human_directive, sticky}
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
import uuid
from typing import TYPE_CHECKING, Any

logger = logging.getLogger("tinker.human_judge")

if TYPE_CHECKING:
    from runtime.orchestrator.config import OrchestratorConfig
    from runtime.orchestrator.state import OrchestratorState


class HumanJudge:
    """Human-in-the-loop quality control agent.

    Pauses the orchestrator loop and waits for a human to score, review,
    and optionally steer the Architect's output.

    Parameters
    ----------
    config : OrchestratorConfig — reads judge_mode, human_judge_timeout, etc.
    state  : OrchestratorState  — pending reviews are written here.
    event_bus : optional EventBus for emitting review events.
    """

    def __init__(
        self,
        config: OrchestratorConfig,
        state: OrchestratorState,
        event_bus: Any = None,
    ) -> None:
        self._config = config
        self._state = state
        self._event_bus = event_bus
        # Pending asyncio Events — keyed by review_id.
        self._events: dict[str, asyncio.Event] = {}
        # Active sticky directives (persist across loops until cleared).
        self.sticky_directives: list[dict[str, str]] = []

    # ── Public API ────────────────────────────────────────────────────────────

    def should_request_review(
        self,
        llm_score: float | None = None,
        iteration: int = 0,
    ) -> bool:
        """Decide whether to request a human review for this iteration.

        Parameters
        ----------
        llm_score : The LLM critic's score (None if not yet called).
        iteration : The current micro loop iteration number.

        Returns
        -------
        bool : True if a human review should be requested.
        """
        mode = self._config.judge_mode

        if mode == "llm":
            return False

        if mode == "human":
            return True

        if mode == "hybrid":
            # Review if score below threshold
            if llm_score is not None and llm_score < self._config.hybrid_score_threshold:
                return True
            # Review every N loops
            return bool(
                self._config.hybrid_review_interval > 0
                and iteration > 0
                and iteration % self._config.hybrid_review_interval == 0
            )

        if mode == "on_demand":
            # Check if human manually requested a review
            return bool(getattr(self._state, "human_review_requested", False))

        return False

    async def request_review(
        self,
        task: dict,
        architect_result: dict,
        llm_critic_result: dict | None = None,
        iteration: int = 0,
    ) -> dict:
        """Pause and wait for a human to review the Architect's proposal.

        Creates a pending review visible via the web API, then waits for
        the human to submit their score + feedback + optional directive.

        Parameters
        ----------
        task : The current task dict.
        architect_result : The Architect's output to review.
        llm_critic_result : The LLM critic's result (if hybrid mode).
        iteration : Current micro loop iteration.

        Returns
        -------
        dict with keys: score, content, tokens_used, human_directive, sticky
        """
        review_id = str(uuid.uuid4())[:8]

        logger.info(
            "HumanJudge: requesting review (review_id=%s, task=%s, mode=%s)",
            review_id,
            task.get("id", "?"),
            self._config.judge_mode,
        )

        # Build the pending review object
        pending = {
            "id": review_id,
            "status": "pending",
            "requested_at": time.time(),
            "task": {
                "id": task.get("id", ""),
                "title": task.get("title", ""),
                "description": task.get("description", "")[:500],
                "subsystem": task.get("subsystem", ""),
            },
            "architect_proposal": {
                "content": architect_result.get("content", "")[:3000],
                "decisions": architect_result.get("decisions", []),
                "knowledge_gaps": architect_result.get("knowledge_gaps", []),
            },
            "llm_critic": None,
            "iteration": iteration,
            # Human response fields — None until submitted
            "score": None,
            "feedback": None,
            "directive": None,
            "sticky": False,
        }

        if llm_critic_result:
            pending["llm_critic"] = {
                "score": llm_critic_result.get("score"),
                "content": llm_critic_result.get("content", "")[:1000],
            }

        # Write to state so the web API can serve it
        if not hasattr(self._state, "pending_reviews"):
            self._state.pending_reviews = {}
        self._state.pending_reviews[review_id] = pending

        # Create the asyncio Event
        event = asyncio.Event()
        self._events[review_id] = event

        # Emit event if bus available
        await self._emit(
            "HUMAN_REVIEW_REQUESTED",
            {
                "review_id": review_id,
                "task_id": task.get("id", ""),
                "llm_score": llm_critic_result.get("score") if llm_critic_result else None,
                "mode": self._config.judge_mode,
            },
        )

        try:
            # Determine mode: CLI or API
            if sys.stdin.isatty():
                result = await self._cli_review(review_id, pending, event)
            else:
                result = await self._api_wait(review_id, event)
        except TimeoutError:
            logger.warning(
                "HumanJudge: timeout waiting for review (review_id=%s) — "
                "falling back to LLM critic result",
                review_id,
            )
            await self._emit("HUMAN_REVIEW_TIMEOUT", {"review_id": review_id})

            # Fall back to LLM critic result if available
            if llm_critic_result:
                return {
                    "score": llm_critic_result.get("score", 0.5),
                    "content": llm_critic_result.get("content", ""),
                    "tokens_used": 0,
                    "human_directive": None,
                    "sticky": False,
                    "source": "llm_fallback",
                }
            return {
                "score": 0.5,
                "content": "Human review timed out, no LLM fallback available.",
                "tokens_used": 0,
                "human_directive": None,
                "sticky": False,
                "source": "timeout_default",
            }
        finally:
            self._events.pop(review_id, None)
            if hasattr(self._state, "pending_reviews"):
                self._state.pending_reviews.pop(review_id, None)
            # Clear on-demand flag after review
            if hasattr(self._state, "human_review_requested"):
                self._state.human_review_requested = False

        # Process directive
        directive = result.get("human_directive")
        is_sticky = result.get("sticky", False)
        if directive and is_sticky:
            self.sticky_directives.append(
                {
                    "directive": directive,
                    "added_at": time.time(),
                    "review_id": review_id,
                }
            )
            logger.info(
                "HumanJudge: sticky directive added (review_id=%s): %s",
                review_id,
                directive[:80],
            )

        await self._emit(
            "HUMAN_REVIEW_SUBMITTED",
            {
                "review_id": review_id,
                "score": result.get("score"),
                "has_directive": bool(directive),
                "sticky": is_sticky,
            },
        )

        return result

    def resolve(self, review_id: str, response: dict) -> bool:
        """Called by the web API to submit a human review.

        Parameters
        ----------
        review_id : The ID of the pending review.
        response : dict with keys: score, feedback, directive (optional), sticky (optional)

        Returns
        -------
        bool : True if review_id was found and resolved.
        """
        if not hasattr(self._state, "pending_reviews"):
            return False
        if review_id not in self._state.pending_reviews:
            logger.warning("HumanJudge.resolve: unknown review_id '%s'", review_id)
            return False

        pending = self._state.pending_reviews[review_id]
        pending["score"] = float(response.get("score", 0.5))
        pending["feedback"] = response.get("feedback", "")
        pending["directive"] = response.get("directive")
        pending["sticky"] = bool(response.get("sticky", False))
        pending["status"] = "submitted"

        # Fire the event to wake up the waiting coroutine
        event = self._events.get(review_id)
        if event:
            event.set()
        return True

    def list_pending(self) -> list[dict]:
        """Return all pending human reviews."""
        if not hasattr(self._state, "pending_reviews"):
            return []
        return [r for r in self._state.pending_reviews.values() if r.get("status") == "pending"]

    def get_active_directives(self) -> list[str]:
        """Return all active sticky directives for injection into context."""
        return [d["directive"] for d in self.sticky_directives]

    def clear_sticky_directive(self, index: int) -> bool:
        """Remove a sticky directive by index."""
        if 0 <= index < len(self.sticky_directives):
            removed = self.sticky_directives.pop(index)
            logger.info("HumanJudge: cleared sticky directive: %s", removed["directive"][:50])
            return True
        return False

    def clear_all_sticky_directives(self) -> int:
        """Remove all sticky directives. Returns count removed."""
        count = len(self.sticky_directives)
        self.sticky_directives.clear()
        return count

    def get_context_block(self) -> str:
        """Build a context injection block from active directives.

        Returns a formatted string for inclusion in the Architect's context,
        or empty string if no directives are active.
        """
        directives = self.get_active_directives()
        if not directives:
            return ""
        lines = ["[HUMAN DIRECTIVES — Active steering instructions from the operator]"]
        for i, d in enumerate(directives, 1):
            lines.append(f"  {i}. {d}")
        lines.append("[END HUMAN DIRECTIVES]")
        return "\n".join(lines)

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _cli_review(
        self,
        review_id: str,
        pending: dict,
        event: asyncio.Event,
    ) -> dict:
        """Interactive CLI review for terminal sessions."""
        # Display the proposal
        lines = [
            "",
            "=" * 70,
            f"  HUMAN REVIEW REQUESTED  [id: {review_id}]",
            "=" * 70,
            f"  Task     : {pending['task']['title']}",
            f"  Subsystem: {pending['task']['subsystem']}",
            "-" * 70,
            "  ARCHITECT PROPOSAL:",
            "",
            pending["architect_proposal"]["content"][:2000],
            "",
        ]

        if pending.get("llm_critic"):
            lines.extend(
                [
                    "-" * 70,
                    f"  LLM Critic Score: {pending['llm_critic']['score']}",
                    f"  LLM Critic Says : {pending['llm_critic']['content'][:500]}",
                    "",
                ]
            )

        lines.extend(
            [
                "-" * 70,
                "  Your review:",
                "  Score (0.0-1.0): ",
            ]
        )

        sys.stdout.write("\n".join(lines))
        sys.stdout.flush()

        timeout = self._config.human_judge_timeout

        def _read_review() -> dict:
            try:
                score_str = sys.stdin.readline().strip()
                score = max(0.0, min(1.0, float(score_str))) if score_str else 0.5

                sys.stdout.write("  Feedback (press Enter to skip): ")
                sys.stdout.flush()
                feedback = sys.stdin.readline().strip()

                sys.stdout.write("  Directive / steering instruction (press Enter to skip): ")
                sys.stdout.flush()
                directive = sys.stdin.readline().strip()

                is_sticky = False
                if directive:
                    sys.stdout.write("  Make directive sticky (persist across loops)? [y/N]: ")
                    sys.stdout.flush()
                    sticky_ans = sys.stdin.readline().strip().lower()
                    is_sticky = sticky_ans in ("y", "yes")

                return {
                    "score": score,
                    "content": feedback or f"Human score: {score}",
                    "tokens_used": 0,
                    "human_directive": directive or None,
                    "sticky": is_sticky,
                    "source": "human_cli",
                }
            except Exception:
                return {
                    "score": 0.5,
                    "content": "CLI review input error",
                    "tokens_used": 0,
                    "human_directive": None,
                    "sticky": False,
                    "source": "human_cli_error",
                }

        if timeout > 0:
            result = await asyncio.wait_for(
                asyncio.to_thread(_read_review),
                timeout=timeout,
            )
        else:
            result = await asyncio.to_thread(_read_review)

        sys.stdout.write(f"\n  Review submitted (score={result['score']}).\n\n")
        sys.stdout.flush()
        return result

    async def _api_wait(
        self,
        review_id: str,
        event: asyncio.Event,
    ) -> dict:
        """Wait for the web API to call resolve()."""
        timeout = self._config.human_judge_timeout or None  # None = wait forever

        logger.info(
            "HumanJudge: waiting for API review (review_id=%s, timeout=%s)",
            review_id,
            f"{timeout:.0f}s" if timeout else "forever",
        )

        if timeout and timeout > 0:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        else:
            await event.wait()

        # Read the response from state
        pending = {}
        if hasattr(self._state, "pending_reviews"):
            pending = self._state.pending_reviews.get(review_id, {})

        return {
            "score": pending.get("score", 0.5),
            "content": pending.get("feedback", ""),
            "tokens_used": 0,
            "human_directive": pending.get("directive"),
            "sticky": pending.get("sticky", False),
            "source": "human_api",
        }

    async def _emit(self, event_name: str, payload: dict) -> None:
        """Emit an event on the bus if available."""
        if self._event_bus is None:
            return
        try:
            from core.events import Event, EventType

            # Map string to EventType (use CUSTOM for human-specific events)
            event_type = getattr(EventType, event_name, EventType.CUSTOM)
            await self._event_bus.publish(
                Event(type=event_type, payload=payload, source="human_judge")
            )
        except Exception as exc:
            logger.debug("HumanJudge event emit failed (non-fatal): %s", exc)
