"""
grub/contracts/__init__.py
==========================
Public re-exports for the contracts package.

'Contracts' are the data shapes that Grub and every Minion agree on.
Think of them as the API between the orchestrator and its sub-agents:
  - GrubTask   : what Grub hands to a Minion ("here's what to implement")
  - MinionResult: what a Minion hands back ("here's what I produced")

Keeping these in a separate package means neither Grub nor any Minion
imports from each other directly — they only import from contracts/.
This is the 'dependency inversion' principle: depend on abstractions,
not on concrete implementations.
"""

from .task import GrubTask, TaskPriority
from .result import MinionResult, ResultStatus

__all__ = [
    "GrubTask",
    "TaskPriority",
    "MinionResult",
    "ResultStatus",
]
