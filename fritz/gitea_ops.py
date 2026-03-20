"""
fritz/gitea_ops.py
───────────────────
Gitea operations driver — talks directly to the Gitea REST API via httpx.

Designed for self-hosted Gitea instances. Key differences vs GitHub:
  - Base URL is configurable (e.g. https://gitea.yourdomain.com or http://localhost:3000)
  - Auth header uses "token <value>" format (same as GitHub PATs)
  - Self-signed TLS is common; tls_verify=False supported
  - CI/CD is provider-agnostic: Gitea Actions, Woodpecker, Drone, or none
  - Webhooks are first-class (useful for triggering local pipelines)
  - SSH port is often non-standard on self-hosted deployments

API reference: https://gitea.example.com/api/swagger  (available on every Gitea instance)

All methods return FritzRemoteResult (same type as github_ops for consistency).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .config import FritzConfig
from .github_ops import FritzRemoteResult  # reuse the same result type
from .identity import build_auth_header

logger = logging.getLogger(__name__)


class FritzGitea:
    """
    Gitea REST API driver.

    Instantiate with a resolved token (not a key name).
    All methods are async.
    """

    def __init__(self, config: FritzConfig, token: str) -> None:
        base = config.gitea_base_url.rstrip("/")
        self._api_base = f"{base}/api/v1"
        self._owner = config.gitea_owner
        self._repo = config.gitea_repo
        self._token = token
        self._tls_verify = config.gitea_tls_verify
        self._ci_provider = config.gitea_ci_provider
        self._headers = {
            **build_auth_header(token),
            "Content-Type": "application/json",
        }

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers=self._headers,
            verify=self._tls_verify,
            timeout=30,
        )

    def _url(self, path: str) -> str:
        return f"{self._api_base}{path}"

    async def _get(self, path: str) -> Any:
        async with self._client() as client:
            resp = await client.get(self._url(path))
            resp.raise_for_status()
            return resp.json()

    async def _post(self, path: str, data: dict) -> Any:
        async with self._client() as client:
            resp = await client.post(self._url(path), json=data)
            resp.raise_for_status()
            return resp.json()

    async def _patch(self, path: str, data: dict) -> Any:
        async with self._client() as client:
            resp = await client.patch(self._url(path), json=data)
            resp.raise_for_status()
            return resp.json()

    async def _delete(self, path: str) -> bool:
        async with self._client() as client:
            resp = await client.delete(self._url(path))
            return resp.status_code in (200, 204)

    # ── Identity / connection ─────────────────────────────────────────────────

    async def whoami(self) -> FritzRemoteResult:
        """Return the authenticated Gitea user. Used to test credentials."""
        try:
            data = await self._get("/user")
            return FritzRemoteResult(ok=True, operation="whoami", data=data)
        except Exception as exc:
            return FritzRemoteResult(ok=False, operation="whoami", error=str(exc))

    # ── Pull Requests ─────────────────────────────────────────────────────────

    async def create_pr(
        self,
        title: str,
        body: str,
        head: str,
        base: str | None = None,
        draft: bool = False,
    ) -> FritzRemoteResult:
        base = base or "main"
        try:
            data = await self._post(
                f"/repos/{self._owner}/{self._repo}/pulls",
                {
                    "title": title,
                    "body": body,
                    "head": head,
                    "base": base,
                    "draft_id": 1 if draft else 0,  # Gitea draft field
                },
            )
            return FritzRemoteResult(
                ok=True,
                operation="create_pr",
                data=data,
                url=data.get("html_url", ""),
            )
        except Exception as exc:
            logger.error("gitea create_pr failed: %s", exc)
            return FritzRemoteResult(ok=False, operation="create_pr", error=str(exc))

    async def merge_pr(
        self,
        pr_number: int,
        method: str = "squash",
        commit_title: str | None = None,
        commit_message: str | None = None,
    ) -> FritzRemoteResult:
        """
        Merge a PR.
        method: merge | rebase | squash | fast-forward-only
        Gitea calls these: merge | rebase | squash | fast-forward-only
        """
        styles = {
            "merge": "merge",
            "rebase": "rebase",
            "squash": "squash",
            "rebase": "rebase",
        }
        payload: dict[str, Any] = {"Do": styles.get(method, "squash")}
        if commit_title:
            payload["MergeMessageField"] = commit_title
        try:
            async with self._client() as client:
                resp = await client.post(
                    self._url(f"/repos/{self._owner}/{self._repo}/pulls/{pr_number}/merge"),
                    json=payload,
                )
                ok = resp.status_code in (200, 204)
                return FritzRemoteResult(ok=ok, operation="merge_pr")
        except Exception as exc:
            logger.error("gitea merge_pr #%d failed: %s", pr_number, exc)
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

    # ── Branches ──────────────────────────────────────────────────────────────

    async def create_branch(
        self, name: str, from_branch: str = "main"
    ) -> FritzRemoteResult:
        try:
            data = await self._post(
                f"/repos/{self._owner}/{self._repo}/branches",
                {"new_branch_name": name, "old_branch_name": from_branch},
            )
            return FritzRemoteResult(ok=True, operation="create_branch", data=data)
        except Exception as exc:
            return FritzRemoteResult(ok=False, operation="create_branch", error=str(exc))

    async def delete_branch(self, name: str) -> FritzRemoteResult:
        try:
            ok = await self._delete(
                f"/repos/{self._owner}/{self._repo}/branches/{name}"
            )
            return FritzRemoteResult(ok=ok, operation="delete_branch")
        except Exception as exc:
            return FritzRemoteResult(ok=False, operation="delete_branch", error=str(exc))

    async def set_branch_protection(
        self, branch: str, rules: dict[str, Any]
    ) -> FritzRemoteResult:
        """
        Set branch protection rules.
        rules dict mirrors Gitea's BranchProtection API payload.
        Key fields: enable_push, required_approvals, enable_status_check, etc.
        """
        try:
            # Gitea uses PUT to create/replace protection rules
            async with self._client() as client:
                resp = await client.post(
                    self._url(f"/repos/{self._owner}/{self._repo}/branch_protections"),
                    json={"branch_name": branch, **rules},
                )
                ok = resp.status_code in (200, 201)
                data = resp.json() if ok else None
                return FritzRemoteResult(ok=ok, operation="set_branch_protection", data=data)
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

    # ── Collaborators ─────────────────────────────────────────────────────────

    async def add_collaborator(
        self, username: str, permission: str = "write"
    ) -> FritzRemoteResult:
        """
        Add a collaborator.
        permission: read | write | admin
        """
        try:
            async with self._client() as client:
                resp = await client.put(
                    self._url(
                        f"/repos/{self._owner}/{self._repo}/collaborators/{username}"
                    ),
                    json={"permission": permission},
                )
                ok = resp.status_code in (200, 204)
                return FritzRemoteResult(ok=ok, operation="add_collaborator")
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
            return FritzRemoteResult(
                ok=True,
                operation="list_collaborators",
                data={"collaborators": data},
            )
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

    # ── Webhooks (Gitea-specific) ─────────────────────────────────────────────

    async def create_webhook(
        self,
        target_url: str,
        events: list[str] | None = None,
        secret: str = "",
    ) -> FritzRemoteResult:
        """
        Create a repository webhook.
        events: list of Gitea event names, e.g. ["push", "pull_request", "release"]
        Default: all events.
        """
        events = events or ["push", "pull_request", "issues", "release"]
        try:
            data = await self._post(
                f"/repos/{self._owner}/{self._repo}/hooks",
                {
                    "type": "gitea",
                    "config": {
                        "url": target_url,
                        "content_type": "json",
                        "secret": secret,
                    },
                    "events": events,
                    "active": True,
                },
            )
            return FritzRemoteResult(ok=True, operation="create_webhook", data=data)
        except Exception as exc:
            return FritzRemoteResult(ok=False, operation="create_webhook", error=str(exc))

    async def delete_webhook(self, hook_id: int) -> FritzRemoteResult:
        try:
            ok = await self._delete(
                f"/repos/{self._owner}/{self._repo}/hooks/{hook_id}"
            )
            return FritzRemoteResult(ok=ok, operation="delete_webhook")
        except Exception as exc:
            return FritzRemoteResult(ok=False, operation="delete_webhook", error=str(exc))

    async def list_webhooks(self) -> FritzRemoteResult:
        try:
            data = await self._get(f"/repos/{self._owner}/{self._repo}/hooks")
            return FritzRemoteResult(ok=True, operation="list_webhooks", data={"hooks": data})
        except Exception as exc:
            return FritzRemoteResult(ok=False, operation="list_webhooks", error=str(exc))

    # ── CI Status ─────────────────────────────────────────────────────────────

    async def get_ci_status(self, commit_sha: str) -> FritzRemoteResult:
        """
        Get combined commit status (works for Gitea Actions and external CI
        providers that report status via the Gitea Statuses API).
        """
        try:
            data = await self._get(
                f"/repos/{self._owner}/{self._repo}/commits/{commit_sha}/statuses"
            )
            # Compute combined state from list of statuses
            statuses = data if isinstance(data, list) else []
            if not statuses:
                state = "pending"
            elif all(s.get("status") == "success" for s in statuses):
                state = "success"
            elif any(s.get("status") in ("failure", "error") for s in statuses):
                state = "failure"
            else:
                state = "pending"
            return FritzRemoteResult(
                ok=True,
                operation="get_ci_status",
                data={"state": state, "statuses": statuses},
            )
        except Exception as exc:
            return FritzRemoteResult(ok=False, operation="get_ci_status", error=str(exc))

    async def set_commit_status(
        self,
        sha: str,
        state: str,
        description: str = "",
        context: str = "fritz",
        target_url: str = "",
    ) -> FritzRemoteResult:
        """
        Report a commit status (useful when Fritz itself is a CI step).
        state: pending | success | error | failure | warning
        """
        try:
            data = await self._post(
                f"/repos/{self._owner}/{self._repo}/statuses/{sha}",
                {
                    "state": state,
                    "description": description,
                    "context": context,
                    "target_url": target_url,
                },
            )
            return FritzRemoteResult(ok=True, operation="set_commit_status", data=data)
        except Exception as exc:
            return FritzRemoteResult(ok=False, operation="set_commit_status", error=str(exc))

    async def wait_for_ci(
        self, commit_sha: str, timeout: int = 600, poll_interval: int = 15
    ) -> FritzRemoteResult:
        """Poll CI status until resolved or timeout."""
        elapsed = 0
        while elapsed < timeout:
            result = await self.get_ci_status(commit_sha)
            if not result.ok:
                return result
            state = (result.data or {}).get("state", "pending")
            if state == "success":
                return FritzRemoteResult(ok=True, operation="wait_for_ci", data=result.data)
            if state in ("failure", "error"):
                return FritzRemoteResult(
                    ok=False, operation="wait_for_ci", error=f"CI state: {state}", data=result.data
                )
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
        return FritzRemoteResult(
            ok=False,
            operation="wait_for_ci",
            error=f"CI did not complete within {timeout}s",
        )

    # ── Repo info ─────────────────────────────────────────────────────────────

    async def get_repo(self) -> FritzRemoteResult:
        try:
            data = await self._get(f"/repos/{self._owner}/{self._repo}")
            return FritzRemoteResult(ok=True, operation="get_repo", data=data)
        except Exception as exc:
            return FritzRemoteResult(ok=False, operation="get_repo", error=str(exc))
