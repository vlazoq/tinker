"""
agents/grub/registry.py
================
The Minion Registry — a central lookup table for all Minions and Skills.

Why a registry?
---------------
Without a registry, Grub would have to hard-code 'from minions.coder import
CoderMinion' and know about every Minion directly.  That makes it impossible
to add a new Minion without editing Grub's core code.

With a registry, Grub just asks: "give me a Minion that can do 'coding'."
The registry handles the lookup.  Adding a new Minion = register it in one
place.  Grub doesn't change.

This is the 'open/closed principle': open for extension (add new minions),
closed for modification (don't touch Grub's core).

Skills work the same way: a skill is just a name → text mapping.  Any Minion
can request a skill by name at runtime.

How to add a new Minion
-----------------------
1. Create grub/minions/my_minion.py  (subclass BaseMinion, implement run())
2. Add two lines here:
       from minions.my_minion import MyMinion
       registry.register_minion("my_minion", MyMinion)
3. Done.  Grub can now delegate tasks to "my_minion".

How to add a new Skill
----------------------
1. Create grub/skills/my_skill.md  (just a text file with the skill content)
2. Done.  Skills are auto-discovered from the skills/ directory.
   OR explicitly: registry.register_skill("my_skill", "skill text here")
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Type

if TYPE_CHECKING:
    from .minions.base import BaseMinion
    from .config import GrubConfig

logger = logging.getLogger(__name__)


class MinionRegistry:
    """
    Registry of all available Minion classes and Skills.

    Usage
    -----
    ::

        registry = MinionRegistry(config)
        registry.load_defaults()

        # Get a Minion instance
        minion = registry.get_minion("coder")

        # Get skill text to inject into a prompt
        skill_text = registry.get_skill("python_expert")
    """

    def __init__(self, config: "GrubConfig") -> None:
        self._config = config
        self._minions: dict[str, Type["BaseMinion"]] = {}
        self._skills: dict[str, str] = {}
        self._skills_dir = Path(__file__).parent / "skills"

    # ── Minion management ─────────────────────────────────────────────────────

    def register_minion(self, name: str, cls: Type["BaseMinion"]) -> None:
        """
        Register a Minion class under a name.

        Parameters
        ----------
        name : Short identifier (e.g. "coder", "tester").
        cls  : The Minion class (not an instance — the class itself).
        """
        self._minions[name] = cls
        logger.debug("Registered minion: %s → %s", name, cls.__name__)

    def get_minion(self, name: str) -> "BaseMinion":
        """
        Instantiate and return a Minion by name.

        The registry creates a fresh instance each time.  Minions are
        stateless between tasks (state is in the task/result), so this is safe.

        Parameters
        ----------
        name : Minion name (must have been registered first).

        Raises
        ------
        KeyError : If the name is not registered.
        """
        if name not in self._minions:
            available = ", ".join(sorted(self._minions.keys()))
            raise KeyError(f"Minion '{name}' not registered. Available: {available}")
        cls = self._minions[name]
        # Load skills for this minion from config
        skill_names = self._config.minion_skills.get(name, [])
        skills = [self.get_skill(s) for s in skill_names]
        return cls(
            name=name,
            config=self._config,
            skills=skills,
        )

    def list_minions(self) -> list[str]:
        """Return names of all registered Minions."""
        return sorted(self._minions.keys())

    # ── Skill management ──────────────────────────────────────────────────────

    def register_skill(self, name: str, text: str) -> None:
        """
        Register a skill by name and text content.

        Parameters
        ----------
        name : Short identifier (e.g. "python_expert").
        text : The full skill text (injected into Minion system prompts).
        """
        self._skills[name] = text
        logger.debug("Registered skill: %s (%d chars)", name, len(text))

    def get_skill(self, name: str) -> str:
        """
        Return skill text by name.

        Tries in this order:
          1. In-memory registry (explicitly registered)
          2. File: skills/{name}   (if name already has extension)
          3. File: skills/{name}.md

        Parameters
        ----------
        name : Skill name or filename (e.g. "python_expert" or "python_expert.md").

        Returns
        -------
        str : Skill text, or empty string if not found.
        """
        # In-memory hit
        if name in self._skills:
            return self._skills[name]

        # Try loading from disk
        candidates = [
            self._skills_dir / name,
            self._skills_dir / f"{name}.md",
            self._skills_dir / f"{name}.txt",
        ]
        for path in candidates:
            if path.exists():
                text = path.read_text(encoding="utf-8")
                self._skills[name] = text  # cache
                logger.debug("Loaded skill from file: %s", path)
                return text

        logger.warning("Skill '%s' not found in registry or skills/ directory", name)
        return ""

    def load_all_skills(self) -> None:
        """
        Auto-discover and load all .md and .txt files from the skills/ directory.

        Called at startup.  After this, all skill files are available by
        their filename (without extension).
        """
        if not self._skills_dir.exists():
            logger.warning("Skills directory not found: %s", self._skills_dir)
            return

        for path in self._skills_dir.iterdir():
            if path.suffix in (".md", ".txt") and path.is_file():
                name = path.stem  # filename without extension
                if name not in self._skills:
                    try:
                        self._skills[name] = path.read_text(encoding="utf-8")
                        logger.debug("Auto-loaded skill: %s", name)
                    except Exception as exc:
                        logger.warning("Could not load skill %s: %s", path, exc)

        logger.info("Skills loaded: %s", ", ".join(sorted(self._skills.keys())))

    def list_skills(self) -> list[str]:
        """Return names of all loaded skills."""
        return sorted(self._skills.keys())

    # ── Bulk loader ───────────────────────────────────────────────────────────

    def load_defaults(self) -> None:
        """
        Register all built-in Minions and auto-load all skill files.

        Call this once at startup.  After this the registry is ready to use.

        To add a new Minion permanently:
          1. Create the Minion class in grub/minions/
          2. Import it here
          3. Call self.register_minion("name", TheClass)
        """
        # ── Import and register all built-in Minions ──────────────────────────
        try:
            from .minions.coder import CoderMinion
            from .minions.reviewer import ReviewerMinion
            from .minions.tester import TesterMinion
            from .minions.debugger import DebuggerMinion
            from .minions.refactorer import RefactorerMinion

            self.register_minion("coder", CoderMinion)
            self.register_minion("reviewer", ReviewerMinion)
            self.register_minion("tester", TesterMinion)
            self.register_minion("debugger", DebuggerMinion)
            self.register_minion("refactorer", RefactorerMinion)

            logger.info(
                "Registered %d built-in minions: %s",
                len(self._minions),
                ", ".join(self.list_minions()),
            )
        except ImportError as exc:
            logger.error("Failed to import a built-in Minion: %s", exc)

        # ── Auto-load all skill files ─────────────────────────────────────────
        self.load_all_skills()

    def summary(self) -> dict[str, Any]:
        """Return a summary dict for logging/diagnostics."""
        return {
            "minions": self.list_minions(),
            "skills": self.list_skills(),
        }
