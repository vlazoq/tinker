"""
core/models/library.py
=================
ModelEntry dataclass and ModelLibrary — the local registry of available models.

A "model library" is just a JSON file on disk that lists every AI model you
have pulled and want Tinker to be able to use.  You add models once (either
manually or via the Ollama discovery sync), and then reference them by their
``id`` when building presets.

All models are local-first: they point to an Ollama instance on localhost or
your LAN.  There is no built-in concept of cloud API credentials here.

File format (tinker_models.json)
---------------------------------
::

    {
      "version": 1,
      "models": [
        {
          "id": "qwen3-7b-local",
          "model_tag": "qwen3:7b",
          "display_name": "Qwen 3 7B (Local)",
          "ollama_url": "http://localhost:11434",
          "context_window": 32768,
          "notes": "Good all-rounder, fast on 3090",
          "capabilities": ["coding", "reasoning"]
        }
      ]
    }

Environment variable
---------------------
``TINKER_MODELS_FILE`` — override the default ``./tinker_models.json`` path.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_LIBRARY_VERSION = 1
_DEFAULT_FILE = Path(os.getenv("TINKER_MODELS_FILE", "./tinker_models.json"))


@dataclass
class ModelEntry:
    """
    One model available for use in Tinker.

    Fields
    ------
    id            : Unique slug you choose (e.g. ``"qwen3-7b-local"``).
                    Used as the reference key in presets.
    model_tag     : Ollama model identifier (e.g. ``"qwen3:7b"``).
                    This is the value passed to Ollama's API.
    display_name  : Human-readable label shown in the Dashboard.
    ollama_url    : Base URL of the Ollama instance that has this model.
                    Typically ``http://localhost:11434`` or a LAN address.
    context_window: Maximum tokens the model accepts per request.
                    Used to set ``MachineConfig.context_window`` on hot-reload.
    notes         : Free-form text for your own reference.
    capabilities  : Optional list of tags (e.g. ``["coding", "fast"]``).
                    Shown as badges in the Dashboard.  Not used by the engine.
    """

    id: str
    model_tag: str
    display_name: str
    ollama_url: str = "http://localhost:11434"
    context_window: int = 8192
    notes: str = ""
    capabilities: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ModelEntry":
        return cls(
            id=d["id"],
            model_tag=d["model_tag"],
            display_name=d.get("display_name", d["model_tag"]),
            ollama_url=d.get("ollama_url", "http://localhost:11434"),
            context_window=int(d.get("context_window", 8192)),
            notes=d.get("notes", ""),
            capabilities=list(d.get("capabilities", [])),
        )


class ModelLibrary:
    """
    Persistent registry of available models.

    Loads and saves a JSON file.  All mutations (add/update/remove) are
    written atomically so a crash mid-write does not corrupt the file.

    Parameters
    ----------
    path : Path to the JSON library file.  Defaults to ``TINKER_MODELS_FILE``
           env var or ``./tinker_models.json``.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path else _DEFAULT_FILE
        self._models: dict[str, ModelEntry] = {}
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            logger.debug("ModelLibrary: no file at %s, starting empty", self._path)
            self._seed_defaults()
            return
        try:
            data = json.loads(self._path.read_text())
            for m in data.get("models", []):
                entry = ModelEntry.from_dict(m)
                self._models[entry.id] = entry
            logger.info(
                "ModelLibrary: loaded %d models from %s", len(self._models), self._path
            )
        except Exception as exc:
            logger.error("ModelLibrary: failed to load %s: %s", self._path, exc)

    def save(self) -> None:
        """Write the library to disk atomically."""
        data = {
            "version": _LIBRARY_VERSION,
            "models": [m.to_dict() for m in self._models.values()],
        }
        self._atomic_write(data)

    def _atomic_write(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, str(self._path))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _seed_defaults(self) -> None:
        """Populate a sensible starting library if no file exists."""
        defaults = [
            ModelEntry(
                id="qwen3-7b-local",
                model_tag="qwen3:7b",
                display_name="Qwen 3 7B (Local)",
                ollama_url="http://localhost:11434",
                context_window=32768,
                notes="Good all-rounder. Default Tinker server model.",
                capabilities=["reasoning", "general"],
            ),
            ModelEntry(
                id="phi3-mini-local",
                model_tag="phi3:mini",
                display_name="Phi-3 Mini (Local)",
                ollama_url="http://localhost:11434",
                context_window=4096,
                notes="Fast and lightweight. Default Judge/Critic model.",
                capabilities=["fast", "review"],
            ),
            ModelEntry(
                id="qwen25-coder-7b-local",
                model_tag="qwen2.5-coder:7b",
                display_name="Qwen 2.5 Coder 7B (Local)",
                ollama_url="http://localhost:11434",
                context_window=32768,
                notes="Code-specialised 7B. Good Main for coding tasks.",
                capabilities=["coding", "fast"],
            ),
            ModelEntry(
                id="qwen25-coder-32b-local",
                model_tag="qwen2.5-coder:32b",
                display_name="Qwen 2.5 Coder 32B (Local)",
                ollama_url="http://localhost:11434",
                context_window=32768,
                notes="Heavy code model. Requires high-VRAM GPU.",
                capabilities=["coding", "large"],
            ),
        ]
        for e in defaults:
            self._models[e.id] = e
        self.save()
        logger.info("ModelLibrary: seeded %d default entries", len(defaults))

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def all(self) -> list[ModelEntry]:
        """Return all models, sorted by display_name."""
        return sorted(self._models.values(), key=lambda m: m.display_name.lower())

    def get(self, model_id: str) -> Optional[ModelEntry]:
        return self._models.get(model_id)

    def add(self, entry: ModelEntry) -> None:
        """Add or replace a model entry, then save."""
        self._models[entry.id] = entry
        self.save()
        logger.info("ModelLibrary: added/updated %s (%s)", entry.id, entry.model_tag)

    def remove(self, model_id: str) -> bool:
        """Remove a model by id.  Returns True if it existed."""
        if model_id not in self._models:
            return False
        del self._models[model_id]
        self.save()
        logger.info("ModelLibrary: removed %s", model_id)
        return True

    def ids(self) -> list[str]:
        return list(self._models.keys())

    def __len__(self) -> int:
        return len(self._models)
