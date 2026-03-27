"""
agents/grub/minions/tester.py
======================
TesterMinion — writes tests for implemented code and runs them.

What it does
------------
1. Reads the implementation files (from CoderMinion)
2. Asks the LLM to write pytest tests
3. Writes the test file to disk
4. Runs pytest and captures results
5. If tests fail: asks the LLM to fix the implementation or the tests
6. Repeats until all tests pass or max_iterations reached

Why separate from Coder?
-------------------------
Writing code and testing code require different mindsets.  The Coder focuses
on "does this implement the spec?".  The Tester focuses on "can I break this?"
Splitting them means each can use a different model and different skills.

STATUS: FULLY IMPLEMENTED
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from .base import BaseMinion
from ..contracts.task import GrubTask
from ..contracts.result import MinionResult, ResultStatus, TestSummary
from ..tools.file_ops import read_file, write_file, ensure_dir
from ..tools.shell import run_tests


class TesterMinion(BaseMinion):
    """
    Writes and runs pytest tests for implemented code.

    Assigned model: config.models["tester"]  (default: qwen3:7b)
    """

    MINION_NAME = "tester"

    BASE_SYSTEM_PROMPT = """You are an expert Python test engineer writing
pytest tests for production code.

Your job: write comprehensive, meaningful tests.

Rules:
- Use pytest (not unittest)
- Write at least one test per public function/method
- Test: happy path, edge cases, error cases
- Use descriptive test names: test_<function>_<scenario>
- Use pytest fixtures for shared setup
- Mock external dependencies (HTTP calls, DB, file system)
- Output ONLY the test code in a fenced block:
  ```python
  # filepath: tests/test_<module_name>.py
  ... test code ...
  ```

Good test names:
  test_router_returns_404_for_unknown_path
  test_router_raises_on_invalid_method
  test_router_handles_empty_body

Bad test names:
  test_1
  test_router
  my_test
