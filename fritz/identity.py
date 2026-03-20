"""
fritz/identity.py
──────────────────
Fritz identity management — controls how Fritz authenticates and how its
commits appear in git history.

Two modes:
  bot      — Fritz uses a dedicated bot/service account you created
             (e.g. github.com/fritz-tinker-bot). Commits show as that account.
  delegate — Fritz uses YOUR account because you explicitly gave it your token.
             Commits show your name/email. This is authorization, not spoofing.

The mode is set in fritz_config.json (identity_mode) or FRITZ_IDENTITY_MODE env.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .config import FritzConfig
from .credentials import FritzCredentials


class IdentityMode(str, Enum):
    BOT = "bot"
    DELEGATE = "delegate"


@dataclass
class FritzIdentity:
    mode: IdentityMode
    git_name: str
    git_email: str
    github_token: str | None
    gitea_token: str | None


def build_identity(config: FritzConfig, creds: FritzCredentials) -> FritzIdentity:
    """Construct a FritzIdentity from config + resolved credentials."""
    return FritzIdentity(
        mode=IdentityMode(config.identity_mode),
        git_name=config.git_name,
        git_email=config.git_email,
        github_token=creds.github_token,
        gitea_token=creds.gitea_token,
    )


def apply_git_identity(identity: FritzIdentity, repo_path: str | Path) -> None:
    """
    Write user.name and user.email into the repo's local git config.
    This ensures commits are attributed to the correct identity without
    touching the global git config.
    """
    repo = Path(repo_path)
    subprocess.run(
        ["git", "config", "user.name", identity.git_name],
        cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", identity.git_email],
        cwd=repo, check=True, capture_output=True
    )


def build_auth_header(token: str) -> dict[str, str]:
    """HTTP Authorization header for GitHub or Gitea API calls."""
    return {"Authorization": f"token {token}"}


def build_clone_url_with_token(base_url: str, owner: str, repo: str, token: str) -> str:
    """
    Build an HTTPS clone URL with embedded token for push/pull over HTTPS.
    Format: https://<token>@host/owner/repo.git
    Used when SSH is not configured.
    """
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(base_url)
    netloc = f"{token}@{parsed.netloc}" if parsed.netloc else token
    return urlunparse(parsed._replace(netloc=netloc, path=f"/{owner}/{repo}.git"))
