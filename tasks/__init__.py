"""
tasks/__init__.py — Public surface of the Task Engine package
=============================================================

What this file does
--------------------
This is the "front door" of the tasks package.  Instead of forcing every
caller to know exactly which sub-module a class lives in, this file imports
everything important and re-exports it in one flat namespace.

That means the rest of Tinker can write:

    from tasks import Task, TaskEngine, TaskQueue

…instead of hunting through five different files for the right import.

How the Task Engine fits into Tinker
--------------------------------------
Tinker is an autonomous AI that continuously thinks about software
architecture.  To keep that thinking organised, all work is broken into
**Tasks** — small, well-defined units like "research vector index options"
or "design the API gateway layer".

The Task Engine is the subsystem that manages those tasks from birth to
retirement:

  1. ``schema.py``    — defines what a Task *is* (its data shape).
  2. ``registry.py``  — stores tasks permanently in a SQLite database.
  3. ``scorer.py``    — assigns each waiting task a numeric priority score.
  4. ``resolver.py``  — tracks which tasks are blocked by other tasks.
  5. ``queue.py``     — picks the next task to work on (using scores +
                        a small random slot for "exploration" tasks).
  6. ``generator.py`` — turns the AI's JSON output into new Task objects.
  7. ``engine.py``    — a single façade that glues all of the above together
                        behind a clean async API for the Orchestrator.

Everything in ``__all__`` below is part of the public contract.  Anything
not listed here is considered an internal implementation detail.
"""

# ── Schema types ──────────────────────────────────────────────────────────────
# These are the data shapes everything else is built on.
# Import them first so callers can type-annotate their own code.
from .schema import Task, TaskStatus, TaskType, Subsystem

# ── Core components ───────────────────────────────────────────────────────────
# Each of these is a self-contained class; they collaborate through the engine.
from .abstract_registry import AbstractTaskRegistry  # Backend interface (ABC)
from .registry import SQLiteTaskRegistry, TaskRegistry  # SQLite store
from .postgres_registry import PostgresTaskRegistry  # PostgreSQL store
from .registry_factory import create_task_registry  # Backend factory
from .generator import TaskGenerator  # Parses AI output → new Tasks
from .scorer import PriorityScorer, ScorerWeights  # Priority score
from .resolver import DependencyResolver, DependencyCycleError  # Deps
from .queue import TaskQueue  # Selects the next task to run

# ── Top-level façade ──────────────────────────────────────────────────────────
# TaskEngine is the only thing the Orchestrator normally needs to touch.
from .engine import TaskEngine

# ── Public API declaration ────────────────────────────────────────────────────
# Listing names in __all__ is a convention that signals "these are the exports
# this package is responsible for".  Tools like linters and documentation
# generators use this list to know what's intentionally public.
__all__ = [
    # --- Schema: the data building blocks ------------------------------------
    "Task",  # The main unit of work (a dataclass with many fields)
    "TaskStatus",  # Enum: PENDING, ACTIVE, COMPLETE, BLOCKED, FAILED, …
    "TaskType",  # Enum: DESIGN, RESEARCH, CRITIQUE, SYNTHESIS, …
    "Subsystem",  # Enum: which part of Tinker a task belongs to
    # --- Registry backends ---------------------------------------------------
    "AbstractTaskRegistry",  # ABC: contract every backend must implement
    "SQLiteTaskRegistry",  # Default: single-file SQLite (zero dependencies)
    "PostgresTaskRegistry",  # Optional: shared PostgreSQL store
    "TaskRegistry",  # Alias for SQLiteTaskRegistry (backwards compat)
    "create_task_registry",  # Factory: picks backend from env / argument
    # --- Components: the specialised workers ---------------------------------
    "TaskGenerator",  # Turn AI architect output into Task objects
    "PriorityScorer",  # Score a task → float in [0, 1]
    "ScorerWeights",  # Configuration knobs for the scorer
    "DependencyResolver",  # Block/unblock tasks based on their deps
    "DependencyCycleError",  # Exception raised when a dep cycle is found
    "TaskQueue",  # Pick the highest-priority task to run next
    # --- Façade: the single entry point for external code --------------------
    "TaskEngine",  # Async wrapper used by the Orchestrator
]
