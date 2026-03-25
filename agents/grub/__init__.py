"""
agents/grub/__init__.py
================
Grub — the implementation agent for the Tinker system.

Grub is Phase 2 of the Tinker pipeline:
  Tinker (Phase 1): Design and architecture thinking
  Grub   (Phase 2): Code implementation from Tinker's designs

Quick start
-----------
::

    from agents.grub.agent  import GrubAgent
    from agents.grub.config import GrubConfig

    # Load config from grub_config.json (created on first run)
    agent = GrubAgent.from_config()
    await agent.run()

See docs/tutorial/15-grub-overview.md for the full tutorial.
"""

from .agent import GrubAgent
from .config import GrubConfig
from .registry import MinionRegistry

__version__ = "0.1.0"

__all__ = [
    "GrubAgent",
    "GrubConfig",
    "MinionRegistry",
]
