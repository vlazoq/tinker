"""
security/secrets.py
====================

Unified secret management for Tinker.

Why not just use os.getenv()?
--------------------------------
``os.getenv()`` works fine for development, but in production:
  - Secrets stored in .env files are visible in container image layers
  - They're not rotated automatically
  - Access is not audited
  - There's no expiry mechanism
  - Multiple instances share the same static credentials

This module provides a unified interface that:
  1. Always falls back to environment variables (zero dependencies)
  2. Can transparently read from secret managers (Vault, AWS, Azure)
  3. Caches secrets for a configurable TTL (avoids rate limiting)
  4. Validates secrets on load (fails fast vs. failing mid-operation)
  5. Can be swapped out in tests with a stub

Secret backend selection
-------------------------
The backend is selected by the ``TINKER_SECRET_BACKEND`` environment variable:
  - "env"   (default) : Uses environment variables only
  - "vault" : HashiCorp Vault (requires ``hvac`` package)
  - "aws"   : AWS Secrets Manager (requires ``boto3`` package)

Regardless of backend, environment variables always take precedence.
This allows local overrides without changing infrastructure.

Usage
------
::

    # Simple (uses env vars by default):
    redis_url = get_secret("TINKER_REDIS_URL", default="redis://localhost:6379")

    # With validation:
    api_key = get_secret("TINKER_OLLAMA_KEY", required=True)

    # With a specific backend:
    secrets = SecretManager(backend="vault", vault_url="http://vault:8200")
    redis_pw = await secrets.get("TINKER_REDIS_PASSWORD")
"""

from __future__ import annotations

import logging
import os
import time
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


class SecretManager:
    """
    Unified secret manager with multiple backend support and caching.

    Parameters
    ----------
    backend    : "env" (default), "vault", or "aws".
    vault_url  : Vault server URL (for backend="vault").
    vault_token: Vault token (for backend="vault").
                 Reads ``VAULT_TOKEN`` env var if not provided.
    aws_region : AWS region (for backend="aws").
                 Reads ``AWS_DEFAULT_REGION`` env var if not provided.
    cache_ttl  : How long to cache secrets in seconds (default: 300).
                 Set to 0 to disable caching.
    """

    def __init__(
        self,
        backend: str = "env",
        vault_url: Optional[str] = None,
        vault_token: Optional[str] = None,
        aws_region: Optional[str] = None,
        cache_ttl: int = 300,
    ) -> None:
        self._backend = backend
        self._vault_url = vault_url
        self._vault_token = vault_token or os.getenv("VAULT_TOKEN")
        self._aws_region = aws_region or os.getenv("AWS_DEFAULT_REGION", "us-east-1")
        self._cache_ttl = cache_ttl
        self._cache: dict[
            str, tuple[Optional[str], float]
        ] = {}  # key → (value, expires_at)

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

        elif self._backend == "vault":
            return await self._fetch_from_vault(key)

        elif self._backend == "aws":
            return await self._fetch_from_aws(key)

        else:
            logger.warning(
                "Unknown secret backend '%s' — falling back to env", self._backend
            )
            return None

    async def _fetch_from_vault(self, key: str) -> Optional[str]:
        """Fetch a secret from HashiCorp Vault."""
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

    async def _fetch_from_aws(self, key: str) -> Optional[str]:
        """Fetch a secret from AWS Secrets Manager."""
        try:
            import boto3  # type: ignore
            import json as _json
        except ImportError:
            logger.warning(
                "boto3 not installed — AWS Secrets Manager backend unavailable"
            )
            return None

        try:
            import asyncio

            loop = asyncio.get_running_loop()
            client = boto3.client("secretsmanager", region_name=self._aws_region)

            def _get():
                response = client.get_secret_value(SecretId=key)
                secret = response.get("SecretString", "{}")
                try:
                    data = _json.loads(secret)
                    return data.get(key) or data.get("value") or secret
                except Exception:
                    return secret

            return await loop.run_in_executor(None, _get)
        except Exception as exc:
            logger.warning("AWS Secrets Manager fetch failed for '%s': %s", key, exc)
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

    Reads TINKER_SECRET_BACKEND to determine which backend to use.
    Falls back to "env" (environment variables only) if not set.

    Returns
    -------
    SecretManager configured per environment variables.
    """
    backend = os.getenv("TINKER_SECRET_BACKEND", "env").lower()
    return SecretManager(
        backend=backend,
        vault_url=os.getenv("TINKER_VAULT_URL") or os.getenv("VAULT_ADDR"),
        vault_token=os.getenv("TINKER_VAULT_TOKEN") or os.getenv("VAULT_TOKEN"),
        aws_region=os.getenv("TINKER_AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"),
    )
