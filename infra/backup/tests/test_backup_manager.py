"""
Tests for backup/backup_manager.py
=====================================

Covers full backup, incremental backup (skips unchanged files),
verify-after-write, count-based retention, and restore.
"""

from __future__ import annotations

import json
import pytest

from infra.backup.backup_manager import BackupManager


@pytest.fixture
def bm(tmp_path):
    """BackupManager wired to temporary files."""
    # Create dummy source files
    duckdb = tmp_path / "tinker_session.duckdb"
    sqlite = tmp_path / "tinker_tasks.sqlite"
    chroma = tmp_path / "chroma_db"
    chroma.mkdir()
    (chroma / "data.bin").write_bytes(b"\x00" * 64)

    duckdb.write_bytes(b"duckdb_data_v1")
    sqlite.write_bytes(b"sqlite_data_v1")

    return BackupManager(
        backup_dir=str(tmp_path / "backups"),
        duckdb_path=str(duckdb),
        sqlite_path=str(sqlite),
        chroma_path=str(chroma),
        retention_days=7,
        keep_count=2,
    )


class TestFullBackup:
    @pytest.mark.asyncio
    async def test_creates_snapshot_directory(self, bm):
        backup_id = await bm.backup()
        snapshot = bm._backup_dir / backup_id
        assert snapshot.exists()

    @pytest.mark.asyncio
    async def test_manifest_created(self, bm):
        backup_id = await bm.backup()
        manifest_path = bm._backup_dir / backup_id / "manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        assert manifest["id"] == backup_id
        assert manifest["mode"] == "full"

    @pytest.mark.asyncio
    async def test_checksums_file_created(self, bm):
        backup_id = await bm.backup()
        assert (bm._backup_dir / backup_id / "checksums.json").exists()

    @pytest.mark.asyncio
    async def test_files_copied(self, bm):
        backup_id = await bm.backup()
        manifest = json.loads(
            (bm._backup_dir / backup_id / "manifest.json").read_text()
        )
        assert manifest["files"]["duckdb"]["status"] == "ok"
        assert manifest["files"]["sqlite"]["status"] == "ok"
        assert manifest["files"]["chroma"]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_missing_source_file_skipped(self, tmp_path):
        """Backup continues even when a source file doesn't exist."""
        bm = BackupManager(
            backup_dir=str(tmp_path / "backups"),
            duckdb_path=str(tmp_path / "nonexistent.duckdb"),
            sqlite_path=str(tmp_path / "nonexistent.sqlite"),
            chroma_path=str(tmp_path / "no_chroma"),
        )
        backup_id = await bm.backup()
        manifest = json.loads(
            (bm._backup_dir / backup_id / "manifest.json").read_text()
        )
        assert manifest["files"]["duckdb"]["status"] == "skipped"


class TestIncrementalBackup:
    @pytest.mark.asyncio
    async def test_unchanged_file_skipped(self, bm):
        # Full backup first
        await bm.backup()
        # Incremental — files unchanged
        backup_id2 = await bm.backup(incremental=True)
        manifest = json.loads(
            (bm._backup_dir / backup_id2 / "manifest.json").read_text()
        )
        assert manifest["mode"] == "incremental"
        assert manifest["files"]["duckdb"]["status"] == "unchanged"
        assert manifest["files"]["sqlite"]["status"] == "unchanged"

    @pytest.mark.asyncio
    async def test_changed_file_backed_up(self, bm):
        await bm.backup()
        # Modify source file
        bm._duckdb_path.write_bytes(b"duckdb_data_v2_changed")
        backup_id2 = await bm.backup(incremental=True)
        manifest = json.loads(
            (bm._backup_dir / backup_id2 / "manifest.json").read_text()
        )
        assert manifest["files"]["duckdb"]["status"] == "ok"
        assert manifest["files"]["sqlite"]["status"] == "unchanged"

    @pytest.mark.asyncio
    async def test_first_incremental_without_prior_full_backs_up_everything(self, bm):
        # No prior backup — incremental falls back to full
        backup_id = await bm.backup(incremental=True)
        manifest = json.loads(
            (bm._backup_dir / backup_id / "manifest.json").read_text()
        )
        assert manifest["files"]["duckdb"]["status"] == "ok"


class TestVerify:
    @pytest.mark.asyncio
    async def test_verify_clean_backup_returns_true(self, bm):
        backup_id = await bm.backup()
        assert await bm.verify(backup_id) is True

    @pytest.mark.asyncio
    async def test_verify_tampered_backup_returns_false(self, bm):
        backup_id = await bm.backup()
        # Tamper with the backed-up duckdb file
        snapshot_dir = bm._backup_dir / backup_id
        for f in snapshot_dir.glob("*.duckdb"):
            f.write_bytes(b"tampered!")
        assert await bm.verify(backup_id) is False

    @pytest.mark.asyncio
    async def test_verify_missing_backup_returns_false(self, bm):
        assert await bm.verify("nonexistent_backup_id") is False


class TestPruneOldBackups:
    @pytest.mark.asyncio
    async def test_keep_count_prevents_over_pruning(self, tmp_path):
        """keep_count=2 means at least 2 backups survive even with retention_days=0."""
        import datetime as dt
        from datetime import timezone

        bm = BackupManager(
            backup_dir=str(tmp_path / "backups"),
            duckdb_path=str(tmp_path / "db.duckdb"),
            sqlite_path=str(tmp_path / "tasks.sqlite"),
            chroma_path=str(tmp_path / "chroma"),
            retention_days=0,  # would prune everything by age
            keep_count=2,
        )

        # Create 3 synthetic backup entries manually
        for i in range(3):
            d = bm._backup_dir / f"fake_backup_{i:03d}"
            d.mkdir(parents=True)
            manifest = {
                "id": d.name,
                "created_at": (
                    dt.datetime.now(timezone.utc) - dt.timedelta(days=10 + i)
                ).isoformat(),
                "files": {},
                "errors": [],
            }
            (d / "manifest.json").write_text(json.dumps(manifest))

        deleted = await bm.prune_old_backups()
        remaining = [d for d in bm._backup_dir.iterdir() if d.is_dir()]
        assert len(remaining) >= 2
        assert deleted == 1  # only the oldest beyond keep_count was pruned


class TestListBackups:
    @pytest.mark.asyncio
    async def test_list_returns_newest_first(self, bm):
        id1 = await bm.backup()
        id2 = await bm.backup()
        backups = await bm.list_backups()
        ids = [b["id"] for b in backups]
        # id2 should appear before id1 (newer)
        assert ids.index(id2) < ids.index(id1)
