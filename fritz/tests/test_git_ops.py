"""
fritz/tests/test_git_ops.py
────────────────────────────
Tests for FritzGitOps. Subprocess calls are mocked so no real git repo is needed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fritz.config import FritzConfig
from fritz.git_ops import FritzGitOps, FritzGitResult
from fritz.identity import FritzIdentity, IdentityMode


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def identity() -> FritzIdentity:
    return FritzIdentity(
        mode=IdentityMode.BOT,
        git_name="Fritz",
        git_email="fritz@tinker.local",
        github_token=None,
        gitea_token=None,
    )


@pytest.fixture()
def config(tmp_path: Path) -> FritzConfig:
    return FritzConfig(repo_path=str(tmp_path))


@pytest.fixture()
def git_ops(config: FritzConfig, identity: FritzIdentity) -> FritzGitOps:
    ops = FritzGitOps(config, identity)
    ops._identity_applied = True  # skip actual git config calls in tests
    return ops


def _ok(stdout: str = "") -> MagicMock:
    """Build a fake subprocess.CompletedProcess result (success)."""
    r = MagicMock()
    r.returncode = 0
    r.stdout = stdout
    r.stderr = ""
    return r


def _fail(stderr: str = "error") -> MagicMock:
    r = MagicMock()
    r.returncode = 1
    r.stdout = ""
    r.stderr = stderr
    return r


def patch_run(result):
    """Patch subprocess.run to return the given result."""
    return patch("subprocess.run", return_value=result)


# ── current_branch ────────────────────────────────────────────────────────────


class TestCurrentBranch:
    @pytest.mark.asyncio
    async def test_returns_branch_name(self, git_ops):
        with patch_run(_ok("feature/cool\n")):
            branch = await git_ops.current_branch()
        assert branch == "feature/cool"

    @pytest.mark.asyncio
    async def test_strips_whitespace(self, git_ops):
        with patch_run(_ok("  main  \n")):
            branch = await git_ops.current_branch()
        assert branch == "main"


# ── status ────────────────────────────────────────────────────────────────────


class TestStatus:
    @pytest.mark.asyncio
    async def test_clean_tree(self, git_ops):
        with patch_run(_ok("")):
            result = await git_ops.status()
        assert result.ok
        assert result.stdout == ""

    @pytest.mark.asyncio
    async def test_dirty_tree(self, git_ops):
        with patch_run(_ok(" M src/foo.py\n?? bar.py")):
            result = await git_ops.status()
        assert result.ok
        assert "foo.py" in result.stdout


# ── commit ────────────────────────────────────────────────────────────────────


class TestCommit:
    @pytest.mark.asyncio
    async def test_successful_commit(self, git_ops):
        with patch("subprocess.run", return_value=_ok()):
            result = await git_ops.commit("fix: test")
        assert result.ok

    @pytest.mark.asyncio
    async def test_failed_add_propagates(self, git_ops):
        with patch("subprocess.run", return_value=_fail("not a git repo")):
            result = await git_ops.commit("fix: test")
        assert not result.ok
        assert "not a git repo" in result.stderr

    @pytest.mark.asyncio
    async def test_commit_specific_files(self, git_ops):
        calls = []
        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _ok()
        with patch("subprocess.run", side_effect=fake_run):
            await git_ops.commit("fix: test", files=["src/a.py", "src/b.py"])
        # First call: git add -- src/a.py src/b.py
        assert "add" in calls[0]
        assert "src/a.py" in calls[0]


# ── push ──────────────────────────────────────────────────────────────────────


class TestPush:
    @pytest.mark.asyncio
    async def test_push_success(self, git_ops):
        with patch("subprocess.run", return_value=_ok()):
            # Need current_branch to work too
            git_ops._run_sync = lambda op, args: FritzGitResult(ok=True, operation=op, stdout="main")
            result = await git_ops.push("feature/test")
        assert result.ok

    @pytest.mark.asyncio
    async def test_push_uses_force_with_lease(self, git_ops):
        cmds = []
        def fake_run(cmd, **kwargs):
            cmds.append(cmd)
            return _ok()
        with patch("subprocess.run", side_effect=fake_run):
            await git_ops.push("feature/test", force=True)
        assert any("--force-with-lease" in " ".join(c) for c in cmds)

    @pytest.mark.asyncio
    async def test_push_failure(self, git_ops):
        with patch("subprocess.run", return_value=_fail("rejected")):
            result = await git_ops.push("feature/test")
        assert not result.ok

    @pytest.mark.asyncio
    async def test_push_all_remotes(self, git_ops):
        calls = []
        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _ok()
        with patch("subprocess.run", side_effect=fake_run):
            results = await git_ops.push_all_remotes("feature/test")
        # all remotes succeed (no remotes → empty dict is fine too)
        assert all(r.ok for r in results.values())


# ── branch operations ─────────────────────────────────────────────────────────


class TestBranchOps:
    @pytest.mark.asyncio
    async def test_create_branch(self, git_ops):
        with patch("subprocess.run", return_value=_ok()):
            result = await git_ops.create_branch("feature/new")
        assert result.ok

    @pytest.mark.asyncio
    async def test_create_branch_from(self, git_ops):
        cmds = []
        def fake_run(cmd, **kwargs):
            cmds.append(cmd)
            return _ok()
        with patch("subprocess.run", side_effect=fake_run):
            await git_ops.create_branch("feature/new", from_branch="main")
        assert "main" in " ".join(cmds[-1])

    @pytest.mark.asyncio
    async def test_checkout(self, git_ops):
        with patch("subprocess.run", return_value=_ok()):
            result = await git_ops.checkout("feature/x")
        assert result.ok

    @pytest.mark.asyncio
    async def test_delete_branch(self, git_ops):
        with patch("subprocess.run", return_value=_ok()):
            result = await git_ops.delete_branch("feature/old")
        assert result.ok

    @pytest.mark.asyncio
    async def test_branch_exists_true(self, git_ops):
        with patch("subprocess.run", return_value=_ok("  feature/x\n")):
            exists = await git_ops.branch_exists("feature/x")
        assert exists is True

    @pytest.mark.asyncio
    async def test_branch_exists_false(self, git_ops):
        with patch("subprocess.run", return_value=_ok("")):
            exists = await git_ops.branch_exists("feature/x")
        assert exists is False


# ── merge ─────────────────────────────────────────────────────────────────────


class TestMerge:
    @pytest.mark.asyncio
    async def test_regular_merge(self, git_ops):
        with patch("subprocess.run", return_value=_ok()):
            result = await git_ops.merge("feature/x", method="merge")
        assert result.ok

    @pytest.mark.asyncio
    async def test_squash_merge_commits(self, git_ops):
        """Squash merge calls git merge --squash then git commit."""
        cmds = []
        def fake_run(cmd, **kwargs):
            cmds.append(cmd)
            return _ok()
        with patch("subprocess.run", side_effect=fake_run):
            await git_ops.merge("feature/x", method="squash", message="squashed")
        cmd_strings = [" ".join(c) for c in cmds]
        assert any("--squash" in s for s in cmd_strings)
        assert any("commit" in s for s in cmd_strings)

    @pytest.mark.asyncio
    async def test_rebase(self, git_ops):
        with patch("subprocess.run", return_value=_ok()):
            result = await git_ops.merge("feature/x", method="rebase")
        assert result.ok


# ── FritzGitResult ────────────────────────────────────────────────────────────


class TestFritzGitResult:
    def test_ok_str(self):
        r = FritzGitResult(ok=True, operation="push", stdout="Everything up-to-date")
        assert "OK" in str(r)
        assert "push" in str(r)

    def test_fail_str(self):
        r = FritzGitResult(ok=False, operation="push", stderr="rejected")
        assert "FAIL" in str(r)

    def test_output_prefers_stdout(self):
        r = FritzGitResult(ok=True, operation="x", stdout="hello", stderr="world")
        assert r.output == "hello"

    def test_output_falls_back_to_stderr(self):
        r = FritzGitResult(ok=False, operation="x", stdout="", stderr="error msg")
        assert r.output == "error msg"
