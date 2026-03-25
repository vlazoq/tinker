"""
Entry point: python -m tinker.fritz

Modes:
  (default)        — Verify all connections and print Fritz status
  --push BRANCH    — Push a branch (respects push policy)
  --ship           — Commit all staged changes and ship (PR flow or direct)
  --verify         — Test GitHub/Gitea credentials and print authenticated user
  --config PATH    — Path to fritz_config.json (default: fritz_config.json)

Examples:
  python -m tinker.fritz --verify
  python -m tinker.fritz --push my-feature
  python -m tinker.fritz --ship --message "fix: typo" --task-id grub-abc123
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .agent import FritzAgent
from .config import FritzConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [fritz] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


async def _run(args: argparse.Namespace) -> int:
    config = FritzConfig.from_file(args.config)
    agent = FritzAgent(config)

    try:
        await agent.setup()
    except Exception as exc:
        logger.error("Fritz setup failed: %s", exc)
        return 1

    if args.verify:
        results = await agent.verify_connections()
        for platform, ok in results.items():
            status = "OK" if ok else "FAIL"
            print(f"  {platform:12s} {status}")
        return 0 if all(results.values()) else 1

    if args.push:
        result = await agent.git.push(args.push)
        print(result)
        return 0 if result.ok else 1

    if args.ship:
        result = await agent.commit_and_ship(
            message=args.message or "chore: automated commit by Fritz",
            task_id=args.task_id or "manual",
            task_description=args.message or "",
        )
        print(result)
        return 0 if result.ok else 1

    # Default: print status
    branch = await agent.git.current_branch()
    status = await agent.git.status()
    print(f"Fritz ready.")
    print(f"  Branch:   {branch}")
    print(f"  Status:   {status.stdout.strip() or 'clean'}")
    print(f"  GitHub:   {'enabled' if config.github_enabled else 'disabled'}")
    print(f"  Gitea:    {'enabled' if config.gitea_enabled else 'disabled'}")
    print(f"  Identity: {config.identity_mode} ({config.git_name} <{config.git_email}>)")
    pp = config.push_policy
    print(f"  Policy:   push_to_main={pp.allow_push_to_main} require_pr={pp.require_pr} require_ci={pp.require_ci_green}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fritz — Git/GitHub/Gitea integration for Tinker+Grub"
    )
    parser.add_argument(
        "--config",
        default="fritz_config.json",
        help="Path to fritz_config.json",
    )
    parser.add_argument("--verify", action="store_true", help="Test credentials")
    parser.add_argument("--push", metavar="BRANCH", help="Push a branch")
    parser.add_argument("--ship", action="store_true", help="Commit and ship")
    parser.add_argument("--message", "-m", help="Commit message (used with --ship)")
    parser.add_argument("--task-id", help="Task ID (used with --ship)")

    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
