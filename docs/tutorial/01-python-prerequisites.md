# Chapter 01 — Python Prerequisites

This chapter covers the Python features used throughout Tinker.  If you
already know `async/await`, dataclasses, and type hints well, skim it and
move on.  If anything is unfamiliar, read it carefully — these patterns
appear in every chapter.

---

## 1.1 — Type Hints

Python lets you annotate what types a variable or function uses.  The
interpreter does **not** enforce these at runtime — they are for humans
and tools (like VS Code's autocomplete) to understand the code.

```python
# Without type hints — what does this function return?
def get_task(task_id):
    ...

# With type hints — now it's obvious
def get_task(task_id: str) -> dict:
    ...

# Importing types from the typing module
from typing import Optional, Any

def find_task(task_id: str) -> Optional[dict]:
    # Optional[dict] means "a dict, or None"
    ...

def store(key: str, value: Any) -> None:
    # Any means "literally anything"
    # None return means the function doesn't return a value
    ...
```

You will see `list[str]`, `dict[str, int]`, `Optional[float]`, and `Any`
throughout Tinker.

---

## 1.2 — Dataclasses

A `dataclass` is a convenient way to define a class that just holds data.
Instead of writing a long `__init__` method yourself, Python writes it for
you based on the field declarations.

```python
from dataclasses import dataclass, field
from typing import Optional

# Without dataclass — verbose
class TaskResult:
    def __init__(self, task_id: str, content: str, score: float):
        self.task_id = task_id
        self.content = content
        self.score = score

# With dataclass — same thing, much shorter
@dataclass
class TaskResult:
    task_id: str
    content: str
    score: float

# Using it
result = TaskResult(task_id="abc", content="Here is the design...", score=0.85)
print(result.task_id)   # "abc"
print(result)           # TaskResult(task_id='abc', content='Here is...', score=0.85)
```

The `field()` function lets you set default values that are computed at
object creation time (important for lists and dicts — never use a mutable
default directly):

```python
@dataclass
class OrchestratorState:
    total_micro_loops: int = 0           # simple default
    history: list = field(default_factory=list)  # mutable default — ALWAYS use field()
    started_at: float = field(default_factory=time.monotonic)  # computed at creation
```

---

## 1.3 — Enums

An `Enum` is a set of named constants.  Instead of using raw strings like
`"running"` or `"failed"` (which are easy to typo), you define them once:

```python
from enum import Enum

class LoopStatus(str, Enum):
    RUNNING  = "running"
    SUCCESS  = "success"
    FAILED   = "failed"
    SHUTDOWN = "shutdown"

# Using it
status = LoopStatus.RUNNING
print(status)          # LoopStatus.RUNNING
print(status.value)    # "running"
print(status == "running")  # True  ← because we inherit from str
```

The `str, Enum` base means the value is also a plain string, so it
serialises to JSON cleanly without any extra code.

---

## 1.4 — Async / Await — The Most Important Concept

This is the hardest concept to understand if you haven't seen it before.
Take your time here.

### The Problem

Most of Tinker's work is waiting: waiting for an AI model to respond
(3–15 seconds), waiting for a database write, waiting for a web request.
If your code *blocks* during a wait, nothing else can happen:

```python
# Synchronous (blocking) — BAD for Tinker
def complete(prompt):
    response = http_call(prompt)   # blocks for 10 seconds
    return response                # nothing else can run during those 10s
```

### The Solution: Coroutines

A *coroutine* is a function that can pause itself and let other code run
while it is waiting.  You create a coroutine with `async def` and pause
it with `await`:

```python
# Asynchronous (non-blocking) — GOOD for Tinker
async def complete(prompt):
    response = await http_call(prompt)   # pauses HERE, lets other things run
    return response                      # resumes when the HTTP call is done
```

When Python sees `await`, it pauses this coroutine and switches to another
one.  When the awaited operation finishes, Python switches back and resumes
from the exact point it left off.

### Running Coroutines

A coroutine on its own does nothing — you have to run it:

```python
import asyncio

async def say_hello():
    await asyncio.sleep(1)    # pause for 1 second
    print("Hello!")

# Method 1: run one coroutine from regular code
asyncio.run(say_hello())

# Method 2: inside an async function, use await
async def main():
    await say_hello()
```

### Running Multiple Coroutines "at the Same Time"

This is the key benefit of asyncio.  You can run multiple coroutines
concurrently with `asyncio.gather()`:

```python
async def main():
    # These run concurrently — total time ~1s, not ~3s
    result_a, result_b, result_c = await asyncio.gather(
        ai_call_1(),    # takes 1s
        ai_call_2(),    # takes 1s
        ai_call_3(),    # takes 1s
    )
```

### Running Blocking Code Without Blocking Everything

Some libraries (like SQLite) don't have async versions.  You can run them
in a thread pool so they don't block the event loop:

```python
import asyncio

def blocking_db_query(sql):
    # This is a regular synchronous function
    ...

async def async_wrapper():
    # Run the blocking function in a thread — event loop stays responsive
    result = await asyncio.to_thread(blocking_db_query, "SELECT * FROM tasks")
    return result
```

You will see `asyncio.to_thread()` everywhere in Tinker's database code.

### The Golden Rule

> If a function uses `await`, it must be `async def`.
> If you call an `async def` function, you must `await` it.

```python
# CORRECT
async def do_work():
    result = await some_async_function()  # ✅

# WRONG — this just creates a coroutine object, doesn't run it
async def do_work():
    result = some_async_function()        # ❌ forgot await — result is a coroutine object
```

---

## 1.5 — Context Managers (the `with` statement)

You use `with` to ensure cleanup happens even if an error occurs:

```python
# Without context manager — risky
con = sqlite3.connect("mydb.sqlite")
rows = con.execute("SELECT * FROM tasks").fetchall()
con.close()    # if the line above throws an error, close() is never called!

# With context manager — safe
with sqlite3.connect("mydb.sqlite") as con:
    rows = con.execute("SELECT * FROM tasks").fetchall()
    # con.close() is called automatically, even if an error occurs
```

Async context managers work the same way but with `async with`:

```python
async def fetch(url: str):
    async with httpx.AsyncClient() as client:   # opens connection
        response = await client.get(url)
        return response.json()
    # connection is closed automatically here
```

---

## 1.6 — Environment Variables

Tinker reads all its configuration from environment variables so you can
change settings without editing code.  The `os.getenv()` function reads them:

```python
import os

# Read an env var with a default if not set
redis_url = os.getenv("TINKER_REDIS_URL", "redis://localhost:6379")
port      = int(os.getenv("TINKER_HEALTH_PORT", "8081"))
enabled   = os.getenv("TINKER_FEATURE_X", "true").lower() == "true"
```

The `python-dotenv` library loads a `.env` file into the environment at
startup so you don't have to `export` every variable manually:

```bash
# .env file
TINKER_REDIS_URL=redis://localhost:6379
TINKER_HEALTH_PORT=8081
```

```python
from dotenv import load_dotenv
load_dotenv()    # reads .env and sets os.environ entries
```

---

## 1.7 — Pathlib (Cross-Platform File Paths)

Never concatenate file paths with strings.  Use `pathlib.Path`:

```python
from pathlib import Path

# BAD — breaks on Windows because Windows uses backslashes
path = "/home/user/tinker" + "/" + "data" + "/" + "tasks.sqlite"

# GOOD — works on Linux, macOS, and Windows
base = Path("/home/user/tinker")
path = base / "data" / "tasks.sqlite"   # forward slashes work as the / operator
print(path)        # /home/user/tinker/data/tasks.sqlite (on Linux)
                   # C:\home\user\tinker\data\tasks.sqlite (on Windows)

# Useful Path methods
path.exists()           # True if the file exists
path.parent             # /home/user/tinker/data
path.name               # tasks.sqlite
path.suffix             # .sqlite
path.read_text()        # read the file content as a string
path.write_text("...")  # write a string to the file
```

---

## 1.8 — Exception Handling

```python
try:
    result = call_ai_model(prompt)
except httpx.ConnectError as exc:
    # Specific exception type — only catches connection errors
    print(f"AI model not reachable: {exc}")
    result = None
except Exception as exc:
    # Catch-all — catches anything not caught above
    print(f"Unexpected error: {exc}")
    result = None
finally:
    # Runs ALWAYS — even if no exception occurred, even if you return inside try
    cleanup()
```

In Tinker you will often see exceptions caught and logged, then the code
continues with a safe default.  This is deliberate — the orchestrator
must keep running even when individual components fail.

---

## 1.9 — Logging (Not Print)

Never use `print()` in production code.  Use `logging`:

```python
import logging

# Create a logger for this module
logger = logging.getLogger(__name__)
# __name__ is the module's dotted path, e.g. "orchestrator.micro_loop"

# Log at different severity levels
logger.debug("Very detailed: loop iteration %d", loop_count)
logger.info("Normal operation: task %s started", task_id)
logger.warning("Something unexpected but not fatal: %s", msg)
logger.error("Something failed: %s", exc)
logger.critical("System is broken: %s", exc)
```

The `%s` and `%d` format markers are used instead of f-strings in logging
calls — this avoids building the string if the log level is too low to
display it (a small but real performance saving in tight loops).

---

## Quick Reference Card

Keep this open in another tab as you build:

```
async def   → defines a coroutine (async function)
await       → pauses here and lets other coroutines run
asyncio.run()  → starts the event loop with one root coroutine
asyncio.gather()  → run multiple coroutines concurrently
asyncio.to_thread()  → run a blocking function without blocking the loop

@dataclass  → auto-generate __init__ from field declarations
field(default_factory=list)  → mutable default in a dataclass

class X(str, Enum)  → named constants that are also plain strings

Path(__file__).parent  → the directory this file lives in
Path(a) / "b" / "c"  → cross-platform path joining

os.getenv("KEY", "default")  → read env var with fallback

with open(...) as f  → context manager, always closes file
async with client    → async context manager

logger = logging.getLogger(__name__)  → module-level logger
```

---

**Now let's build something.**

→ Next: [Chapter 02 — The Model Client](./02-model-client.md)
