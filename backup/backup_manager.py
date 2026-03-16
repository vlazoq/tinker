"""
backup/backup_manager.py
=========================

Automated backup and restore for Tinker's durable data stores.

What gets backed up
--------------------
Tinker has three durable stores that survive process restarts:

  DuckDB    (tinker_session.duckdb)   — Session artifacts from micro loops
  SQLite    (tinker_tasks.sqlite)     — Task registry and task engine state
  ChromaDB  (./chroma_db/)           — Research archive (vector embeddings)
  Git repo  (./tinker_workspace/)    — Architecture state (already versioned)

Redis is intentionally excluded — it's ephemeral working memory.

Backup strategy
---------------
Each backup creates a timestamped snapshot directory containing:
  - A copy of the DuckDB file
  - A copy of the SQLite file
  - A tar.gz of the ChromaDB directory
  - A JSON manifest with metadata (timestamp, file sizes, checksums)

Backups are kept for ``retention_days`` (default: 7 days), with older
backups pruned automatically.

Usage
------
::

    bm = BackupManager(
        backup_dir     = "./tinker_backups",
        duckdb_path    = "./tinker_session.duckdb",
        sqlite_path    = "./tinker_tasks.sqlite",
        chroma_path    = "./chroma_db",
        retention_days = 7,
    )

    # Perform a backup:
    backup_id = await bm.backup()
    print(f"Backup created: {backup_id}")

    # List available backups:
    backups = await bm.list_backups()
    for b in backups:
        print(b["id"], b["created_at"], b["total_size_mb"])

    # Restore from a specific backup:
    await bm.restore(backup_id)

    # Or restore the latest:
    await bm.restore_latest()

CLI
---
::

    python -m backup --backup
    python -m backup --restore
    python -m backup --restore --backup-id <id>
    python -m backup --list
    python -m backup --prune
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import os
import shutil
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _file_sha256(path: Path) -> str:
    """Compute the SHA-256 checksum of a file."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except (FileNotFoundError, PermissionError):
        return ""
    return h.hexdigest()


def _dir_size_bytes(path: Path) -> int:
    """Recursively compute total size of a directory in bytes."""
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    except Exception:
        pass
    return total


