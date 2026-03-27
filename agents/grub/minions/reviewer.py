"""
agents/grub/minions/reviewer.py
========================
ReviewerMinion — reviews code against the design document and coding standards.

What it does
------------
1. Reads the code files produced by CoderMinion
2. Reads the original design artifact
3. Asks the LLM to evaluate:
   - Does the code match the design?
   - Is the code clean and well-structured?
   - Are there security issues?
   - What needs improvement?
4. Extracts a numeric score (0.0–1.0)
5. Returns a MinionResult with the score and detailed notes

The Reviewer does NOT write any code.  It only reads and evaluates.

STATUS: FULLY IMPLEMENTED
"""

from __future__ import annotations

import time

from .base import BaseMinion
from ..contracts.task import GrubTask
from ..contracts.result import MinionResult, ResultStatus
from ..tools.file_ops import read_file
from ..tools.code_analysis import summarise_file


class ReviewerMinion(BaseMinion):
    """
    Reviews code quality and alignment with the design document.

    Assigned model: config.models["reviewer"]  (default: qwen3:7b)
    """

    MINION_NAME = "reviewer"

    BASE_SYSTEM_PROMPT = """You are a senior software engineer performing a
thorough code review.

Your job: evaluate code against its design document and coding standards.

Always output your review in this EXACT format:

## Summary
[2-3 sentence summary of what was reviewed]

## Design Alignment
[Does the code implement what the design document specified?]
- Matches: [list what matches]
- Missing: [list what is missing or incomplete]

## Code Quality
[Evaluate: readability, structure, naming, comments, error handling]
- Strengths: [list strengths]
- Issues: [list issues]

## Security
[Any security concerns: input validation, injection, auth, secrets in code]

## Recommendations
[Specific, actionable improvements ordered by priority]
1. [most important]
2. ...

## Score: [X]/10
[One sentence justifying the score]

Be strict but fair. A score of 7+ means production-ready with minor issues.
A score of 5–6 means it works but needs significant improvement.
Below 5 means it needs to be rewritten.
"""

    async def run(self, task: GrubTask) -> MinionResult:
        """
        Review code files against the design document.

        The task should have files_to_review in task.context, or
        Grub should pass the CoderMinion's files_written list.

        Steps
        -----
        1. Determine which files to review
        2. Load code content
        3. Load design artifact
        4. Build review prompt with code statistics
        5. Call LLM for review
        6. Extract score from response
        7. Return result with score and notes
        """
        t0 = time.monotonic()
        self.logger.info("ReviewerMinion.run: task=%s (%s)", task.id[:8], task.title)

        # ── 1. Determine files to review ──────────────────────────────────────
        files_to_review = task.context.get("files_to_review", task.target_files)
        if not files_to_review:
            return self._make_failed_result(
                task, "No files to review — set task.context['files_to_review']"
            )

        # ── 2. Load code content ──────────────────────────────────────────────
        code_sections = []
        for fpath in files_to_review:
            ok, content = read_file(fpath)
            if ok:
                # Get code statistics too
                try:
                    stats = summarise_file(fpath)
                    stats_str = (
                        f"[{stats['lines']['total']} lines, "
                        f"{len(stats['functions'])} functions, "
                        f"imports: {', '.join(stats['imports'][:8])}]"
                    )
                except Exception:
                    stats_str = ""
                code_sections.append(
                    f"### File: {fpath} {stats_str}\n```python\n{content}\n```"
                )
            else:
                code_sections.append(f"### File: {fpath}\n[Could not read: {content}]")

        if not code_sections:
            return self._make_failed_result(task, "Could not read any review files")

        # ── 3. Load design artifact ───────────────────────────────────────────
        design_text = ""
        if task.artifact_path:
            ok, content = read_file(task.artifact_path)
            if ok:
                design_text = content

        # ── 4. Build review prompt ────────────────────────────────────────────
        prompt_parts = [
            f"## Task Being Reviewed\n{task.title}\n\n{task.description}",
        ]
        if design_text:
            # Summarize instead of truncating — preserves key decisions.
            design_excerpt = await self.compress_context(design_text, "design document")
            prompt_parts.append(f"## Original Design Document\n{design_excerpt}")

        prompt_parts.append("## Code to Review\n" + "\n\n".join(code_sections))
        prompt_parts.append(
            "Please review the code above. Follow the output format exactly. "
            "Be specific about issues and include a numeric score at the end."
        )

        prompt = "\n\n".join(prompt_parts)

        # ── 5. Call LLM ───────────────────────────────────────────────────────
        response = await self._llm(prompt, temperature=0.1)

        if response.startswith("ERROR:"):
            return self._make_failed_result(task, response)

        # ── 6. Extract score ──────────────────────────────────────────────────
        score = self._score_from_text(response)

        # ── 7. Determine status ───────────────────────────────────────────────
        threshold = self.config.quality_threshold
        if score >= threshold:
            status = ResultStatus.SUCCESS
            summary = f"Review passed (score={score:.2f} >= threshold={threshold:.2f})"
        else:
            status = ResultStatus.NEEDS_RETRY
            summary = (
                f"Review failed (score={score:.2f} < threshold={threshold:.2f}). "
                "Code needs improvement before acceptance."
            )

        duration = time.monotonic() - t0

        # Log structured metrics for observability dashboards.
        self._log_metrics(task.id, status.value, score, duration)

        return MinionResult(
            task_id=task.id,
            minion_name=self.name,
            status=status,
            score=score,
            files_written=[],  # reviewer doesn't write files
            summary=summary,
            notes=response,
            duration_seconds=duration,
            raw_llm_output=response,
        )
