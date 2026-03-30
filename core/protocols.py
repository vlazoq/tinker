"""
core/protocols.py
=================

Structural protocols (interfaces) for Tinker's core infrastructure components.

Why protocols?
--------------
The orchestrator depends on a TaskEngine and a ContextAssembler, but it should
not be coupled to the concrete implementations in ``runtime/tasks/engine.py``
and ``core/context/assembler.py``.  By depending on protocols instead of
classes, we gain:

  * **Substitutability** — swap any component for a different implementation
    (an in-memory stub, a remote service, a test double) without touching
    the orchestrator or bootstrap layer.

  * **Testability** — unit-test the orchestrator with a lightweight mock that
    satisfies the protocol, rather than a full TaskEngine backed by SQLite.

  * **Documentation** — the protocol is a single source of truth for what
    the orchestrator expects from each component.

Usage
-----
The orchestrator and loop functions receive injected components.  Type hints
should reference the protocol, not the concrete class::

    def __init__(self, task_engine: TaskEngineProtocol, ...):
        ...

To verify that a concrete class satisfies the protocol at runtime::

    assert isinstance(engine, TaskEngineProtocol)

Relationship to concrete classes
---------------------------------
``TaskEngine``        → satisfies ``TaskEngineProtocol``
``ContextAssembler``  → satisfies ``ContextAssemblerProtocol``
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TaskEngineProtocol(Protocol):
    """
    Protocol for the task engine used by the orchestrator.

    The task engine manages the lifecycle of tasks: selecting the next task
    to work on, marking tasks as complete or failed, generating child tasks
    from architect output, and injecting exploration tasks when stagnation
    is detected.

    All methods are async so the orchestrator can await them uniformly.
    """

    async def select_task(self) -> dict | None:
        """
        Return the highest-priority pending task as a plain dict, or None
        if there are no tasks available.
        """
        ...

    async def complete_task(
        self,
        task_id: str,
        artifact_id: str | None = None,
        outputs: list[str] | None = None,
        tokens_used: int = 0,
        duration_seconds: float = 0.0,
    ) -> None:
        """
        Mark a task as complete.

        Parameters
        ----------
        task_id          : ID of the task to complete.
        artifact_id      : Single artifact produced (micro loop convention).
        outputs          : List of output identifiers (queue convention).
        tokens_used      : LLM tokens consumed while completing this task.
        duration_seconds : Wall-clock time taken to complete this task.
        """
        ...

    async def fail_task(
        self,
        task_id: str,
        reason: str = "",
    ) -> None:
        """
        Mark a task as failed with an optional reason.

        Called by the orchestrator when an agent produces an unusable result
        or an exception is raised during execution.
        """
        ...

    async def generate_tasks(
        self,
        parent_task: dict,
        architect_result: dict,
        critic_result: dict,
    ) -> list[dict]:
        """
        Parse the architect's output and enqueue new child tasks.

        Returns the new tasks as plain dicts in orchestrator format.
        """
        ...

    async def enqueue_exploration_task(
        self,
        title: str = "Explore an under-researched architectural area",
        description: str = "",
        subsystem: Any = None,
    ) -> dict:
        """
        Create and immediately enqueue an exploration task.

        Called by the orchestrator when the stagnation monitor fires a
        SPAWN_EXPLORATION or ESCALATE_LOOP directive.

        Returns the newly queued task in orchestrator-dict format.
        """
        ...


@runtime_checkable
class ContextAssemblerProtocol(Protocol):
    """
    Protocol for the context assembler used by the orchestrator.

    The context assembler translates the current task and memory state into
    a token-budgeted prompt dict that agents can consume.  The orchestrator
    calls ``build()`` once per micro-loop iteration.
    """

    async def build(
        self,
        task: dict,
        max_artifacts: int = 10,
        role: Any = None,
    ) -> dict:
        """
        Build a context dict for the given task.

        Parameters
        ----------
        task          : Raw task dict from the task engine.
        max_artifacts : Hint for how many prior artifacts to surface.
        role          : The agent role to build context for (AgentRole enum).
                        Defaults to ARCHITECT when not supplied.

        Returns
        -------
        dict with at minimum ``{"task": dict, "prompt": str, ...}``.
        """
        ...
