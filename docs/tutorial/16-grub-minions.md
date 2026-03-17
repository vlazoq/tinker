# Chapter 16 — Minions in Detail

## What a Minion Is

A Minion is a specialized AI agent that does one type of coding work.
Every Minion follows the same interface: it accepts a `GrubTask` and
returns a `MinionResult`.

The five built-in Minions form an assembly line:

```
GrubTask
    │
    ▼
CoderMinion         "Write the implementation from the design"
    │ files_written
    ▼
ReviewerMinion      "Is this good? Score it. What needs fixing?"
    │ score, notes
    ▼  (loop back to Coder if score < threshold)
TesterMinion        "Write tests and run them"
    │ test_results
    ▼  (call Debugger if tests fail)
DebuggerMinion      "Tests are failing — find the bug and fix it"
    │ fixed_files
    ▼
RefactorerMinion    "Clean up the working code"
    │
    ▼
MinionResult (final)
```

---

## The Data Contracts

### GrubTask — what Grub hands to a Minion

```python
@dataclass
class GrubTask:
    title:          str               # "Implement API gateway request router"
    description:    str               # full instructions
    artifact_path:  str = ""          # path to Tinker design doc
    target_files:   list[str] = []    # which files to create/modify
    language:       str = "python"
    subsystem:      str = "unknown"
    tinker_task_id: str = ""          # traceability back to Tinker
    context:        dict = {}         # extra key/value context
```

The most important fields:
- `description` — this is what the Coder actually reads to decide what to implement
- `artifact_path` — path to the Tinker `.md` design document (extra context)
- `context` — used to pass data between pipeline stages (e.g. `files_to_review`)

### MinionResult — what every Minion returns

```python
@dataclass
class MinionResult:
    task_id:       str           # which GrubTask this is for
    minion_name:   str           # "coder", "reviewer", etc.
    status:        ResultStatus  # SUCCESS / PARTIAL / FAILED / NEEDS_RETRY
    score:         float         # 0.0–1.0 quality score
    files_written: list[str]     # paths of files created/modified
    test_results:  TestSummary   # pass/fail counts from pytest
    summary:       str           # short human-readable summary
    notes:         str           # reviewer feedback, error messages, etc.
    feedback_for_tinker: str     # if set, Grub creates a new Tinker task
```

Grub reads `status` and `score` to decide what to do next:
- `SUCCESS` + score ≥ threshold → accept, move to next stage
- `FAILED` or score < threshold → retry (up to `max_iterations`)
- `NEEDS_RETRY` → retry with `notes` fed back to the next attempt
- All retries exhausted → DLQ (logged for human review)

---

## BaseMinion — the abstract parent

Every Minion inherits from `BaseMinion`.  It provides:

```python
class BaseMinion(ABC):
    @abstractmethod
    async def run(self, task: GrubTask) -> MinionResult: ...

    # LLM communication
    async def _llm(self, prompt, system_prompt, temperature) -> str: ...

    # Helpers
    def _build_system_prompt(self, extra_context="") -> str: ...
    def _extract_code_blocks(self, text, language="") -> list[str]: ...
    def _score_from_text(self, text) -> float: ...
    def _make_failed_result(self, task, reason) -> MinionResult: ...
```

The only method you **must** override is `run()`.

### `_build_system_prompt()`

Combines BASE_SYSTEM_PROMPT + all loaded skills:

```
[BASE_SYSTEM_PROMPT]
---
[skill 1 text]
---
[skill 2 text]
```

### `_llm()`

Sends a prompt to Ollama and returns the text response:

```python
response = await self._llm(
    prompt       = "Write a router class that...",
    temperature  = 0.2,    # low temp for code generation
    max_tokens   = 4096,
)
if response.startswith("ERROR:"):
    return self._make_failed_result(task, response)
```

### `_extract_code_blocks()`

Parses fenced code blocks from LLM output:

```python
# LLM output: "Here's the code:\n```python\ndef hello(): ...\n```"
blocks = self._extract_code_blocks(response, language="python")
# blocks = ["def hello(): ..."]
```

### `_score_from_text()`

Extracts a numeric score from reviewer output:

```python
# LLM output: "...overall this is solid code. Score: 8/10"
score = self._score_from_text(response)
# score = 0.8
```

---

## CoderMinion — writes code

**Model**: `qwen2.5-coder:32b` (configurable)
**Skills**: `python_expert`, `clean_code`, `software_architecture`

### What it does step by step

```
1. Read task.artifact_path  → design document text
2. Read task.target_files   → existing code (if any, for partial implementation)
3. Build prompt:
   "Here is the design: [design text]
    Here is existing code: [existing files]
    Implement the above. Output fenced code blocks with filepath comments."
4. Call LLM (temperature=0.2 for deterministic code)
5. Extract code blocks from response
6. Parse filepath from first-line comment in each block:
   # filepath: api_gateway/router.py
7. Write each file to disk
8. Run syntax check (python -m py_compile)
9. Return MinionResult
```

### Output format the LLM must follow

The Coder's system prompt instructs the LLM to write code blocks like this:

```
```python
# filepath: api_gateway/router.py
from __future__ import annotations

class Router:
    ...
```
```

The `# filepath:` comment tells the Coder where to write the file.
If no filepath comment is found, the file is written to `output_dir/`.

### Self-assessed score

The Coder gives itself a conservative score (max 0.75) — it can't fully
judge its own quality.  The Reviewer will score it properly.

---

## ReviewerMinion — judges code quality

