"""
core/models/presets.py
=================
ModelPreset dataclass and PresetManager — named task-mode configurations.

A preset says "for task X, use model A as Main and model B as Judge."
Activating a preset writes ``tinker_active_preset.json`` to disk; the
orchestrator picks that up at its next meso-loop boundary and hot-reloads
the ModelRouter without a process restart.

File formats
------------
``tinker_presets.json`` — all saved presets::

    {
      "version": 1,
      "presets": [
        {
          "name": "coding",
          "display_name": "Coding",
          "description": "Code-specialised Main, fast Judge",
          "main_model_id": "qwen25-coder-32b-local",
          "judge_model_id": "phi3-mini-local",
          "grub_overrides": {"coder": "qwen2.5-coder:32b"},
          "notes": ""
        }
      ]
    }

``tinker_active_preset.json`` — currently active preset::

    {"preset_name": "coding", "activated_at": "2026-03-23T12:00:00"}

Environment variables
---------------------
``TINKER_PRESETS_FILE``       — override ``./tinker_presets.json``
``TINKER_ACTIVE_PRESET_FILE`` — override ``./tinker_active_preset.json``
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC
from pathlib import Path

from .library import ModelLibrary

logger = logging.getLogger(__name__)

_PRESETS_VERSION = 1
_DEFAULT_PRESETS_FILE = Path(os.getenv("TINKER_PRESETS_FILE", "./tinker_presets.json"))
_DEFAULT_ACTIVE_FILE = Path(os.getenv("TINKER_ACTIVE_PRESET_FILE", "./tinker_active_preset.json"))


@dataclass
class ModelPreset:
    """
    A named task-mode configuration.

    Fields
    ------
    name          : Unique slug (e.g. ``"coding"``).
    display_name  : Human-readable label for the Dashboard.
    description   : Short sentence explaining when to use this preset.
    main_model_id : ID of the ModelEntry to use as the Main (SERVER) slot.
    judge_model_id: ID of the ModelEntry to use as the Judge (SECONDARY) slot.
    grub_overrides: Optional dict mapping Grub minion names to Ollama model
                    tags.  Example: ``{"coder": "qwen2.5-coder:32b"}``.
                    If empty, Grub keeps using its own config unchanged.
    notes         : Free-form text for your reference.
    """

    name: str
    display_name: str
    description: str = ""
    main_model_id: str = ""
    judge_model_id: str = ""
    grub_overrides: dict[str, str] = field(default_factory=dict)
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> ModelPreset:
        return cls(
            name=d["name"],
            display_name=d.get("display_name", d["name"].title()),
            description=d.get("description", ""),
            main_model_id=d.get("main_model_id", ""),
            judge_model_id=d.get("judge_model_id", ""),
            grub_overrides=dict(d.get("grub_overrides", {})),
            notes=d.get("notes", ""),
        )


class PresetManager:
    """
    Manages named model presets and the active preset selection.

    Parameters
    ----------
    library         : ModelLibrary instance for looking up model entries.
    presets_path    : Path to the presets JSON file.
    active_path     : Path to the active-preset JSON file.
    """

    def __init__(
        self,
        library: ModelLibrary,
        presets_path: Path | str | None = None,
        active_path: Path | str | None = None,
    ) -> None:
        self._library = library
        self._presets_path = Path(presets_path) if presets_path else _DEFAULT_PRESETS_FILE
        self._active_path = Path(active_path) if active_path else _DEFAULT_ACTIVE_FILE
        self._presets: dict[str, ModelPreset] = {}
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._presets_path.exists():
            logger.debug("PresetManager: no presets file, seeding defaults")
            self._seed_defaults()
            return
        try:
            data = json.loads(self._presets_path.read_text())
            for p in data.get("presets", []):
                preset = ModelPreset.from_dict(p)
                self._presets[preset.name] = preset
            logger.info(
                "PresetManager: loaded %d presets from %s",
                len(self._presets),
                self._presets_path,
            )
        except Exception as exc:
            logger.error("PresetManager: failed to load %s: %s", self._presets_path, exc)

    def _save_presets(self) -> None:
        data = {
            "version": _PRESETS_VERSION,
            "presets": [p.to_dict() for p in self._presets.values()],
        }
        self._atomic_write(self._presets_path, data)

    def _atomic_write(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, str(path))
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def _seed_defaults(self) -> None:
        """Create four sensible default presets on first run."""
        lib_ids = set(self._library.ids())

        def _pick(preferred: str, fallback: str) -> str:
            return preferred if preferred in lib_ids else fallback

        defaults = [
            ModelPreset(
                name="balanced",
                display_name="Balanced",
                description="General-purpose: mid-size Main, fast Judge. Good default.",
                main_model_id=_pick("qwen3-7b-local", ""),
                judge_model_id=_pick("phi3-mini-local", ""),
                notes="Default active preset.",
            ),
            ModelPreset(
                name="coding",
                display_name="Coding",
                description="Code-specialised Main for implementation tasks.",
                main_model_id=_pick("qwen25-coder-32b-local", "qwen25-coder-7b-local"),
                judge_model_id=_pick("phi3-mini-local", ""),
                grub_overrides={
                    "coder": "qwen2.5-coder:32b",
                    "debugger": "qwen2.5-coder:32b",
                },
                notes="Switch to this for coding/implementation micro loops.",
            ),
            ModelPreset(
                name="architecting",
                display_name="Architecting",
                description="Larger reasoning model for architecture and design tasks.",
                main_model_id=_pick("qwen3-7b-local", ""),
                judge_model_id=_pick("qwen25-coder-7b-local", "phi3-mini-local"),
                notes="Use when the task is high-level design or planning.",
            ),
            ModelPreset(
                name="lightweight",
                display_name="Lightweight",
                description="Both slots use fast small models. Good for slow hardware.",
                main_model_id=_pick("qwen25-coder-7b-local", "phi3-mini-local"),
                judge_model_id=_pick("phi3-mini-local", ""),
                notes="Useful when the GPU is busy or on lower-end hardware.",
            ),
        ]
        for p in defaults:
            self._presets[p.name] = p
        self._save_presets()
        # Activate "balanced" by default if no active preset exists
        if not self._active_path.exists():
            self.activate("balanced")
        logger.info("PresetManager: seeded %d default presets", len(defaults))

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def all(self) -> list[ModelPreset]:
        return list(self._presets.values())

    def get(self, name: str) -> ModelPreset | None:
        return self._presets.get(name)

    def add(self, preset: ModelPreset) -> None:
        """Add or replace a preset, then save."""
        self._presets[preset.name] = preset
        self._save_presets()
        logger.info("PresetManager: saved preset '%s'", preset.name)

    def remove(self, name: str) -> bool:
        """Remove a preset.  Returns False if it did not exist."""
        if name not in self._presets:
            return False
        del self._presets[name]
        self._save_presets()
        logger.info("PresetManager: removed preset '%s'", name)
        return True

    # ── Active preset ─────────────────────────────────────────────────────────

    def activate(self, name: str) -> ModelPreset:
        """
        Set the active preset and write ``tinker_active_preset.json``.

        The orchestrator watches this file's mtime between meso loops.
        When it changes, the ModelRouter is hot-reloaded with the new models.

        Raises
        ------
        KeyError : if ``name`` is not a known preset.
        """
        if name not in self._presets:
            raise KeyError(f"Unknown preset: '{name}'")
        from datetime import datetime

        data = {
            "preset_name": name,
            "activated_at": datetime.now(UTC).isoformat(),
        }
        self._atomic_write(self._active_path, data)
        logger.info("PresetManager: activated preset '%s'", name)
        return self._presets[name]

    def active_name(self) -> str | None:
        """Return the name of the currently active preset, or None."""
        if not self._active_path.exists():
            return None
        try:
            data = json.loads(self._active_path.read_text())
            return data.get("preset_name")
        except Exception:
            return None

    def active_preset(self) -> ModelPreset | None:
        """Return the active ModelPreset object, or None."""
        name = self.active_name()
        return self._presets.get(name) if name else None

    def active_file_mtime(self) -> float:
        """Return mtime of the active-preset file, or 0.0 if it doesn't exist."""
        try:
            return self._active_path.stat().st_mtime
        except OSError:
            return 0.0

    def resolved_active(self) -> dict:
        """
        Return the active preset with model entries resolved from the library.

        Used by the Dashboard to show full model details (tag, URL, context)
        without a separate library lookup.
        """
        preset = self.active_preset()
        if preset is None:
            return {"preset": None, "main": None, "judge": None}
        main = self._library.get(preset.main_model_id)
        judge = self._library.get(preset.judge_model_id)
        return {
            "preset": preset.to_dict(),
            "main": main.to_dict() if main else None,
            "judge": judge.to_dict() if judge else None,
        }
