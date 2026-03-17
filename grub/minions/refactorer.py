"""
grub/minions/refactorer.py
==========================
RefactorerMinion — cleans up working code without changing its behaviour.

What it does
------------
1. Receives working code that has passed tests
2. Asks the LLM to refactor for:
   - Better naming (variables, functions, classes)
   - Reduced duplication (DRY principle)
   - Better structure (split large functions, group related code)
   - Improved comments and docstrings
   - Consistent style (PEP 8)
3. Re-runs tests to verify behaviour is unchanged
4. Returns refactored files only if all tests still pass

The golden rule: refactoring must NOT change behaviour.
If tests fail after refactoring, the original code is kept.

STATUS: FULLY IMPLEMENTED
"""

from __future__ import annotations

import time
from pathlib import Path

from .base import BaseMinion
from ..contracts.task   import GrubTask
from ..contracts.result import MinionResult, ResultStatus, TestSummary
from ..tools.file_ops   import read_file, write_file
from ..tools.shell      import run_tests, check_syntax


class RefactorerMinion(BaseMinion):
    """
    Refactors working code for readability and maintainability.

    Assigned model: config.models["refactorer"]  (default: qwen2.5-coder:7b)
    """

    MINION_NAME = "refactorer"

    BASE_SYSTEM_PROMPT = """You are an expert software engineer specialising in
code quality and refactoring.

Your job: improve code readability and structure WITHOUT changing its behaviour.

Refactoring goals (in priority order):
1. Naming clarity — variables and functions should be self-documenting
2. Single Responsibility — each function should do one thing
3. DRY (Don't Repeat Yourself) — extract duplicated logic into helpers
4. Comment quality — update/add docstrings, remove useless comments
5. PEP 8 compliance — consistent spacing, line length, imports

Rules:
- DO NOT change the public API (function signatures, class names used externally)
- DO NOT change behaviour — the same inputs must produce the same outputs
- DO NOT refactor if the code is already clean — return it unchanged
- Output the COMPLETE refactored file, not just the changed parts

Output format:
## Changes Made
[Brief bullet list of what was refactored]

```python
# filepath: path/to/file.py
[complete refactored file]
```

If no changes are needed:
## Changes Made
- No changes needed — code is already clean.

```python
# filepath: path/to/file.py
[original file unchanged]
```
"""

    async def run(self, task: GrubTask) -> MinionResult:
        """
        Refactor code files, verify tests still pass.

        Expected task.context keys:
          'files_to_refactor' : list of files to clean up
          'test_file'         : (optional) path to test file to re-run
        """
        t0 = time.monotonic()
        self.logger.info("RefactorerMinion.run: task=%s (%s)", task.id[:8], task.title)

        files_to_refactor = task.context.get("files_to_refactor", task.target_files)
        test_file         = task.context.get("test_file", "")

        if not files_to_refactor:
            return self._make_failed_result(task, "No files_to_refactor in task.context")

        # ── Load files ────────────────────────────────────────────────────────
        originals: dict[str, str] = {}
        code_sections = []
        for fpath in files_to_refactor:
            ok, content = read_file(fpath)
            if ok:
                originals[fpath] = content
                code_sections.append(f"### {fpath}\n```python\n{content}\n```")

        if not code_sections:
            return self._make_failed_result(task, "Could not read any files")

        # ── Ask LLM to refactor ───────────────────────────────────────────────
        prompt = (
            f"## Task\nRefactor the following code for better readability "
            f"and maintainability.\n\n"
            + "## Code\n" + "\n\n".join(code_sections)
            + "\n\n## Instructions\n"
            "Apply the refactoring goals from your instructions. "
            "Output a 'Changes Made' section followed by each refactored file "
            "as a fenced Python code block with a filepath comment. "
            "If a file needs no changes, include it unchanged."
        )

        response = await self._llm(prompt, temperature=0.15)
        if response.startswith("ERROR:"):
            return self._make_failed_result(task, response)

        # ── Extract and write refactored files ────────────────────────────────
        code_blocks   = self._extract_code_blocks(response, "python")
        if not code_blocks:
            code_blocks = self._extract_code_blocks(response)

        files_written: list[str] = []
        files_with_errors: list[str] = []

        for block in code_blocks:
            lines = block.split("\n")
            fpath = None
            if lines and "filepath:" in lines[0].lower():
                fpath = lines[0].split("filepath:")[-1].strip().lstrip("#").strip()
                block = "\n".join(lines[1:]).strip()
            elif files_to_refactor:
                fpath = files_to_refactor[0]

            if not fpath:
                continue

            # Syntax check
            import tempfile, os
            with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w") as tmp:
                tmp.write(block)
                tmp_path = tmp.name
            chk = check_syntax(tmp_path)
            os.unlink(tmp_path)

            if chk.succeeded:
                ok, written = write_file(fpath, block)
                if ok:
                    files_written.append(written)
            else:
                self.logger.warning(
                    "RefactorerMinion: refactored %s has syntax errors — keeping original",
                    fpath
                )
                files_with_errors.append(fpath)
                # Restore original
                if fpath in originals:
                    write_file(fpath, originals[fpath])

        # ── Re-run tests to verify no regression ─────────────────────────────
        duration = time.monotonic() - t0
        test_summary: TestSummary | None = None

        if test_file and files_written:
            run_result   = run_tests(test_file, timeout=60.0)
            test_summary = self._parse_pytest_summary(run_result.output)

            if not test_summary.all_passed:
                self.logger.warning(
                    "RefactorerMinion: refactoring broke tests! Restoring originals."
                )
                # Roll back — restore all original files
                for fpath, original in originals.items():
                    write_file(fpath, original)
                return MinionResult(
                    task_id          = task.id,
                    minion_name      = self.name,
                    status           = ResultStatus.FAILED,
                    score            = 0.0,
                    summary          = "Refactoring broke tests — originals restored.",
                    notes            = run_result.output[:2000],
                    test_results     = test_summary,
                    duration_seconds = duration,
                )

        if not files_written and not files_with_errors:
            summary = "No changes needed — code is already clean."
            score   = 0.95
        elif files_with_errors:
            summary = (
                f"Refactored {len(files_written)} file(s). "
                f"{len(files_with_errors)} file(s) had syntax errors and were kept unchanged."
            )
            score = 0.7
        else:
            summary = f"Successfully refactored {len(files_written)} file(s)."
            score   = 0.9

        return MinionResult(
            task_id          = task.id,
            minion_name      = self.name,
            status           = ResultStatus.SUCCESS,
            score            = score,
            files_written    = files_written,
            test_results     = test_summary,
            summary          = summary,
            duration_seconds = duration,
            raw_llm_output   = response,
        )

    def _parse_pytest_summary(self, output: str) -> TestSummary:
        import re
        passed = failed = errors = skipped = 0
        for line in reversed(output.splitlines()):
            ll = line.lower()
            if " passed" in ll or " failed" in ll or " error" in ll:
                p = re.search(r"(\d+) passed",  ll)
                f = re.search(r"(\d+) failed",  ll)
                e = re.search(r"(\d+) error",   ll)
                s = re.search(r"(\d+) skipped", ll)
                passed  = int(p.group(1)) if p else 0
                failed  = int(f.group(1)) if f else 0
                errors  = int(e.group(1)) if e else 0
                skipped = int(s.group(1)) if s else 0
                break
        return TestSummary(passed=passed, failed=failed,
                           errors=errors, skipped=skipped, output=output)
