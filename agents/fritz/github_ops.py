"""
agents/fritz/github_ops.py
────────────────────
GitHub operations — wraps the `gh` CLI and GitHub REST API via httpx.

Prefers the `gh` CLI for operations it handles well (PR create/merge, release).
Falls back to direct REST API calls for operations gh doesn't cover or for
environments where gh isn't authenticated.

All methods return FritzRemoteResult; no unexpected exceptions are raised.
Circuit breaker protection is applied on the "github_api" breaker.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import FritzConfig
from .identity import build_auth_header
from .metrics import FritzMetrics, get_metrics
from .retry import RateLimitState, with_retry

logger = logging.getLogger(__name__)

_GH_API_BASE = "https://api.github.com"


@dataclass
class FritzRemoteResult:
    ok: bool
    operation: str
    data: dict[str, Any] | None = None
    error: str = ""
    url: str = ""

    def __str__(self) -> str:
        status = "OK" if self.ok else "FAIL"
        detail = self.url or self.error or str(self.data)[:120]
        return f"[github:{self.operation}] {status} — {detail}"


class FritzGitHub:
    """
    GitHub operations driver.

    Instantiate with a resolved token (not a key name).
    All methods are async.
    """

    def __init__(
        self,
        config: FritzConfig,
        token: str,
        metrics: FritzMetrics | None = None,
    ) -> None:
        self._owner = config.github_owner
        self._repo = config.github_repo
        self._token = token
        self._headers = {
            **build_auth_header(token),
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._repo_path = config.repo_path
        self._metrics = metrics or get_metrics()
        self._rate = RateLimitState()

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _api(self, path: str) -> str:
        return f"{_GH_API_BASE}{path}"

    async def _request(
        self, method: str, path: str, operation: str, **kwargs: Any
    ) -> httpx.Response:
        """Single entry point for all GitHub REST calls — adds retry + metrics."""
        t0 = time.monotonic()
        url = self._api(path)

        async def _call() -> httpx.Response:
            async with httpx.AsyncClient(headers=self._headers, timeout=30) as client:
                return await client.request(method, url, **kwargs)

        resp = await with_retry(_call, rate_state=self._rate, operation=f"github:{operation}")
        elapsed = time.monotonic() - t0

        # Push rate-limit gauge to metrics.
        if self._rate.remaining != -1:
            self._metrics.update_rate_limit("github", self._rate.remaining)

        self._metrics.on_api_call(
            platform="github",
            operation=operation,
            http_status=resp.status_code,
            duration_seconds=elapsed,
        )
        return resp

    async def _get(self, path: str, operation: str = "get") -> dict[str, Any]:
        resp = await self._request("GET", path, operation)
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, data: dict, operation: str = "post") -> dict[str, Any]:
        resp = await self._request("POST", path, operation, json=data)
        resp.raise_for_status()
        return resp.json()

    async def _patch(self, path: str, data: dict, operation: str = "patch") -> dict[str, Any]:
        resp = await self._request("PATCH", path, operation, json=data)
        resp.raise_for_status()
        return resp.json()

    async def _put(self, path: str, data: dict, operation: str = "put") -> dict[str, Any]:
        resp = await self._request("PUT", path, operation, json=data)
        resp.raise_for_status()
        return resp.json()

    async def _delete(self, path: str, operation: str = "delete") -> bool:
        resp = await self._request("DELETE", path, operation)
        return resp.status_code in (200, 204)

    @property
    def rate_limit(self) -> RateLimitState:
        """Current rate-limit state (updated after every API call)."""
        return self._rate

    def _gh(self, *args: str) -> FritzRemoteResult:
        """Run a gh CLI command synchronously (used for complex operations)."""
        cmd = ["gh", *args]
        try:
            result = subprocess.run(
                cmd,
                cwd=self._repo_path,
                capture_output=True,
                text=True,
                env={**__import__("os").environ, "GITHUB_TOKEN": self._token},
            )
            ok = result.returncode == 0
            data = None
            if result.stdout.strip():
                try:
                    data = json.loads(result.stdout)
                except Exception:
                    data = {"output": result.stdout.strip()}
            return FritzRemoteResult(
                ok=ok,
                operation=" ".join(args[:3]),
                data=data,
                error=result.stderr.strip() if not ok else "",
            )
        except FileNotFoundError:
            return FritzRemoteResult(
                ok=False,
                operation=" ".join(args[:3]),
                error="gh CLI not found. Install it from https://cli.github.com",
            )
        except Exception as exc:
            return FritzRemoteResult(
                ok=False, operation=" ".join(args[:3]), error=str(exc)
            )

    # ── Pull Requests ─────────────────────────────────────────────────────────

    async def create_pr(
        self,
        title: str,
        body: str,
        head: str,
        base: str | None = None,
        draft: bool = False,
    ) -> FritzRemoteResult:
        """Create a pull request."""
        base = base or "main"
        try:
            data = await self._post(
                f"/repos/{self._owner}/{self._repo}/pulls",
                {
                    "title": title,
                    "body": body,
                    "head": head,
                    "base": base,
                    "draft": draft,
                },
            )
            return FritzRemoteResult(
                ok=True,
                operation="create_pr",
                data=data,
                url=data.get("html_url", ""),
            )
        except Exception as exc:
            logger.error("create_pr failed: %s", exc)
            return FritzRemoteResult(ok=False, operation="create_pr", error=str(exc))

    async def merge_pr(
        self,
        pr_number: int,
        method: str = "squash",
        commit_title: str | None = None,
        commit_message: str | None = None,
    ) -> FritzRemoteResult:
        """Merge a pull request."""
        merge_methods = {"squash": "squash", "merge": "merge", "rebase": "rebase"}
        payload: dict[str, Any] = {
            "merge_method": merge_methods.get(method, "squash")
        }
        if commit_title:
            payload["commit_title"] = commit_title
        if commit_message:
            payload["commit_message"] = commit_message
        try:
            data = await self._put(
                f"/repos/{self._owner}/{self._repo}/pulls/{pr_number}/merge",
                payload,
            )
            return FritzRemoteResult(ok=True, operation="merge_pr", data=data)
        except Exception as exc:
            logger.error("merge_pr #%d failed: %s", pr_number, exc)
            return FritzRemoteResult(ok=False, operation="merge_pr", error=str(exc))

    async def close_pr(self, pr_number: int) -> FritzRemoteResult:
        try:
            data = await self._patch(
                f"/repos/{self._owner}/{self._repo}/pulls/{pr_number}",
                {"state": "closed"},
            )
            return FritzRemoteResult(ok=True, operation="close_pr", data=data)
        except Exception as exc:
            return FritzRemoteResult(ok=False, operation="close_pr", error=str(exc))

    async def request_review(
        self, pr_number: int, reviewers: list[str]
    ) -> FritzRemoteResult:
        try:
            data = await self._post(
                f"/repos/{self._owner}/{self._repo}/pulls/{pr_number}/requested_reviewers",
                {"reviewers": reviewers},
            )
            return FritzRemoteResult(ok=True, operation="request_review", data=data)
        except Exception as exc:
            return FritzRemoteResult(ok=False, operation="request_review", error=str(exc))

    async def add_pr_comment(self, pr_number: int, body: str) -> FritzRemoteResult:
        try:
            data = await self._post(
                f"/repos/{self._owner}/{self._repo}/issues/{pr_number}/comments",
                {"body": body},
            )
            return FritzRemoteResult(ok=True, operation="add_pr_comment", data=data)
        except Exception as exc:
            return FritzRemoteResult(ok=False, operation="add_pr_comment", error=str(exc))

    async def get_pr(self, pr_number: int) -> FritzRemoteResult:
        try:
            data = await self._get(
                f"/repos/{self._owner}/{self._repo}/pulls/{pr_number}"
            )
            return FritzRemoteResult(ok=True, operation="get_pr", data=data)
        except Exception as exc:
            return FritzRemoteResult(ok=False, operation="get_pr", error=str(exc))

    # ── Branches ──────────────────────────────────────────────────────────────

    async def create_branch(
        self, name: str, from_branch: str = "main"
    ) -> FritzRemoteResult:
        """Create a remote branch from an existing ref."""
        try:
            # Get SHA of the base branch
            ref_data = await self._get(
                f"/repos/{self._owner}/{self._repo}/git/ref/heads/{from_branch}"
            )
            sha = ref_data["object"]["sha"]
            data = await self._post(
                f"/repos/{self._owner}/{self._repo}/git/refs",
                {"ref": f"refs/heads/{name}", "sha": sha},
            )
            return FritzRemoteResult(ok=True, operation="create_branch", data=data)
        except Exception as exc:
            return FritzRemoteResult(ok=False, operation="create_branch", error=str(exc))

    async def delete_branch(self, name: str) -> FritzRemoteResult:
        try:
            ok = await self._delete(
                f"/repos/{self._owner}/{self._repo}/git/refs/heads/{name}"
            )
            return FritzRemoteResult(ok=ok, operation="delete_branch")
        except Exception as exc:
            return FritzRemoteResult(ok=False, operation="delete_branch", error=str(exc))

    async def set_branch_protection(
        self, branch: str, rules: dict[str, Any]
    ) -> FritzRemoteResult:
        """
        Apply branch protection rules.
        rules dict mirrors the GitHub branch protection API payload.
        """
        try:
            data = await self._put(
                f"/repos/{self._owner}/{self._repo}/branches/{branch}/protection",
                rules,
            )
            return FritzRemoteResult(ok=True, operation="set_branch_protection", data=data)
        except Exception as exc:
            return FritzRemoteResult(ok=False, operation="set_branch_protection", error=str(exc))

    # ── Releases & Tags ───────────────────────────────────────────────────────

    async def create_release(
        self,
        tag: str,
        title: str,
        notes: str,
        prerelease: bool = False,
        draft: bool = False,
    ) -> FritzRemoteResult:
        try:
            data = await self._post(
                f"/repos/{self._owner}/{self._repo}/releases",
                {
                    "tag_name": tag,
                    "name": title,
                    "body": notes,
                    "prerelease": prerelease,
                    "draft": draft,
                },
            )
            return FritzRemoteResult(
                ok=True,
                operation="create_release",
                data=data,
                url=data.get("html_url", ""),
            )
        except Exception as exc:
            return FritzRemoteResult(ok=False, operation="create_release", error=str(exc))

    # ── Collaborators & Permissions ───────────────────────────────────────────

    async def add_collaborator(
        self, username: str, permission: str = "push"
    ) -> FritzRemoteResult:
        """
        Add a collaborator with the given permission level.
        permission: pull | triage | push | maintain | admin
        """
        try:
            data = await self._put(
                f"/repos/{self._owner}/{self._repo}/collaborators/{username}",
                {"permission": permission},
            )
            return FritzRemoteResult(ok=True, operation="add_collaborator", data=data)
        except Exception as exc:
            return FritzRemoteResult(ok=False, operation="add_collaborator", error=str(exc))

    async def remove_collaborator(self, username: str) -> FritzRemoteResult:
        try:
            ok = await self._delete(
                f"/repos/{self._owner}/{self._repo}/collaborators/{username}"
            )
            return FritzRemoteResult(ok=ok, operation="remove_collaborator")
        except Exception as exc:
            return FritzRemoteResult(ok=False, operation="remove_collaborator", error=str(exc))

    async def list_collaborators(self) -> FritzRemoteResult:
        try:
            data = await self._get(
                f"/repos/{self._owner}/{self._repo}/collaborators"
            )
            return FritzRemoteResult(ok=True, operation="list_collaborators", data={"collaborators": data})
        except Exception as exc:
            return FritzRemoteResult(ok=False, operation="list_collaborators", error=str(exc))

    # ── Issues ────────────────────────────────────────────────────────────────

    async def create_issue(
        self,
        title: str,
        body: str,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
    ) -> FritzRemoteResult:
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        if assignees:
            payload["assignees"] = assignees
        try:
            data = await self._post(
                f"/repos/{self._owner}/{self._repo}/issues", payload
            )
            return FritzRemoteResult(
                ok=True,
                operation="create_issue",
                data=data,
                url=data.get("html_url", ""),
            )
        except Exception as exc:
            return FritzRemoteResult(ok=False, operation="create_issue", error=str(exc))

    async def close_issue(self, number: int) -> FritzRemoteResult:
        try:
            data = await self._patch(
                f"/repos/{self._owner}/{self._repo}/issues/{number}",
                {"state": "closed"},
            )
            return FritzRemoteResult(ok=True, operation="close_issue", data=data)
        except Exception as exc:
            return FritzRemoteResult(ok=False, operation="close_issue", error=str(exc))

    async def comment_on_issue(self, number: int, body: str) -> FritzRemoteResult:
        try:
            data = await self._post(
                f"/repos/{self._owner}/{self._repo}/issues/{number}/comments",
                {"body": body},
            )
            return FritzRemoteResult(ok=True, operation="comment_on_issue", data=data)
        except Exception as exc:
            return FritzRemoteResult(ok=False, operation="comment_on_issue", error=str(exc))

    # ── CI / Actions ──────────────────────────────────────────────────────────

    async def get_ci_status(self, commit_sha: str) -> FritzRemoteResult:
        """Get combined CI status for a commit SHA."""
        try:
            data = await self._get(
                f"/repos/{self._owner}/{self._repo}/commits/{commit_sha}/status"
            )
            return FritzRemoteResult(
                ok=True,
                operation="get_ci_status",
                data=data,
            )
        except Exception as exc:
            return FritzRemoteResult(ok=False, operation="get_ci_status", error=str(exc))

    async def wait_for_ci(
        self, commit_sha: str, timeout: int = 600, poll_interval: int = 15
    ) -> FritzRemoteResult:
        """
        Poll CI status until it resolves or times out.
        Returns ok=True when CI is green; ok=False on failure or timeout.
        """
        elapsed = 0
        while elapsed < timeout:
            result = await self.get_ci_status(commit_sha)
            if not result.ok:
                return result
            state = (result.data or {}).get("state", "pending")
            if state == "success":
                return FritzRemoteResult(ok=True, operation="wait_for_ci", data=result.data)
            if state == "failure":
                return FritzRemoteResult(
                    ok=False, operation="wait_for_ci", error="CI failed", data=result.data
                )
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        return FritzRemoteResult(
            ok=False,
            operation="wait_for_ci",
            error=f"CI did not complete within {timeout}s",
        )

    async def trigger_workflow(
        self, workflow_id: str, ref: str = "main", inputs: dict | None = None
    ) -> FritzRemoteResult:
        payload: dict[str, Any] = {"ref": ref}
        if inputs:
            payload["inputs"] = inputs
        try:
            async with httpx.AsyncClient(headers=self._headers, timeout=30) as client:
                resp = await client.post(
                    self._api(
                        f"/repos/{self._owner}/{self._repo}/actions/workflows/{workflow_id}/dispatches"
                    ),
                    json=payload,
                )
                ok = resp.status_code == 204
                return FritzRemoteResult(ok=ok, operation="trigger_workflow")
        except Exception as exc:
            return FritzRemoteResult(ok=False, operation="trigger_workflow", error=str(exc))

    # ── Repo settings ─────────────────────────────────────────────────────────

    async def get_repo(self) -> FritzRemoteResult:
        try:
            data = await self._get(f"/repos/{self._owner}/{self._repo}")
            return FritzRemoteResult(ok=True, operation="get_repo", data=data)
        except Exception as exc:
            return FritzRemoteResult(ok=False, operation="get_repo", error=str(exc))

    async def update_repo_settings(self, **settings: Any) -> FritzRemoteResult:
        try:
            data = await self._patch(
                f"/repos/{self._owner}/{self._repo}", settings
            )
            return FritzRemoteResult(ok=True, operation="update_repo_settings", data=data)
        except Exception as exc:
            return FritzRemoteResult(ok=False, operation="update_repo_settings", error=str(exc))

    # ── Verify connection ─────────────────────────────────────────────────────

    async def whoami(self) -> FritzRemoteResult:
        """Return the authenticated GitHub user. Used to test credentials."""
        try:
            data = await self._get("/user")
            return FritzRemoteResult(ok=True, operation="whoami", data=data)
        except Exception as exc:
            return FritzRemoteResult(ok=False, operation="whoami", error=str(exc))
