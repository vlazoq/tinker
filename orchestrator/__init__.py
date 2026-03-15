"""
orchestrator/__init__.py
========================

This file is the "front door" of the orchestrator package.

What is a Python package?
--------------------------
When Python sees a folder that contains a file called ``__init__.py``, it
treats the whole folder as a single importable unit — a *package*.  This
``__init__.py`` runs automatically the moment someone writes:

    import orchestrator
    # or
    from orchestrator import Orchestrator

What does this file do?
------------------------
Two things, both very simple:

1. **Re-exports the two most important names** so that callers can import them
   directly from ``orchestrator`` instead of having to know which sub-file
   they live in.  For example, you can write:

       from orchestrator import Orchestrator

   instead of the longer:

       from orchestrator.orchestrator import Orchestrator

2. **Declares the public API** using the ``__all__`` list.  ``__all__`` tells
   tools like auto-documentation generators and linters *"these are the names
   you're allowed to use from outside this package; everything else is an
   implementation detail."*

Where does the orchestrator fit in Tinker?
------------------------------------------
Tinker is an AI system that continuously improves a software architecture by
running three nested reasoning loops:

  Micro loop  — the smallest unit of work.  Picks one task, runs it through
                the Architect AI (which proposes a design) then the Critic AI
                (which evaluates it), stores the result, and generates new
                tasks.  Runs as fast as possible, hundreds of times per hour.

  Meso loop   — a "mid-level reflection".  After a subsystem has been worked
                on several times by micro loops, the Synthesizer AI reads all
                those small artifacts and produces a coherent subsystem-level
                design document.  Runs every few micro loops per subsystem.

  Macro loop  — the "big picture".  Every few hours, the Synthesizer reads
                ALL subsystem documents and produces a snapshot of the entire
                architecture.  This is committed to version control so humans
                can track Tinker's reasoning over time.

The ``Orchestrator`` class in ``orchestrator.py`` is the engine that drives
all three loops.  ``OrchestratorConfig`` (in ``config.py``) is the single
place where every tunable number (timeouts, trigger counts, sleep durations)
lives.

Quick-start example
-------------------
    from orchestrator import Orchestrator, OrchestratorConfig
    from orchestrator.stubs import build_stub_components

    # Build a set of simple in-memory stand-ins for the real AI components.
    components = build_stub_components()

    # Create the orchestrator, injecting all components it needs.
    orch = Orchestrator(config=OrchestratorConfig(), **components)

    # Run forever (until Ctrl-C or orch.request_shutdown() is called).
    import asyncio
    asyncio.run(orch.run())
"""

# Pull Orchestrator up to the top level of the package so callers don't need
# to know it lives in orchestrator/orchestrator.py.
from .orchestrator import Orchestrator

# Pull OrchestratorConfig up too — callers nearly always need both together.
from .config import OrchestratorConfig

# __all__ is the official "public surface" of this package.
# Static analysis tools, documentation generators, and "from orchestrator import *"
# all respect this list.  If a name isn't here, it's considered private.
__all__ = ["Orchestrator", "OrchestratorConfig"]
