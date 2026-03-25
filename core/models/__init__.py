"""
core/models/
=======
Model Library, Preset Manager, and Ollama Discovery for Tinker.

This package lets you manage a local registry of AI models and define named
"presets" — task-specific configurations that designate which model fills
the Main (SERVER) slot and which fills the Judge (SECONDARY/Critic) slot.

The two active slots
--------------------
Tinker always uses exactly two model slots at runtime:

  Main  → the heavy-lifting model used by Architect, Researcher, Synthesizer.
           Mapped to the ``SERVER`` MachineConfig in core/llm/router.py.

  Judge → the fast review/critique model used by Critic.
           Mapped to the ``SECONDARY`` MachineConfig in core/llm/router.py.

Why presets?
------------
Different tasks call for different models.  A coding task benefits from a
code-specialized model in the Main slot; an architecture-planning task may
want a larger general-purpose model instead.  Rather than editing environment
variables and restarting, you switch a preset from the Dashboard and the
change takes effect at the next meso-loop boundary — no restart required.

Storage
-------
All state is persisted as plain JSON files in the working directory:

  tinker_models.json         — the model library (your available models)
  tinker_presets.json        — named presets
  tinker_active_preset.json  — the currently active preset name

These paths can be overridden via environment variables:
  TINKER_MODELS_FILE, TINKER_PRESETS_FILE, TINKER_ACTIVE_PRESET_FILE

Public API
----------
    from core.models import ModelLibrary, PresetManager, OllamaSync

    lib = ModelLibrary()
    mgr = PresetManager(lib)
    sync = OllamaSync(["http://localhost:11434"])
"""

from .library import ModelEntry, ModelLibrary
from .presets import ModelPreset, PresetManager
from .ollama_sync import OllamaSync

__all__ = [
    "ModelEntry",
    "ModelLibrary",
    "ModelPreset",
    "PresetManager",
    "OllamaSync",
]
