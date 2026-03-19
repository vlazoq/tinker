"""
Tests for security/secrets.py
================================

Verifies env-based secret lookup, default values, caching, and error paths.
These tests always use the 'env' backend so they require no external services.
"""

from __future__ import annotations

import os
import pytest

from security.secrets import SecretManager, get_secret


class TestGetSecret:
    def test_returns_env_var_value(self, monkeypatch):
        monkeypatch.setenv("MY_SECRET_KEY", "super_secret_value")
        assert get_secret("MY_SECRET_KEY") == "super_secret_value"

    def test_returns_default_when_missing(self):
        # Ensure the key is not set
        os.environ.pop("DEFINITELY_MISSING_KEY_XYZ", None)
        result = get_secret("DEFINITELY_MISSING_KEY_XYZ", default="fallback")
        assert result == "fallback"

    def test_required_raises_when_missing(self):
        os.environ.pop("REQUIRED_SECRET_XYZ", None)
        with pytest.raises(ValueError, match="REQUIRED_SECRET_XYZ"):
            get_secret("REQUIRED_SECRET_XYZ", required=True)

    def test_required_not_raised_when_present(self, monkeypatch):
        monkeypatch.setenv("REQUIRED_SECRET_PRESENT", "value")
        result = get_secret("REQUIRED_SECRET_PRESENT", required=True)
        assert result == "value"


class TestSecretManagerEnvBackend:
    @pytest.mark.asyncio
    async def test_get_from_env(self, monkeypatch):
        monkeypatch.setenv("TINKER_TEST_SECRET", "abc123")
        mgr = SecretManager(backend="env")
        assert await mgr.get("TINKER_TEST_SECRET") == "abc123"

    @pytest.mark.asyncio
    async def test_get_with_default(self):
        os.environ.pop("MISSING_SECRET_ABC", None)
        mgr = SecretManager(backend="env")
        assert (
            await mgr.get("MISSING_SECRET_ABC", default="default_val") == "default_val"
        )

    @pytest.mark.asyncio
    async def test_caches_value(self):
        import time as _time

        mgr = SecretManager(backend="env", cache_ttl=60)
        # Populate the cache manually (simulates a previous backend fetch)
        mgr._cache["CACHED_KEY"] = ("cached_value", _time.monotonic() + 60)
        # Env var is NOT set — should return cached value
        os.environ.pop("CACHED_KEY", None)
        v = await mgr.get("CACHED_KEY")
        assert v == "cached_value"

    def test_unknown_backend_falls_back_silently(self):
        # Unknown backend logs a warning and falls back to env — does not raise at init
        mgr = SecretManager(backend="unknown_backend_xyz")
        assert mgr._backend == "unknown_backend_xyz"
