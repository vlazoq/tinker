"""
tinker/task_engine/__init__.py
───────────────────────────────
Public surface of the Task Engine.

Import everything you need from here:

    from tinker.task_engine import (
        Task, TaskType, TaskStatus, Subsystem,
        TaskRegistry,
        TaskGenerator,
        PriorityScorer, ScorerWeights,
        DependencyResolver, DependencyCycleError,
        TaskQueue,
    )
"""

from .schema import Task, TaskStatus, TaskType, Subsystem
from .registry import TaskRegistry
from .generator import TaskGenerator
from .scorer import PriorityScorer, ScorerWeights
from .resolver import DependencyResolver, DependencyCycleError
from .queue import TaskQueue

__all__ = [
    # Schema
    "Task",
    "TaskStatus",
    "TaskType",
    "Subsystem",
    # Components
    "TaskRegistry",
    "TaskGenerator",
    "PriorityScorer",
    "ScorerWeights",
    "DependencyResolver",
    "DependencyCycleError",
    "TaskQueue",
]
