"""
orchestrator/state.py
=====================

Everything the orchestrator knows about itself, right now.

Why does this file exist?
--------------------------
The orchestrator runs in a tight loop, 24/7.  At any moment, someone (a human
engineer, a monitoring dashboard, an automated health-check) might want to ask:

  * "What task is it working on?"
  * "How many loops has it done?"
  * "Did the last meso synthesis succeed?"
  * "How long has it been running?"

Rather than scattering that information across many variables in many files,
this module provides a single, clean data model:

  ``OrchestratorState``   — the master record of live orchestrator state
  ``MicroLoopRecord``     — what happened in one micro loop
  ``MesoLoopRecord``      — what happened in one meso synthesis
  ``MacroLoopRecord``     — what happened in one macro snapshot
  ``LoopLevel``           — which loop is currently active (or idle)
  ``LoopStatus``          — did the loop succeed, fail, or is it still running?

This state is serialised to a JSON file on disk after every micro loop so the
Dashboard process can read it without any shared memory or locks.

What is a dataclass?
---------------------
The ``@dataclass`` decorator (used on most classes here) automatically writes
the ``__init__`` method based on the class's field declarations.  It also
adds ``__repr__`` so objects print nicely in logs.  Think of it as a
shorthand for defining a simple "bag of data" object.

What is an Enum?
-----------------
``Enum`` (from Python's standard library) lets you define a set of named
constants.  Instead of using raw strings like "micro" or "running" throughout
the codebase (which are easy to mis-spell and hard to search for), we use
``LoopLevel.MICRO`` and ``LoopStatus.RUNNING``.  The ``str, Enum`` base
means the value is *also* a plain string, so it serialises cleanly to JSON.

Monotonic vs wall-clock time
-----------------------------
You'll notice two kinds of timestamps here:

  ``time.monotonic()`` — counts seconds since some arbitrary starting point.
    It *never goes backwards*, even if the system clock is adjusted (e.g. by
    NTP).  Perfect for measuring durations.

  ``time.time()`` — the real-world clock (Unix timestamp).  Can jump forwards
    or backwards.  Used when we want a timestamp humans can read (e.g. "started
    at 14:32 UTC").

We use monotonic for all internal timing and convert to wall-clock only when
producing output for human consumption in ``to_dict()``.
"""
from __future__ import annotations

import json
import time
# ``asdict`` converts a dataclass instance into a plain Python dict, which
# we need before we can call ``json.dumps()``.
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class LoopLevel(str, Enum):
    """
    Which of the three reasoning loops (or no loop) is currently active.

    The three loops form a hierarchy:
      MICRO  — running constantly; picks one task and works on it
      MESO   — fires occasionally when a subsystem has enough micro results
               to summarise
      MACRO  — fires every few hours to snapshot the entire architecture
      IDLE   — the orchestrator is between loops (startup, shutdown, or paused)

    Inheriting from both ``str`` and ``Enum`` means that
    ``LoopLevel.MICRO == "micro"`` is True, so the value serialises to plain
    JSON without any special handling.
    """
    MICRO = "micro"
    MESO = "meso"
    MACRO = "macro"
    IDLE = "idle"


class LoopStatus(str, Enum):
    """
    The outcome (or current state) of a single loop run.

    RUNNING  — the loop started but hasn't finished yet
    SUCCESS  — the loop completed all its steps without errors
    FAILED   — the loop encountered an error and could not finish
    SHUTDOWN — the orchestrator is shutting down cleanly (used on the top-level
               OrchestratorState, not on individual loop records)
    """
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SHUTDOWN = "shutdown"


