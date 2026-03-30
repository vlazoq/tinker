"""Git integration mixin for ArchitectureStateManager."""

from __future__ import annotations

import logging
import subprocess

from .schema import ArchitectureState

logger = logging.getLogger(__name__)


class GitIntegrationMixin:
    """Git repo init, commit, and commit-message building."""

    def _ensure_git_repo(self) -> None:
        """
        Initialise a Git repository in the workspace if one doesn't exist.
        Also sets a local git user identity (required for commits to work)
        using non-real values (tinker@local / Tinker) since this is all local.
        """
        git_dir = self.workspace / ".git"
        if not git_dir.exists():
            self._run_git("init")
            # Git requires user.email and user.name to make commits.
            # We use dummy values since this is just a local history tool.
            self._run_git("config", "user.email", "tinker@local")
            self._run_git("config", "user.name", "Tinker")
            logger.info("Initialised Git repo at %s", self.workspace)

    def _git_commit(self, message: str) -> None:
        """
        Stage all changed files and make a Git commit with the given message.
        Silently ignores the "nothing to commit" case (which happens if the
        state didn't actually change between two updates).
        """
        try:
            self._run_git("add", "--all")  # stage everything in the workspace
            self._run_git("commit", "-m", message)
        except subprocess.CalledProcessError as exc:
            # "nothing to commit" is not a real error — it just means no files changed
            if "nothing to commit" not in (exc.output or ""):
                logger.warning("Git commit failed: %s", exc)

    def _run_git(self, *args: str) -> str:
        """
        Run a git command in the workspace directory and return its stdout.
        Raises subprocess.CalledProcessError on failure (unless the output
        contains "nothing to commit", which we treat as success).

        Parameters
        ----------
        *args : The git subcommand and arguments, e.g. ("commit", "-m", "msg").

        Returns
        -------
        The stripped stdout text from the git command.
        """
        result = subprocess.run(
            ["git", *args],  # ["git", "commit", "-m", "msg", ...]
            cwd=self.workspace,  # run the command inside the workspace folder
            capture_output=True,  # capture stdout and stderr instead of printing
            text=True,  # return strings instead of bytes
        )
        if result.returncode != 0 and "nothing to commit" not in result.stdout:
            raise subprocess.CalledProcessError(
                result.returncode, args, result.stdout, result.stderr
            )
        return result.stdout.strip()

    @staticmethod
    def _build_commit_message(
        old: ArchitectureState,
        new: ArchitectureState,
        update: dict,
    ) -> str:
        """
        Build a descriptive Git commit message summarising what changed.

        The message format is:
            loop 0042: arch-state update | +2 component(s), +1 decision(s) |
            overall_confidence=0.72 | [optional loop note snippet]

        Why this format?
        - `git log --oneline` shows you a clear timeline of AI progress.
        - The loop number tells you exactly which iteration produced the change.
        - The counts show at a glance how much new information was added.

        Parameters
        ----------
        old    : The state BEFORE the update (used to calculate "what's new").
        new    : The state AFTER the update.
        update : The raw update dict (used to extract the loop_note).
        """
        loop = new.macro_loop
        # Count how many items were ADDED in each collection
        new_comps = len(new.components) - len(old.components)
        new_decs = len(new.decisions) - len(old.decisions)
        new_qs = len(new.open_questions) - len(old.open_questions)
        conf = new.overall_confidence.value

        # Build the message by assembling pipe-separated parts
        parts = [f"loop {loop:04d}: arch-state update"]
        details = []
        if new_comps > 0:
            details.append(f"+{new_comps} component(s)")
        if new_decs > 0:
            details.append(f"+{new_decs} decision(s)")
        if new_qs > 0:
            details.append(f"+{new_qs} question(s)")
        if details:
            parts.append(", ".join(details))
        parts.append(f"overall_confidence={conf:.2f}")
        # Include the first 120 characters of the loop_note (if any) for context
        if update.get("loop_note"):
            parts.append(update["loop_note"][:120])

        return " | ".join(parts)
