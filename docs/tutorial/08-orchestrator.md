# Chapter 08 — The Orchestrator

## The Problem

We have all the building blocks.  Now we need the thing that:

1. Runs forever in a loop
2. Picks the next task, processes it, stores the result
3. Fires the meso synthesis when a subsystem has enough micro artifacts
4. Fires the macro snapshot every 4 hours
5. Writes its state to disk so dashboards can monitor it
6. Shuts down gracefully when asked

This is the **Orchestrator** — the central nervous system of Tinker.

---

## The Architecture Decision

We build the orchestrator in layers:

1. **`OrchestratorState`** (Chapter 00's `state.py`) — what the orchestrator knows about itself
2. **`run_micro_loop()`** — one complete task cycle
3. **`run_meso_loop()`** — subsystem synthesis
4. **`run_macro_loop()`** — architectural snapshot + git commit
5. **`Orchestrator`** — manages the three loops and state

Each loop function is kept in its own file so it stays focused and
testable.  The `Orchestrator` class just decides *when* to call them.

---

## Step 1 — State (Already Built)

`orchestrator/state.py` already exists in the repo — we covered its
design in Chapter 00.  It has:
- `OrchestratorState` dataclass — all counters, history, current task
- `to_dict()` — serialise to JSON for the dashboard
- `write_snapshot(path)` — atomically write state to disk

---

## Step 2 — The Micro Loop

This is the most frequently-run code in the whole system.

```python
# tinker/orchestrator/micro_loop.py

"""
run_micro_loop — one complete task cycle.

Steps:
  1. Pick a task from the queue
  2. Assemble context from memory
  3. Call Architect AI
  4. Maybe do a web search (if Architect requested it)
  5. Call Critic AI
  6. Store artifact in memory
  7. Generate follow-up tasks from knowledge gaps
  8. Mark task complete
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..context.assembler  import ContextAssembler
from ..memory.manager     import MemoryManager
from ..prompts.architect  import build_architect_prompt, parse_architect_response
from ..prompts.critic     import build_critic_prompt, parse_critic_response
from ..tasks.engine       import TaskEngine
from ..tasks.registry     import Task

logger = logging.getLogger(__name__)


class MicroLoopError(Exception):
    """Raised when a micro loop fails unrecoverably."""


@dataclass
class MicroLoopResult:
    """Everything produced by one micro loop iteration."""
    task_id:         str
    artifact_id:     str
    critic_score:    float
    new_tasks_count: int
    prompt_tokens:   int
    completion_tokens: int


async def run_micro_loop(
    task:       Task,
    session_id: str,
    # Component dependencies — all injected, none imported here
    llm,          # ModelRouter
    memory:     MemoryManager,
    tools,        # ToolLayer
    assembler:  ContextAssembler,
    task_engine: TaskEngine,
    task_generator,   # TaskGenerator
    config,       # OrchestratorConfig
) -> MicroLoopResult:
    """
    Execute one complete micro loop for the given task.

    Raises MicroLoopError if the loop cannot complete.
    """
    logger.info("Micro loop: task=%s subsystem=%s", task.id[:8], task.subsystem)

    total_pt = total_ct = 0

    # ── Step 1: Assemble context ─────────────────────────────────────────────
    context = await assembler.assemble(
        session_id       = session_id,
        task_title       = task.title,
        task_description = task.description,
        subsystem        = task.subsystem,
    )

    # ── Step 2: First call to Architect ─────────────────────────────────────
    prompt = build_architect_prompt(
        task_title       = task.title,
        task_description = task.description,
        subsystem        = task.subsystem,
        context          = context,
    )
    try:
        text, pt, ct = await llm.complete(prompt, role="architect")
        total_pt += pt; total_ct += ct
    except Exception as exc:
        raise MicroLoopError(f"Architect call failed: {exc}") from exc

    arch = parse_architect_response(text)

    # ── Step 3: Tool call (web search) if Architect requested it ────────────
    research = ""
    if arch.tool_call and arch.tool_call.startswith("web_search:"):
        query = arch.tool_call[len("web_search:"):]
        logger.debug("Architect requested web search: %r", query)
        research = await tools.web_search(query, n=5)

        # Ask Architect again with the research results
        prompt2 = build_architect_prompt(
            task_title       = task.title,
            task_description = task.description,
            subsystem        = task.subsystem,
            context          = context,
            research_results = research,
        )
        try:
            text, pt, ct = await llm.complete(prompt2, role="architect")
            total_pt += pt; total_ct += ct
        except Exception as exc:
            logger.warning("Architect second call failed, using first result: %s", exc)
        else:
            arch = parse_architect_response(text)

    # ── Step 4: Critic review ────────────────────────────────────────────────
    critic_prompt = build_critic_prompt(
        task_title        = task.title,
        architect_content = arch.content,
        subsystem         = task.subsystem,
    )
    try:
        crit_text, pt, ct = await llm.complete(critic_prompt, role="critic")
        total_pt += pt; total_ct += ct
    except Exception as exc:
        logger.warning("Critic call failed, skipping critique: %s", exc)
        crit_text = '{"score": 0.5, "summary": "Critic unavailable"}'

    crit = parse_critic_response(crit_text)
    logger.info(
        "Critic score: %.2f — %s",
        crit.score, crit.summary[:60],
    )

    # ── Step 5: Store artifact ───────────────────────────────────────────────
    artifact_id = await memory.store_artifact(
        session_id    = session_id,
        task_id       = task.id,
        artifact_type = "design",
        content       = arch.content,
        metadata      = {
            "subsystem":    task.subsystem,
            "critic_score": crit.score,
            "task_type":    task.type.value,
        },
    )
    logger.debug("Stored artifact %s", artifact_id[:8])

    # ── Step 6: Write artifact to disk (optional) ────────────────────────────
    try:
        await tools.write_artifact(
            artifact_id   = artifact_id,
            subsystem     = task.subsystem,
            content       = arch.content,
            artifact_type = "design",
        )
    except Exception as exc:
        logger.warning("Could not write artifact to disk: %s", exc)

    # ── Step 7: Generate follow-up tasks from knowledge gaps ─────────────────
    new_task_ids = await task_generator.from_knowledge_gaps(
        gaps            = arch.knowledge_gaps,
        subsystem       = task.subsystem,
        parent_task_id  = task.id,
    )

    # ── Step 8: Mark task complete ───────────────────────────────────────────
    await task_engine.mark_complete(task.id)

    return MicroLoopResult(
        task_id           = task.id,
        artifact_id       = artifact_id,
        critic_score      = crit.score,
        new_tasks_count   = len(new_task_ids),
        prompt_tokens     = total_pt,
        completion_tokens = total_ct,
    )
```

---

## Step 3 — The Meso Loop

The meso loop fires when a subsystem has accumulated `N` micro results.
It reads those artifacts and asks the Synthesizer to produce a coherent
design document.

```python
# tinker/orchestrator/meso_loop.py

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..memory.manager     import MemoryManager
from ..prompts.synthesizer import build_synthesizer_prompt, parse_synthesizer_response

logger = logging.getLogger(__name__)


@dataclass
class MesoLoopResult:
    subsystem: str
    document_id: str
    artifacts_read: int
    prompt_tokens: int
    completion_tokens: int


async def run_meso_loop(
    subsystem:  str,
    session_id: str,
    llm,
    memory: MemoryManager,
    tools,
) -> MesoLoopResult:
    """
    Synthesise recent micro-loop artifacts for one subsystem.
    """
    logger.info("Meso loop: subsystem=%s", subsystem)

    # Fetch recent design artifacts for this subsystem
    artifacts = await memory.get_recent_artifacts(
        session_id    = session_id,
        artifact_type = "design",
        limit         = 10,
    )
    # Filter to just this subsystem
    sub_artifacts = [
        a["content"] for a in artifacts
        if isinstance(a.get("metadata"), dict)
        and a["metadata"].get("subsystem") == subsystem
    ]

    if not sub_artifacts:
        sub_artifacts = [a["content"] for a in artifacts[:5]]

    logger.info("Synthesising %d artifacts for %s", len(sub_artifacts), subsystem)

    prompt = build_synthesizer_prompt(subsystem, sub_artifacts)
    text, pt, ct = await llm.complete(prompt, role="synthesizer")
    result = parse_synthesizer_response(text)

    # Store the synthesis as an artifact
    doc_id = await memory.store_artifact(
        session_id    = session_id,
        task_id       = f"meso_{subsystem}",
        artifact_type = "synthesis",
        content       = result.document,
        metadata      = {"subsystem": subsystem, "summary": result.summary},
    )

    # Write to disk
    try:
        await tools.write_artifact(doc_id, subsystem, result.document, "synthesis")
    except Exception:
        pass

    return MesoLoopResult(
        subsystem         = subsystem,
        document_id       = doc_id,
        artifacts_read    = len(sub_artifacts),
        prompt_tokens     = pt,
        completion_tokens = ct,
    )
```

---

## Step 4 — The Macro Loop

```python
# tinker/orchestrator/macro_loop.py

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..memory.manager import MemoryManager

logger = logging.getLogger(__name__)


@dataclass
class MacroLoopResult:
    snapshot_version: int
    doc_ids: list[str]
    prompt_tokens: int
    completion_tokens: int


async def run_macro_loop(
    snapshot_version: int,
    session_id: str,
    llm,
    memory: MemoryManager,
    tools,
    arch_state_manager=None,  # optional git commit
) -> MacroLoopResult:
    """
    Produce a full system-wide architectural snapshot.
    """
    logger.info("Macro loop: snapshot v%d", snapshot_version)

    # Collect all synthesis documents
    syntheses = await memory.get_recent_artifacts(
        session_id    = session_id,
        artifact_type = "synthesis",
        limit         = 20,
    )

    if not syntheses:
        logger.info("No synthesis artifacts yet — skipping macro")
        return MacroLoopResult(snapshot_version, [], 0, 0)

    content_list = [f"## {a.get('metadata', {}).get('subsystem', 'Unknown')}\n{a['content']}"
                    for a in syntheses]
    combined = "\n\n---\n\n".join(content_list)

    from ..prompts.synthesizer import SYNTHESIZER_SYSTEM_PROMPT
    prompt = (
        f"# Architectural Snapshot v{snapshot_version}\n\n"
        f"Synthesise the following subsystem design documents into a "
        f"complete system architecture overview:\n\n{combined[:12000]}"
    )
    text, pt, ct = await llm.complete(prompt, role="synthesizer")

    doc_id = await memory.store_artifact(
        session_id    = session_id,
        task_id       = f"macro_v{snapshot_version}",
        artifact_type = "macro_snapshot",
        content       = text,
        metadata      = {"snapshot_version": snapshot_version},
    )

    try:
        await tools.write_artifact(
            doc_id, "architecture", text, f"snapshot_v{snapshot_version}"
        )
    except Exception:
        pass

    if arch_state_manager:
        try:
            await arch_state_manager.commit(
                f"Architectural snapshot v{snapshot_version}"
            )
        except Exception as exc:
            logger.warning("Git commit failed: %s", exc)

    return MacroLoopResult(snapshot_version, [doc_id], pt, ct)
```

---

## Step 5 — The Orchestrator

```python
# tinker/orchestrator/orchestrator.py  (simplified core)

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from dataclasses import dataclass
from typing import Any

from .state       import OrchestratorState, LoopLevel, LoopStatus
from .micro_loop  import run_micro_loop, MicroLoopError
from .meso_loop   import run_meso_loop
from .macro_loop  import run_macro_loop

logger = logging.getLogger(__name__)


@dataclass
class OrchestratorConfig:
    meso_trigger_count:        int   = 5      # micro loops per subsystem before meso
    max_consecutive_failures:  int   = 3      # failures before sleep
    failure_backoff_seconds:   float = 10.0
    macro_interval_seconds:    float = 14400.0  # 4 hours
    state_path:                str   = "./tinker_state.json"


class Orchestrator:
    """
    The main loop controller.

    Runs micro loops continuously, fires meso loops on subsystem thresholds,
    fires macro loops on a timer.  Writes state snapshots after each micro loop.
    """

    def __init__(
        self,
        config:        OrchestratorConfig,
        session_id:    str,
        llm,           # ModelRouter
        memory,        # MemoryManager
        tools,         # ToolLayer
        assembler,     # ContextAssembler
        task_engine,   # TaskEngine
        task_generator,
        arch_state_manager = None,
    ) -> None:
        self._config      = config
        self._session_id  = session_id
        self._llm         = llm
        self._memory      = memory
        self._tools       = tools
        self._assembler   = assembler
        self._task_engine = task_engine
        self._task_gen    = task_generator
        self._arch_sm     = arch_state_manager

        self.state = OrchestratorState()
        self._shutdown_event = asyncio.Event()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def request_shutdown(self) -> None:
        """Ask the orchestrator to stop after the current loop."""
        self._shutdown_event.set()
        self.state.shutdown_requested = True
        logger.info("Shutdown requested")

    async def run(self) -> None:
        """
        Main entry point.  Runs until request_shutdown() is called.
        """
        self._install_signal_handlers()
        self.state.status = LoopStatus.RUNNING
        logger.info("Orchestrator started (session=%s)", self._session_id)

        try:
            while not self._shutdown_event.is_set():
                await self._tick()
        except asyncio.CancelledError:
            logger.info("Orchestrator cancelled — shutting down")
        finally:
            await self._on_shutdown()

    async def _tick(self) -> None:
        """
        One iteration of the main loop.
        Runs one micro loop (or sleeps if no tasks).
        Fires meso/macro if their triggers are met.
        """
        # ── Macro check (timer-based) ────────────────────────────────────────
        elapsed = time.monotonic() - self.state.last_macro_at
        if elapsed >= self._config.macro_interval_seconds:
            await self._run_macro()

        # ── Task selection ───────────────────────────────────────────────────
        task = await self._task_engine.next_task()
        if task is None:
            logger.debug("No pending tasks — sleeping 30s")
            await self._interruptible_sleep(30.0)
            return

        # ── Micro loop ───────────────────────────────────────────────────────
        await self._task_engine.mark_active(task.id)
        self.state.current_task_id  = task.id
        self.state.current_subsystem = task.subsystem
        self.state.current_level    = LoopLevel.MICRO
        self._try_write_snapshot()

        try:
            result = await run_micro_loop(
                task           = task,
                session_id     = self._session_id,
                llm            = self._llm,
                memory         = self._memory,
                tools          = self._tools,
                assembler      = self._assembler,
                task_engine    = self._task_engine,
                task_generator = self._task_gen,
                config         = self._config,
            )
            self.state.total_micro_loops += 1
            self.state.consecutive_failures = 0
            count = self.state.increment_subsystem(task.subsystem)
            logger.info(
                "Micro loop OK: score=%.2f new_tasks=%d [%d/%d for %s]",
                result.critic_score,
                result.new_tasks_count,
                count, self._config.meso_trigger_count, task.subsystem,
            )

            # ── Meso check (count-based per subsystem) ───────────────────────
            if count >= self._config.meso_trigger_count:
                await self._run_meso(task.subsystem)
                self.state.reset_subsystem_count(task.subsystem)

        except MicroLoopError as exc:
            logger.error("Micro loop failed: %s", exc)
            self.state.consecutive_failures += 1
            await self._task_engine.mark_failed(task.id)

            if self.state.consecutive_failures >= self._config.max_consecutive_failures:
                backoff = self._config.failure_backoff_seconds
                logger.warning("Too many failures — sleeping %.0fs", backoff)
                await self._interruptible_sleep(backoff)

        finally:
            self.state.current_task_id   = None
            self.state.current_subsystem = None
            self.state.current_level     = LoopLevel.IDLE
            self._try_write_snapshot()

    async def _run_meso(self, subsystem: str) -> None:
        self.state.current_level = LoopLevel.MESO
        self._try_write_snapshot()
        try:
            result = await run_meso_loop(
                subsystem  = subsystem,
                session_id = self._session_id,
                llm        = self._llm,
                memory     = self._memory,
                tools      = self._tools,
            )
            self.state.total_meso_loops += 1
            logger.info("Meso OK: %s — %d artifacts synthesised",
                        subsystem, result.artifacts_read)
        except Exception as exc:
            logger.error("Meso loop failed for %s: %s", subsystem, exc)
        finally:
            self.state.current_level = LoopLevel.IDLE

    async def _run_macro(self) -> None:
        self.state.current_level = LoopLevel.MACRO
        self._try_write_snapshot()
        try:
            result = await run_macro_loop(
                snapshot_version  = self.state.total_macro_loops + 1,
                session_id        = self._session_id,
                llm               = self._llm,
                memory            = self._memory,
                tools             = self._tools,
                arch_state_manager= self._arch_sm,
            )
            self.state.total_macro_loops += 1
            self.state.last_macro_at = time.monotonic()
            logger.info("Macro OK: snapshot v%d", result.snapshot_version)
        except Exception as exc:
            logger.error("Macro loop failed: %s", exc)
        finally:
            self.state.current_level = LoopLevel.IDLE

    async def _on_shutdown(self) -> None:
        self.state.status = LoopStatus.SHUTDOWN
        self._try_write_snapshot()
        logger.info(
            "Orchestrator stopped — micro=%d meso=%d macro=%d",
            self.state.total_micro_loops,
            self.state.total_meso_loops,
            self.state.total_macro_loops,
        )

    def _try_write_snapshot(self) -> None:
        """Write state to disk.  Non-fatal if it fails."""
        try:
            self.state.write_snapshot(self._config.state_path)
        except Exception as exc:
            logger.debug("Snapshot write failed: %s", exc)

    async def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep, but wake up immediately if shutdown is requested."""
        try:
            await asyncio.wait_for(
                self._shutdown_event.wait(),
                timeout=seconds,
            )
        except asyncio.TimeoutError:
            pass   # finished sleeping normally

    def _install_signal_handlers(self) -> None:
        """Register Ctrl-C and SIGTERM handlers for graceful shutdown."""
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self.request_shutdown)
        except (NotImplementedError, AttributeError):
            # Windows: ProactorEventLoop doesn't support add_signal_handler
            if sys.platform == "win32":
                signal.signal(
                    signal.SIGINT,
                    lambda _s, _f: self.request_shutdown()
                )
            else:
                logger.warning("Signal handlers not available in this environment")
```

---

## Key Design Patterns in the Orchestrator

### Pattern 1: Try/Finally for Cleanup

```python
try:
    result = await run_micro_loop(...)
    # happy path — update state
except MicroLoopError as exc:
    # failure path — record the failure
finally:
    self.state.current_task_id = None   # ALWAYS runs, even on exception
    self._try_write_snapshot()           # ALWAYS write state
```

The `finally` block guarantees the state is always written and the
"current task" is always cleared, even if the loop threw an exception.

### Pattern 2: Interruptible Sleep

```python
async def _interruptible_sleep(self, seconds: float) -> None:
    try:
        await asyncio.wait_for(
            self._shutdown_event.wait(),  # wait for the event...
            timeout=seconds               # ...but only for this long
        )
    except asyncio.TimeoutError:
        pass   # timeout = we slept the full duration normally
```

This is more complex than `await asyncio.sleep(seconds)` but it means
the orchestrator responds to Ctrl+C *immediately* even if it was in the
middle of a 30-second sleep.

### Pattern 3: The Shutdown Event

```python
self._shutdown_event = asyncio.Event()

def request_shutdown(self):
    self._shutdown_event.set()   # any awaiter of this event will unblock

while not self._shutdown_event.is_set():
    await self._tick()
```

An `asyncio.Event` is a thread-safe flag.  `.set()` wakes up anyone
waiting for it.  `.is_set()` returns True/False without waiting.

---

→ Next: [Chapter 09 — Resilience](./09-resilience.md)
