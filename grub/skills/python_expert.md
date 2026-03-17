# Python Expert Skill

You have expert-level Python knowledge. Apply these standards to all code you write or review.

## Code Style (PEP 8)
- Max line length: 100 characters
- Use 4 spaces for indentation (never tabs)
- Two blank lines between top-level definitions, one between methods
- Imports at top: stdlib → third-party → local, each group separated by a blank line
- Use `from __future__ import annotations` at the top of every file

## Type Hints
- All function parameters and return values must have type hints
- Use `Optional[X]` or `X | None` for nullable values
- Use `list[str]` not `List[str]` (Python 3.9+)
- Use `dict[str, Any]` not `Dict[str, Any]`

## Docstrings
- All public functions, classes, and methods must have docstrings
- Use this format:
  ```
  """Short one-line description.

  Parameters
  ----------
  param_name : type
      Description.

  Returns
  -------
  type
      Description.
  """
  ```
- Private methods (starting with _) do not need docstrings unless complex

## Error Handling
- Use specific exception types, not bare `except:`
- Always log errors with context: `logger.error("context: %s", exc)`
- Use `try/except/finally` for resource cleanup
- Raise `ValueError` for bad inputs, `RuntimeError` for state errors
- Never silently swallow exceptions in production code

## Python Idioms
- Use f-strings for formatting: `f"Hello {name}"` not `"Hello %s" % name`
- Use `pathlib.Path` for file paths, not `os.path`
- Use `dataclasses` or `@dataclass` for data containers
- Use `Enum` for named constants, not plain strings
- Use context managers (`with`) for file I/O and locks
- Prefer list/dict comprehensions over loops for simple transformations
- Use `any()` / `all()` instead of loops where appropriate

## Async Python
- Use `async def` / `await` for all I/O-bound operations
- Never use `time.sleep()` in async code — use `await asyncio.sleep()`
- Use `asyncio.to_thread()` to run blocking code without blocking the event loop
- Use `asyncio.gather()` to run independent coroutines concurrently

## Common Mistakes to Avoid
- Mutable default arguments: never `def f(x=[])`, use `def f(x=None)` then `x = x or []`
- String concatenation in loops: use `"".join(parts)` not `+=`
- `is` vs `==`: use `is` only for None/True/False checks
- Bare `except: pass`: always specify the exception type
- Global state: avoid module-level mutable variables
