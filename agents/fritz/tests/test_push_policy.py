"""
agents/fritz/tests/test_push_policy.py
────────────────────────────────
Tests for PushPolicy: evaluate_push, evaluate_merge, suggest_branch_name,
validate_config, and the PolicyViolation exception.
"""

from __future__ import annotations

import pytest

from agents.fritz.config import FritzConfig
from agents.fritz.push_policy import PolicyViolation, PushPolicy


def _policy(
    allow_push_to_main: bool = False,
    require_pr: bool = True,
    require_ci_green: bool = True,
    protected_branches: list[str] | None = None,
    auto_merge_method: str = "squash",
    default_branch: str = "main",
) -> PushPolicy:
    cfg = FritzConfig(default_branch=default_branch)
    cfg.push_policy.allow_push_to_main = allow_push_to_main
    cfg.push_policy.require_pr = require_pr
    cfg.push_policy.require_ci_green = require_ci_green
    cfg.push_policy.protected_branches = protected_branches or ["production", "release"]
    cfg.push_policy.auto_merge_method = auto_merge_method
    return PushPolicy(cfg)


# ── evaluate_push ─────────────────────────────────────────────────────────────


class TestEvaluatePush:
    def test_feature_branch_always_allowed(self):
        pol = _policy()
        d = pol.evaluate_push("feature/cool-thing")
        assert d.allowed is True

    def test_topic_branch_not_blocked(self):
        pol = _policy(allow_push_to_main=False)
        d = pol.evaluate_push("fix/typo")
        assert d.allowed is True

    def test_main_blocked_by_default(self):
        pol = _policy(allow_push_to_main=False)
        d = pol.evaluate_push("main")
        assert d.allowed is False
        assert d.requires_pr is True

    def test_master_blocked_by_default(self):
        pol = _policy(allow_push_to_main=False)
        assert pol.evaluate_push("master").allowed is False

    def test_main_allowed_when_policy_permits(self):
        pol = _policy(allow_push_to_main=True, require_pr=False)
        d = pol.evaluate_push("main")
        assert d.allowed is True

    def test_main_blocked_when_require_pr_even_if_allow_main(self):
        pol = _policy(allow_push_to_main=True, require_pr=True)
        d = pol.evaluate_push("main")
        assert d.allowed is False
        assert d.requires_pr is True

    def test_protected_branch_always_blocked(self):
        pol = _policy(protected_branches=["hotfix", "staging"])
        assert pol.evaluate_push("hotfix").allowed is False
        assert pol.evaluate_push("staging").allowed is False

    def test_protected_branch_blocked_even_if_allow_main(self):
        pol = _policy(
            allow_push_to_main=True,
            require_pr=False,
            protected_branches=["deploy"],
        )
        assert pol.evaluate_push("deploy").allowed is False

    def test_custom_default_branch_blocked(self):
        pol = _policy(allow_push_to_main=False, default_branch="trunk")
        assert pol.evaluate_push("trunk").allowed is False

    def test_ci_required_on_direct_main_push(self):
        pol = _policy(allow_push_to_main=True, require_pr=False, require_ci_green=True)
        d = pol.evaluate_push("main")
        assert d.requires_ci is True

    def test_ci_not_required_when_disabled(self):
        pol = _policy(allow_push_to_main=True, require_pr=False, require_ci_green=False)
        d = pol.evaluate_push("main")
        assert d.requires_ci is False


# ── evaluate_merge ────────────────────────────────────────────────────────────


class TestEvaluateMerge:
    def test_merge_allowed_when_ci_success(self):
        pol = _policy(require_ci_green=True)
        d = pol.evaluate_merge("feature/foo", "success")
        assert d.allowed is True

    def test_merge_blocked_when_ci_failure(self):
        pol = _policy(require_ci_green=True)
        d = pol.evaluate_merge("feature/foo", "failure")
        assert d.allowed is False

    def test_merge_blocked_when_ci_pending(self):
        pol = _policy(require_ci_green=True)
        d = pol.evaluate_merge("feature/foo", "pending")
        assert d.allowed is False

    def test_merge_blocked_when_ci_unknown(self):
        pol = _policy(require_ci_green=True)
        d = pol.evaluate_merge("feature/foo", None)
        assert d.allowed is False
        assert d.requires_ci is True

    def test_merge_allowed_when_ci_disabled(self):
        pol = _policy(require_ci_green=False)
        # CI status doesn't matter when require_ci_green is False
        d = pol.evaluate_merge("feature/foo", "failure")
        assert d.allowed is True

    def test_merge_allowed_without_ci_when_not_required(self):
        pol = _policy(require_ci_green=False)
        d = pol.evaluate_merge("feature/foo", None)
        assert d.allowed is True


# ── suggest_branch_name ───────────────────────────────────────────────────────


class TestSuggestBranchName:
    def test_returns_fritz_prefix(self):
        pol = _policy()
        name = pol.suggest_branch_name("abc123", "fix parser bug")
        assert name.startswith("fritz/")

    def test_contains_task_id_prefix(self):
        pol = _policy()
        name = pol.suggest_branch_name("grub-deadbeef", "fix parser bug")
        assert "grub-dea" in name  # first 8 chars of task_id

    def test_slugifies_description(self):
        pol = _policy()
        name = pol.suggest_branch_name("t1", "Fix the Off-By-One Error!")
        assert " " not in name
        assert "!" not in name
        assert name == name.lower()

    def test_short_task_id(self):
        pol = _policy()
        name = pol.suggest_branch_name("x", "something")
        assert "fritz/x-" in name

    def test_long_description_truncated(self):
        pol = _policy()
        name = pol.suggest_branch_name("t1", "a" * 200)
        assert len(name) <= 60  # fritz/ + 8 + - + 40 + some slack


# ── validate_config ───────────────────────────────────────────────────────────


class TestValidateConfig:
    def test_no_warnings_for_safe_defaults(self):
        pol = _policy(allow_push_to_main=False, require_pr=True, require_ci_green=True)
        assert pol.validate_config() == []

    def test_warns_push_to_main_without_ci(self):
        pol = _policy(allow_push_to_main=True, require_pr=False, require_ci_green=False)
        warnings = pol.validate_config()
        assert any("require_ci_green" in w or "untested" in w for w in warnings)

    def test_warns_push_to_main_without_pr(self):
        pol = _policy(allow_push_to_main=True, require_pr=False, require_ci_green=True)
        warnings = pol.validate_config()
        assert any("require_pr" in w or "review" in w for w in warnings)

    def test_no_warning_when_require_pr_protects_main(self):
        # require_pr=True means main is still gated — no bypass warning needed
        pol = _policy(allow_push_to_main=True, require_pr=True, require_ci_green=False)
        # push_to_main + no CI, but require_pr=True means PR acts as gate
        # The warning about no-CI may still fire but push_to_main + no_pr warning shouldn't
        warnings = pol.validate_config()
        assert not any("bypass" in w.lower() for w in warnings)


# ── PolicyViolation ───────────────────────────────────────────────────────────


class TestPolicyViolation:
    def test_attributes(self):
        exc = PolicyViolation("push", "main", "Direct push blocked.")
        assert exc.action == "push"
        assert exc.target == "main"
        assert exc.reason == "Direct push blocked."

    def test_str(self):
        exc = PolicyViolation("merge", "production", "Protected branch.")
        assert "merge" in str(exc)
        assert "production" in str(exc)
        assert "Protected branch." in str(exc)

    def test_is_exception(self):
        with pytest.raises(PolicyViolation):
            raise PolicyViolation("push", "main", "blocked")
