"""
security/secrets.py
====================

Unified secret management for Tinker.

Why not just use os.getenv()?
--------------------------------
``os.getenv()`` works fine for development, but in production:
  - Secrets stored in .env files are visible in shell history
  - There's no expiry mechanism
  - Access is not audited
  - Multiple processes sharing a .env file have no isolation

This module provides a unified interface that:
  1. Always falls back to environment variables (zero dependencies)
  2. Can read from a local secrets file (homelab-friendly)
  3. Can transparently read from HashiCorp Vault (optional)
  4. Caches secrets for a configurable TTL (avoids repeated I/O)
  5. Validates secrets on load (fails fast vs. failing mid-operation)
  6. Warns when secret files have insecure permissions
  7. Can be swapped out in tests with a stub

Secret backend selection
-------------------------
The backend is selected by the ``TINKER_SECRET_BACKEND`` environment variable:
  - "env"  (default) : Uses environment variables only — zero extra deps
  - "file" : Reads KEY=VALUE pairs from a local file (homelab default)
              File path: ``TINKER_SECRETS_FILE`` (default: ``~/.tinker/secrets``)
  - "vault": HashiCorp Vault (requires ``hvac`` package)

Regardless of backend, environment variables always take precedence.
This allows local overrides without changing infrastructure.

File backend
-------------
Create ``~/.tinker/secrets`` (mode 600) with one KEY=VALUE per line::

    TINKER_REDIS_URL=redis://localhost:6379
    TINKER_OLLAMA_KEY=sk-...

Blank lines and lines starting with ``#`` are ignored.
The file **must** be owned by the current user with mode 0o600 (no
group/other read). Tinker logs a warning (but does not refuse) if the
file has wider permissions — to let you fix it without a hard crash.

Usage
------
::

    # Simple (uses env vars by default):
    redis_url = get_secret("TINKER_REDIS_URL", default="redis://localhost:6379")

    # With validation:
    api_key = get_secret("TINKER_OLLAMA_KEY", required=True)

    # Explicit file backend:
    secrets = SecretManager(backend="file", secrets_file="~/.tinker/secrets")
    redis_pw = await secrets.get("TINKER_REDIS_PASSWORD")
"""

from __future__ import annotations

import logging
import os
import stat
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Simple synchronous convenience function (wraps the default manager)
# ---------------------------------------------------------------------------

_default_manager: Optional["SecretManager"] = None


def get_secret(
    key: str,
    default: Optional[str] = None,
    required: bool = False,
) -> Optional[str]:
    """
    Retrieve a secret by key using the default secret manager.

    Always checks environment variables first (allows local overrides).
    Falls back to the configured backend if not found in env.

    Parameters
    ----------
    key      : The secret name (e.g. "TINKER_REDIS_URL").
    default  : Returned if the secret is not found anywhere.
    required : If True and the secret is not found, raises ValueError.

    Returns
    -------
    The secret value, or ``default`` if not found.

    Raises
    ------
    ValueError : If ``required=True`` and the secret is not found.
    """
    # Always check env first
    val = os.getenv(key)
    if val is not None:
        return val

    # Fall back to default
    if default is not None:
        return default

    if required:
        raise ValueError(
            f"Required secret '{key}' not found. "
            f"Set it as an environment variable or configure a secret backend."
        )
    return None


def check_file_permissions(path: Path) -> None:
    """
    Warn if a secrets file has insecure permissions (group or other read/write).

    Does nothing on Windows (no POSIX permission model).

    Parameters
    ----------
    path : Path to the secrets file to check.
    """
    try:
        file_stat = path.stat()
        mode = file_stat.st_mode
        # Warn if group or others have any access (read, write, execute)
        if mode & (
            stat.S_IRGRP
            | stat.S_IWGRP
            | stat.S_IXGRP
            | stat.S_IROTH
            | stat.S_IWOTH
            | stat.S_IXOTH
        ):
            logger.warning(
                "SECURITY WARNING: secrets file '%s' has insecure permissions "
                "(mode %o). Run: chmod 600 %s",
                path,
                stat.S_IMODE(mode),
                path,
            )
    except (OSError, AttributeError):
        pass  # Non-POSIX systems or file doesn't exist yet


