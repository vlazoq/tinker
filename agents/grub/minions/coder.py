"""
agents/grub/minions/coder.py
=====================
CoderMinion — reads a design artifact and writes implementation code.

What it does
------------
1. Reads the Tinker design artifact (a .md file describing what to build)
2. Reads any existing target files (so it can extend, not overwrite)
3. Asks the LLM to write the implementation
4. Extracts code blocks from the LLM response
5. Writes each file to disk
6. Returns a MinionResult with files_written and a self-assessed score

The Coder does NOT run tests (that's TesterMinion) and does NOT review
quality (that's ReviewerMinion).  Each Minion does one thing.

STATUS: FULLY IMPLEMENTED
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from .base import BaseMinion
from ..contracts.task import GrubTask
from ..contracts.result import MinionResult, ResultStatus
from ..tools.file_ops import read_file, write_file, ensure_dir
from ..tools.shell import check_syntax


class CoderMinion(BaseMinion):
    """
    Writes implementation code from a Tinker design artifact.

    Assigned model: config.models["coder"]  (default: qwen2.5-coder:32b)
    """

    MINION_NAME = "coder"

    BASE_SYSTEM_PROMPT = """You are an expert software engineer implementing
production-quality code from architecture design documents.

Your job:
1. Read the design document carefully
2. Implement the described functionality exactly as specified
3. Write clean, well-commented, working code
4. Output ONLY the code — no extra explanation outside code blocks

Output format:
- For each file to create or modify, output a fenced code block with
  a comment on the first line showing the file path:
  ```python
  # filepath: path/to/file.py
  ... code here ...
  ```
- If creating multiple files, output multiple code blocks in order
- Do not output anything outside the code blocks except brief file-path labels