@dataclass
class MicroLoopRecord:
    """
    A complete record of one micro-loop iteration.

    A new instance of this record is created at the start of each micro loop
    and filled in progressively as each step completes.  If a step fails, the
    record captures the error message.  At the end of the loop the completed
    record is appended to ``OrchestratorState.micro_history``.

    Fields
    ------
    iteration          : Which micro-loop number this was (1, 2, 3, ...).
    task_id            : The unique ID of the task that was worked on.
    subsystem          : Which part of the architecture this task belongs to
                         (e.g. "api_gateway", "auth_service").  Used to decide
                         when to trigger a meso synthesis.
    started_at         : Monotonic timestamp when this loop began.
    finished_at        : Monotonic timestamp when it ended (None while running).
    status             : SUCCESS, FAILED, or RUNNING.
    architect_tokens   : How many tokens the Architect AI used.  Useful for
                         cost monitoring.
    critic_tokens      : How many tokens the Critic AI used.
    artifact_id        : The ID of the artifact stored in memory on success.
    new_tasks_generated: How many follow-up tasks the task engine created.
    researcher_calls   : How many Tool Layer (researcher) lookups were made to
                         fill knowledge gaps the Architect flagged.
    error              : The error message if status == FAILED.
    """
    # The sequential number of this micro loop since the orchestrator started.
    iteration: int
    # Unique identifier for the task that was processed.
    task_id: str
    # The architectural subsystem this task belongs to (e.g. "cache_layer").
    subsystem: str
    # Monotonic start time — used to compute duration, not for display.
    started_at: float
    # Set to None while the loop is still running, then filled on completion.
    finished_at: Optional[float] = None
    # Starts as RUNNING, updated to SUCCESS or FAILED at the end.
    status: LoopStatus = LoopStatus.RUNNING
    # Token counts from each AI call — useful for billing / cost monitoring.
    architect_tokens: int = 0
    critic_tokens: int = 0
    # The ID of the artifact stored in memory when the loop succeeds.
    artifact_id: Optional[str] = None
    # Number of new tasks the task engine queued as follow-ups to this one.
    new_tasks_generated: int = 0
    # How many Tool Layer lookups were triggered to fill knowledge gaps.
    researcher_calls: int = 0
    # Human-readable error string, populated only when status == FAILED.
    error: Optional[str] = None

    def duration(self) -> float:
        """
        Return how long this loop ran (or has been running) in seconds.

        If the loop hasn't finished yet (``finished_at`` is None), we compute
        the elapsed time from ``started_at`` to right now using monotonic time.
        Once finished, we return the fixed duration.
        """
        if self.finished_at is None:
            # Loop is still in progress — measure from start to now.
            return time.monotonic() - self.started_at
        # Loop is done — use the recorded end time for an exact measurement.
        return self.finished_at - self.started_at


@dataclass
class MesoLoopRecord:
    """
    A complete record of one meso-synthesis run.

    The meso loop fires after a subsystem has accumulated enough micro-loop
    artifacts.  It asks the Synthesizer AI to read those artifacts and produce
    a coherent subsystem-level design document.

    Fields
    ------
    subsystem            : Which subsystem was synthesised (e.g. "messaging").
    trigger_iteration    : Which micro-loop iteration triggered this meso run.
                           Useful for correlating the meso result with the
                           micro history.
    started_at           : Monotonic start time.
    artifacts_synthesised: How many artifacts the synthesizer read.
    finished_at          : Monotonic end time (None while running).
    status               : SUCCESS, FAILED, or RUNNING.
    document_id          : The ID of the subsystem design document stored in
                           memory on success.
    error                : Error message if status == FAILED.
    """
    subsystem: str
    # The micro-loop iteration count at the moment this meso run was triggered.
    trigger_iteration: int
    started_at: float
    # Filled in after we fetch artifacts from memory.
    artifacts_synthesised: int = 0
    finished_at: Optional[float] = None
    status: LoopStatus = LoopStatus.RUNNING
    # The memory store ID of the resulting subsystem design document.
    document_id: Optional[str] = None
    error: Optional[str] = None