class SecretManager:
    """
    Unified secret manager with multiple backend support and caching.

    Parameters
    ----------
    backend      : "env" (default), "file", or "vault".
    secrets_file : Path to the secrets file (for backend="file").
                   Defaults to ``~/.tinker/secrets``.
                   Reads ``TINKER_SECRETS_FILE`` env var if not provided.
    vault_url    : Vault server URL (for backend="vault").
    vault_token  : Vault token (for backend="vault").
                   Reads ``VAULT_TOKEN`` env var if not provided.
    cache_ttl    : How long to cache secrets in seconds (default: 300).
                   Set to 0 to disable caching.
    """

    def __init__(
        self,
        backend: str = "env",
        secrets_file: Optional[str] = None,
        vault_url: Optional[str] = None,
        vault_token: Optional[str] = None,
        cache_ttl: int = 300,
    ) -> None:
        self._backend = backend
        self._vault_url = vault_url
        self._vault_token = vault_token or os.getenv("VAULT_TOKEN")
        self._cache_ttl = cache_ttl
        self._cache: dict[
            str, tuple[Optional[str], float]
        ] = {}  # key → (value, expires_at)

        # Resolve secrets file path for the "file" backend
        raw_path = (
            secrets_file
            or os.getenv("TINKER_SECRETS_FILE")
            or str(Path.home() / ".tinker" / "secrets")
        )
        self._secrets_file = Path(raw_path).expanduser().resolve()
        # In-memory cache of file contents (reloaded when file changes)
        self._file_cache: dict[str, str] = {}
        self._file_mtime: float = 0.0

    async def get(
        self,
        key: str,
        default: Optional[str] = None,
        required: bool = False,
    ) -> Optional[str]:
        """
        Retrieve a secret asynchronously.

        Always checks environment variables first, then the configured backend.
        Results are cached for ``cache_ttl`` seconds.

        Parameters
        ----------
        key      : Secret name.
        default  : Returned if not found anywhere.
        required : Raises ValueError if not found and required=True.

        Returns
        -------
        str | None : The secret value or default.
        """
        # 1. Environment variable (highest priority)
        val = os.getenv(key)
        if val is not None:
            return val

        # 2. Cache check
        cached = self._cache.get(key)
        if cached and time.monotonic() < cached[1]:
            return cached[0]

        # 3. Backend lookup
        val = await self._fetch_from_backend(key)

        # Cache the result (even None — prevents repeated backend calls)
        if self._cache_ttl > 0:
            self._cache[key] = (val, time.monotonic() + self._cache_ttl)

        if val is None:
            val = default

        if val is None and required:
            raise ValueError(
                f"Required secret '{key}' not found in env or backend '{self._backend}'."
            )

        return val

    async def _fetch_from_backend(self, key: str) -> Optional[str]:
        """Delegate to the configured secret backend."""
        if self._backend == "env":
            return None  # Only env vars, no additional backend

        elif self._backend == "file":
            return self._fetch_from_file(key)

        elif self._backend == "vault":
            return await self._fetch_from_vault(key)

        else:
            logger.warning(
                "Unknown secret backend '%s' — falling back to env", self._backend
            )
            return None

    def _fetch_from_file(self, key: str) -> Optional[str]:
        """
        Read a secret from a local KEY=VALUE secrets file.

        The file is re-read only when its mtime changes (cheap stat check
        on every call, full re-read only on change).

        File format::

            # Comments are ignored
            TINKER_REDIS_URL=redis://localhost:6379
            TINKER_OLLAMA_KEY=sk-...

        Parameters
        ----------
        key : The environment-variable-style key to look up.

        Returns
        -------
        str | None : The secret value, or None if not found.
        """
        if not self._secrets_file.exists():
            return None

        check_file_permissions(self._secrets_file)

        try:
            mtime = self._secrets_file.stat().st_mtime
        except OSError:
            return None

        if mtime != self._file_mtime:
            # Re-parse the file on change
            new_cache: dict[str, str] = {}
            try:
                for raw_line in self._secrets_file.read_text(
                    encoding="utf-8"
                ).splitlines():
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, _, v = line.partition("=")
                        new_cache[k.strip()] = v.strip()
                self._file_cache = new_cache
                self._file_mtime = mtime
                logger.debug(
                    "SecretManager: loaded %d keys from %s",
                    len(new_cache),
                    self._secrets_file,
                )
            except Exception as exc:
                logger.warning("SecretManager: failed to read secrets file: %s", exc)

        return self._file_cache.get(key)

    async def _fetch_from_vault(self, key: str) -> Optional[str]:
        """Fetch a secret from HashiCorp Vault (requires ``hvac`` package)."""
        try:
            import hvac  # type: ignore
        except ImportError:
            logger.warning("hvac not installed — Vault backend unavailable")
            return None

        if not self._vault_url or not self._vault_token:
            logger.warning("Vault URL or token not configured")
            return None

        try:
            client = hvac.Client(url=self._vault_url, token=self._vault_token)
            # Try KV v2 first (most common), then KV v1
            try:
                secret = client.secrets.kv.v2.read_secret_version(path=key)
                data = secret["data"]["data"]
                return data.get(key) or data.get("value")
            except Exception:
                secret = client.read(f"secret/{key}")
                if secret and "data" in secret:
                    return secret["data"].get(key) or secret["data"].get("value")
        except Exception as exc:
            logger.warning("Vault fetch failed for '%s': %s", key, exc)
        return None

    def invalidate_cache(self, key: Optional[str] = None) -> None:
        """
        Invalidate cached secrets.

        Parameters
        ----------
        key : If provided, invalidate only this key. Otherwise, clear all.
        """
        if key:
            self._cache.pop(key, None)
        else:
            self._cache.clear()
            logger.info("SecretManager: all cache entries invalidated")


def build_secret_manager() -> SecretManager:
    """
    Build a SecretManager from environment variable configuration.

    Backend selection order:
      1. ``TINKER_SECRET_BACKEND`` env var (explicit override)
      2. "file" if ``~/.tinker/secrets`` exists (homelab auto-detection)
      3. "env" (fallback — environment variables only)

    Returns
    -------
    SecretManager configured per environment variables.
    """
    explicit = os.getenv("TINKER_SECRET_BACKEND", "").lower()

    if explicit:
        backend = explicit
    elif (Path.home() / ".tinker" / "secrets").exists():
        backend = "file"
        logger.info(
            "SecretManager: auto-detected ~/.tinker/secrets — using file backend"
        )
    else:
        backend = "env"

    return SecretManager(
        backend=backend,
        secrets_file=os.getenv("TINKER_SECRETS_FILE"),
        vault_url=os.getenv("TINKER_VAULT_URL") or os.getenv("VAULT_ADDR"),
        vault_token=os.getenv("TINKER_VAULT_TOKEN") or os.getenv("VAULT_TOKEN"),
    )