"""

    async def run(self, task: GrubTask) -> MinionResult:
        """
        Write tests for implementation files, run them, fix if needed.

        The implementation files should be in task.context['files_to_test']
        or in task.target_files.
        """
        t0 = time.monotonic()
        self.logger.info("TesterMinion.run: task=%s (%s)", task.id[:8], task.title)

        files_to_test = task.context.get("files_to_test", task.target_files)
        if not files_to_test:
            return self._make_failed_result(task, "No files to test specified")

        # ── Load implementation code ──────────────────────────────────────────
        code_sections = []
        for fpath in files_to_test:
            ok, content = read_file(fpath)
            if ok:
                code_sections.append(f"### {fpath}\n```python\n{content}\n```")

        if not code_sections:
            return self._make_failed_result(
                task, "Could not read any implementation files"
            )

        # ── Ask LLM to write tests ────────────────────────────────────────────
        prompt = (
            f"## Task\n{task.title}\n\n"
            f"## Implementation Code\n"
            + "\n\n".join(code_sections)
            + "\n\n## Instructions\n"
            "Write comprehensive pytest tests for the above code. "
            "Cover happy paths, edge cases, and error handling. "
            "Use mocking for any external dependencies. "
            "Output a single fenced Python code block with filepath comment."
        )

        response = await self._llm(
            prompt, temperature=0.2,
            timeout=self.config.timeouts.get(self.name, 120.0),
        )
        if response.startswith("ERROR:"):
            return self._make_failed_result(task, response)

        # ── Extract and write test file ───────────────────────────────────────
        code_blocks = self._extract_code_blocks(response, "python")
        if not code_blocks:
            code_blocks = self._extract_code_blocks(response)
        if not code_blocks:
            return MinionResult(
                task_id=task.id,
                minion_name=self.name,
                status=ResultStatus.NEEDS_RETRY,
                score=0.1,
                notes="LLM produced no code blocks for tests.",
                raw_llm_output=response,
            )

        test_code = code_blocks[0]
        test_lines = test_code.split("\n")

        # Extract filepath from first line comment
        test_filepath = None
        if test_lines and "filepath:" in test_lines[0].lower():
            test_filepath = (
                test_lines[0].split("filepath:")[-1].strip().lstrip("#").strip()
            )
            test_code = "\n".join(test_lines[1:]).strip()

        if not test_filepath:
            # Default test file path
            module_name = Path(files_to_test[0]).stem if files_to_test else "module"
            test_filepath = str(
                Path(self.config.output_dir) / "tests" / f"test_{module_name}.py"
            )

        ensure_dir(str(Path(test_filepath).parent))
        ok, written_path = write_file(test_filepath, test_code)
        if not ok:
            return self._make_failed_result(
                task, f"Could not write test file: {written_path}"
            )

        self.logger.info("TesterMinion: wrote test file %s", written_path)

        # ── Run the tests ─────────────────────────────────────────────────────
        max_fix_iterations = min(self.config.max_iterations, 3)
        test_summary = None

        for iteration in range(1, max_fix_iterations + 1):
            result = run_tests(
                test_filepath,
                cwd=self.config.output_dir or ".",
                timeout=60.0,
            )
            test_summary = self._parse_pytest_output(result.output)
            self.logger.info(
                "TesterMinion iter %d: passed=%d failed=%d errors=%d",
                iteration,
                test_summary.passed,
                test_summary.failed,
                test_summary.errors,
            )

            if test_summary.all_passed:
                break

            if iteration < max_fix_iterations:
                # Ask LLM to fix the failures
                fix_prompt = (
                    f"The following test failures occurred:\n\n"
                    f"```\n{result.output[:3000]}\n```\n\n"
                    f"Here is the test file:\n```python\n{test_code}\n```\n\n"
                    f"Here is the implementation:\n```python\n{code_sections[0]}\n```\n\n"
                    "Fix the test file (or the implementation if the test is correct). "
                    "Output the corrected code as a fenced Python block."
                )
                fix_response = await self._llm(
                    fix_prompt, temperature=0.1,
                    timeout=self.config.timeouts.get(self.name, 120.0),
                )
                if not fix_response.startswith("ERROR:"):
                    fixed_blocks = self._extract_code_blocks(fix_response, "python")
                    if fixed_blocks:
                        test_code = fixed_blocks[0]
                        write_file(test_filepath, test_code)

        # ── Build result ──────────────────────────────────────────────────────
        duration = time.monotonic() - t0

        if test_summary and test_summary.all_passed:
            score = 0.9
            status = ResultStatus.SUCCESS
            summary = (
                f"All {test_summary.passed} tests passed in {iteration} iteration(s)."
            )
        elif test_summary:
            score = test_summary.passed / max(test_summary.total, 1)
            status = ResultStatus.PARTIAL
            summary = (
                f"{test_summary.passed}/{test_summary.total} tests passed. "
                f"{test_summary.failed} failed, {test_summary.errors} errors."
            )
        else:
            score = 0.0
            status = ResultStatus.FAILED
            summary = "Could not run tests."

        # Log structured metrics for observability dashboards.
        self._log_metrics(task.id, status.value, score, duration)

        return MinionResult(
            task_id=task.id,
            minion_name=self.name,
            status=status,
            score=score,
            files_written=[written_path],
            test_results=test_summary,
            summary=summary,
            duration_seconds=duration,
            iterations=iteration,
            raw_llm_output=response,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _parse_pytest_output(self, output: str) -> TestSummary:
        """
        Parse pytest stdout to extract pass/fail counts.

        Looks for lines like:
          '5 passed, 2 failed, 1 error in 3.45s'
          '7 passed in 1.23s'
        """
        passed = failed = errors = skipped = 0

        # Look for the summary line at the end of pytest output
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
            passed=passed,
            failed=failed,
            errors=errors,
            skipped=skipped,
            output=output,
        )
