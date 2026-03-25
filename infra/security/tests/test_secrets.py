"""
Tests for security/secrets.py
================================

Verifies env-based secret lookup, file-backend, permission checks,
caching, and error paths.
"""

from __future__ import annotations

import os
import pytest

from infra.security.secrets import SecretManager, check_file_permissions, get_secret


class TestGetSecret:
    def test_returns_env_var_value(self, monkeypatch):
        monkeypatch.setenv("MY_SECRET_KEY", "super_secret_value")
        assert get_secret("MY_SECRET_KEY") == "super_secret_value"

    def test_returns_default_when_missing(self):
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
        mgr._cache["CACHED_KEY"] = ("cached_value", _time.monotonic() + 60)
        os.environ.pop("CACHED_KEY", None)
        v = await mgr.get("CACHED_KEY")
        assert v == "cached_value"

    def test_unknown_backend_falls_back_silently(self):
        mgr = SecretManager(backend="unknown_backend_xyz")
        assert mgr._backend == "unknown_backend_xyz"


class TestSecretManagerFileBackend:
    @pytest.mark.asyncio
    async def test_reads_key_from_file(self, tmp_path):
        secrets_file = tmp_path / "secrets"
        secrets_file.write_text("TINKER_DB_URL=sqlite:///test.db\n# comment\n\n")
        secrets_file.chmod(0o600)
        mgr = SecretManager(backend="file", secrets_file=str(secrets_file))
        os.environ.pop("TINKER_DB_URL", None)
        assert await mgr.get("TINKER_DB_URL") == "sqlite:///test.db"

    @pytest.mark.asyncio
    async def test_env_overrides_file(self, tmp_path, monkeypatch):
        secrets_file = tmp_path / "secrets"
        secrets_file.write_text("TINKER_KEY=from_file\n")
        secrets_file.chmod(0o600)
        monkeypatch.setenv("TINKER_KEY", "from_env")
        mgr = SecretManager(backend="file", secrets_file=str(secrets_file))
        assert await mgr.get("TINKER_KEY") == "from_env"

    @pytest.mark.asyncio
    async def test_missing_key_returns_default(self, tmp_path):
        secrets_file = tmp_path / "secrets"
        secrets_file.write_text("OTHER_KEY=value\n")
        secrets_file.chmod(0o600)
        os.environ.pop("MISSING_KEY_XYZ", None)
        mgr = SecretManager(backend="file", secrets_file=str(secrets_file))
        assert await mgr.get("MISSING_KEY_XYZ", default="fallback") == "fallback"

    @pytest.mark.asyncio
    async def test_nonexistent_file_returns_none(self, tmp_path):
        mgr = SecretManager(backend="file", secrets_file=str(tmp_path / "no_such_file"))
        os.environ.pop("SOME_KEY_ZZZ", None)
        assert await mgr.get("SOME_KEY_ZZZ") is None

    @pytest.mark.asyncio
    async def test_file_reload_on_change(self, tmp_path):
        secrets_file = tmp_path / "secrets"
        secrets_file.write_text("KEY=v1\n")
        secrets_file.chmod(0o600)
        os.environ.pop("KEY", None)
        mgr = SecretManager(backend="file", secrets_file=str(secrets_file), cache_ttl=0)
        assert await mgr.get("KEY") == "v1"

        # Simulate file change (ensure different mtime)
        import time

        time.sleep(0.01)
        secrets_file.write_text("KEY=v2\n")
        secrets_file.touch()  # update mtime

        assert await mgr.get("KEY") == "v2"

    def test_ignores_comments_and_blank_lines(self, tmp_path):
        secrets_file = tmp_path / "secrets"
        secrets_file.write_text("# This is a comment\n\nKEY1=val1\n  KEY2 = val2  \n")
        secrets_file.chmod(0o600)
        os.environ.pop("KEY1", None)
        os.environ.pop("KEY2", None)
        mgr = SecretManager(backend="file", secrets_file=str(secrets_file))
        assert mgr._fetch_from_file("KEY1") == "val1"
        assert mgr._fetch_from_file("KEY2") == "val2"


class TestCheckFilePermissions:
    def test_warns_on_world_readable(self, tmp_path, caplog):
        import logging

        f = tmp_path / "secrets"
        f.write_text("KEY=val\n")
        f.chmod(0o644)  # world-readable
        with caplog.at_level(logging.WARNING, logger="infra.security.secrets"):
            check_file_permissions(f)
        assert "insecure" in caplog.text.lower() or "SECURITY" in caplog.text

    def test_no_warning_on_strict_permissions(self, tmp_path, caplog):
        import logging

        f = tmp_path / "secrets"
        f.write_text("KEY=val\n")
        f.chmod(0o600)
        with caplog.at_level(logging.WARNING, logger="infra.security.secrets"):
            check_file_permissions(f)
        assert "insecure" not in caplog.text.lower()

    def test_nonexistent_file_does_not_raise(self, tmp_path):
        check_file_permissions(tmp_path / "no_such_file")
