"""
fritz/tests/test_gitea_ops.py
──────────────────────────────
Tests for FritzGitea. HTTP calls are mocked at the _request level so no real
Gitea instance is needed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agents.fritz.config import FritzConfig
from agents.fritz.gitea_ops import FritzGitea
from agents.fritz.github_ops import FritzRemoteResult
from agents.fritz.metrics import FritzMetrics


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_config(
    owner: str = "acme",
    repo: str = "widget",
    base_url: str = "https://gitea.example.com",
) -> FritzConfig:
    return FritzConfig(
        gitea_enabled=True,
        gitea_base_url=base_url,
        gitea_owner=owner,
        gitea_repo=repo,
    )


def _make_gitea(owner: str = "acme", repo: str = "widget") -> FritzGitea:
    cfg = _make_config(owner, repo)
    metrics = MagicMock(spec=FritzMetrics)
    metrics.update_rate_limit = MagicMock()
    metrics.on_api_call = MagicMock()
    return FritzGitea(cfg, token="gitea_test_token", metrics=metrics)


def _resp(status: int, body: dict | list | None = None) -> httpx.Response:
    import json as _json
    content = _json.dumps(body or {}).encode()
    response = httpx.Response(status, content=content)
    response.request = httpx.Request("GET", "https://gitea.example.com/api/v1/")
    return response


def _patch_request(gitea: FritzGitea, status: int, body: dict | list | None = None):
    resp = _resp(status, body)
    return patch.object(gitea, "_request", new=AsyncMock(return_value=resp))


# ── whoami ────────────────────────────────────────────────────────────────────


class TestWhoami:
    @pytest.mark.asyncio
    async def test_success(self):
        gitea = _make_gitea()
        with _patch_request(gitea, 200, {"login": "fritz-bot", "id": 1}):
            result = await gitea.whoami()
        assert result.ok
        assert result.data["login"] == "fritz-bot"

    @pytest.mark.asyncio
    async def test_failure(self):
        gitea = _make_gitea()
        with patch.object(gitea, "_request", new=AsyncMock(side_effect=Exception("401"))):
            result = await gitea.whoami()
        assert not result.ok
        assert result.operation == "whoami"


# ── create_pr ─────────────────────────────────────────────────────────────────


class TestCreatePr:
    @pytest.mark.asyncio
    async def test_success(self):
        gitea = _make_gitea()
        pr_data = {
            "number": 10,
            "html_url": "https://gitea.example.com/acme/widget/pulls/10",
        }
        with _patch_request(gitea, 201, pr_data):
            result = await gitea.create_pr("Fix typo", "body", "feature/typo", "main")
        assert result.ok
        assert result.url.endswith("/10")

    @pytest.mark.asyncio
    async def test_failure(self):
        gitea = _make_gitea()
        with patch.object(gitea, "_request", new=AsyncMock(side_effect=Exception("API error"))):
            result = await gitea.create_pr("Fix", "body", "feature/x")
        assert not result.ok
        assert result.operation == "create_pr"

    @pytest.mark.asyncio
    async def test_default_base_is_main(self):
        gitea = _make_gitea()
        pr_data = {"number": 11, "html_url": "https://gitea.example.com/acme/widget/pulls/11"}
        with _patch_request(gitea, 201, pr_data):
            result = await gitea.create_pr("Fix", "body", "feature/x")
        assert result.ok


# ── close_pr ──────────────────────────────────────────────────────────────────


class TestClosePr:
    @pytest.mark.asyncio
    async def test_success(self):
        gitea = _make_gitea()
        with patch.object(
            gitea, "_patch", new=AsyncMock(return_value={"state": "closed"})
        ):
            result = await gitea.close_pr(5)
        assert result.ok

    @pytest.mark.asyncio
    async def test_failure(self):
        gitea = _make_gitea()
        with patch.object(gitea, "_patch", new=AsyncMock(side_effect=Exception("not found"))):
            result = await gitea.close_pr(99)
        assert not result.ok


# ── create_branch ─────────────────────────────────────────────────────────────


class TestCreateBranch:
    @pytest.mark.asyncio
    async def test_success(self):
        gitea = _make_gitea()
        branch_data = {"name": "feature/new"}
        with _patch_request(gitea, 201, branch_data):
            result = await gitea.create_branch("feature/new", from_branch="main")
        assert result.ok

    @pytest.mark.asyncio
    async def test_failure(self):
        gitea = _make_gitea()
        with patch.object(gitea, "_request", new=AsyncMock(side_effect=Exception("conflict"))):
            result = await gitea.create_branch("feature/existing")
        assert not result.ok


# ── delete_branch ─────────────────────────────────────────────────────────────


class TestDeleteBranch:
    @pytest.mark.asyncio
    async def test_success(self):
        gitea = _make_gitea()
        with _patch_request(gitea, 204):
            result = await gitea.delete_branch("feature/old")
        assert result.ok

    @pytest.mark.asyncio
    async def test_failure(self):
        gitea = _make_gitea()
        with patch.object(gitea, "_request", new=AsyncMock(side_effect=Exception("not found"))):
            result = await gitea.delete_branch("feature/missing")
        assert not result.ok


# ── create_release ────────────────────────────────────────────────────────────


class TestCreateRelease:
    @pytest.mark.asyncio
    async def test_success(self):
        gitea = _make_gitea()
        release_data = {
            "id": 5,
            "html_url": "https://gitea.example.com/acme/widget/releases/tag/v1.0",
        }
        with _patch_request(gitea, 201, release_data):
            result = await gitea.create_release("v1.0", "Version 1.0", "Notes")
        assert result.ok
        assert result.url.endswith("v1.0")

    @pytest.mark.asyncio
    async def test_failure(self):
        gitea = _make_gitea()
        with patch.object(gitea, "_request", new=AsyncMock(side_effect=Exception("error"))):
            result = await gitea.create_release("v1.0", "Version 1.0", "Notes")
        assert not result.ok


# ── webhooks ──────────────────────────────────────────────────────────────────


class TestWebhooks:
    @pytest.mark.asyncio
    async def test_create_webhook(self):
        gitea = _make_gitea()
        hook_data = {"id": 3, "config": {"url": "https://ci.example.com/hook"}}
        with _patch_request(gitea, 201, hook_data):
            result = await gitea.create_webhook("https://ci.example.com/hook")
        assert result.ok

    @pytest.mark.asyncio
    async def test_delete_webhook(self):
        gitea = _make_gitea()
        with _patch_request(gitea, 204):
            result = await gitea.delete_webhook(3)
        assert result.ok

    @pytest.mark.asyncio
    async def test_list_webhooks(self):
        gitea = _make_gitea()
        hooks = [{"id": 1}, {"id": 2}]
        with _patch_request(gitea, 200, hooks):
            result = await gitea.list_webhooks()
        assert result.ok
        assert len(result.data["hooks"]) == 2


# ── get_ci_status ─────────────────────────────────────────────────────────────


class TestGetCiStatus:
    @pytest.mark.asyncio
    async def test_all_success(self):
        gitea = _make_gitea()
        statuses = [{"status": "success"}, {"status": "success"}]
        with _patch_request(gitea, 200, statuses):
            result = await gitea.get_ci_status("abc123")
        assert result.ok
        assert result.data["state"] == "success"

    @pytest.mark.asyncio
    async def test_any_failure(self):
        gitea = _make_gitea()
        statuses = [{"status": "success"}, {"status": "failure"}]
        with _patch_request(gitea, 200, statuses):
            result = await gitea.get_ci_status("abc123")
        assert result.ok
        assert result.data["state"] == "failure"

    @pytest.mark.asyncio
    async def test_empty_statuses_are_pending(self):
        gitea = _make_gitea()
        with _patch_request(gitea, 200, []):
            result = await gitea.get_ci_status("abc123")
        assert result.ok
        assert result.data["state"] == "pending"

    @pytest.mark.asyncio
    async def test_failure_on_api_error(self):
        gitea = _make_gitea()
        with patch.object(gitea, "_request", new=AsyncMock(side_effect=Exception("network"))):
            result = await gitea.get_ci_status("abc123")
        assert not result.ok


# ── set_commit_status ─────────────────────────────────────────────────────────


class TestSetCommitStatus:
    @pytest.mark.asyncio
    async def test_success(self):
        gitea = _make_gitea()
        status_data = {"id": 1, "status": "success"}
        with _patch_request(gitea, 201, status_data):
            result = await gitea.set_commit_status("abc123", "success", "All good")
        assert result.ok

    @pytest.mark.asyncio
    async def test_failure(self):
        gitea = _make_gitea()
        with patch.object(gitea, "_request", new=AsyncMock(side_effect=Exception("error"))):
            result = await gitea.set_commit_status("abc123", "failure")
        assert not result.ok


# ── wait_for_ci ───────────────────────────────────────────────────────────────


class TestWaitForCi:
    @pytest.mark.asyncio
    async def test_immediate_success(self):
        gitea = _make_gitea()
        with patch.object(
            gitea,
            "get_ci_status",
            new=AsyncMock(return_value=FritzRemoteResult(
                ok=True, operation="get_ci_status", data={"state": "success"}
            )),
        ):
            result = await gitea.wait_for_ci("sha123", timeout=60, poll_interval=1)
        assert result.ok

    @pytest.mark.asyncio
    async def test_failure_state(self):
        gitea = _make_gitea()
        with patch.object(
            gitea,
            "get_ci_status",
            new=AsyncMock(return_value=FritzRemoteResult(
                ok=True, operation="get_ci_status", data={"state": "failure"}
            )),
        ):
            result = await gitea.wait_for_ci("sha123", timeout=60, poll_interval=1)
        assert not result.ok

    @pytest.mark.asyncio
    async def test_timeout(self):
        gitea = _make_gitea()
        with patch.object(
            gitea,
            "get_ci_status",
            new=AsyncMock(return_value=FritzRemoteResult(
                ok=True, operation="get_ci_status", data={"state": "pending"}
            )),
        ), patch("asyncio.sleep", new=AsyncMock()):
            result = await gitea.wait_for_ci("sha123", timeout=2, poll_interval=5)
        assert not result.ok
        assert "did not complete" in result.error


# ── rate_limit property ───────────────────────────────────────────────────────


class TestRateLimitProperty:
    def test_returns_rate_limit_state(self):
        gitea = _make_gitea()
        from agents.fritz.retry import RateLimitState
        assert isinstance(gitea.rate_limit, RateLimitState)

    def test_base_url_stripped(self):
        """Trailing slash in base URL should be handled."""
        cfg = _make_config(base_url="https://gitea.example.com/")
        metrics = MagicMock(spec=FritzMetrics)
        metrics.update_rate_limit = MagicMock()
        metrics.on_api_call = MagicMock()
        gitea = FritzGitea(cfg, token="tok", metrics=metrics)
        assert not gitea._api_base.endswith("//")


# ── issues ────────────────────────────────────────────────────────────────────


class TestIssues:
    @pytest.mark.asyncio
    async def test_create_issue(self):
        gitea = _make_gitea()
        issue_data = {"number": 1, "html_url": "https://gitea.example.com/acme/widget/issues/1"}
        with _patch_request(gitea, 201, issue_data):
            result = await gitea.create_issue("Bug", "description")
        assert result.ok

    @pytest.mark.asyncio
    async def test_close_issue(self):
        gitea = _make_gitea()
        with patch.object(
            gitea, "_patch", new=AsyncMock(return_value={"state": "closed"})
        ):
            result = await gitea.close_issue(1)
        assert result.ok

    @pytest.mark.asyncio
    async def test_comment_on_issue(self):
        gitea = _make_gitea()
        with _patch_request(gitea, 201, {"id": 99}):
            result = await gitea.comment_on_issue(1, "Looks good!")
        assert result.ok
