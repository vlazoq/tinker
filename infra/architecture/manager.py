"""
tinker/architecture/manager.py
================================

What this file does
--------------------
This file is the "front door" for the entire architecture package.  Everything
the rest of Tinker needs to do with architecture state goes through the
`ArchitectureStateManager` class defined here.

Think of it as a combination of:
  - A librarian (loads the document from disk, hands it out, puts it back)
  - A scribe (applies new knowledge via the merger, saves updated versions)
  - A historian (keeps a timestamped archive of every version in a history folder)
  - A summariser (compresses the document to fit inside an LLM context window)
  - A Git wrapper (optionally commits each version to a Git repository)

Typical usage by the orchestrator
-----------------------------------
    from infra.architecture import ArchitectureStateManager

    # Step 1: Create a manager (loads existing state from disk if it exists)
    mgr = ArchitectureStateManager(workspace="./tinker_workspace")

    # Step 2: After an AI loop produces output, apply its update
    mgr.apply_update({
        "loop_note": "Identified the API Gateway component",
        "components": [
            {"name": "API Gateway", "description": "Routes all external traffic",
             "responsibilities": ["Rate limiting", "Auth token validation"],
             "confidence_value": 0.75}
        ]
    })

    # Step 3: Get a compressed summary to inject into the next AI prompt
    context_text = mgr.summarise(budget_tokens=800)

    # Step 4: See what changed since the last loop
    print(mgr.diff())

File layout on disk
--------------------
    workspace/
    ├── architecture_state.json     ← the "live" current version
    └── history/
        ├── loop_0001_20240115T120000Z.json
        ├── loop_0002_20240115T120045Z.json
        └── ...                     ← one snapshot per apply_update() call

The "live" file is always the latest version.  The history folder keeps
every previous version so you can diff, roll back, or analyse progress.

Git integration
----------------
When `auto_git=True` (the default), the manager will:
1. Initialise a Git repository in the workspace folder if one doesn't exist.
2. After each update, run `git add --all` and `git commit` with a
   descriptive message showing what changed.

This gives you a complete version history viewable with `git log`.
Set `auto_git=False` for unit tests or environments without Git.

Responsibilities
-----------------
1. Hold the current ArchitectureState in memory.
2. Accept update payloads and produce new versions via the merger.
3. Persist each version to disk as JSON and auto-commit to Git.
4. Produce compressed text summaries for context assembly.
5. Produce human-readable diffs between any two versions.
6. Answer structured queries (low-confidence items, open questions, etc.).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .merger import merge_update
from .schema import (
    ArchitectureState,
)

# Module-level logger.  Log messages from this file will be labelled
# with the module path, e.g. "tinker.architecture.manager".
logger = logging.getLogger(__name__)


from ._diffing import DiffingMixin
from ._git_integration import GitIntegrationMixin
from ._persistence import PersistenceMixin
from ._queries import QueriesMixin
from ._summarizer import SummarizerMixin


class ArchitectureStateManager(
    PersistenceMixin,
    GitIntegrationMixin,
    SummarizerMixin,
    DiffingMixin,
    QueriesMixin,
):
    """
    Manages the full lifecycle of an ArchitectureState document.

    This is the only class outside callers need to interact with.  All the
    schema types, merger logic, and JSON serialisation are internal details.

    Parameters
    ----------
    workspace : Path | str
        Path to the directory where state files and Git history are stored.
        Created automatically if it doesn't exist.
        Default: "./tinker_workspace"
    system_name : str
        Human-readable name for the system being designed.  Only used when
        creating a brand-new state (ignored if one already exists on disk).
        Default: "Unknown System"
    auto_git : bool
        If True, automatically initialise a Git repo in the workspace and
        commit after every `apply_update()` call.
        Set False for unit tests or read-only usage.
        Default: True

    Attributes (public)
    -------------------
    workspace  : The resolved Path to the workspace directory.
    auto_git   : Whether Git commits are enabled.
    state      : Property — returns the current in-memory ArchitectureState.
    macro_loop : Property — shortcut for state.macro_loop.
    """

    # The name of the "live" current-state file (always in workspace root)
    STATE_FILENAME = "architecture_state.json"
    # The subdirectory where historical snapshots are stored
    HISTORY_DIR = "history"

    def __init__(
        self,
        workspace: Path | str = "./tinker_workspace",
        system_name: str = "Unknown System",
        auto_git: bool = True,
    ) -> None:
        self.workspace = Path(workspace)
        self.auto_git = auto_git

        # Build the full paths for convenience
        self._state_path = self.workspace / self.STATE_FILENAME
        self._hist_dir = self.workspace / self.HISTORY_DIR

        # Create the workspace and history directories if they don't exist yet.
        # parents=True means it will create intermediate directories too.
        # exist_ok=True means it won't error if the directory already exists.
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._hist_dir.mkdir(parents=True, exist_ok=True)

        # Initialise a Git repo in the workspace if auto_git is on
        if auto_git:
            self._ensure_git_repo()

        # Load existing state from disk, or create a fresh one
        if self._state_path.exists():
            # There's an existing state file — load it
            self._state = ArchitectureState.model_validate_json(self._state_path.read_text())
            logger.info("Loaded existing state (loop %d)", self._state.macro_loop)
        else:
            # First run — create a blank state document
            self._state = ArchitectureState(system_name=system_name)
            logger.info("Initialised fresh state for '%s'", system_name)

    # ── Public Properties ────────────────────────────────────────────

    @property
    def state(self) -> ArchitectureState:
        """The current in-memory ArchitectureState (read-only view)."""
        return self._state

    @property
    def macro_loop(self) -> int:
        """Convenience shortcut for state.macro_loop."""
        return self._state.macro_loop

    # ── Update / Merge ───────────────────────────────────────────────

    def commit(self, payload: dict[str, Any]) -> str:
        """
        Accept a macro-loop snapshot payload, merge it into the current state,
        and return a short commit identifier.

        This is the interface the Orchestrator's macro loop expects:
        ``commit_hash = await arch_state_manager.commit(commit_payload)``

        The payload may include:
          - ``content`` : the AI-generated architectural narrative (stored as a loop note)
          - ``version``  : the snapshot version number
          - ``total_micro_loops`` / ``total_meso_loops`` : loop counters for the note
          - any other keys are merged as-is into the update payload

        Returns
        -------
        str : Short hex identifier derived from the Git commit hash (or a UUID
              if Git is disabled).
        """
        update: dict[str, Any] = {k: v for k, v in payload.items() if k != "content"}
        if payload.get("content"):
            version = payload.get("version", "?")
            micro = payload.get("total_micro_loops", "?")
            update["loop_note"] = f"[macro v{version} micro={micro}] {payload['content'][:200]}"

        self.apply_update(update)

        # Return the latest git commit hash if available, otherwise a timestamp token.
        if self.auto_git:
            try:
                return self._run_git("rev-parse", "--short", "HEAD")
            except Exception as exc:
                logger.warning("Could not get git commit hash: %s — using timestamp fallback", exc)
        # Deterministic fallback: ISO timestamp prefix instead of random UUID
        # so the hash is meaningful and reproducible for debugging.
        import time as _time

        return f"t{int(_time.time())}"

    def apply_update(self, update: dict[str, Any]) -> ArchitectureState:
        """
        Merge a new update payload into the current state, save it to disk,
        and optionally commit to Git.  Returns the newly produced state.

        This is the main method the orchestrator calls after each AI loop.

        Parameters
        ----------
        update : A plain dict of new information from the AI.  See merger.py
                 for the full list of supported keys.

        Returns
        -------
        The new ArchitectureState after the merge.

        Side effects
        -----------
        - Updates self._state to the new version.
        - Writes the new state to architecture_state.json.
        - Writes a timestamped snapshot to the history/ directory.
        - If auto_git is True, runs git add + git commit.
        """
        old_state = self._state
        # The merger produces a brand-new state object — nothing is mutated
        new_state = merge_update(old_state, update)
        self._state = new_state  # update our in-memory reference

        # Save the new state in both the "live" file and the history archive
        self._persist(new_state)
        self._archive_snapshot(new_state)

        # Optionally commit all changes to Git
        if self.auto_git:
            msg = self._build_commit_message(old_state, new_state, update)
            self._git_commit(msg)

        logger.info(
            "State updated → loop %d | components=%d decisions=%d questions=%d",
            new_state.macro_loop,
            len(new_state.components),
            len(new_state.decisions),
            len(new_state.open_questions),
        )
        return new_state
