"""
agents/grub/minions/base.py
====================
BaseMinion — the abstract base class every Minion inherits from.

What a Minion is
----------------
A Minion is a specialized AI agent focused on one type of coding task.
It has:
  - A system prompt that defines its role and expertise
  - A set of Skills (text injected into its prompt)
  - A set of Tools it can use (file read/write, shell commands)
  - A run() method that takes a GrubTask and returns a MinionResult

The key design choice: every Minion follows the same interface.
Grub doesn't need to know if it's talking to CoderMinion or TesterMinion —
it just calls minion.run(task) and gets back a MinionResult.

How to create a new Minion
--------------------------
1. Create a new file in grub/minions/
2. Subclass BaseMinion
3. Override: MINION_NAME, BASE_SYSTEM_PROMPT, and run()
4. Register it in registry.py

The most important method to override is run().  It should:
  - Read the task and any relevant files
  - Call self._llm() one or more times
  - Write output files using the tools
  - Return a MinionResult

STATUS: FULLY IMPLEMENTED
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Optional, TYPE_CHECKING

import httpx

from exceptions import MinionTimeoutError
from ..contracts.task import GrubTask
from ..contracts.result import MinionResult, ResultStatus
from ..context_summarizer import MinionContextSummarizer

if TYPE_CHECKING:
    from ..config import GrubConfig

logger = logging.getLogger(__name__)


class BaseMinion(ABC):
    """
    Abstract base class for all Grub Minions.

    Subclasses MUST override:
      - MINION_NAME       : short identifier string
      - BASE_SYSTEM_PROMPT: the core system prompt for this minion
      - run()             : the main execution method

    Subclasses MAY override:
      - _build_system_prompt() : if you need custom skill injection logic
      - _parse_response()      : if your minion's LLM output has special structure
    """

    # Override these in every subclass
    MINION_NAME: str = "base"
    BASE_SYSTEM_PROMPT: str = "You are a helpful AI assistant."

    def __init__(
        self,
        name: str,
        config: "GrubConfig",
        skills: list[str] | None = None,
    ) -> None:
        """
        Parameters
        ----------
        name   : The registry name for this minion (e.g. "coder").
        config : GrubConfig instance with model names, URLs, thresholds, etc.
        skills : List of pre-loaded skill texts (already read from disk).
                 The registry handles loading them — you don't need to do this.
        """
        self.name = name
        self.config = config
        self.skills = skills or []
        self.logger = logging.getLogger(f"grub.minion.{name}")

        # Context summarizer — compresses large inputs instead of truncating.
        # Uses the reviewer's model (fast 7B) or the minion's own model if
        # context_summarizer_model is not set.
        _summarizer_model = (
            config.context_summarizer_model
            or config.models.get("reviewer", config.models.get(name, "qwen3:7b"))
        )
        _summarizer_url = config.ollama_urls.get(name, "http://localhost:11434")
        self._summarizer = MinionContextSummarizer(
            model=_summarizer_model,
            ollama_url=_summarizer_url,
            max_chars=config.context_max_chars,
            target_chars=config.context_target_chars,
            enabled=config.context_summarization_enabled,
        )

    # ── Public interface (what Grub calls) ────────────────────────────────────

    @abstractmethod
    async def run(self, task: GrubTask) -> MinionResult:
        """
        Execute the task and return a result.

        This is the ONLY method Grub calls.  Everything else is internal.

        Parameters
        ----------
        task : The GrubTask describing what to do.

        Returns
        -------
        MinionResult with status, score, files_written, etc.
        """
        ...

    # ── LLM communication ─────────────────────────────────────────────────────

    def _build_system_prompt(self, extra_context: str = "") -> str:
        """
        Build the full system prompt by combining:
          1. BASE_SYSTEM_PROMPT (the minion's core instructions)
          2. All loaded skills (injected as extra expertise)
          3. Any extra_context for this specific task

        The structure is:
          [Core role instructions]
          ---
          [Skill 1 text]
          ---
          [Skill 2 text]
          ---
          [Extra context]
        """
        parts = [self.BASE_SYSTEM_PROMPT.strip()]

        for skill_text in self.skills:
            if skill_text.strip():
                parts.append(skill_text.strip())

        if extra_context.strip():
            parts.append(extra_context.strip())

        return "\n\n---\n\n".join(parts)

    async def _llm(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        timeout: float = 120.0,
    ) -> str:
        """
        Send a prompt to the Ollama model assigned to this Minion.

        This is a low-level helper.  Most Minions call _llm() in their
        run() method to get raw text back, then parse it themselves.

        Parameters
        ----------
        prompt        : The user message (the task/question for the model).
        system_prompt : Override the system prompt.  If None, uses
                        self._build_system_prompt().
        temperature   : Sampling temperature. Lower = more deterministic.
                        0.1–0.3 for coding tasks. 0.7+ for creative tasks.
        max_tokens    : Maximum tokens in the response.
        timeout       : Per-call timeout in seconds.  Defaults to 120s.
                        When a per-minion timeout is configured in
                        ``GrubConfig.timeouts``, callers should pass that
                        value here.  If the LLM call exceeds this limit,
                        ``MinionTimeoutError`` is raised so the orchestrator
                        can decide whether to retry or route to another minion.

        Returns
        -------
        str : The model's text response, or an error message starting with
              "ERROR:" if the call failed.

        Raises
        ------
        MinionTimeoutError
            If the LLM call exceeds *timeout* seconds.  This is a structured
            exception (inherits ``GrubError``) with ``retryable=True`` so the
            orchestrator can distinguish timeouts from permanent failures.
        """
        if system_prompt is None:
            system_prompt = self._build_system_prompt()

        model = self.config.models.get(self.name, "qwen3:7b")
        ollama_url = self.config.ollama_urls.get(self.name, "http://localhost:11434")
        # Use the httpx-level timeout from config as a safety net; the
        # asyncio.wait_for wrapper below provides the authoritative deadline.
        http_timeout = self.config.request_timeout

        payload = {
            "model": model,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
        }

        self.logger.debug("_llm: model=%s, prompt_len=%d", model, len(prompt))
        t0 = time.monotonic()

        try:
            # Wrap the entire HTTP round-trip in asyncio.wait_for so that
            # even a hanging connection is terminated after *timeout* seconds.
            async def _do_request() -> str:
                async with httpx.AsyncClient(timeout=http_timeout) as client:
                    r = await client.post(
                        f"{ollama_url}/api/chat",
                        json=payload,
                    )
                    r.raise_for_status()
                    data = r.json()
                    return data.get("message", {}).get("content", "")

            content = await asyncio.wait_for(_do_request(), timeout=timeout)
            elapsed = time.monotonic() - t0
            self.logger.debug(
                "_llm OK: %.1fs, %d chars returned", elapsed, len(content)
            )
            return content

        except asyncio.TimeoutError:
            # asyncio.wait_for exceeded the deadline — raise a structured
            # MinionTimeoutError so the caller can decide to retry or fail.
            elapsed = time.monotonic() - t0
            raise MinionTimeoutError(
                f"Minion '{self.name}' LLM call timed out after {timeout:.0f}s",
                context={
                    "minion": self.name,
                    "timeout_seconds": timeout,
                    "elapsed_seconds": round(elapsed, 2),
                    "model": model,
                    "ollama_url": ollama_url,
                },
            )
        except httpx.ConnectError:
            msg = f"ERROR: Cannot connect to Ollama at {ollama_url}. Is it running?"
            self.logger.error(msg)
            return msg
        except httpx.TimeoutException:
            msg = f"ERROR: Ollama timed out after {http_timeout}s."
            self.logger.error(msg)
            return msg
        except Exception as exc:
            msg = f"ERROR: LLM call failed: {exc}"
            self.logger.error(msg)
            return msg

    async def compress_context(self, text: str, label: str = "context") -> str:
        """
        Compress ``text`` if it exceeds the configured context size limit.

        Delegates to MinionContextSummarizer.  If the text is already short
        enough, returns it unchanged with no LLM call.

        Parameters
        ----------
        text  : The text to (possibly) compress.
        label : Human-readable name for the context type (for logging and the
                LLM prompt), e.g. "design document", "test output".

        Returns
        -------
        str : Original or compressed text.
        """
        return await self._summarizer.compress(text, label)

    # ── Metrics ────────────────────────────────────────────────────────────────

    def _log_metrics(self, task_id: str, status: str, score: float, duration_s: float) -> None:
        """
        Emit a structured log line with minion execution metrics.

        These structured fields are machine-parseable by log aggregators
        (Loki, ELK, CloudWatch) for building dashboards and alerts.

        Parameters
        ----------
        task_id    : Unique identifier for the task being executed.
        status     : Outcome of the run (e.g. "success", "failed", "partial").
        score      : Quality score assigned by the minion (0.0–1.0).
        duration_s : Wall-clock seconds the run() method took.
        """
        logger.info(
            "minion_metrics | minion=%s task=%s status=%s score=%.3f duration=%.2fs",
            self.name, task_id, status, score, duration_s,
            extra={
                "event": "minion_metrics",
                "minion": self.name,
                "task_id": task_id,
                "status": status,
                "score": score,
                "duration_seconds": duration_s,
            },
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _extract_code_blocks(self, text: str, language: str = "") -> list[str]:
        """
        Extract fenced code blocks from LLM output.

        LLMs typically wrap code in ```python ... ``` or ``` ... ```.
        This helper extracts the content inside those fences.

        Parameters
        ----------
        text     : Raw LLM output text.
        language : Optional language filter (e.g. "python").
                   Empty string matches any language fence.

        Returns
        -------
        List of code strings (one per code block found).
        """
        import re

        # Match ```language\n...\n``` or just ```\n...\n```
        if language:
            pattern = rf"```{re.escape(language)}\n(.*?)```"
        else:
            pattern = r"```(?:\w+)?\n(.*?)```"
        matches = re.findall(pattern, text, re.DOTALL)
        # Fallback: if no fenced blocks found, check for ``` without newline
        if not matches:
            matches = re.findall(r"```(?:\w+)?(.*?)```", text, re.DOTALL)
        return [m.strip() for m in matches if m.strip()]

    def _score_from_text(self, text: str) -> float:
        """
        Extract a numeric score from LLM reviewer output.

        Looks for patterns like "Score: 0.82", "score: 8/10", "Rating: 7".
        Falls back to 0.5 if no score is found.

        Parameters
        ----------
        text : Raw text from the reviewer LLM.

        Returns
        -------
        float between 0.0 and 1.0
        """
        import re

        # Pattern: "score: 0.82" or "Score: 0.82"
        m = re.search(r"score[:\s]+([0-9]+\.?[0-9]*)\s*(/\s*10)?", text, re.IGNORECASE)
        if m:
            val = float(m.group(1))
            if m.group(2):  # "8/10" format
                val = val / 10.0
            return max(0.0, min(1.0, val))

        # Pattern: "8/10" standalone
        m = re.search(r"([0-9]+)\s*/\s*10", text)
        if m:
            return max(0.0, min(1.0, int(m.group(1)) / 10.0))

        # No score found — return neutral
        self.logger.debug("_score_from_text: no score pattern found, defaulting to 0.5")
        return 0.5

    def _make_failed_result(self, task: GrubTask, reason: str) -> MinionResult:
        """
        Convenience: create a FAILED MinionResult with an error message.

        Also logs failure metrics so observability dashboards track early exits.

        Parameters
        ----------
        task   : The task that failed.
        reason : Human-readable explanation of why it failed.
        """
        self._log_metrics(task.id, ResultStatus.FAILED.value, 0.0, 0.0)
        return MinionResult(
            task_id=task.id,
            minion_name=self.name,
            status=ResultStatus.FAILED,
            score=0.0,
            notes=reason,
            summary=f"Failed: {reason[:100]}",
        )
