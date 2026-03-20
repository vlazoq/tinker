"""
fritz/agent.py
───────────────
FritzAgent — top-level orchestrator for all Fritz git/GitHub/Gitea operations.

This is the single entry point that Grub (and any other caller) uses.
It wires together: credentials → identity → git_ops → github/gitea_ops →
push_policy → audit logging.

Typical usage from Grub
───────────────────────
    fritz = FritzAgent(config)
    await fritz.setup()

    result = await fritz.commit_and_ship(
        files=["src/fix.py", "tests/test_fix.py"],
        message="fix: correct off-by-one in parser",
        task_id="grub-abc123",
        task_description="correct off-by-one in parser",
    )
    print(result)  # FritzShipResult with branch, pr_url, merge_status

Standalone usage
────────────────
    fritz = await FritzAgent.from_config("fritz_config.json")
    await fritz.push(branch="my-feature")
    await fritz.create_pr(title="My fix", body="...", head="my-feature")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import FritzConfig
from .credentials import FritzCredentials
from .git_ops import FritzGitOps, FritzGitResult
from .gitea_ops import FritzGitea
from .github_ops import FritzGitHub, FritzRemoteResult
from .identity import FritzIdentity, build_identity
from .platform import GitPlatform, detect_platform, extract_owner_repo
from .push_policy import PolicyViolation, PushPolicy

logger = logging.getLogger(__name__)


@dataclass
class FritzShipResult:
    """Outcome of a full commit-and-ship cycle."""

    ok: bool
    branch: str = ""
    commit_sha: str = ""
    pr_url: str = ""
    pr_number: int | None = None
    merged: bool = False
    direct_push: bool = False
    errors: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        status = "OK" if self.ok else "FAIL"
        parts = [f"[fritz:ship] {status}"]
        if self.branch:
            parts.append(f"branch={self.branch}")
        if self.pr_url:
            parts.append(f"pr={self.pr_url}")
        if self.merged:
            parts.append("merged=true")
        if self.errors:
            parts.append(f"errors={self.errors}")
        return " ".join(parts)


class FritzAgent:
    """
    Top-level Fritz orchestrator.

    Lifecycle:
        1. Instantiate with a FritzConfig
        2. Call await setup() to load credentials and apply git identity
        3. Call any operation method (commit_and_ship, push, create_pr, etc.)
    """

    def __init__(self, config: FritzConfig) -> None:
        self.config = config
        self._creds: FritzCredentials | None = None
        self._identity: FritzIdentity | None = None
        self._git: FritzGitOps | None = None
        self._github: FritzGitHub | None = None
        self._gitea: FritzGitea | None = None
        self._policy: PushPolicy | None = None
        self._ready = False

    @classmethod
    async def from_config(
        cls, path: str | Path = "fritz_config.json"
    ) -> "FritzAgent":
        """Convenience: load config from file and call setup()."""
        config = FritzConfig.from_file(path)
        agent = cls(config)
        await agent.setup()
        return agent

    async def setup(self) -> None:
        """
        Load credentials, build identity, initialise all drivers.
        Must be called before any operation.
        """
        errors = self.config.validate()
        if errors:
            raise ValueError(f"Fritz config invalid: {errors}")

        # Warn about dangerous policy combos
        policy = PushPolicy(self.config)
        for warning in policy.validate_config():
            logger.warning("FritzAgent policy warning: %s", warning)
        self._policy = policy

        # Credentials
        creds = FritzCredentials(self.config)
        await creds.load()
        self._creds = creds

        # Identity
        self._identity = build_identity(self.config, creds)

        # Local git
        self._git = FritzGitOps(self.config, self._identity)

        # Remote drivers
        if self.config.github_enabled and creds.github_token:
            self._github = FritzGitHub(self.config, creds.github_token)

        if self.config.gitea_enabled and creds.gitea_token:
            self._gitea = FritzGitea(self.config, creds.gitea_token)

        self._ready = True
        logger.info(
            "FritzAgent ready. identity=%s github=%s gitea=%s",
            self.config.identity_mode,
            "enabled" if self._github else "disabled",
            "enabled" if self._gitea else "disabled",
        )

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def git(self) -> FritzGitOps:
        self._check_ready()
        assert self._git is not None
        return self._git

    @property
    def github(self) -> FritzGitHub:
        self._check_ready()
        if self._github is None:
            raise RuntimeError(
                "GitHub is not enabled or token is missing. "
                "Set github_enabled=true and provide FRITZ_GITHUB_TOKEN."
            )
        return self._github

    @property
    def gitea(self) -> FritzGitea:
        self._check_ready()
        if self._gitea is None:
            raise RuntimeError(
                "Gitea is not enabled or token is missing. "
                "Set gitea_enabled=true and provide FRITZ_GITEA_TOKEN."
            )
        return self._gitea

    @property
    def policy(self) -> PushPolicy:
        self._check_ready()
        assert self._policy is not None
        return self._policy

    def _check_ready(self) -> None:
        if not self._ready:
            raise RuntimeError("FritzAgent.setup() has not been called.")

    # ── High-level: commit and ship ───────────────────────────────────────────

    async def commit_and_ship(
        self,
        message: str,
        files: list[str] | None = None,
        task_id: str = "task",
        task_description: str = "",
        base_branch: str | None = None,
        pr_title: str | None = None,
        pr_body: str | None = None,
        reviewers: list[str] | None = None,
        auto_merge: bool = True,
    ) -> FritzShipResult:
        """
        Full pipeline: create branch → commit → push → (optionally) PR → merge.

        This is the main entry point for Grub to hand off completed work.

        Flow depends on push policy:
          - allow_push_to_main=True, require_pr=False → direct push to default branch
          - everything else → feature branch + PR + optional auto-merge
        """
        base = base_branch or self.config.default_branch

        # Decide: direct push or PR flow?
        direct_decision = self._policy.evaluate_push(base)  # type: ignore[union-attr]

        if direct_decision.allowed:
            return await self._direct_push_flow(
                message=message,
                files=files,
                branch=base,
            )
        else:
            branch_name = self._policy.suggest_branch_name(  # type: ignore[union-attr]
                task_id, task_description or message[:50]
            )
            return await self._pr_flow(
                message=message,
                files=files,
                feature_branch=branch_name,
                base_branch=base,
                pr_title=pr_title or message,
                pr_body=pr_body or f"Automated PR from Fritz\n\nTask: {task_description}",
                reviewers=reviewers or [],
                auto_merge=auto_merge,
            )

    async def _direct_push_flow(
        self,
        message: str,
        files: list[str] | None,
        branch: str,
    ) -> FritzShipResult:
        """Commit and push directly to a branch (main or feature)."""
        commit_result = await self.git.commit(message, files)
        if not commit_result.ok:
            return FritzShipResult(
                ok=False, branch=branch, errors=[commit_result.stderr]
            )

        sha = await self.git.head_sha()

        push_results = await self.git.push_all_remotes(branch)
        errors = [
            f"{remote}: {r.stderr}"
            for remote, r in push_results.items()
            if not r.ok
        ]

        await self._audit(
            "FRITZ_DIRECT_PUSH",
            branch=branch,
            sha=sha,
            message=message,
            errors=errors,
        )

        return FritzShipResult(
            ok=not errors,
            branch=branch,
            commit_sha=sha,
            direct_push=True,
            errors=errors,
        )

    async def _pr_flow(
        self,
        message: str,
        files: list[str] | None,
        feature_branch: str,
        base_branch: str,
        pr_title: str,
        pr_body: str,
        reviewers: list[str],
        auto_merge: bool,
    ) -> FritzShipResult:
        """Create branch → commit → push → PR → (optionally) auto-merge."""
        # Create and switch to feature branch
        branch_result = await self.git.create_branch(feature_branch, from_branch=base_branch)
        if not branch_result.ok:
            # Branch might already exist — try to check it out
            checkout_result = await self.git.checkout(feature_branch)
            if not checkout_result.ok:
                return FritzShipResult(
                    ok=False,
                    branch=feature_branch,
                    errors=[f"Could not create/checkout branch: {branch_result.stderr}"],
                )

        # Commit
        commit_result = await self.git.commit(message, files)
        if not commit_result.ok:
            return FritzShipResult(
                ok=False, branch=feature_branch, errors=[commit_result.stderr]
            )

        sha = await self.git.head_sha()

        # Push to all remotes
        push_results = await self.git.push_all_remotes(feature_branch)
        push_errors = [
            f"{remote}: {r.stderr}"
            for remote, r in push_results.items()
            if not r.ok
        ]
        if push_errors:
            return FritzShipResult(
                ok=False, branch=feature_branch, commit_sha=sha, errors=push_errors
            )

        # Create PR on all enabled platforms
        pr_url = ""
        pr_number = None
        pr_errors: list[str] = []

        if self._github:
            pr_result = await self._github.create_pr(
                title=pr_title,
                body=pr_body,
                head=feature_branch,
                base=base_branch,
            )
            if pr_result.ok:
                pr_url = pr_result.url
                pr_number = (pr_result.data or {}).get("number")
                if reviewers:
                    await self._github.request_review(pr_number, reviewers)
            else:
                pr_errors.append(f"GitHub PR: {pr_result.error}")

        if self._gitea:
            gitea_pr = await self._gitea.create_pr(
                title=pr_title,
                body=pr_body,
                head=feature_branch,
                base=base_branch,
            )
            if not gitea_pr.ok:
                pr_errors.append(f"Gitea PR: {gitea_pr.error}")
            elif not pr_url:
                pr_url = gitea_pr.url

        await self._audit(
            "FRITZ_PR_CREATED",
            branch=feature_branch,
            base=base_branch,
            sha=sha,
            pr_url=pr_url,
            errors=pr_errors,
        )

        # Auto-merge if requested and policy allows
        merged = False
        merge_errors: list[str] = []

        if auto_merge and pr_number and self._github:
            ci_status: str | None = None
            if self._policy.evaluate_merge("", None).requires_ci:  # type: ignore[union-attr]
                ci_result = await self._github.wait_for_ci(
                    sha, timeout=self.config.push_policy.ci_timeout_seconds
                )
                ci_status = "success" if ci_result.ok else "failure"

            merge_decision = self._policy.evaluate_merge(  # type: ignore[union-attr]
                feature_branch, ci_status
            )
            if merge_decision.allowed:
                merge_result = await self._github.merge_pr(
                    pr_number, method=self.config.push_policy.auto_merge_method
                )
                merged = merge_result.ok
                if not merged:
                    merge_errors.append(f"Auto-merge failed: {merge_result.error}")
            else:
                logger.info(
                    "Auto-merge skipped for PR #%d: %s",
                    pr_number,
                    merge_decision.reason,
                )

        if merged:
            await self._audit("FRITZ_PR_MERGED", pr_number=pr_number, branch=feature_branch)

        all_errors = pr_errors + merge_errors
        return FritzShipResult(
            ok=not all_errors,
            branch=feature_branch,
            commit_sha=sha,
            pr_url=pr_url,
            pr_number=pr_number,
            merged=merged,
            errors=all_errors,
        )

    # ── Convenience pass-throughs ─────────────────────────────────────────────

    async def push(self, branch: str | None = None, force: bool = False) -> FritzGitResult:
        """Push a branch (respects push policy)."""
        branch = branch or await self.git.current_branch()
        decision = self._policy.evaluate_push(branch)  # type: ignore[union-attr]
        if not decision.allowed:
            raise PolicyViolation("push", branch, decision.reason)
        return await self.git.push(branch, force=force)

    async def create_pr(
        self,
        title: str,
        body: str,
        head: str,
        base: str | None = None,
        platform: str = "auto",
    ) -> FritzRemoteResult:
        """Create a PR on GitHub, Gitea, or both."""
        base = base or self.config.default_branch

        if platform == "github" or (platform == "auto" and self._github):
            return await self.github.create_pr(title, body, head, base)
        if platform == "gitea" or (platform == "auto" and self._gitea):
            return await self.gitea.create_pr(title, body, head, base)

        return FritzRemoteResult(
            ok=False, operation="create_pr", error="No remote platform enabled."
        )

    async def verify_connections(self) -> dict[str, bool]:
        """Test all configured remote connections. Returns {platform: ok}."""
        results: dict[str, bool] = {}
        if self._github:
            r = await self._github.whoami()
            results["github"] = r.ok
            if r.ok:
                user = (r.data or {}).get("login", "?")
                logger.info("GitHub authenticated as: %s", user)
        if self._gitea:
            r = await self._gitea.whoami()
            results["gitea"] = r.ok
            if r.ok:
                user = (r.data or {}).get("login", "?")
                logger.info("Gitea authenticated as: %s", user)
        return results

    # ── Audit logging ─────────────────────────────────────────────────────────

    async def _audit(self, event_type: str, **details: Any) -> None:
        """
        Write a structured event to Tinker's audit log if available.
        Silently skips if audit log isn't configured — Fritz should never
        crash because of a logging failure.
        """
        try:
            from ..observability.audit_log import AuditLog, AuditEventType  # type: ignore[import]

            log_path = self.config.audit_log_path or None
            audit = AuditLog(db_path=log_path) if log_path else AuditLog()
            await audit.log(
                event_type=AuditEventType.CUSTOM,
                actor="fritz",
                resource=details.get("branch", ""),
                outcome="ok" if not details.get("errors") else "error",
                details={"event": event_type, **details},
            )
        except Exception as exc:
            logger.debug("Fritz audit log skipped: %s", exc)
