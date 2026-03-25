"""
grub/config.py
==============
All configuration for the Grub system in one place.

How to switch execution modes
------------------------------
Change ``execution_mode`` in grub_config.json (or set env var GRUB_EXEC_MODE):

  "sequential"  — Option A: one minion at a time (DEFAULT).
                  Best for: single PC, limited VRAM, simplest setup.
                  No extra dependencies.

  "parallel"    — Option B: multiple minions run concurrently as async tasks.
                  Best for: multi-core CPU, when different minions use
                  different models so VRAM isn't doubled.
                  No extra dependencies (uses asyncio).

  "queue"       — Option C: SQLite-backed task queue, minions are workers.
                  Best for: multi-machine setup (your 3090 PC + daily PC),
                  or when you want to add/remove workers without restarting.
                  Requires: nothing extra (SQLite is built-in).

Step-by-step: switching modes
------------------------------
1. Open grub_config.json (auto-created on first run next to main.py).
2. Change: "execution_mode": "sequential"  →  "execution_mode": "parallel"
3. Restart Grub.  That's it.

OR use an env var (useful for CI/testing):
    GRUB_EXEC_MODE=parallel python -m grub --problem "..."

Model assignment
-----------------
Each Minion can use a different Ollama model.  Assign bigger models to tasks
that need more reasoning (coder, debugger) and smaller/faster models to tasks
that just need to check something (reviewer, tester).

On your RTX 3090 (24 GB VRAM) you can comfortably run qwen2.5-coder:32b.
On your daily PC (smaller) use 7b models.

To point a minion at a different machine's Ollama instance:
    "ollama_urls": {
        "coder": "http://192.168.1.10:11434",   ← 3090 machine
        "reviewer": "http://localhost:11434"     ← daily PC
    }
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Default configuration values ─────────────────────────────────────────────

_DEFAULT_MODELS = {
    # High-quality 32B coder for writing and debugging
    "coder": os.getenv("GRUB_CODER_MODEL", "qwen2.5-coder:32b"),
    # Fast 7B for reviewing (doesn't need to write code, just judge it)
    "reviewer": os.getenv("GRUB_REVIEWER_MODEL", "qwen3:7b"),
    # Fast 7B for writing tests
    "tester": os.getenv("GRUB_TESTER_MODEL", "qwen3:7b"),
    # 32B for debugging (needs to deeply understand code)
    "debugger": os.getenv("GRUB_DEBUGGER_MODEL", "qwen2.5-coder:32b"),
    # Medium 7B for refactoring (mostly structural changes)
    "refactorer": os.getenv("GRUB_REFACTORER_MODEL", "qwen2.5-coder:7b"),
}

# Default: all minions use the same Ollama instance
_DEFAULT_OLLAMA_URLS = {
    "coder": os.getenv("GRUB_OLLAMA_URL", "http://localhost:11434"),
    "reviewer": os.getenv("GRUB_OLLAMA_URL", "http://localhost:11434"),
    "tester": os.getenv("GRUB_OLLAMA_URL", "http://localhost:11434"),
    "debugger": os.getenv("GRUB_OLLAMA_URL", "http://localhost:11434"),
    "refactorer": os.getenv("GRUB_OLLAMA_URL", "http://localhost:11434"),
}


@dataclass
class GrubConfig:
    """
    Complete configuration for the Grub system.

    All fields have sensible defaults — you only need to change what differs
    from the defaults for your setup.

    Fields
    ------
    execution_mode      : "sequential" | "parallel" | "queue"
                          See module docstring for details.
    models              : Dict mapping minion name → Ollama model name.
    ollama_urls         : Dict mapping minion name → Ollama base URL.
                          Lets you run different minions on different machines.
    quality_threshold   : Reviewer score (0.0–1.0) needed to accept output.
                          0.75 = "must be 75% good before I accept it".
    max_iterations      : Max times a Minion retries before giving up.
    output_dir          : Where Grub writes implemented code.
    queue_db_path       : SQLite path for Mode C task queue.
    queue_workers       : Number of parallel workers in Mode C.
    tinker_tasks_db     : Path to Tinker's task database (for integration).
    tinker_artifacts_dir: Where Tinker writes design documents.
    grub_artifacts_dir  : Where Grub writes implementation notes.
    enable_git          : Whether Grub commits changes to git automatically.
    request_timeout     : Seconds to wait for Ollama to respond.
    """

    # ── Execution mode ─────────────────────────────────────────────────────────
    # CHANGE THIS to switch between A / B / C
    execution_mode: str = field(
        default_factory=lambda: os.getenv("GRUB_EXEC_MODE", "sequential")
    )

    # ── Model assignments ──────────────────────────────────────────────────────
    models: dict[str, str] = field(default_factory=lambda: dict(_DEFAULT_MODELS))
    ollama_urls: dict[str, str] = field(
        default_factory=lambda: dict(_DEFAULT_OLLAMA_URLS)
    )

    # ── Quality control ────────────────────────────────────────────────────────
    quality_threshold: float = float(os.getenv("GRUB_QUALITY_THRESHOLD", "0.75"))
    max_iterations: int = int(os.getenv("GRUB_MAX_ITERATIONS", "5"))

    # ── Paths ──────────────────────────────────────────────────────────────────
    output_dir: str = os.getenv("GRUB_OUTPUT_DIR", "./grub_output")
    queue_db_path: str = os.getenv("GRUB_QUEUE_DB", "grub_queue.sqlite")
    tinker_tasks_db: str = os.getenv("TINKER_TASK_DB", "tinker_tasks_engine.sqlite")
    tinker_artifacts_dir: str = os.getenv("TINKER_ARTIFACTS_DIR", "./tinker_artifacts")
    grub_artifacts_dir: str = os.getenv("GRUB_ARTIFACTS_DIR", "./grub_artifacts")

    # ── Mode C: Queue settings ─────────────────────────────────────────────────
    # These only matter when execution_mode == "queue"
    queue_workers: int = int(os.getenv("GRUB_QUEUE_WORKERS", "2"))

    # ── Optional features ──────────────────────────────────────────────────────
    enable_git: bool = os.getenv("GRUB_ENABLE_GIT", "false").lower() == "true"
    request_timeout: float = float(os.getenv("GRUB_REQUEST_TIMEOUT", "120.0"))

    # ── Context summarization ──────────────────────────────────────────────────
    # When a minion's input context (design docs, existing code, prior output)
    # exceeds context_max_chars, it is compressed using a small LLM model
    # instead of being hard-truncated.  This preserves more information than
    # a raw cut-off while keeping the prompt within a manageable size.
    #
    # Set GRUB_CONTEXT_SUMMARIZATION=false to disable and revert to truncation.
    context_summarization_enabled: bool = field(
        default_factory=lambda: os.getenv(
            "GRUB_CONTEXT_SUMMARIZATION", "true"
        ).lower() == "true"
    )

    # Text longer than this (in characters) triggers summarization.
    context_max_chars: int = int(os.getenv("GRUB_CONTEXT_MAX_CHARS", "6000"))

    # Target length (in characters) after summarization.  The LLM is asked to
    # aim for this length — actual results may vary by ±20%.
    context_target_chars: int = int(os.getenv("GRUB_CONTEXT_TARGET_CHARS", "3000"))

    # Which Ollama model to use for summarization.  Defaults to the reviewer's
    # model (a fast 7B model).  Summarization doesn't need a large model.
    # Set GRUB_SUMMARIZER_MODEL="" to use each minion's own model.
    context_summarizer_model: str = os.getenv("GRUB_SUMMARIZER_MODEL", "")

    # ── Skills loaded for each minion ──────────────────────────────────────────
    # You can add more skill files by dropping them in grub/skills/ and
    # adding the filename to the list below.
    minion_skills: dict[str, list[str]] = field(
        default_factory=lambda: {
            "coder": ["python_expert.md", "clean_code.md", "software_architecture.md"],
            "reviewer": ["clean_code.md", "security_review.md"],
            "tester": ["python_expert.md", "testing_patterns.md"],
            "debugger": ["python_expert.md", "clean_code.md"],
            "refactorer": ["python_expert.md", "clean_code.md"],
        }
    )

    def validate(self) -> list[str]:
        """
        Check the config for obvious mistakes.

        Returns a list of error strings.  Empty list = config is valid.
        """
        errors = []
        valid_modes = {"sequential", "parallel", "queue"}
        if self.execution_mode not in valid_modes:
            errors.append(
                f"execution_mode '{self.execution_mode}' is invalid. "
                f"Must be one of: {', '.join(sorted(valid_modes))}"
            )
        if not 0.0 <= self.quality_threshold <= 1.0:
            errors.append(
                f"quality_threshold {self.quality_threshold} must be between 0.0 and 1.0"
            )
        if self.max_iterations < 1:
            errors.append(f"max_iterations {self.max_iterations} must be >= 1")
        return errors

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "GrubConfig":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @classmethod
    def load(cls, path: str | Path = "grub_config.json") -> "GrubConfig":
        """
        Load config from a JSON file.

        If the file doesn't exist, returns defaults and writes the file
        so the user can edit it later.

        Parameters
        ----------
        path : Path to the JSON config file.
        """
        p = Path(path)
        if p.exists():
            try:
                data = json.loads(p.read_text())
                return cls.from_dict(data)
            except Exception as exc:
                print(
                    f"[GrubConfig] Warning: could not load {path}: {exc} — using defaults"
                )
        # Write defaults so the user can edit them
        cfg = cls()
        try:
            p.write_text(json.dumps(cfg.to_dict(), indent=2))
            print(f"[GrubConfig] Created default config at {p.resolve()}")
        except Exception as exc:
            logger.warning("GrubConfig: could not write default config to %s: %s", p, exc)
        return cfg

    def save(self, path: str | Path = "grub_config.json") -> None:
        """Save current config to a JSON file."""
        Path(path).write_text(json.dumps(self.to_dict(), indent=2))
