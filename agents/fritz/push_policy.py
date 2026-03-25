"""
agents/fritz/push_policy.py
─────────────────────
Enforces push/merge policies before Fritz takes any remote action.

The policy gate is the single chokepoint between "Fritz wants to push"
and "git actually runs". All checks live here so they're easy to audit
and easy to change without touching git_ops or github_ops.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import FritzConfig, PushPolicyConfig

logger = logging.getLogger(__name__)


class PolicyViolation(Exception):
    """Raised when a Fritz action violates the configured push policy."""

    def __init__(self, action: str, target: str, reason: str) -> None:
        self.action = action
        self.target = target
        self.reason = reason
        super().__init__(f"Policy violation [{action} → {target}]: {reason}")


@dataclass
class PolicyDecision:
    allowed: bool
    reason: str
    requires_pr: bool = False
    requires_ci: bool = False


class PushPolicy:
    """
    Evaluates whether a push or merge is permitted under the current config.

    Usage:
        policy = PushPolicy(config)
        decision = policy.evaluate_push("main")
        if not decision.allowed:
            raise PolicyViolation("push", "main", decision.reason)
    """

    def __init__(self, config: FritzConfig) -> None:
        self._cfg = config.push_policy
        self._default_branch = config.default_branch

    # ── Push evaluation ───────────────────────────────────────────────────────

    def evaluate_push(self, branch: str) -> PolicyDecision:
        """
        Decide whether Fritz may push directly to `branch`.

        Returns a PolicyDecision with allowed=True/False and the reason.
        """
        # Hard-protected branches — never touch regardless of config
        if branch in self._cfg.protected_branches:
            return PolicyDecision(
                allowed=False,
                reason=f"'{branch}' is in protected_branches and cannot be pushed to directly.",
            )

        # Default branch (main/master) — gated by allow_push_to_main
        if branch == self._default_branch or branch in ("main", "master"):
            if not self._cfg.allow_push_to_main:
                return PolicyDecision(
                    allowed=False,
                    reason=(
                        f"Direct push to '{branch}' is disabled. "
                        f"Set allow_push_to_main=true in fritz_config.json or "
                        f"env FRITZ_ALLOW_PUSH_TO_MAIN=true."
                    ),
                    requires_pr=True,
                )
            if self._cfg.require_pr:
                return PolicyDecision(
                    allowed=False,
                    reason=(
                        f"require_pr=true: changes to '{branch}' must go via a PR "
                        f"even when allow_push_to_main is enabled."
                    ),
                    requires_pr=True,
                )
            logger.warning(
                "Fritz is pushing directly to '%s'. allow_push_to_main=True.", branch
            )
            return PolicyDecision(
                allowed=True,
                reason="Direct push to default branch allowed by policy.",
                requires_ci=self._cfg.require_ci_green,
            )

        # Feature / topic branches — always allowed
        return PolicyDecision(
            allowed=True,
            reason="Feature branch push — no restrictions.",
            requires_ci=False,
        )

    def evaluate_merge(self, pr_branch: str, ci_status: str | None = None) -> PolicyDecision:
        """
        Decide whether Fritz may auto-merge a PR from `pr_branch`.

        ci_status: "success" | "failure" | "pending" | None (unknown)
        """
        if self._cfg.require_ci_green:
            if ci_status is None:
                return PolicyDecision(
                    allowed=False,
                    reason="require_ci_green=True but CI status is unknown. Cannot merge.",
                    requires_ci=True,
                )
            if ci_status != "success":
                return PolicyDecision(
                    allowed=False,
                    reason=f"require_ci_green=True but CI status is '{ci_status}'. Cannot merge.",
                    requires_ci=True,
                )

        return PolicyDecision(
            allowed=True,
            reason="Merge permitted by policy.",
        )

    def suggest_branch_name(self, task_id: str, description: str) -> str:
        """
        Generate a safe, policy-compliant branch name for a Fritz task.
        Always creates a feature branch (never pushes directly to main by default).
        """
        import re
        slug = re.sub(r"[^a-z0-9]+", "-", description.lower())[:40].strip("-")
        short_id = task_id[:8] if len(task_id) > 8 else task_id
        return f"fritz/{short_id}-{slug}"

    def validate_config(self) -> list[str]:
        """Return warnings about potentially dangerous policy combinations."""
        warnings = []
        if self._cfg.allow_push_to_main and not self._cfg.require_ci_green:
            warnings.append(
                "allow_push_to_main=True AND require_ci_green=False: Fritz can push "
                "untested code directly to main."
            )
        if self._cfg.allow_push_to_main and not self._cfg.require_pr:
            warnings.append(
                "allow_push_to_main=True AND require_pr=False: Fritz bypasses code review."
            )
        return warnings
