"""
fritz/git_ops.py
─────────────────
Local git operations — wraps the git CLI via subprocess.

All methods are async (run in a thread pool to avoid blocking the event loop).
Every operation is platform-agnostic: it just talks to local git, regardless
of whether the remote is GitHub, Gitea, or anything else.

FritzGitResult
──────────────
Every method returns a FritzGitResult (success flag + output + error).
Callers should check .ok before proceeding; no unexpected exceptions are raised.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from .config import FritzConfig
from .identity import FritzIdentity, apply_git_identity

logger = logging.getLogger(__name__)


@dataclass
class FritzGitResult:
    ok: bool
    operation: str
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0

    @property
    def output(self) -> str:
        return self.stdout.strip() or self.stderr.strip()

    def __str__(self) -> str:
        status = "OK" if self.ok else "FAIL"
        return f"[git:{self.operation}] {status} — {self.output[:200]}"


class FritzGitOps:
    """
    Local git operations for a single repository.

    Instantiate once per Fritz session; the identity is applied to the repo's
    local git config on first call so all subsequent commits use the right
    name/email.
    """

    def __init__(self, config: FritzConfig, identity: FritzIdentity) -> None:
        self._repo = Path(config.repo_path).resolve()
        self._identity = identity
        self._identity_applied = False

    # ── Internal helpers ─────────────────────────────────────────────────────

    async def _run(self, *args: str, check: bool = False) -> FritzGitResult:
        """Execute a git command asynchronously in the repo directory."""
        await self._ensure_identity()
        operation = " ".join(args[:3])
        return await asyncio.to_thread(self._run_sync, operation, args)

    def _run_sync(self, operation: str, args: Sequence[str]) -> FritzGitResult:
        cmd = ["git", *args]
        try:
            result = subprocess.run(
                cmd,
                cwd=self._repo,
                capture_output=True,
                text=True,
            )
            ok = result.returncode == 0
            if not ok:
                logger.warning("git %s failed (rc=%d): %s", operation, result.returncode, result.stderr.strip())
            return FritzGitResult(
                ok=ok,
                operation=operation,
                stdout=result.stdout,
                stderr=result.stderr,
                returncode=result.returncode,
            )
        except Exception as exc:
            logger.error("git %s exception: %s", operation, exc)
            return FritzGitResult(ok=False, operation=operation, stderr=str(exc), returncode=-1)

    async def _ensure_identity(self) -> None:
        if not self._identity_applied:
            await asyncio.to_thread(apply_git_identity, self._identity, self._repo)
            self._identity_applied = True

    # ── Status & info ─────────────────────────────────────────────────────────

    async def current_branch(self) -> str:
        """Return the name of the currently checked-out branch."""
        result = await self._run("branch", "--show-current")
        return result.stdout.strip()

    async def status(self) -> FritzGitResult:
        return await self._run("status", "--short")

    async def get_remote_url(self, remote: str = "origin") -> str:
        result = await self._run("remote", "get-url", remote)
        return result.stdout.strip()

    async def list_remotes(self) -> list[str]:
        result = await self._run("remote")
        return [r.strip() for r in result.stdout.splitlines() if r.strip()]

    async def log(self, n: int = 10, format: str = "%H %s") -> list[str]:
        result = await self._run("log", f"-{n}", f"--pretty=format:{format}")
        return [line for line in result.stdout.splitlines() if line]

    async def head_sha(self) -> str:
        result = await self._run("rev-parse", "HEAD")
        return result.stdout.strip()

    # ── Staging & committing ──────────────────────────────────────────────────

    async def add(self, files: list[str] | None = None) -> FritzGitResult:
        """Stage specific files, or all changes if files is None."""
        if files:
            return await self._run("add", "--", *files)
        return await self._run("add", "-A")

    async def commit(
        self,
        message: str,
        files: list[str] | None = None,
        allow_empty: bool = False,
    ) -> FritzGitResult:
        """Stage files (or all) and create a commit."""
        add_result = await self.add(files)
        if not add_result.ok:
            return add_result

        args = ["commit", "-m", message]
        if allow_empty:
            args.append("--allow-empty")
        return await self._run(*args)

    # ── Branches ─────────────────────────────────────────────────────────────

    async def create_branch(
        self, name: str, from_branch: str | None = None
    ) -> FritzGitResult:
        """Create a new branch, optionally from a specific base branch."""
        if from_branch:
            return await self._run("checkout", "-b", name, from_branch)
        return await self._run("checkout", "-b", name)

    async def checkout(self, branch: str) -> FritzGitResult:
        return await self._run("checkout", branch)

    async def delete_branch(self, name: str, force: bool = False) -> FritzGitResult:
        flag = "-D" if force else "-d"
        return await self._run("branch", flag, name)

    async def branch_exists(self, name: str) -> bool:
        result = await self._run("branch", "--list", name)
        return bool(result.stdout.strip())

    async def remote_branch_exists(self, name: str, remote: str = "origin") -> bool:
        result = await self._run("ls-remote", "--heads", remote, name)
        return bool(result.stdout.strip())

    # ── Push & pull ───────────────────────────────────────────────────────────

    async def push(
        self,
        branch: str | None = None,
        remote: str = "origin",
        force: bool = False,
        set_upstream: bool = True,
    ) -> FritzGitResult:
        """Push a branch to a remote."""
        branch = branch or await self.current_branch()
        args = ["push"]
        if set_upstream:
            args += ["-u"]
        if force:
            args += ["--force-with-lease"]  # safer than --force
        args += [remote, branch]
        return await self._run(*args)

    async def push_all_remotes(
        self,
        branch: str | None = None,
        force: bool = False,
    ) -> dict[str, FritzGitResult]:
        """Push to every configured remote simultaneously."""
        branch = branch or await self.current_branch()
        remotes = await self.list_remotes()

        results = await asyncio.gather(
            *[self.push(branch, remote=r, force=force) for r in remotes],
            return_exceptions=True,
        )

        outcome: dict[str, FritzGitResult] = {}
        for remote, result in zip(remotes, results):
            if isinstance(result, Exception):
                outcome[remote] = FritzGitResult(
                    ok=False, operation="push", stderr=str(result)
                )
            else:
                outcome[remote] = result  # type: ignore[assignment]
        return outcome

    async def pull(
        self, branch: str | None = None, remote: str = "origin"
    ) -> FritzGitResult:
        branch = branch or await self.current_branch()
        return await self._run("pull", remote, branch)

    async def fetch(self, remote: str = "origin", branch: str | None = None) -> FritzGitResult:
        if branch:
            return await self._run("fetch", remote, branch)
        return await self._run("fetch", remote)

    # ── Merging & tagging ─────────────────────────────────────────────────────

    async def merge(
        self, branch: str, method: str = "merge", message: str | None = None
    ) -> FritzGitResult:
        """
        Merge a branch into the current branch.
        method: merge | squash | rebase
        """
        if method == "rebase":
            return await self._run("rebase", branch)
        if method == "squash":
            result = await self._run("merge", "--squash", branch)
            if not result.ok:
                return result
            msg = message or f"Squash merge {branch}"
            return await self.commit(msg)
        # Default merge
        args = ["merge", branch]
        if message:
            args += ["-m", message]
        return await self._run(*args)

    async def tag(
        self, name: str, message: str | None = None, commit: str = "HEAD"
    ) -> FritzGitResult:
        if message:
            return await self._run("tag", "-a", name, "-m", message, commit)
        return await self._run("tag", name, commit)

    async def push_tag(self, name: str, remote: str = "origin") -> FritzGitResult:
        return await self._run("push", remote, f"refs/tags/{name}")

    # ── Stash ─────────────────────────────────────────────────────────────────

    async def stash(self, message: str | None = None) -> FritzGitResult:
        if message:
            return await self._run("stash", "push", "-m", message)
        return await self._run("stash", "push")

    async def stash_pop(self) -> FritzGitResult:
        return await self._run("stash", "pop")

    # ── Config ────────────────────────────────────────────────────────────────

    async def set_config(self, key: str, value: str) -> FritzGitResult:
        """Set a local git config value in this repository."""
        return await self._run("config", key, value)

    async def add_remote(self, name: str, url: str) -> FritzGitResult:
        return await self._run("remote", "add", name, url)

    async def set_remote_url(self, name: str, url: str) -> FritzGitResult:
        return await self._run("remote", "set-url", name, url)
