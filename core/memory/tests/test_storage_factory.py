"""
Tests for core/memory/storage_factory.py.
"""

from __future__ import annotations

import pytest

from core.memory.storage_factory import create_storage_adapter


class TestCreateStorageAdapter:
    def test_redis_backend(self):
        adapter = create_storage_adapter("redis")
        from core.memory.storage import RedisAdapter

        assert isinstance(adapter, RedisAdapter)

    def test_duckdb_backend(self, tmp_path):
        adapter = create_storage_adapter("duckdb", path=str(tmp_path / "test.duckdb"))
        from core.memory.storage import DuckDBAdapter

        assert isinstance(adapter, DuckDBAdapter)

    def test_chroma_backend(self, tmp_path):
        adapter = create_storage_adapter(
            "chroma", path=str(tmp_path / "chroma"), collection_name="test_col"
        )
        from core.memory.storage import ChromaAdapter

        assert isinstance(adapter, ChromaAdapter)

    def test_chromadb_alias(self, tmp_path):
        adapter = create_storage_adapter(
            "chromadb", path=str(tmp_path / "chroma2"), collection_name="test_col2"
        )
        from core.memory.storage import ChromaAdapter

        assert isinstance(adapter, ChromaAdapter)

    def test_sqlite_backend(self, tmp_path):
        adapter = create_storage_adapter("sqlite", path=str(tmp_path / "test.sqlite"))
        from core.memory.storage import SQLiteAdapter

        assert isinstance(adapter, SQLiteAdapter)

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unknown storage backend"):
            create_storage_adapter("mongodb")

    def test_env_var_used_for_redis_url(self, monkeypatch):
        monkeypatch.setenv("TINKER_REDIS_URL", "redis://myhost:6379")
        adapter = create_storage_adapter("redis")
        assert adapter.url == "redis://myhost:6379"

    def test_kwarg_overrides_env_for_redis(self, monkeypatch):
        monkeypatch.setenv("TINKER_REDIS_URL", "redis://envhost:6379")
        adapter = create_storage_adapter("redis", url="redis://kwarghost:6379")
        # kwarg takes priority
        assert "kwarghost" in adapter.url

    def test_case_insensitive_backend(self, tmp_path):
        adapter = create_storage_adapter("SQLite", path=str(tmp_path / "ci.sqlite"))
        from core.memory.storage import SQLiteAdapter

        assert isinstance(adapter, SQLiteAdapter)