Quality requirements:
- Every function must have a docstring
- Use type hints for all function parameters and return values
- Handle errors with try/except where appropriate
- Do not use placeholder 'pass' statements — implement everything
"""

    async def run(self, task: GrubTask) -> MinionResult:
        """
        Read design artifact, write implementation files.

        Steps
        -----
        1. Load design artifact from disk
        2. Load any existing target files (for context / partial implementation)
        3. Build a detailed prompt
        4. Call the LLM
        5. Parse code blocks from response
        6. Write files
        7. Run syntax check
        8. Return result
        """
        t0 = time.monotonic()
        self.logger.info("CoderMinion.run: task=%s (%s)", task.id[:8], task.title)

        # ── 1. Load design artifact ───────────────────────────────────────────
        design_text = ""
        if task.artifact_path:
            ok, content = read_file(task.artifact_path)
            if ok:
                design_text = content
                self.logger.debug(
                    "Loaded design artifact: %s (%d chars)",
                    task.artifact_path,
                    len(design_text),
                )
            else:
                self.logger.warning(
                    "Could not load artifact %s: %s", task.artifact_path, content
                )

        # ── 2. Load existing target files ─────────────────────────────────────
        existing_code = ""
        for fpath in task.target_files:
            ok, content = read_file(fpath)
            if ok:
                existing_code += (
                    f"\n\n# Existing file: {fpath}\n```python\n{content}\n```"
                )

        # ── 3. Build prompt ───────────────────────────────────────────────────
        prompt_parts = [
            f"## Task\n{task.title}",
            f"\n## Description\n{task.description}",
        ]
        if design_text:
            prompt_parts.append(f"\n## Design Document\n{design_text}")
        if existing_code:
            prompt_parts.append(
                f"\n## Existing Code (extend/modify as needed)\n{existing_code}"
            )
        if task.target_files:
            files_str = "\n".join(f"  - {f}" for f in task.target_files)
            prompt_parts.append(f"\n## Files to Create/Modify\n{files_str}")
        if task.context:
            ctx_str = "\n".join(f"  {k}: {v}" for k, v in task.context.items())
            prompt_parts.append(f"\n## Additional Context\n{ctx_str}")

        prompt_parts.append(
            "\n## Instructions\n"
            "Implement the above. Output one fenced code block per file. "
            "Start each block with a comment: # filepath: path/to/file.py"
        )

        prompt = "\n".join(prompt_parts)
        system = self._build_system_prompt()

        # ── 4. Call LLM ───────────────────────────────────────────────────────
        response = await self._llm(
            prompt, system_prompt=system, temperature=0.2,
            timeout=self.config.timeouts.get(self.name, 120.0),
        )

        if response.startswith("ERROR:"):
            return self._make_failed_result(task, response)

        # ── 5. Parse code blocks ──────────────────────────────────────────────
        files_written = []
        code_blocks = self._extract_code_blocks(response, language=task.language)

        if not code_blocks:
            # Fallback: maybe the whole response is code
            code_blocks = self._extract_code_blocks(response)

        if not code_blocks:
            self.logger.warning("CoderMinion: no code blocks found in LLM response")
            duration = time.monotonic() - t0
            self._log_metrics(task.id, ResultStatus.NEEDS_RETRY.value, 0.1, duration)
            return MinionResult(
                task_id=task.id,
                minion_name=self.name,
                status=ResultStatus.NEEDS_RETRY,
                score=0.1,
                notes="LLM did not produce any code blocks. Response:\n"
                + response[:500],
                summary="No code blocks found in LLM output.",
                raw_llm_output=response,
                duration_seconds=duration,
            )

        # ── 6. Write files ────────────────────────────────────────────────────
        syntax_errors = []
        ensure_dir(self.config.output_dir)

        # Ordered list of regex patterns to extract a filepath from the first
        # line of a code block.  We try each pattern in order; the first match
        # wins.  This handles the various conventions LLMs use when labelling
        # output files:
        #   - "# filepath: path/to/file.py"   (our canonical format)
        #   - "# file: path/to/file.py"        (shorter variant)
        #   - "## filename: path/to/file.py"   (Markdown heading variant)
        #   - "path: path/to/file.py"          (frontmatter-style)
        _FILEPATH_PATTERNS: list[re.Pattern[str]] = [
            re.compile(r"^#\s*filepath:\s*(.+)", re.IGNORECASE),
            re.compile(r"^#\s*file:\s*(.+)", re.IGNORECASE),
            re.compile(r"^##\s*filename:\s*(.+)", re.IGNORECASE),
            re.compile(r"^path:\s*(.+)", re.IGNORECASE),
        ]

        for i, block in enumerate(code_blocks):
            # Try to extract filepath from first line comment using multiple
            # fallback patterns — LLMs are inconsistent with their labelling.
            lines = block.split("\n")
            filepath = None

            if lines:
                for pattern in _FILEPATH_PATTERNS:
                    m = pattern.match(lines[0].strip())
                    if m:
                        filepath = m.group(1).strip().lstrip("#").strip()
                        # Remove the filepath line from the code block so it
                        # doesn't end up in the written file.
                        block = "\n".join(lines[1:]).strip()
                        break

            if filepath is None and task.target_files and i < len(task.target_files):
                filepath = task.target_files[i]
            elif filepath is None:
                # Default: write to output_dir with a generated name
                ext = ".py" if task.language == "python" else f".{task.language}"
                filepath = str(
                    Path(self.config.output_dir) / f"{task.subsystem}_{i}{ext}"
                )

            ok, result_path = write_file(filepath, block)
            if ok:
                files_written.append(result_path)
                self.logger.info("CoderMinion: wrote %s", filepath)

                # Syntax check for Python files
                if filepath.endswith(".py"):
                    check = check_syntax(filepath)
                    if not check.succeeded:
                        syntax_errors.append(f"{filepath}: {check.stderr.strip()}")
            else:
                self.logger.warning(
                    "CoderMinion: could not write %s: %s", filepath, result_path
                )

        # ── 7. Build result ───────────────────────────────────────────────────
        duration = time.monotonic() - t0

        if syntax_errors:
            # Log structured metrics before returning — allows dashboards to
            # track partial successes caused by syntax errors.
            self._log_metrics(task.id, ResultStatus.PARTIAL.value, 0.4, duration)
            return MinionResult(
                task_id=task.id,
                minion_name=self.name,
                status=ResultStatus.PARTIAL,
                score=0.4,
                files_written=files_written,
                summary=f"Wrote {len(files_written)} file(s) but with syntax errors.",
                notes="Syntax errors:\n" + "\n".join(syntax_errors),
                duration_seconds=duration,
                raw_llm_output=response,
            )

        # Self-assessed score: did it produce files for all requested targets?
        target_coverage = (
            len(files_written) / max(len(task.target_files), 1)
            if task.target_files
            else (1.0 if files_written else 0.0)
        )
        score = min(
            0.75, 0.5 + 0.25 * target_coverage
        )  # max 0.75 (Reviewer will score higher)

        # Log structured metrics for observability dashboards.
        final_status = ResultStatus.SUCCESS if files_written else ResultStatus.FAILED
        self._log_metrics(task.id, final_status.value, score, duration)

        return MinionResult(
            task_id=task.id,
            minion_name=self.name,
            status=final_status,
            score=score,
            files_written=files_written,
            summary=f"Wrote {len(files_written)} file(s): {', '.join(files_written[:3])}",
            duration_seconds=duration,
            raw_llm_output=response,
            feedback_for_tinker=(
                f"Code implementation for '{task.title}' written to: "
                + ", ".join(files_written[:3])
            ),
        )
