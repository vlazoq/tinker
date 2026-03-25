"""
agents/fritz/credentials.py
─────────────────────
Loads Fritz credentials from Tinker's SecretManager.

Tokens are NEVER stored in config files or passed as plain arguments.
The config holds only the *key name* (e.g. "FRITZ_GITHUB_TOKEN");
this module resolves the actual value at runtime from:
  1. Environment variables (highest priority)
  2. ~/.tinker/secrets file (KEY=VALUE format, mode 600)
  3. HashiCorp Vault (if configured)

Usage
─────
    creds = FritzCredentials(config)
    await creds.load()
    token = creds.github_token   # None if not configured
"""

from __future__ import annotations

import logging

# Fritz lives inside the tinker package but is also importable standalone
# (the UI files add the tinker root to sys.path and do `from agents.fritz.config import ...`).
# Support both import styles with a graceful fallback.
try:
    from ..security.secrets import SecretManager
except ImportError:
    from infra.security.secrets import SecretManager  # type: ignore[no-redef]

from .config import FritzConfig

logger = logging.getLogger(__name__)


class FritzCredentials:
    """Resolves Fritz tokens via Tinker's SecretManager."""

    def __init__(self, config: FritzConfig) -> None:
        self._config = config
        self._secrets = SecretManager()
        self.github_token: str | None = None
        self.gitea_token: str | None = None

    async def load(self) -> None:
        """Fetch all configured tokens. Call once before using credentials."""
        if self._config.github_enabled:
            self.github_token = await self._secrets.get(
                self._config.github_token_key
            )
            if not self.github_token:
                logger.warning(
                    "GitHub enabled but token key '%s' resolved to empty. "
                    "GitHub operations will fail.",
                    self._config.github_token_key,
                )

        if self._config.gitea_enabled:
            self.gitea_token = await self._secrets.get(
                self._config.gitea_token_key
            )
            if not self.gitea_token:
                logger.warning(
                    "Gitea enabled but token key '%s' resolved to empty. "
                    "Gitea operations will fail.",
                    self._config.gitea_token_key,
                )

    def require_github(self) -> str:
        """Return GitHub token or raise if unavailable."""
        if not self.github_token:
            raise RuntimeError(
                f"GitHub token not available. "
                f"Set env var '{self._config.github_token_key}' or add it to "
                f"~/.tinker/secrets."
            )
        return self.github_token

    def require_gitea(self) -> str:
        """Return Gitea token or raise if unavailable."""
        if not self.gitea_token:
            raise RuntimeError(
                f"Gitea token not available. "
                f"Set env var '{self._config.gitea_token_key}' or add it to "
                f"~/.tinker/secrets."
            )
        return self.gitea_token
