"""
agents/fritz/tests/test_credentials.py
────────────────────────────────
Tests for FritzCredentials. SecretManager is mocked so no vault / secrets file
is needed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.fritz.config import FritzConfig
from agents.fritz.credentials import FritzCredentials


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_creds(
    github_enabled: bool = True,
    gitea_enabled: bool = False,
    github_token_key: str = "FRITZ_GITHUB_TOKEN",
    gitea_token_key: str = "FRITZ_GITEA_TOKEN",
    gitea_base_url: str = "https://gitea.example.com",
) -> FritzCredentials:
    cfg = FritzConfig(
        github_enabled=github_enabled,
        gitea_enabled=gitea_enabled,
        github_token_key=github_token_key,
        gitea_token_key=gitea_token_key,
        gitea_base_url=gitea_base_url,
    )
    return FritzCredentials(cfg)


def _mock_secrets(github: str | None = "ghp_token", gitea: str | None = None):
    """Return a SecretManager mock whose get() returns the given values in order."""
    sm = MagicMock()
    sm.get = AsyncMock(side_effect=lambda key: {
        "FRITZ_GITHUB_TOKEN": github,
        "FRITZ_GITEA_TOKEN": gitea,
    }.get(key))
    return sm


# ── load — GitHub only ────────────────────────────────────────────────────────


class TestLoadGitHubOnly:
    @pytest.mark.asyncio
    async def test_loads_github_token(self):
        creds = _make_creds(github_enabled=True, gitea_enabled=False)
        with patch("agents.fritz.credentials.SecretManager", return_value=_mock_secrets("ghp_test")):
            creds._secrets = _mock_secrets("ghp_test")
            await creds.load()
        assert creds.github_token == "ghp_test"

    @pytest.mark.asyncio
    async def test_github_disabled_skips_load(self):
        creds = _make_creds(github_enabled=False, gitea_enabled=False)
        sm = MagicMock()
        sm.get = AsyncMock(return_value="should-not-be-called")
        creds._secrets = sm
        await creds.load()
        sm.get.assert_not_called()
        assert creds.github_token is None

    @pytest.mark.asyncio
    async def test_missing_token_logs_warning(self, caplog):
        import logging
        creds = _make_creds(github_enabled=True)
        sm = MagicMock()
        sm.get = AsyncMock(return_value=None)
        creds._secrets = sm
        with caplog.at_level(logging.WARNING, logger="agents.fritz.credentials"):
            await creds.load()
        assert creds.github_token is None
        assert any("GitHub" in m for m in caplog.messages)


# ── load — Gitea ──────────────────────────────────────────────────────────────


class TestLoadGitea:
    @pytest.mark.asyncio
    async def test_loads_gitea_token(self):
        creds = _make_creds(github_enabled=False, gitea_enabled=True)
        sm = MagicMock()
        sm.get = AsyncMock(side_effect=lambda key: {
            "FRITZ_GITEA_TOKEN": "gitea_secret",
        }.get(key))
        creds._secrets = sm
        await creds.load()
        assert creds.gitea_token == "gitea_secret"

    @pytest.mark.asyncio
    async def test_missing_gitea_token_logs_warning(self, caplog):
        import logging
        creds = _make_creds(github_enabled=False, gitea_enabled=True)
        sm = MagicMock()
        sm.get = AsyncMock(return_value=None)
        creds._secrets = sm
        with caplog.at_level(logging.WARNING, logger="agents.fritz.credentials"):
            await creds.load()
        assert creds.gitea_token is None
        assert any("Gitea" in m for m in caplog.messages)


# ── load — both platforms ─────────────────────────────────────────────────────


class TestLoadBothPlatforms:
    @pytest.mark.asyncio
    async def test_loads_both_tokens(self):
        creds = _make_creds(github_enabled=True, gitea_enabled=True)
        sm = MagicMock()
        sm.get = AsyncMock(side_effect=lambda key: {
            "FRITZ_GITHUB_TOKEN": "ghp_xyz",
            "FRITZ_GITEA_TOKEN": "gitea_abc",
        }.get(key))
        creds._secrets = sm
        await creds.load()
        assert creds.github_token == "ghp_xyz"
        assert creds.gitea_token == "gitea_abc"


# ── require_github ────────────────────────────────────────────────────────────


class TestRequireGitHub:
    def test_returns_token_when_present(self):
        creds = _make_creds()
        creds.github_token = "ghp_present"
        assert creds.require_github() == "ghp_present"

    def test_raises_when_missing(self):
        creds = _make_creds()
        creds.github_token = None
        with pytest.raises(RuntimeError, match="GitHub token"):
            creds.require_github()

    def test_error_mentions_env_var(self):
        creds = _make_creds(github_token_key="MY_GH_TOKEN")
        creds.github_token = None
        with pytest.raises(RuntimeError, match="MY_GH_TOKEN"):
            creds.require_github()


# ── require_gitea ─────────────────────────────────────────────────────────────


class TestRequireGitea:
    def test_returns_token_when_present(self):
        creds = _make_creds()
        creds.gitea_token = "gitea_present"
        assert creds.require_gitea() == "gitea_present"

    def test_raises_when_missing(self):
        creds = _make_creds()
        creds.gitea_token = None
        with pytest.raises(RuntimeError, match="Gitea token"):
            creds.require_gitea()

    def test_error_mentions_env_var(self):
        creds = _make_creds(gitea_token_key="MY_GITEA_TOKEN")
        creds.gitea_token = None
        with pytest.raises(RuntimeError, match="MY_GITEA_TOKEN"):
            creds.require_gitea()


# ── initial state ─────────────────────────────────────────────────────────────


class TestInitialState:
    def test_tokens_start_as_none(self):
        creds = _make_creds()
        assert creds.github_token is None
        assert creds.gitea_token is None
