"""
agents/fritz/platform.py
──────────────────
Platform detection — figures out whether a remote URL points to GitHub,
Gitea, GitLab, or an unknown self-hosted instance.

Fritz uses this to route operations to the right driver (github_ops vs
gitea_ops) without requiring the user to specify the platform manually.
"""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import urlparse


class GitPlatform(StrEnum):
    GITHUB = "github"
    GITEA = "gitea"
    GITLAB = "gitlab"
    UNKNOWN = "unknown"


_GITHUB_HOSTS = {"github.com", "www.github.com"}
_GITLAB_HOSTS = {"gitlab.com", "www.gitlab.com"}


def detect_platform(remote_url: str, gitea_base_url: str = "") -> GitPlatform:
    """
    Infer the platform from a remote URL.

    Detection order:
      1. github.com   → GITHUB
      2. gitlab.com   → GITLAB
      3. matches configured gitea_base_url → GITEA
      4. Unknown host → GITEA (safe default for self-hosted)

    Args:
        remote_url:    The git remote URL (SSH or HTTPS).
        gitea_base_url: The configured Gitea base URL from FritzConfig.
                        Used to confirm self-hosted Gitea matches.
    """
    url = remote_url.strip()

    # Normalise SSH format: git@github.com:owner/repo.git → //github.com/...
    if url.startswith("git@"):
        # git@host:path → extract host
        host_part = url[4:].split(":")[0]
    else:
        parsed = urlparse(url)
        host_part = parsed.netloc.split("@")[-1].split(":")[0]

    host_lower = host_part.lower()

    if host_lower in _GITHUB_HOSTS:
        return GitPlatform.GITHUB

    if host_lower in _GITLAB_HOSTS:
        return GitPlatform.GITLAB

    if gitea_base_url:
        gitea_host = urlparse(gitea_base_url).netloc.lower()
        if gitea_host and host_lower == gitea_host:
            return GitPlatform.GITEA

    # Unknown host — default to Gitea since it covers most self-hosted cases.
    return GitPlatform.GITEA


def extract_owner_repo(remote_url: str) -> tuple[str, str]:
    """
    Parse owner and repo name out of a remote URL.

    Handles:
      https://github.com/owner/repo.git
      git@github.com:owner/repo.git
      https://token@gitea.host/owner/repo.git
    """
    url = remote_url.strip()

    if url.startswith("git@"):
        # git@host:owner/repo.git
        path_part = url.split(":", 1)[-1]
    else:
        parsed = urlparse(url)
        path_part = parsed.path

    # Strip leading slash and .git suffix
    path_part = path_part.lstrip("/").removesuffix(".git")
    parts = path_part.split("/")
    if len(parts) >= 2:
        return parts[0], parts[1]
    return "", path_part
