"""
agents/fritz/tests/test_identity.py
──────────────────────────────
Tests for FritzIdentity, IdentityMode, build_identity, apply_git_identity,
build_auth_header, and build_clone_url_with_token.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agents.fritz.config import FritzConfig
from agents.fritz.credentials import FritzCredentials
from agents.fritz.identity import (
    FritzIdentity,
    IdentityMode,
    apply_git_identity,
    build_auth_header,
    build_clone_url_with_token,
    build_identity,
)

# ── IdentityMode ──────────────────────────────────────────────────────────────


class TestIdentityMode:
    def test_bot_value(self):
        assert IdentityMode.BOT == "bot"

    def test_delegate_value(self):
        assert IdentityMode.DELEGATE == "delegate"

    def test_from_string_bot(self):
        assert IdentityMode("bot") == IdentityMode.BOT

    def test_from_string_delegate(self):
        assert IdentityMode("delegate") == IdentityMode.DELEGATE

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            IdentityMode("admin")


# ── FritzIdentity dataclass ───────────────────────────────────────────────────


class TestFritzIdentity:
    def _make(self, mode: IdentityMode = IdentityMode.BOT) -> FritzIdentity:
        return FritzIdentity(
            mode=mode,
            git_name="Fritz Bot",
            git_email="fritz@example.com",
            github_token="ghp_token",
            gitea_token=None,
        )

    def test_attributes(self):
        identity = self._make()
        assert identity.mode == IdentityMode.BOT
        assert identity.git_name == "Fritz Bot"
        assert identity.git_email == "fritz@example.com"
        assert identity.github_token == "ghp_token"
        assert identity.gitea_token is None

    def test_delegate_mode(self):
        identity = self._make(IdentityMode.DELEGATE)
        assert identity.mode == IdentityMode.DELEGATE


# ── build_identity ────────────────────────────────────────────────────────────


class TestBuildIdentity:
    def test_bot_mode(self):
        cfg = FritzConfig(
            identity_mode="bot",
            git_name="Fritz Bot",
            git_email="fritz@bot.local",
        )
        creds = MagicMock(spec=FritzCredentials)
        creds.github_token = "ghp_bot_token"
        creds.gitea_token = None

        identity = build_identity(cfg, creds)

        assert identity.mode == IdentityMode.BOT
        assert identity.git_name == "Fritz Bot"
        assert identity.git_email == "fritz@bot.local"
        assert identity.github_token == "ghp_bot_token"
        assert identity.gitea_token is None

    def test_delegate_mode(self):
        cfg = FritzConfig(
            identity_mode="delegate",
            git_name="Alice Dev",
            git_email="alice@example.com",
        )
        creds = MagicMock(spec=FritzCredentials)
        creds.github_token = "ghp_alice_token"
        creds.gitea_token = "gitea_alice_token"

        identity = build_identity(cfg, creds)

        assert identity.mode == IdentityMode.DELEGATE
        assert identity.git_name == "Alice Dev"
        assert identity.gitea_token == "gitea_alice_token"

    def test_tokens_from_creds(self):
        """build_identity should take tokens from credentials, not config."""
        cfg = FritzConfig(identity_mode="bot")
        creds = MagicMock(spec=FritzCredentials)
        creds.github_token = "tok_from_creds"
        creds.gitea_token = "gitea_from_creds"

        identity = build_identity(cfg, creds)
        assert identity.github_token == "tok_from_creds"
        assert identity.gitea_token == "gitea_from_creds"


# ── apply_git_identity ────────────────────────────────────────────────────────


class TestApplyGitIdentity:
    def test_calls_git_config(self, tmp_path: Path):
        identity = FritzIdentity(
            mode=IdentityMode.BOT,
            git_name="Fritz",
            git_email="fritz@test.com",
            github_token=None,
            gitea_token=None,
        )
        cmds_run = []

        def fake_run(cmd, **kwargs):
            cmds_run.append(cmd)
            r = MagicMock()
            r.returncode = 0
            return r

        with patch("subprocess.run", side_effect=fake_run):
            apply_git_identity(identity, tmp_path)

        assert len(cmds_run) == 2
        # First call sets user.name
        assert "user.name" in cmds_run[0]
        assert "Fritz" in cmds_run[0]
        # Second call sets user.email
        assert "user.email" in cmds_run[1]
        assert "fritz@test.com" in cmds_run[1]

    def test_uses_local_scope(self, tmp_path: Path):
        """git config must NOT pass --global."""
        identity = FritzIdentity(
            mode=IdentityMode.BOT,
            git_name="Fritz",
            git_email="fritz@test.com",
            github_token=None,
            gitea_token=None,
        )
        cmds_run = []

        def fake_run(cmd, **kwargs):
            cmds_run.append(cmd)
            r = MagicMock()
            r.returncode = 0
            return r

        with patch("subprocess.run", side_effect=fake_run):
            apply_git_identity(identity, tmp_path)

        for cmd in cmds_run:
            assert "--global" not in cmd


# ── build_auth_header ─────────────────────────────────────────────────────────


class TestBuildAuthHeader:
    def test_token_format(self):
        header = build_auth_header("ghp_mytoken")
        assert header == {"Authorization": "token ghp_mytoken"}

    def test_returns_dict(self):
        header = build_auth_header("secret")
        assert isinstance(header, dict)

    def test_authorization_key(self):
        header = build_auth_header("abc")
        assert "Authorization" in header

    def test_value_starts_with_token(self):
        header = build_auth_header("mytoken")
        assert header["Authorization"].startswith("token ")


# ── build_clone_url_with_token ────────────────────────────────────────────────


class TestBuildCloneUrlWithToken:
    def test_github_https(self):
        url = build_clone_url_with_token("https://github.com", "acme", "widget", "ghp_tok")
        assert "ghp_tok@github.com" in url
        assert url.endswith("acme/widget.git")

    def test_gitea_https(self):
        url = build_clone_url_with_token(
            "https://gitea.example.com", "devops", "platform", "gitea_tok"
        )
        assert "gitea_tok@gitea.example.com" in url
        assert "/devops/platform.git" in url

    def test_scheme_preserved(self):
        url = build_clone_url_with_token("https://github.com", "o", "r", "tok")
        assert url.startswith("https://")

    def test_path_contains_owner_and_repo(self):
        url = build_clone_url_with_token("https://host.com", "myorg", "myrepo", "t")
        assert "myorg/myrepo.git" in url

    def test_token_embedded_before_host(self):
        url = build_clone_url_with_token("https://github.com", "o", "r", "TOKEN123")
        assert "TOKEN123@" in url
        # token should NOT appear in the path part after the host
        host_and_after = url.split("TOKEN123@")[-1]
        assert "TOKEN123" not in host_and_after
