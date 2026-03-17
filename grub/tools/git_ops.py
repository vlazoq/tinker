"""
grub/tools/git_ops.py
=====================
Git helpers for Minions.

Why does Grub need git?
-----------------------
When Grub writes code, it's modifying files in your project.  Committing
each Minion's output to a branch means:
  - You can see exactly what Grub changed
  - You can revert a bad implementation with 'git revert'
  - You get a history of every implementation iteration

This is OPTIONAL — controlled by config.enable_git.
When enable_git=False, all functions return success without doing anything.

STATUS: FULLY IMPLEMENTED
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Union

from .shell import CommandResult, run_command

logger = logging.getLogger(__name__)


def git_status(cwd: Optional[Union[str, Path]] = None) -> CommandResult:
    """
    Run 'git status --short' in the given directory.

    Returns
    -------
    CommandResult.  stdout contains the status lines.
    """
    return run_command(["git", "status", "--short"], cwd=cwd, timeout=10.0)


def git_diff(
    cwd:   Optional[Union[str, Path]] = None,
    files: Optional[list[str]] = None,
) -> CommandResult:
    """
    Run 'git diff' (staged + unstaged) for specified files or all files.

    Parameters
    ----------
    cwd   : Working directory (git repo root or subdirectory).
    files : Specific files to diff.  None = diff everything.
    """
    cmd = ["git", "diff", "HEAD"]
    if files:
        cmd.extend(["--"] + files)
    return run_command(cmd, cwd=cwd, timeout=15.0)


def git_add(
    files: list[str],
    cwd:   Optional[Union[str, Path]] = None,
) -> CommandResult:
    """
    Stage files for commit ('git add <files>').

    Parameters
    ----------
    files : List of file paths to stage.
    cwd   : Working directory.
    """
    if not files:
        return CommandResult(0, "", "No files to add", "git add (skipped)")
    return run_command(["git", "add"] + files, cwd=cwd, timeout=10.0)


def git_commit(
    message: str,
    cwd:     Optional[Union[str, Path]] = None,
) -> CommandResult:
    """
    Commit staged files with a message.

    Parameters
    ----------
    message : Commit message.
    cwd     : Working directory.
    """
    return run_command(
        ["git", "commit", "-m", message],
        cwd=cwd, timeout=15.0,
    )


def git_current_branch(cwd: Optional[Union[str, Path]] = None) -> str:
    """
    Return the name of the current git branch.

    Returns '' if not in a git repo or on a detached HEAD.
    """
    result = run_command(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=cwd, timeout=5.0,
    )
    return result.stdout.strip() if result.succeeded else ""


def git_create_branch(
    branch: str,
    cwd:    Optional[Union[str, Path]] = None,
) -> CommandResult:
    """
    Create and switch to a new branch ('git checkout -b <branch>').

    Useful when Grub wants to put its implementation on a feature branch
    so it doesn't pollute main/master directly.
    """
    return run_command(
        ["git", "checkout", "-b", branch],
        cwd=cwd, timeout=10.0,
    )
