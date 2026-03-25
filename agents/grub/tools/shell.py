"""
agents/grub/tools/shell.py
===================
Run shell commands and test suites safely.

Security note
-------------
These functions run arbitrary shell commands.  Only call them with commands
you constructed yourself — never with raw strings from the LLM output.
The Minions build commands programmatically (e.g. ["pytest", filepath]),
not by passing LLM text to the shell.

STATUS: FULLY IMPLEMENTED
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

logger = logging.getLogger(__name__)


@dataclass
class CommandResult:
    """Result of running a shell command."""

    returncode: int
    stdout: str
    stderr: str
    command: str  # the command that was run (for logging)

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        """Combined stdout + stderr."""
        parts = []
        if self.stdout.strip():
            parts.append(self.stdout)
        if self.stderr.strip():
            parts.append(self.stderr)
        return "\n".join(parts)


def run_command(
    cmd: list[str],
    cwd: Optional[Union[str, Path]] = None,
    timeout: float = 60.0,
    env: Optional[dict] = None,
) -> CommandResult:
    """
    Run a command synchronously and return its output.

    Parameters
    ----------
    cmd     : Command as a list of strings. Example: ["pytest", "tests/", "-v"]
              NEVER pass a single string from user/LLM input — that risks
              shell injection.  Always use a list.
    cwd     : Working directory for the command. Defaults to current dir.
    timeout : Max seconds to wait. Raises TimeoutError if exceeded.
    env     : Optional environment variables dict. Merged with current env.

    Returns
    -------
    CommandResult with returncode, stdout, stderr.

    Example
    -------
    ::

        result = run_command(["python", "-m", "pytest", "tests/", "-v"])
        if result.succeeded:
            print("Tests passed!")
        else:
            print("Tests failed:", result.stderr)
    """
    cmd_str = " ".join(str(c) for c in cmd)
    logger.debug("run_command: %s (cwd=%s)", cmd_str, cwd)

    # Merge env if provided
    run_env = dict(os.environ)
    if env:
        run_env.update(env)

    try:
        proc = subprocess.run(
            [str(c) for c in cmd],
            capture_output=True,
            text=True,
            cwd=str(cwd) if cwd else None,
            timeout=timeout,
            env=run_env,
        )
        result = CommandResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            command=cmd_str,
        )
        if result.succeeded:
            logger.debug("run_command OK: %s", cmd_str)
        else:
            logger.info("run_command exit %d: %s", proc.returncode, cmd_str)
        return result
    except subprocess.TimeoutExpired:
        logger.warning("run_command timeout after %.0fs: %s", timeout, cmd_str)
        return CommandResult(
            returncode=-1,
            stdout="",
            stderr=f"Command timed out after {timeout}s",
            command=cmd_str,
        )
    except Exception as exc:
        logger.warning("run_command failed: %s — %s", cmd_str, exc)
        return CommandResult(
            returncode=-1,
            stdout="",
            stderr=str(exc),
            command=cmd_str,
        )


async def run_command_async(
    cmd: list[str],
    cwd: Optional[Union[str, Path]] = None,
    timeout: float = 60.0,
) -> CommandResult:
    """
    Async version of run_command.  Used by Grub's parallel/queue modes.

    Same parameters and return type as run_command().
    """
    cmd_str = " ".join(str(c) for c in cmd)
    try:
        proc = await asyncio.create_subprocess_exec(
            *[str(c) for c in cmd],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return CommandResult(-1, "", f"Timed out after {timeout}s", cmd_str)

        return CommandResult(
            returncode=proc.returncode,
            stdout=stdout_b.decode(errors="replace"),
            stderr=stderr_b.decode(errors="replace"),
            command=cmd_str,
        )
    except Exception as exc:
        return CommandResult(-1, "", str(exc), cmd_str)


def run_tests(
    test_path: Union[str, Path],
    cwd: Optional[Union[str, Path]] = None,
    timeout: float = 120.0,
    extra_args: list[str] | None = None,
) -> CommandResult:
    """
    Run pytest on a file or directory.

    Parameters
    ----------
    test_path  : File or directory containing tests.
    cwd        : Working directory.
    timeout    : Max seconds for the test run.
    extra_args : Additional pytest args, e.g. ["-x", "--tb=short"].

    Returns
    -------
    CommandResult.  Check result.succeeded to know if all tests passed.

    Example
    -------
    ::

        result = run_tests("tests/test_router.py", cwd="./my_project")
        print(result.stdout)   # pytest output
    """
    cmd = [sys.executable, "-m", "pytest", str(test_path), "-v", "--tb=short"]
    if extra_args:
        cmd.extend(extra_args)
    return run_command(cmd, cwd=cwd, timeout=timeout)


def check_syntax(filepath: Union[str, Path]) -> CommandResult:
    """
    Check Python syntax without running the file.

    Uses 'python -m py_compile' — fast, no imports, just syntax.

    Parameters
    ----------
    filepath : Path to the Python file to check.
    """
    return run_command(
        [sys.executable, "-m", "py_compile", str(filepath)],
        timeout=10.0,
    )
