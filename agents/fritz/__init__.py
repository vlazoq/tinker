"""
agents/fritz/
──────
Fritz is Tinker's Git/GitHub/Gitea integration layer.

It gives Tinker+Grub the same version-control capabilities Claude Code has,
plus extras:
  - Dedicated bot account OR delegated user credentials
  - Configurable push-to-main policy
  - Full GitHub API: PRs, merges, releases, collaborators, CI gating
  - Full Gitea API: self-hosted Git platform support (drop-in alternative)
  - Simultaneous multi-remote push (e.g. GitHub + local Gitea)
  - Structured audit trail for every git/remote action

Typical usage
─────────────
    from agents.fritz import FritzAgent, FritzConfig

    config = FritzConfig.from_file("fritz_config.json")
    fritz  = FritzAgent(config)

    await fritz.commit_and_ship(
        files=["src/fix.py"],
        message="fix: correct off-by-one in parser",
        task_id="grub-abc123",
    )
"""

from .config import FritzConfig

# FritzAgent is loaded lazily so that ``import agents`` does not pull in
# the entire httpx dependency chain (agent → gitea_ops → httpx).
# Only code that actually *uses* FritzAgent pays the import cost.


def __getattr__(name: str):
    if name == "FritzAgent":
        from .agent import FritzAgent

        globals()["FritzAgent"] = FritzAgent
        return FritzAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["FritzAgent", "FritzConfig"]
