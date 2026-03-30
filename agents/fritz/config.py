"""
agents/fritz/config.py
───────────────
FritzConfig dataclass — single source of truth for all Fritz settings.

All fields can be overridden by environment variables (FRITZ_* prefix).
Config files are JSON; the file is auto-created with defaults on first run.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PushPolicyConfig:
    """Controls what Fritz is allowed to push where."""

    allow_push_to_main: bool = False
    """If True, Fritz may push directly to the default branch (main/master).
    If False, all changes go via feature branch + PR."""

    require_ci_green: bool = True
    """Block merges until CI reports success. Set False to merge regardless."""

    require_pr: bool = True
    """Always go via a Pull Request, even when allow_push_to_main is True.
    Overrides allow_push_to_main — the PR can still auto-merge."""

    auto_merge_method: str = "squash"
    """How to merge PRs automatically. Options: squash | merge | rebase"""

    protected_branches: list[str] = field(default_factory=lambda: ["production", "release"])
    """Branches Fritz will NEVER push to directly, regardless of other settings.
    Note: 'main'/'master' protection is controlled by allow_push_to_main."""

    ci_timeout_seconds: int = 600
    """How long to wait for CI before giving up."""

    def can_push_direct(self, branch: str) -> bool:
        if branch in self.protected_branches:
            return False
        if branch in ("main", "master"):
            return self.allow_push_to_main and not self.require_pr
        return True


@dataclass
class FritzConfig:
    """
    Full Fritz configuration.

    Load order (highest priority first):
      1. Environment variables  (FRITZ_*)
      2. JSON config file       (fritz_config.json)
      3. Dataclass defaults     (below)
    """

    # ── Repository ────────────────────────────────────────────────────────────
    repo_path: str = "."
    """Absolute or relative path to the git repository Fritz operates on."""

    default_branch: str = "main"
    """Default base branch for PRs and direct pushes."""

    # ── Identity ──────────────────────────────────────────────────────────────
    identity_mode: str = "bot"
    """
    How Fritz authenticates:
      bot      — Dedicated Fritz/bot GitHub account (recommended)
      delegate — Your own account; you explicitly provide your token
    """

    git_name: str = "Fritz"
    """Name shown in git commits (user.name)."""

    git_email: str = "fritz@tinker.local"
    """Email shown in git commits (user.email)."""

    # ── GitHub ────────────────────────────────────────────────────────────────
    github_enabled: bool = True
    github_token_key: str = "FRITZ_GITHUB_TOKEN"
    """Key name in SecretManager that holds the GitHub PAT."""

    github_owner: str = ""
    """GitHub organisation or username owning the repo."""

    github_repo: str = ""
    """GitHub repository name (without owner prefix)."""

    # ── Gitea ─────────────────────────────────────────────────────────────────
    gitea_enabled: bool = False
    gitea_base_url: str = ""
    """Base URL of the Gitea instance, e.g. https://gitea.yourdomain.com"""

    gitea_token_key: str = "FRITZ_GITEA_TOKEN"
    """Key name in SecretManager that holds the Gitea PAT."""

    gitea_tls_verify: bool = True
    """Set False for self-signed TLS certificates on local Gitea instances."""

    gitea_ssh_port: int = 22
    """SSH port for the Gitea server (often non-standard on self-hosted)."""

    gitea_owner: str = ""
    """Gitea organisation or username owning the repo."""

    gitea_repo: str = ""
    """Gitea repository name."""

    gitea_ci_provider: str = "gitea_actions"
    """CI system attached to Gitea: gitea_actions | woodpecker | drone | none"""

    # ── Push Policy ───────────────────────────────────────────────────────────
    push_policy: PushPolicyConfig = field(default_factory=PushPolicyConfig)

    # ── Observability ─────────────────────────────────────────────────────────
    audit_log_path: str = ""
    """Path to the SQLite audit log. Empty = use Tinker's default audit log."""

    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def from_file(cls, path: str | Path = "fritz_config.json") -> FritzConfig:
        """Load config from JSON file + apply env var overrides."""
        p = Path(path)
        data: dict[str, Any] = {}

        if p.exists():
            with contextlib.suppress(Exception):
                data = json.loads(p.read_text())
        else:
            # Auto-create with defaults so users have a starting template.
            cfg = cls()
            cfg.save(p)
            return cfg

        policy_data = data.pop("push_policy", {})
        cfg = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        cfg.push_policy = PushPolicyConfig(
            **{k: v for k, v in policy_data.items() if k in PushPolicyConfig.__dataclass_fields__}
        )
        cfg._apply_env_overrides()
        return cfg

    def _apply_env_overrides(self) -> None:
        """Environment variables always win over config file values."""
        _str(self, "repo_path", "FRITZ_REPO_PATH")
        _str(self, "default_branch", "FRITZ_DEFAULT_BRANCH")
        _str(self, "identity_mode", "FRITZ_IDENTITY_MODE")
        _str(self, "git_name", "FRITZ_GIT_NAME")
        _str(self, "git_email", "FRITZ_GIT_EMAIL")
        _bool(self, "github_enabled", "FRITZ_GITHUB_ENABLED")
        _str(self, "github_token_key", "FRITZ_GITHUB_TOKEN_KEY")
        _str(self, "github_owner", "FRITZ_GITHUB_OWNER")
        _str(self, "github_repo", "FRITZ_GITHUB_REPO")
        _bool(self, "gitea_enabled", "FRITZ_GITEA_ENABLED")
        _str(self, "gitea_base_url", "FRITZ_GITEA_BASE_URL")
        _str(self, "gitea_token_key", "FRITZ_GITEA_TOKEN_KEY")
        _bool(self, "gitea_tls_verify", "FRITZ_GITEA_TLS_VERIFY")
        _int(self, "gitea_ssh_port", "FRITZ_GITEA_SSH_PORT")
        _str(self, "gitea_owner", "FRITZ_GITEA_OWNER")
        _str(self, "gitea_repo", "FRITZ_GITEA_REPO")
        _str(self, "gitea_ci_provider", "FRITZ_GITEA_CI_PROVIDER")
        # push policy env overrides
        pp = self.push_policy
        _bool(pp, "allow_push_to_main", "FRITZ_ALLOW_PUSH_TO_MAIN")
        _bool(pp, "require_ci_green", "FRITZ_REQUIRE_CI_GREEN")
        _bool(pp, "require_pr", "FRITZ_REQUIRE_PR")
        _str(pp, "auto_merge_method", "FRITZ_AUTO_MERGE_METHOD")
        _int(pp, "ci_timeout_seconds", "FRITZ_CI_TIMEOUT")

    def save(self, path: str | Path = "fritz_config.json") -> None:
        """Persist config to JSON (excluding secrets — only key names are saved)."""
        data = {
            "repo_path": self.repo_path,
            "default_branch": self.default_branch,
            "identity_mode": self.identity_mode,
            "git_name": self.git_name,
            "git_email": self.git_email,
            "github_enabled": self.github_enabled,
            "github_token_key": self.github_token_key,
            "github_owner": self.github_owner,
            "github_repo": self.github_repo,
            "gitea_enabled": self.gitea_enabled,
            "gitea_base_url": self.gitea_base_url,
            "gitea_token_key": self.gitea_token_key,
            "gitea_tls_verify": self.gitea_tls_verify,
            "gitea_ssh_port": self.gitea_ssh_port,
            "gitea_owner": self.gitea_owner,
            "gitea_repo": self.gitea_repo,
            "gitea_ci_provider": self.gitea_ci_provider,
            "push_policy": {
                "allow_push_to_main": self.push_policy.allow_push_to_main,
                "require_ci_green": self.push_policy.require_ci_green,
                "require_pr": self.push_policy.require_pr,
                "auto_merge_method": self.push_policy.auto_merge_method,
                "protected_branches": self.push_policy.protected_branches,
                "ci_timeout_seconds": self.push_policy.ci_timeout_seconds,
            },
            "audit_log_path": self.audit_log_path,
        }
        Path(path).write_text(json.dumps(data, indent=2))

    def validate(self) -> list[str]:
        """Return a list of validation errors (empty = config is valid)."""
        errors: list[str] = []
        if self.identity_mode not in ("bot", "delegate"):
            errors.append(f"identity_mode must be 'bot' or 'delegate', got '{self.identity_mode}'")
        if self.push_policy.auto_merge_method not in ("squash", "merge", "rebase"):
            errors.append("auto_merge_method must be squash|merge|rebase")
        if self.gitea_enabled and not self.gitea_base_url:
            errors.append("gitea_base_url is required when gitea_enabled is True")
        return errors


# ── Private helpers ───────────────────────────────────────────────────────────


def _str(obj: Any, attr: str, env_key: str) -> None:
    val = os.getenv(env_key)
    if val is not None:
        setattr(obj, attr, val)


def _bool(obj: Any, attr: str, env_key: str) -> None:
    val = os.getenv(env_key)
    if val is not None:
        setattr(obj, attr, val.lower() not in ("false", "0", "no", "off", "disabled"))


def _int(obj: Any, attr: str, env_key: str) -> None:
    val = os.getenv(env_key)
    if val is not None:
        with contextlib.suppress(ValueError):
            setattr(obj, attr, int(val))
