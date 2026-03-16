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
        with pytest.raises(KeyError, match="REQUIRED_SECRET_XYZ"):
            get_secret("REQUIRED_SECRET_XYZ", required=True)

    def test_required_not_raised_when_present(self, monkeypatch):
        monkeypatch.setenv("REQUIRED_SECRET_PRESENT", "value")
        result = get_secret("REQUIRED_SECRET_PRESENT", required=True)
        assert result == "value"


class TestSecretManagerEnvBackend:
    def test_get_from_env(self, monkeypatch):
        monkeypatch.setenv("TINKER_TEST_SECRET", "abc123")
        mgr = SecretManager(backend="env")
        assert mgr.get("TINKER_TEST_SECRET") == "abc123"

    def test_get_with_default(self):
        os.environ.pop("MISSING_SECRET_ABC", None)
        mgr = SecretManager(backend="env")
        assert mgr.get("MISSING_SECRET_ABC", default="default_val") == "default_val"

    def test_caches_value(self, monkeypatch):
        monkeypatch.setenv("CACHED_SECRET", "cached_value")
        mgr = SecretManager(backend="env", cache_ttl=60)
        v1 = mgr.get("CACHED_SECRET")
        # Remove from env — should still return cached value
        monkeypatch.delenv("CACHED_SECRET")
        v2 = mgr.get("CACHED_SECRET")
        assert v1 == v2 == "cached_value"

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="backend"):
            SecretManager(backend="unknown_backend_xyz")
