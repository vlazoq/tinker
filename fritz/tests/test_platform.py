"""
fritz/tests/test_platform.py
─────────────────────────────
Tests for platform detection and owner/repo parsing.
"""

from __future__ import annotations

import pytest

from fritz.platform import GitPlatform, detect_platform, extract_owner_repo


class TestDetectPlatform:
    # ── GitHub ────────────────────────────────────────────────────────────────

    def test_github_https(self):
        assert detect_platform("https://github.com/owner/repo.git") == GitPlatform.GITHUB

    def test_github_ssh(self):
        assert detect_platform("git@github.com:owner/repo.git") == GitPlatform.GITHUB

    def test_github_no_git_suffix(self):
        assert detect_platform("https://github.com/owner/repo") == GitPlatform.GITHUB

    def test_github_www(self):
        assert detect_platform("https://www.github.com/owner/repo") == GitPlatform.GITHUB

    # ── GitLab ────────────────────────────────────────────────────────────────

    def test_gitlab_https(self):
        assert detect_platform("https://gitlab.com/owner/repo.git") == GitPlatform.GITLAB

    def test_gitlab_ssh(self):
        assert detect_platform("git@gitlab.com:owner/repo.git") == GitPlatform.GITLAB

    # ── Gitea (configured base URL) ───────────────────────────────────────────

    def test_gitea_with_matching_base_url(self):
        result = detect_platform(
            "https://git.acme.com/owner/repo.git",
            gitea_base_url="https://git.acme.com",
        )
        assert result == GitPlatform.GITEA

    def test_gitea_ssh_with_base_url(self):
        result = detect_platform(
            "git@git.acme.com:owner/repo.git",
            gitea_base_url="https://git.acme.com",
        )
        assert result == GitPlatform.GITEA

    # ── Unknown / self-hosted falls back to GITEA ─────────────────────────────

    def test_unknown_host_defaults_to_gitea(self):
        result = detect_platform("https://selfhosted.example.com/owner/repo.git")
        assert result == GitPlatform.GITEA

    def test_unknown_without_base_url(self):
        result = detect_platform("git@internal.corp:devteam/service.git")
        assert result == GitPlatform.GITEA


class TestExtractOwnerRepo:
    def test_https_github(self):
        owner, repo = extract_owner_repo("https://github.com/acme/widget.git")
        assert owner == "acme"
        assert repo == "widget"

    def test_https_no_git_suffix(self):
        owner, repo = extract_owner_repo("https://github.com/acme/widget")
        assert owner == "acme"
        assert repo == "widget"

    def test_ssh_github(self):
        owner, repo = extract_owner_repo("git@github.com:acme/widget.git")
        assert owner == "acme"
        assert repo == "widget"

    def test_https_with_token(self):
        owner, repo = extract_owner_repo("https://ghp_tok@github.com/acme/widget.git")
        assert owner == "acme"
        assert repo == "widget"

    def test_gitea_ssh(self):
        owner, repo = extract_owner_repo("git@git.acme.com:devops/platform.git")
        assert owner == "devops"
        assert repo == "platform"

    def test_deep_path_returns_first_two(self):
        owner, repo = extract_owner_repo("https://github.com/org/sub/nested.git")
        assert owner == "org"
        assert repo == "sub"

    def test_minimal_path(self):
        _, repo = extract_owner_repo("git@host:repo.git")
        assert repo == "repo"