@dataclass
class MacroLoopRecord:
    """
    A complete record of one macro architectural snapshot.

    The macro loop fires on a wall-clock timer (every 4 hours by default).
    It collects all subsystem design documents produced by meso loops, asks
    the Synthesizer AI to write a system-wide architectural summary, and
    commits that summary to the architecture state manager (e.g. Git).

    Fields
    ------
    snapshot_version  : A monotonically increasing version number (1, 2, 3,
                        ...) for the architectural snapshot.
    trigger_iteration : Which micro-loop iteration was active when the macro
                        timer fired.
    started_at        : Monotonic start time.
    finished_at       : Monotonic end time (None while running).
    status            : SUCCESS, FAILED, or RUNNING.
    commit_hash       : The short Git commit hash (or equivalent) returned by
                        the architecture state manager.  None on failure.
    error             : Error message if status == FAILED.
    """
    # Sequential version number for this architectural snapshot (1-based).
    snapshot_version: int
    # The total micro-loop count when this snapshot was triggered.
    trigger_iteration: int
    started_at: float
    finished_at: Optional[float] = None
    status: LoopStatus = LoopStatus.RUNNING
    # Short hash returned by arch_state_manager.commit() on success.
    commit_hash: Optional[str] = None
    error: Optional[str] = None


@dataclass
class OrchestratorState:
    """
    The single, authoritative record of everything the orchestrator knows
    about itself right now.

    This object lives in memory inside the ``Orchestrator`` instance and is
    updated after every loop iteration.  It is also serialised to a JSON file
    on disk after every micro loop so that the Dashboard (a separate process)
    can read it without needing shared memory or inter-process locks.

    Design principle: write-once atomicity
    ---------------------------------------
    The state is written to a temporary file and then *renamed* onto the target
    path.  On Unix, ``rename`` is atomic — the Dashboard either reads the old
    snapshot or the new one, never a half-written mixture.  See
    ``write_snapshot()`` for the implementation.

    Uptime and wall-clock time
    --------------------------
    ``started_at`` is a monotonic timestamp (for duration calculations).
    ``wall_start`` is the real wall-clock time (for human display).
    Together they let us convert any monotonic timestamp to a displayable
    wall-clock time in ``to_dict()``.

    History capping
    ---------------
    We keep the last 100 micro records, 50 meso records, and 20 macro records.
    This keeps the JSON file small even after many hours of operation.  When
    reporting to the Dashboard we further trim to the most recent 10/5/3.
    """

    # ── Identity / uptime ───────────────────────────────────────────────────

    # Monotonic timestamp at the moment the orchestrator was created.
    # Used to compute uptime.  NOT a wall-clock time — see wall_start.
    started_at: float = field(default_factory=time.monotonic)

    # Real wall-clock Unix timestamp at the moment the orchestrator started.
    # Combined with started_at, this lets us convert any monotonic time to
    # a real-world timestamp: wall_time = wall_start + (mono_time - started_at)
    wall_start: float = field(default_factory=time.time)

    # ── Loop counters ────────────────────────────────────────────────────────

    # Cumulative number of micro loops completed (or attempted) since startup.
    total_micro_loops: int = 0
    # Cumulative number of meso syntheses completed since startup.
    total_meso_loops: int = 0
    # Cumulative number of macro snapshots committed since startup.
    total_macro_loops: int = 0

    # How many micro loops have failed *in a row* (resets to 0 on success).
    # When this hits config.max_consecutive_failures, the orchestrator sleeps
    # before trying again — a simple form of exponential-backoff-lite.
    consecutive_failures: int = 0

    # ── Per-subsystem tracking (for meso trigger) ────────────────────────────

    # Maps subsystem name → number of successful micro loops on that subsystem
    # since the last meso synthesis.  When any value reaches
    # config.meso_trigger_count, a meso loop fires and the counter resets to 0.
    # Example: {"api_gateway": 3, "auth_service": 5}
    subsystem_micro_counts: dict[str, int] = field(default_factory=dict)

    # ── Current activity ─────────────────────────────────────────────────────

    # Which level of the hierarchy is active right now.
    current_level: LoopLevel = LoopLevel.IDLE
    # The task ID currently being processed (None between loops).
    current_task_id: Optional[str] = None
    # The subsystem of the current task (None between loops).
    current_subsystem: Optional[str] = None

    # ── History ──────────────────────────────────────────────────────────────

    # Rolling window of the last 100 micro loop records.
    micro_history: list[MicroLoopRecord] = field(default_factory=list)
    # Rolling window of the last 50 meso loop records.
    meso_history: list[MesoLoopRecord] = field(default_factory=list)
    # Rolling window of the last 20 macro loop records.
    macro_history: list[MacroLoopRecord] = field(default_factory=list)

    # ── Macro timer ──────────────────────────────────────────────────────────

    # Monotonic timestamp of the most recent macro loop start.
    # Initialised to "right now" so the first macro runs after a full interval,
    # not immediately at startup.
    last_macro_at: float = field(default_factory=time.monotonic)

    # ── Shutdown ─────────────────────────────────────────────────────────────

    # Set to True when shutdown has been requested (via signal or API call).
    # The main loop checks this flag between iterations.
    shutdown_requested: bool = False

    # Overall orchestrator status — RUNNING normally, SHUTDOWN on exit.
    status: LoopStatus = LoopStatus.RUNNING

    # ── Helpers ──────────────────────────────────────────────────────────────

    def increment_subsystem(self, subsystem: str) -> int:
        """
        Add 1 to the micro-loop counter for ``subsystem`` and return the new
        value.

        This is called by the orchestrator after every successful micro loop.
        When the returned value reaches ``config.meso_trigger_count``, the
        orchestrator knows it's time to run a meso synthesis for this subsystem.

        Example:
            state.increment_subsystem("api_gateway")  # returns 1
            state.increment_subsystem("api_gateway")  # returns 2
            ...
            state.increment_subsystem("api_gateway")  # returns 5 → meso fires
        """
        # dict.get(key, 0) safely returns 0 if the key doesn't exist yet,
        # so we don't need a separate "if subsystem not in dict" check.
        self.subsystem_micro_counts[subsystem] = (
            self.subsystem_micro_counts.get(subsystem, 0) + 1
        )
        return self.subsystem_micro_counts[subsystem]

    def reset_subsystem_count(self, subsystem: str) -> None:
        """
        Reset the micro-loop counter for ``subsystem`` to zero.

        Called by the meso loop after a successful synthesis, so the subsystem
        can accumulate another batch of micro-loop artifacts before the next
        synthesis fires.
        """
        self.subsystem_micro_counts[subsystem] = 0

    def add_micro_record(self, record: MicroLoopRecord) -> None:
        """
        Append a completed micro-loop record to the history list.

        To keep memory usage bounded (and the JSON snapshot file small), we
        cap the list at 100 entries.  When it would exceed that, we slice off
        the oldest entries, keeping only the most recent 100.
        """
        self.micro_history.append(record)
        # If the list has grown beyond 100 entries, drop the oldest ones.
        # ``self.micro_history[-100:]`` returns the last 100 elements.
        if len(self.micro_history) > 100:
            self.micro_history = self.micro_history[-100:]

    def add_meso_record(self, record: MesoLoopRecord) -> None:
        """
        Append a completed meso-loop record to the history list (max 50).

        Meso loops are less frequent than micro loops, so we keep fewer of
        them — 50 meso records is still many hours of history.
        """
        self.meso_history.append(record)
        if len(self.meso_history) > 50:
            self.meso_history = self.meso_history[-50:]

    def add_macro_record(self, record: MacroLoopRecord) -> None:
        """
        Append a completed macro-loop record to the history list (max 20).

        Macro loops run at most once every 4 hours, so 20 records represents
        at least 80 hours of history — plenty for debugging and auditing.
        """
        self.macro_history.append(record)
        if len(self.macro_history) > 20:
            self.macro_history = self.macro_history[-20:]

    def to_dict(self) -> dict:
        """
        Return a JSON-serialisable snapshot of the current state.

        This is what gets written to the Dashboard's state file.  A few
        transformations are applied:

        1. Monotonic timestamps are converted to wall-clock times.
           (``time.monotonic()`` values are meaningless outside this process;
           we convert them to real Unix timestamps the Dashboard can display.)

        2. History lists are trimmed to the most recent few entries so the
           JSON file stays small and quick to read.

        3. Enum values are unwrapped to plain strings (e.g. "micro" not
           <LoopLevel.MICRO: 'micro'>) so the JSON is human-readable.
        """
        now_mono = time.monotonic()
        # Uptime in seconds: how long since the orchestrator started.
        uptime = now_mono - self.started_at

        def _record(r):
            """
            Convert a single loop record (MicroLoopRecord, MesoLoopRecord, or
            MacroLoopRecord) to a plain dict, replacing the monotonic
            ``started_at`` with a real wall-clock timestamp for human display.
            """
            # asdict() recursively converts a dataclass to a plain dict,
            # including nested dataclasses and enums.
            d = asdict(r)
            # ``started_at`` is a monotonic value; convert it to a wall-clock
            # Unix timestamp by finding how far it is from our reference point.
            if "started_at" in d:
                d["started_at_wall"] = self.wall_start + (d["started_at"] - self.started_at)
            return d

        return {
            # How many seconds the orchestrator has been running.
            "uptime_seconds": uptime,
            # When the orchestrator started (Unix timestamp, for display).
            "wall_start": self.wall_start,
            # "running", "shutdown", etc.
            "status": self.status.value,
            # "micro", "meso", "macro", or "idle".
            "current_level": self.current_level.value,
            # The task currently being worked on (or None).
            "current_task_id": self.current_task_id,
            # The subsystem of the current task (or None).
            "current_subsystem": self.current_subsystem,
            # Summary counts for the Dashboard's header stats.
            "totals": {
                "micro": self.total_micro_loops,
                "meso": self.total_meso_loops,
                "macro": self.total_macro_loops,
                # Useful for alerting: if this is high, something is wrong.
                "consecutive_failures": self.consecutive_failures,
            },
            # Per-subsystem micro-loop counts since last meso synthesis.
            "subsystem_micro_counts": self.subsystem_micro_counts,
            "shutdown_requested": self.shutdown_requested,
            # Only expose the most recent history in the snapshot to keep
            # the file small — the full history lives in memory.
            "micro_history": [_record(r) for r in self.micro_history[-10:]],
            "meso_history": [_record(r) for r in self.meso_history[-5:]],
            "macro_history": [_record(r) for r in self.macro_history[-3:]],
        }

    def write_snapshot(self, path: str) -> None:
        """
        Atomically overwrite the Dashboard snapshot file at ``path``.

        "Atomic" means the Dashboard will never see a half-written file.  The
        technique used here is:

          1. Write the JSON to a *temporary* file in the same directory.
          2. Call ``os.replace(tmp, path)`` which on Unix is a single atomic
             ``rename`` syscall — the old file and new file swap in one step.
          3. If step 1 or 2 fails, delete the temporary file so we don't litter
             the filesystem with partial writes.

        Why not just ``open(path, 'w').write(...)``?
        That would leave a window where the file exists but is empty or
        incomplete.  If the Dashboard read during that window, it would crash.
        The temp-file-then-rename trick eliminates that window entirely.
        """
        # Imported here (not at the top) to keep the import visible next to
        # the code that uses it, which aids readability in a long file.
        import os, tempfile
        # Serialise the state to a JSON string with pretty-printing.
        data = json.dumps(self.to_dict(), indent=2)
        # We must create the temp file in the *same directory* as the target
        # path, because os.replace() only works within the same filesystem.
        dir_ = os.path.dirname(path) or "."
        # mkstemp creates the temp file and returns (file_descriptor, path).
        fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".json")
        try:
            # os.fdopen wraps the raw file descriptor in a Python file object
            # so we can use .write() on it.
            with os.fdopen(fd, "w") as f:
                f.write(data)
            # Atomic rename: this is the step that makes the new data "live".
            os.replace(tmp, path)
        except Exception:
            # Something went wrong — clean up the temp file so we don't leave
            # garbage behind, then re-raise so the caller knows it failed.
            try:
                os.unlink(tmp)
            except OSError:
                # If unlinking also fails (unlikely), just swallow the error —
                # the original exception is more important to surface.
                pass
            raise