class BackupManager:
    """
    Manages automated backups of all Tinker durable data stores.

    Parameters
    ----------
    backup_dir     : Directory where backup snapshots are stored.
    duckdb_path    : Path to the DuckDB database file.
    sqlite_path    : Path to the SQLite task registry file.
    chroma_path    : Path to the ChromaDB data directory.
    retention_days : Days to keep backups (older ones are pruned). Default: 7.
    compress       : If True (default), compress ChromaDB backup with gzip.
    """

    def __init__(
        self,
        backup_dir: str = "./tinker_backups",
        duckdb_path: str = "./tinker_session.duckdb",
        sqlite_path: str = "./tinker_tasks.sqlite",
        chroma_path: str = "./chroma_db",
        retention_days: int = 7,
        compress: bool = True,
    ) -> None:
        self._backup_dir = Path(backup_dir)
        self._duckdb_path = Path(duckdb_path)
        self._sqlite_path = Path(sqlite_path)
        self._chroma_path = Path(chroma_path)
        self._retention_days = retention_days
        self._compress = compress

        # Ensure backup directory exists
        self._backup_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Backup
    # ------------------------------------------------------------------

    async def backup(self) -> str:
        """
        Create a full backup snapshot of all durable data stores.

        Returns the backup ID (a timestamp string) that can be used to
        restore this specific backup later.

        The backup is written to ``{backup_dir}/{backup_id}/``.

        Returns
        -------
        str : The backup ID.

        Raises
        ------
        RuntimeError : If the backup fails critically (disk full, permission denied).
        """
        backup_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        snapshot_dir = self._backup_dir / backup_id
        snapshot_dir.mkdir(parents=True)

        logger.info("Starting backup snapshot '%s' → %s", backup_id, snapshot_dir)
        t0 = time.monotonic()

        manifest = {
            "id": backup_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "files": {},
            "errors": [],
        }

        # Run all backup steps (errors are non-fatal per step)
        await asyncio.gather(
            self._backup_file(self._duckdb_path, snapshot_dir, "duckdb", manifest),
            self._backup_file(self._sqlite_path, snapshot_dir, "sqlite", manifest),
            self._backup_directory(self._chroma_path, snapshot_dir, "chroma", manifest),
        )

        elapsed = time.monotonic() - t0
        manifest["duration_seconds"] = round(elapsed, 2)
        manifest["total_size_bytes"] = sum(
            f.get("size_bytes", 0) for f in manifest["files"].values()
        )

        # Write the manifest
        manifest_path = snapshot_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))

        if manifest["errors"]:
            logger.warning(
                "Backup '%s' completed with %d errors in %.2fs",
                backup_id, len(manifest["errors"]), elapsed,
            )
        else:
            logger.info(
                "Backup '%s' completed successfully in %.2fs (%d bytes)",
                backup_id, elapsed, manifest["total_size_bytes"],
            )

        return backup_id

    async def _backup_file(
        self, src: Path, dest_dir: Path, label: str, manifest: dict
    ) -> None:
        """Copy a single file to the backup directory."""
        if not src.exists():
            logger.debug("Backup: %s not found at %s — skipping", label, src)
            manifest["files"][label] = {"status": "skipped", "reason": "file not found"}
            return

        dest = dest_dir / src.name
        try:
            shutil.copy2(src, dest)
            size = dest.stat().st_size
            checksum = _file_sha256(dest)
            manifest["files"][label] = {
                "status": "ok",
                "source": str(src),
                "dest": str(dest),
                "size_bytes": size,
                "sha256": checksum,
            }
            logger.debug("Backed up %s (%d bytes)", label, size)
        except Exception as exc:
            msg = f"Failed to backup {label}: {exc}"
            logger.warning(msg)
            manifest["errors"].append(msg)
            manifest["files"][label] = {"status": "error", "error": str(exc)}

    async def _backup_directory(
        self, src: Path, dest_dir: Path, label: str, manifest: dict
    ) -> None:
        """Archive a directory into a tar.gz and place in backup directory."""
        if not src.exists():
            logger.debug("Backup: %s directory not found at %s — skipping", label, src)
            manifest["files"][label] = {"status": "skipped", "reason": "directory not found"}
            return

        archive_name = f"{label}.tar.gz" if self._compress else f"{label}.tar"
        dest = dest_dir / archive_name
        try:
            # Run in executor to avoid blocking the event loop on large directories
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, self._create_archive, src, dest
            )
            size = dest.stat().st_size
            manifest["files"][label] = {
                "status": "ok",
                "source": str(src),
                "dest": str(dest),
                "size_bytes": size,
                "compressed": self._compress,
            }
            logger.debug("Backed up directory %s → %s (%d bytes)", label, archive_name, size)
        except Exception as exc:
            msg = f"Failed to archive {label}: {exc}"
            logger.warning(msg)
            manifest["errors"].append(msg)
            manifest["files"][label] = {"status": "error", "error": str(exc)}

    def _create_archive(self, src: Path, dest: Path) -> None:
        """Synchronous archive creation (runs in executor)."""
        mode = "w:gz" if self._compress else "w"
        with tarfile.open(dest, mode) as tar:
            tar.add(src, arcname=src.name)

    # ------------------------------------------------------------------
    # Restore
    # ------------------------------------------------------------------

    async def restore(self, backup_id: str) -> bool:
        """
        Restore from a specific backup snapshot.

        WARNING: This OVERWRITES the current data files.  Stop Tinker before
        restoring to avoid file corruption.

        Parameters
        ----------
        backup_id : The backup ID string (from ``backup()`` or ``list_backups()``).

        Returns
        -------
        True if restore succeeded, False on error.
        """
        snapshot_dir = self._backup_dir / backup_id
        manifest_path = snapshot_dir / "manifest.json"

        if not snapshot_dir.exists():
            logger.error("Restore failed: backup '%s' not found", backup_id)
            return False

        if not manifest_path.exists():
            logger.error("Restore failed: manifest missing in backup '%s'", backup_id)
            return False

        manifest = json.loads(manifest_path.read_text())
        logger.info("Restoring from backup '%s' (created: %s)", backup_id, manifest.get("created_at"))

        errors = []

        # Restore DuckDB
        duckdb_info = manifest.get("files", {}).get("duckdb", {})
        if duckdb_info.get("status") == "ok":
            src = snapshot_dir / Path(duckdb_info["dest"]).name
            try:
                shutil.copy2(src, self._duckdb_path)
                logger.info("Restored DuckDB from backup")
            except Exception as exc:
                errors.append(f"DuckDB restore: {exc}")

        # Restore SQLite
        sqlite_info = manifest.get("files", {}).get("sqlite", {})
        if sqlite_info.get("status") == "ok":
            src = snapshot_dir / Path(sqlite_info["dest"]).name
            try:
                shutil.copy2(src, self._sqlite_path)
                logger.info("Restored SQLite from backup")
            except Exception as exc:
                errors.append(f"SQLite restore: {exc}")

        # Restore ChromaDB
        chroma_info = manifest.get("files", {}).get("chroma", {})
        if chroma_info.get("status") == "ok":
            archive = snapshot_dir / Path(chroma_info["dest"]).name
            try:
                if self._chroma_path.exists():
                    shutil.rmtree(self._chroma_path)
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None, self._extract_archive, archive, self._chroma_path.parent
                )
                logger.info("Restored ChromaDB from backup")
            except Exception as exc:
                errors.append(f"ChromaDB restore: {exc}")

        if errors:
            logger.error("Restore completed with %d errors: %s", len(errors), errors)
            return False

        logger.info("Restore from backup '%s' completed successfully", backup_id)
        return True

    def _extract_archive(self, archive: Path, dest_dir: Path) -> None:
        """Synchronous archive extraction (runs in executor)."""
        with tarfile.open(archive, "r:*") as tar:
            tar.extractall(dest_dir)

    async def restore_latest(self) -> bool:
        """
        Restore from the most recent available backup.

        Returns True if restore succeeded, False if no backups exist or on error.
        """
        backups = await self.list_backups()
        if not backups:
            logger.error("Restore failed: no backups available")
            return False
        latest = backups[0]  # list_backups returns newest first
        logger.info("Restoring latest backup: %s", latest["id"])
        return await self.restore(latest["id"])

    # ------------------------------------------------------------------
    # Inventory
    # ------------------------------------------------------------------

    async def list_backups(self) -> list[dict]:
        """
        List all available backups, newest first.

        Returns
        -------
        list[dict] : Each dict has: id, created_at, total_size_mb, files, errors.
        """
        backups = []
        for item in sorted(self._backup_dir.iterdir(), reverse=True):
            if not item.is_dir():
                continue
            manifest_path = item / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text())
                backups.append({
                    "id": manifest.get("id", item.name),
                    "created_at": manifest.get("created_at", ""),
                    "total_size_bytes": manifest.get("total_size_bytes", 0),
                    "total_size_mb": round(manifest.get("total_size_bytes", 0) / (1024 * 1024), 2),
                    "duration_seconds": manifest.get("duration_seconds", 0),
                    "errors": manifest.get("errors", []),
                    "files": list(manifest.get("files", {}).keys()),
                })
            except Exception:
                continue
        return backups

    async def prune_old_backups(self) -> int:
        """
        Delete backups older than ``retention_days``.

        Returns the number of backups deleted.
        """
        import datetime as dt
        cutoff = datetime.now(timezone.utc) - dt.timedelta(days=self._retention_days)
        deleted = 0

        for item in self._backup_dir.iterdir():
            if not item.is_dir():
                continue
            manifest_path = item / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text())
                created_str = manifest.get("created_at", "")
                created_at = datetime.fromisoformat(created_str)
                if created_at < cutoff:
                    shutil.rmtree(item)
                    deleted += 1
                    logger.info("Pruned old backup: %s", item.name)
            except Exception as exc:
                logger.debug("Could not prune %s: %s", item.name, exc)

        if deleted:
            logger.info("Pruned %d backups older than %d days", deleted, self._retention_days)
        return deleted
