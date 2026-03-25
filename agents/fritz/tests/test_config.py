"""
agents/fritz/tests/test_config.py
──────────────────────────
Tests for FritzConfig + PushPolicyConfig: loading, env overrides, validation,
save/reload round-trip, and default creation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agents.fritz.config import FritzConfig, PushPolicyConfig


# ── FritzConfig defaults ──────────────────────────────────────────────────────


class TestFritzConfigDefaults:
    def test_default_identity_mode(self):
        cfg = FritzConfig()
        assert cfg.identity_mode == "bot"

    def test_default_github_enabled(self):
        assert FritzConfig().github_enabled is True

    def test_default_gitea_disabled(self):
        assert FritzConfig().gitea_enabled is False

    def test_default_push_policy_safe(self):
        pp = FritzConfig().push_policy
        assert pp.allow_push_to_main is False
        assert pp.require_pr is True
        assert pp.require_ci_green is True
        assert pp.auto_merge_method == "squash"

    def test_protected_branches_defaults(self):
        pp = FritzConfig().push_policy
        assert "production" in pp.protected_branches
        assert "release" in pp.protected_branches


# ── PushPolicyConfig.can_push_direct ─────────────────────────────────────────


class TestCanPushDirect:
    def test_protected_branch_always_blocked(self):
        pp = PushPolicyConfig(allow_push_to_main=True, require_pr=False, protected_branches=["prod"])
        assert pp.can_push_direct("prod") is False

    def test_main_blocked_by_default(self):
        pp = PushPolicyConfig(allow_push_to_main=False)
        assert pp.can_push_direct("main") is False
        assert pp.can_push_direct("master") is False

    def test_main_blocked_when_require_pr(self):
        pp = PushPolicyConfig(allow_push_to_main=True, require_pr=True)
        assert pp.can_push_direct("main") is False

    def test_main_allowed_when_fully_open(self):
        pp = PushPolicyConfig(allow_push_to_main=True, require_pr=False)
        assert pp.can_push_direct("main") is True

    def test_feature_branch_always_allowed(self):
        pp = PushPolicyConfig(allow_push_to_main=False, require_pr=True)
        assert pp.can_push_direct("feature/foo") is True

    def test_feature_branch_not_in_protected(self):
        pp = PushPolicyConfig(protected_branches=["main", "production"])
        assert pp.can_push_direct("dev") is True


# ── FritzConfig.validate ──────────────────────────────────────────────────────


class TestValidate:
    def test_valid_default_config(self):
        assert FritzConfig().validate() == []

    def test_invalid_identity_mode(self):
        cfg = FritzConfig(identity_mode="admin")
        errors = cfg.validate()
        assert any("identity_mode" in e for e in errors)

    def test_invalid_merge_method(self):
        cfg = FritzConfig()
        cfg.push_policy.auto_merge_method = "cherry-pick"
        errors = cfg.validate()
        assert any("auto_merge_method" in e for e in errors)

    def test_gitea_enabled_without_url(self):
        cfg = FritzConfig(gitea_enabled=True, gitea_base_url="")
        errors = cfg.validate()
        assert any("gitea_base_url" in e for e in errors)

    def test_gitea_enabled_with_url_valid(self):
        cfg = FritzConfig(gitea_enabled=True, gitea_base_url="https://git.example.com")
        assert cfg.validate() == []


# ── Save / load round-trip ────────────────────────────────────────────────────


class TestSaveLoad:
    def test_save_creates_file(self, tmp_path: Path):
        cfg = FritzConfig(github_owner="acme", github_repo="widget")
        p = tmp_path / "fritz_config.json"
        cfg.save(p)
        assert p.exists()

    def test_load_restores_values(self, tmp_path: Path):
        p = tmp_path / "fritz_config.json"
        cfg = FritzConfig(
            github_owner="acme",
            github_repo="widget",
            identity_mode="delegate",
            gitea_enabled=True,
            gitea_base_url="https://git.acme.com",
            gitea_owner="acme",
            gitea_repo="widget",
        )
        cfg.push_policy.allow_push_to_main = True
        cfg.push_policy.auto_merge_method = "merge"
        cfg.save(p)

        loaded = FritzConfig.from_file(p)
        assert loaded.github_owner == "acme"
        assert loaded.github_repo == "widget"
        assert loaded.identity_mode == "delegate"
        assert loaded.gitea_enabled is True
        assert loaded.push_policy.allow_push_to_main is True
        assert loaded.push_policy.auto_merge_method == "merge"

    def test_missing_file_creates_default(self, tmp_path: Path):
        p = tmp_path / "new_config.json"
        assert not p.exists()
        cfg = FritzConfig.from_file(p)
        assert p.exists()
        assert cfg.validate() == []

    def test_tokens_not_saved(self, tmp_path: Path):
        """Token values must never be written to the config file."""
        p = tmp_path / "fritz_config.json"
        FritzConfig().save(p)
        raw = p.read_text()
        assert "ghp_" not in raw
        assert "password" not in raw.lower()


# ── Environment variable overrides ───────────────────────────────────────────


class TestEnvOverrides:
    def test_identity_mode_override(self, tmp_path: Path):
        p = tmp_path / "cfg.json"
        FritzConfig().save(p)
        with patch.dict(os.environ, {"FRITZ_IDENTITY_MODE": "delegate"}):
            cfg = FritzConfig.from_file(p)
        assert cfg.identity_mode == "delegate"

    def test_github_owner_override(self, tmp_path: Path):
        p = tmp_path / "cfg.json"
        FritzConfig(github_owner="original").save(p)
        with patch.dict(os.environ, {"FRITZ_GITHUB_OWNER": "overridden"}):
            cfg = FritzConfig.from_file(p)
        assert cfg.github_owner == "overridden"

    def test_gitea_enabled_override_true(self, tmp_path: Path):
        p = tmp_path / "cfg.json"
        FritzConfig(gitea_enabled=False).save(p)
        with patch.dict(os.environ, {"FRITZ_GITEA_ENABLED": "true"}):
            cfg = FritzConfig.from_file(p)
        assert cfg.gitea_enabled is True

    def test_gitea_enabled_override_false_values(self, tmp_path: Path):
        p = tmp_path / "cfg.json"
        FritzConfig(gitea_enabled=True, gitea_base_url="https://x.com").save(p)
        for falsy in ("false", "0", "no", "off", "disabled"):
            with patch.dict(os.environ, {"FRITZ_GITEA_ENABLED": falsy}):
                cfg = FritzConfig.from_file(p)
            assert cfg.gitea_enabled is False, f"Expected False for FRITZ_GITEA_ENABLED={falsy!r}"

    def test_allow_push_to_main_override(self, tmp_path: Path):
        p = tmp_path / "cfg.json"
        FritzConfig().save(p)
        with patch.dict(os.environ, {"FRITZ_ALLOW_PUSH_TO_MAIN": "true"}):
            cfg = FritzConfig.from_file(p)
        assert cfg.push_policy.allow_push_to_main is True

    def test_ci_timeout_override(self, tmp_path: Path):
        p = tmp_path / "cfg.json"
        FritzConfig().save(p)
        with patch.dict(os.environ, {"FRITZ_CI_TIMEOUT": "120"}):
            cfg = FritzConfig.from_file(p)
        assert cfg.push_policy.ci_timeout_seconds == 120

    def test_env_wins_over_file(self, tmp_path: Path):
        """Environment always beats the config file."""
        p = tmp_path / "cfg.json"
        FritzConfig(github_repo="from-file").save(p)
        with patch.dict(os.environ, {"FRITZ_GITHUB_REPO": "from-env"}):
            cfg = FritzConfig.from_file(p)
        assert cfg.github_repo == "from-env"
