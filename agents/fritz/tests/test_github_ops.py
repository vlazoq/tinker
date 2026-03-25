"""
fritz/tests/test_github_ops.py
───────────────────────────────
Tests for FritzGitHub. HTTP calls are mocked at the _request level so no real
network is needed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agents.fritz.config import FritzConfig
from agents.fritz.github_ops import FritzGitHub, FritzRemoteResult
from agents.fritz.metrics import FritzMetrics


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_config(owner: str = "acme", repo: str = "widget") -> FritzConfig:
    return FritzConfig(github_owner=owner, github_repo=repo)


def _make_gh(owner: str = "acme", repo: str = "widget") -> FritzGitHub:
    cfg = _make_config(owner, repo)
    metrics = MagicMock(spec=FritzMetrics)
    metrics.update_rate_limit = MagicMock()
    metrics.on_api_call = MagicMock()
    return FritzGitHub(cfg, token="ghp_test", metrics=metrics)


def _resp(status: int, body: dict | list | None = None) -> httpx.Response:
    """Build a fake httpx.Response."""
    import json as _json
    content = _json.dumps(body or {}).encode()
    response = httpx.Response(status, content=content)
    response.request = httpx.Request("GET", "https://api.github.com/")
    return response


def _patch_request(gh: FritzGitHub, status: int, body: dict | list | None = None):
    """Patch _request on a FritzGitHub instance to return a canned response."""
    resp = _resp(status, body)
    return patch.object(gh, "_request", new=AsyncMock(return_value=resp))


# ── FritzRemoteResult ─────────────────────────────────────────────────────────


class TestFritzRemoteResult:
    def test_ok_str(self):
        r = FritzRemoteResult(ok=True, operation="create_pr", url="https://github.com/pr/1")
        assert "OK" in str(r)
        assert "create_pr" in str(r)

    def test_fail_str(self):
        r = FritzRemoteResult(ok=False, operation="merge_pr", error="Conflict")
        assert "FAIL" in str(r)
        assert "merge_pr" in str(r)

    def test_str_includes_url(self):
        r = FritzRemoteResult(ok=True, operation="x", url="https://example.com")
        assert "https://example.com" in str(r)


# ── whoami ────────────────────────────────────────────────────────────────────


class TestWhoami:
    @pytest.mark.asyncio
    async def test_success(self):
        gh = _make_gh()
        with _patch_request(gh, 200, {"login": "fritz-bot"}):
            result = await gh.whoami()
        assert result.ok
        assert result.data["login"] == "fritz-bot"

    @pytest.mark.asyncio
    async def test_failure(self):
        gh = _make_gh()
        with patch.object(gh, "_request", new=AsyncMock(side_effect=Exception("network error"))):
            result = await gh.whoami()
        assert not result.ok
        assert "network error" in result.error


# ── create_pr ─────────────────────────────────────────────────────────────────


class TestCreatePr:
    @pytest.mark.asyncio
    async def test_success(self):
        gh = _make_gh()
        pr_data = {"number": 42, "html_url": "https://github.com/acme/widget/pull/42"}
        with _patch_request(gh, 201, pr_data):
            result = await gh.create_pr("Fix bug", "details", "feature/x", "main")
        assert result.ok
        assert result.url == "https://github.com/acme/widget/pull/42"
        assert result.data["number"] == 42

    @pytest.mark.asyncio
    async def test_failure_propagates(self):
        gh = _make_gh()
        with patch.object(gh, "_request", new=AsyncMock(side_effect=Exception("API error"))):
            result = await gh.create_pr("Fix bug", "body", "feature/x")
        assert not result.ok
        assert result.operation == "create_pr"

    @pytest.mark.asyncio
    async def test_draft_pr(self):
        gh = _make_gh()
        pr_data = {"number": 7, "html_url": "https://github.com/acme/widget/pull/7"}
        with _patch_request(gh, 201, pr_data):
            result = await gh.create_pr("Draft fix", "body", "feature/draft", draft=True)
        assert result.ok


# ── merge_pr ──────────────────────────────────────────────────────────────────


class TestMergePr:
    @pytest.mark.asyncio
    async def test_squash_merge(self):
        gh = _make_gh()
        with _patch_request(gh, 200, {"merged": True, "sha": "abc"}):
            result = await gh.merge_pr(42, method="squash")
        assert result.ok

    @pytest.mark.asyncio
    async def test_merge_method_fallback(self):
        gh = _make_gh()
        with _patch_request(gh, 200, {"merged": True}):
            result = await gh.merge_pr(1, method="unknown-method")
        assert result.ok

    @pytest.mark.asyncio
    async def test_failure(self):
        gh = _make_gh()
        with patch.object(gh, "_request", new=AsyncMock(side_effect=Exception("conflict"))):
            result = await gh.merge_pr(1)
        assert not result.ok
        assert "conflict" in result.error


# ── close_pr ──────────────────────────────────────────────────────────────────


class TestClosePr:
    @pytest.mark.asyncio
    async def test_success(self):
        gh = _make_gh()
        with _patch_request(gh, 200, {"state": "closed"}):
            result = await gh.close_pr(5)
        assert result.ok

    @pytest.mark.asyncio
    async def test_failure(self):
        gh = _make_gh()
        with patch.object(gh, "_request", new=AsyncMock(side_effect=Exception("not found"))):
            result = await gh.close_pr(99)
        assert not result.ok


# ── get_pr ────────────────────────────────────────────────────────────────────


class TestGetPr:
    @pytest.mark.asyncio
    async def test_success(self):
        gh = _make_gh()
        pr_data = {"number": 3, "state": "open", "title": "My PR"}
        with _patch_request(gh, 200, pr_data):
            result = await gh.get_pr(3)
        assert result.ok
        assert result.data["number"] == 3

    @pytest.mark.asyncio
    async def test_not_found(self):
        gh = _make_gh()
        with patch.object(gh, "_request", new=AsyncMock(side_effect=Exception("404"))):
            result = await gh.get_pr(999)
        assert not result.ok


# ── create_branch ─────────────────────────────────────────────────────────────


class TestCreateBranch:
    @pytest.mark.asyncio
    async def test_success(self):
        gh = _make_gh()
        ref_data = {"object": {"sha": "deadbeef" * 5}}
        branch_data = {"ref": "refs/heads/feature/new"}
        responses = [_resp(200, ref_data), _resp(201, branch_data)]
        with patch.object(gh, "_request", new=AsyncMock(side_effect=responses)):
            result = await gh.create_branch("feature/new", from_branch="main")
        assert result.ok

    @pytest.mark.asyncio
    async def test_base_not_found(self):
        gh = _make_gh()
        with patch.object(gh, "_request", new=AsyncMock(side_effect=Exception("ref not found"))):
            result = await gh.create_branch("feature/new")
        assert not result.ok


# ── delete_branch ─────────────────────────────────────────────────────────────


class TestDeleteBranch:
    @pytest.mark.asyncio
    async def test_success(self):
        gh = _make_gh()
        with _patch_request(gh, 204):
            result = await gh.delete_branch("feature/old")
        assert result.ok

    @pytest.mark.asyncio
    async def test_failure(self):
        gh = _make_gh()
        with patch.object(gh, "_request", new=AsyncMock(side_effect=Exception("not found"))):
            result = await gh.delete_branch("feature/missing")
        assert not result.ok


# ── create_release ────────────────────────────────────────────────────────────


class TestCreateRelease:
    @pytest.mark.asyncio
    async def test_success(self):
        gh = _make_gh()
        release_data = {"id": 10, "html_url": "https://github.com/acme/widget/releases/tag/v1.0"}
        with _patch_request(gh, 201, release_data):
            result = await gh.create_release("v1.0", "Version 1.0", "Release notes")
        assert result.ok
        assert "v1.0" in result.url

    @pytest.mark.asyncio
    async def test_prerelease_flag(self):
        gh = _make_gh()
        release_data = {"id": 11, "html_url": "https://github.com/acme/widget/releases/tag/v2.0-rc"}
        with _patch_request(gh, 201, release_data):
            result = await gh.create_release("v2.0-rc", "RC", "notes", prerelease=True)
        assert result.ok


# ── get_ci_status ─────────────────────────────────────────────────────────────


class TestGetCiStatus:
    @pytest.mark.asyncio
    async def test_success_state(self):
        gh = _make_gh()
        ci_data = {"state": "success", "statuses": []}
        with _patch_request(gh, 200, ci_data):
            result = await gh.get_ci_status("abc123")
        assert result.ok
        assert result.data["state"] == "success"

    @pytest.mark.asyncio
    async def test_failure(self):
        gh = _make_gh()
        with patch.object(gh, "_request", new=AsyncMock(side_effect=Exception("API error"))):
            result = await gh.get_ci_status("abc123")
        assert not result.ok


# ── wait_for_ci ───────────────────────────────────────────────────────────────


class TestWaitForCi:
    @pytest.mark.asyncio
    async def test_immediate_success(self):
        gh = _make_gh()
        with patch.object(
            gh,
            "get_ci_status",
            new=AsyncMock(return_value=FritzRemoteResult(
                ok=True, operation="get_ci_status", data={"state": "success"}
            )),
        ):
            result = await gh.wait_for_ci("sha123", timeout=60, poll_interval=1)
        assert result.ok

    @pytest.mark.asyncio
    async def test_failure_state(self):
        gh = _make_gh()
        with patch.object(
            gh,
            "get_ci_status",
            new=AsyncMock(return_value=FritzRemoteResult(
                ok=True, operation="get_ci_status", data={"state": "failure"}
            )),
        ):
            result = await gh.wait_for_ci("sha123", timeout=60, poll_interval=1)
        assert not result.ok

    @pytest.mark.asyncio
    async def test_timeout(self):
        gh = _make_gh()
        with patch.object(
            gh,
            "get_ci_status",
            new=AsyncMock(return_value=FritzRemoteResult(
                ok=True, operation="get_ci_status", data={"state": "pending"}
            )),
        ), patch("asyncio.sleep", new=AsyncMock()):
            result = await gh.wait_for_ci("sha123", timeout=2, poll_interval=5)
        assert not result.ok
        assert "did not complete" in result.error

    @pytest.mark.asyncio
    async def test_get_ci_status_failure_propagates(self):
        gh = _make_gh()
        with patch.object(
            gh,
            "get_ci_status",
            new=AsyncMock(return_value=FritzRemoteResult(
                ok=False, operation="get_ci_status", error="network error"
            )),
        ):
            result = await gh.wait_for_ci("sha123", timeout=60, poll_interval=1)
        assert not result.ok


# ── create_issue ──────────────────────────────────────────────────────────────


class TestCreateIssue:
    @pytest.mark.asyncio
    async def test_success(self):
        gh = _make_gh()
        issue_data = {"number": 5, "html_url": "https://github.com/acme/widget/issues/5"}
        with _patch_request(gh, 201, issue_data):
            result = await gh.create_issue("Bug title", "Bug body")
        assert result.ok
        assert result.url.endswith("/5")

    @pytest.mark.asyncio
    async def test_with_labels_and_assignees(self):
        gh = _make_gh()
        issue_data = {"number": 6, "html_url": "https://github.com/acme/widget/issues/6"}
        with _patch_request(gh, 201, issue_data):
            result = await gh.create_issue(
                "Bug", "body", labels=["bug"], assignees=["alice"]
            )
        assert result.ok


# ── rate_limit property ───────────────────────────────────────────────────────


class TestRateLimitProperty:
    def test_returns_rate_limit_state(self):
        gh = _make_gh()
        from agents.fritz.retry import RateLimitState
        assert isinstance(gh.rate_limit, RateLimitState)
        assert gh.rate_limit.remaining == -1  # initial state
