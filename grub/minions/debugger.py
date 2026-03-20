"""
grub/minions/debugger.py
========================
DebuggerMinion — given a failing test output, finds and fixes the bug.

What it does
------------
1. Receives the error output (from TesterMinion)
2. Reads the failing code
3. Asks the LLM to identify the root cause
4. Asks the LLM to produce a fix
5. Applies the fix and re-runs the tests
6. Repeats until fixed or max_iterations reached

When is it called?
------------------
Grub calls DebuggerMinion when TesterMinion returns PARTIAL or FAILED
status.  The test output and failing files are passed via task.context.

STATUS: FULLY IMPLEMENTED
"""

from __future__ import annotations

import time

from .base import BaseMinion
from ..contracts.task import GrubTask
from ..contracts.result import MinionResult, ResultStatus, TestSummary
from ..tools.file_ops import read_file, write_file
from ..tools.shell import run_tests, check_syntax


class DebuggerMinion(BaseMinion):
    """
    Diagnoses and fixes failing tests.

    Assigned model: config.models["debugger"]  (default: qwen2.5-coder:32b)
    """

    MINION_NAME = "debugger"

    BASE_SYSTEM_PROMPT = """You are an expert software debugger.

Given failing test output and source code, your job is to:
1. Identify the ROOT CAUSE of the failure (not just the symptom)
2. Produce a minimal, correct fix
3. Explain what was wrong and why

Output format:
## Root Cause
[Explain precisely what is broken and why]

## Fix
[Describe the change needed in plain English]

```python
# filepath: path/to/file_being_fixed.py
[complete corrected file content]
```

Rules:
- Fix the IMPLEMENTATION, not the tests (unless the test is wrong)
- Output the COMPLETE corrected file, not just the changed lines
- Do not introduce new bugs while fixing the current one
- If the test itself is wrong, explain why and output the corrected test
"""

    async def run(self, task: GrubTask) -> MinionResult:
        """
        Diagnose and fix failing tests.

        Expected task.context keys:
          'test_output'     : pytest stdout/stderr from the failing run
          'failing_files'   : list of source files that need fixing
          'test_file'       : path to the test file
        """
        t0 = time.monotonic()
        self.logger.info("DebuggerMinion.run: task=%s (%s)", task.id[:8], task.title)

        test_output = task.context.get("test_output", "")
        failing_files = task.context.get("failing_files", task.target_files)
        test_file = task.context.get("test_file", "")

        if not test_output:
            return self._make_failed_result(
                task, "No test_output in task.context — nothing to debug"
            )

        # ── Load failing source files ─────────────────────────────────────────
        code_sections = []
        for fpath in failing_files:
            ok, content = read_file(fpath)
            if ok:
                code_sections.append(f"### {fpath}\n```python\n{content}\n```")

        # ── Load test file ────────────────────────────────────────────────────
        test_section = ""
        if test_file:
            ok, content = read_file(test_file)
            if ok:
                test_section = (
                    f"\n### Test File: {test_file}\n```python\n{content}\n```"
                )

        max_iter = min(self.config.max_iterations, 3)
        files_fixed = []
        last_test_summary = None

        for iteration in range(1, max_iter + 1):
            self.logger.info("DebuggerMinion: iteration %d/%d", iteration, max_iter)

            # ── Build debug prompt ────────────────────────────────────────────
            # Compress test output if it's large (long stack traces, verbose logs).
            compressed_output = await self.compress_context(
                test_output, "test output and stack trace"
            )
            prompt = (
                f"## Failing Test Output\n```\n{compressed_output}\n```\n\n"
                + "## Source Code\n"
                + "\n\n".join(code_sections)
                + test_section
                + "\n\n## Instructions\n"
                "Identify the root cause and produce a fix. "
                "Output the complete corrected file(s) as fenced Python code blocks "
                "with filepath comments."
            )

            response = await self._llm(prompt, temperature=0.1)

            if response.startswith("ERROR:"):
                return self._make_failed_result(task, response)

            # ── Apply the fix ─────────────────────────────────────────────────
            code_blocks = self._extract_code_blocks(response, "python")
            if not code_blocks:
                code_blocks = self._extract_code_blocks(response)

            newly_fixed = []
            for block in code_blocks:
                lines = block.split("\n")
                fpath = None
                if lines and "filepath:" in lines[0].lower():
                    fpath = lines[0].split("filepath:")[-1].strip().lstrip("#").strip()
                    block = "\n".join(lines[1:]).strip()
                elif failing_files:
                    fpath = failing_files[0]

                if fpath:
                    # Syntax check before writing
                    import tempfile
                    import os

                    with tempfile.NamedTemporaryFile(
                        suffix=".py", delete=False, mode="w"
                    ) as tmp:
                        tmp.write(block)
                        tmp_path = tmp.name
                    chk = check_syntax(tmp_path)
                    os.unlink(tmp_path)

                    if chk.succeeded:
                        ok, written = write_file(fpath, block)
                        if ok:
                            newly_fixed.append(written)
                            self.logger.info("DebuggerMinion: applied fix to %s", fpath)
                    else:
                        self.logger.warning(
                            "DebuggerMinion: fix has syntax errors, skipping: %s",
                            chk.stderr.strip(),
                        )

            files_fixed.extend(newly_fixed)

            # ── Re-run tests ──────────────────────────────────────────────────
            if test_file:
                run_result = run_tests(test_file, timeout=60.0)
                test_output = run_result.output  # update for next iteration
                last_test_summary = self._parse_pytest_summary(run_result.output)

                self.logger.info(
                    "DebuggerMinion after fix: passed=%d failed=%d",
                    last_test_summary.passed,
                    last_test_summary.failed,
                )

                if last_test_summary.all_passed:
                    self.logger.info(
                        "DebuggerMinion: all tests pass after %d iterations", iteration
                    )
                    break
                # Reload source for next iteration
                code_sections = []
                for fpath in failing_files:
                    ok, content = read_file(fpath)
                    if ok:
                        code_sections.append(f"### {fpath}\n```python\n{content}\n```")

        # ── Build result ──────────────────────────────────────────────────────
        duration = time.monotonic() - t0

        if last_test_summary and last_test_summary.all_passed:
            return MinionResult(
                task_id=task.id,
                minion_name=self.name,
                status=ResultStatus.SUCCESS,
                score=0.85,
                files_written=files_fixed,
                test_results=last_test_summary,
                summary=f"Fixed in {iteration} iteration(s). All tests pass.",
                duration_seconds=duration,
                iterations=iteration,
            )
        else:
            passed = last_test_summary.passed if last_test_summary else 0
            total = last_test_summary.total if last_test_summary else 0
            return MinionResult(
                task_id=task.id,
                minion_name=self.name,
                status=ResultStatus.PARTIAL,
                score=passed / max(total, 1),
                files_written=files_fixed,
                test_results=last_test_summary,
                summary=f"Partial fix: {passed}/{total} tests pass after {iteration} iterations.",
                notes=f"Remaining failures:\n{test_output[:2000]}",
                duration_seconds=duration,
                iterations=iteration,
            )

    def _parse_pytest_summary(self, output: str) -> TestSummary:
        import re

        passed = failed = errors = skipped = 0
        for line in reversed(output.splitlines()):
            line = line.lower()
            if " passed" in line or " failed" in line or " error" in line:
                p = re.search(r"(\d+) passed", line)
                f = re.search(r"(\d+) failed", line)
                e = re.search(r"(\d+) error", line)
                s = re.search(r"(\d+) skipped", line)
                passed = int(p.group(1)) if p else 0
                failed = int(f.group(1)) if f else 0
                errors = int(e.group(1)) if e else 0
                skipped = int(s.group(1)) if s else 0
                break
        return TestSummary(
            passed=passed, failed=failed, errors=errors, skipped=skipped, output=output
        )