**Model**: `qwen3:7b` (configurable — doesn't need to write code, just read it)
**Skills**: `clean_code`, `security_review`

### What it does

1. Reads the code files from `task.context["files_to_review"]`
2. Runs `summarise_file()` on each (line counts, function list, imports)
3. Reads the design artifact for comparison
4. Asks the LLM to evaluate design alignment + code quality + security
5. Extracts a numeric score from "Score: X/10" in the response
6. Returns SUCCESS if score ≥ threshold, NEEDS_RETRY otherwise

### The retry loop

If the Reviewer scores below threshold, Grub retries the Coder:

```
Coder v1 → Review (score=0.55, threshold=0.75) → FAIL
  ↓ feedback: "missing error handling, no docstrings, insecure DB query"
Coder v2 (with reviewer notes in description) → Review (score=0.78) → PASS
  ↓
TesterMinion
```

This is the **worker + judge loop** from your original vision.

---

## TesterMinion — writes and runs tests

**Model**: `qwen3:7b` (configurable)
**Skills**: `python_expert`, `testing_patterns`

### What it does

1. Reads the implementation files from `task.context["files_to_test"]`
2. Asks the LLM to write pytest tests (happy path + edge cases + error cases)
3. Writes the test file to `output_dir/tests/test_<module>.py`
4. Runs `pytest` with `subprocess.run(["python", "-m", "pytest", ...])`
5. Parses pytest output for `X passed, Y failed` counts
6. If tests fail: asks LLM to fix and retries (up to 3 times)
7. Returns TestSummary with pass/fail counts

---

## DebuggerMinion — fixes failing tests

**Model**: `qwen2.5-coder:32b` (bigger model — debugging needs deep understanding)
**Skills**: `python_expert`, `clean_code`

### What it does

Called when TesterMinion returns PARTIAL or FAILED.

1. Reads `task.context["test_output"]` — the pytest failure output
2. Reads the failing source files
3. Asks LLM: "Here's the error. Here's the code. What's the bug? Fix it."
4. Applies the fix (syntax-checked before writing)
5. Re-runs the tests
6. Repeats up to `max_iterations` times
7. Returns with the fixed files and updated test results

---

## RefactorerMinion — cleans up working code

**Model**: `qwen2.5-coder:7b` (smaller model — structural changes, not deep logic)
**Skills**: `python_expert`, `clean_code`

### What it does

Called last, after all tests pass.

1. Reads working code files
2. Asks LLM to improve: naming, DRY, structure, docstrings, PEP 8
3. Syntax-checks the refactored output
4. Writes refactored files
5. **Re-runs tests to verify behaviour is unchanged**
6. If tests fail: restores originals (safety net)

The golden rule: **refactoring must not change behaviour**.  If it does,
we roll back.

---

## How to Create a New Minion

Say you want a `SecurityAuditorMinion` that audits for OWASP vulnerabilities.

### Step 1 — Create the file

```python
# grub/minions/security_auditor.py

from .base import BaseMinion
from ..contracts.task   import GrubTask
from ..contracts.result import MinionResult, ResultStatus
from ..tools.file_ops   import read_file


class SecurityAuditorMinion(BaseMinion):
    MINION_NAME = "security_auditor"

    BASE_SYSTEM_PROMPT = """You are a security expert performing a
    security audit focused on OWASP Top 10 vulnerabilities.

    Output format:
    ## Vulnerabilities Found
    [list each issue with severity: CRITICAL/HIGH/MEDIUM/LOW]

    ## Recommendations
    [specific fixes for each issue]

    ## Risk Score: [X]/10
    [10 = no issues, 0 = extremely dangerous]
    """

    async def run(self, task: GrubTask) -> MinionResult:
        files = task.context.get("files_to_audit", task.target_files)
        code_text = ""
        for f in files:
            ok, content = read_file(f)
            if ok:
                code_text += f"\n### {f}\n```python\n{content}\n```\n"

        response = await self._llm(
            f"Audit the following code for security vulnerabilities:\n{code_text}",
            temperature=0.1
        )
        if response.startswith("ERROR:"):
            return self._make_failed_result(task, response)

        score = self._score_from_text(response)
        return MinionResult(
            task_id     = task.id,
            minion_name = self.name,
            status      = ResultStatus.SUCCESS,
            score       = score,
            summary     = f"Security audit complete. Risk score: {score:.0%}",
            notes       = response,
        )
```

### Step 2 — Register it

In `grub/registry.py`, inside `load_defaults()`:

```python
from .minions.security_auditor import SecurityAuditorMinion
self.register_minion("security_auditor", SecurityAuditorMinion)
```

### Step 3 — Add to config (optional)

In `grub_config.json`:
```json
{
  "minion_skills": {
    "security_auditor": ["security_review.md", "python_expert.md"]
  }
}
```

### Step 4 — Use it

```python
agent = GrubAgent.from_config()
auditor = agent.registry.get_minion("security_auditor")
result = await auditor.run(task)
```

That's it.  No changes to Grub's core.

---

## Key Concepts

| Concept | What it means |
|---------|--------------|
| `run(task) → result` | Every Minion has this interface — Grub doesn't care which Minion it's calling |
| Retry loop | Coder → Reviewer → if score < threshold → Coder again (with feedback) |
| Temperature | Low (0.1–0.2) for code (deterministic), higher (0.5+) for creative tasks |
| `# filepath:` comment | LLM's way of telling the Coder where to write the file |
| Test-first debugging | Tester writes tests, if they fail → Debugger fixes source code |
| Refactor last | Refactoring happens only after code works and tests pass |

---

→ Next: [Chapter 17 — Grub + Tinker Integration](./17-grub-integration.md)
