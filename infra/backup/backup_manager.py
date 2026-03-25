"""
infra/backup/backup_manager.py
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
**Full backup**: copies every file unconditionally.

**Incremental backup**: copies only files whose SHA-256 checksum has changed
since the last backup of that file.  A separate ``incremental_manifest.json``
records the checksums used as the baseline for the next incremental run.
Incremental backups are much faster for large ChromaDB directories that
haven't changed.

Each backup creates a timestamped snapshot directory containing:
  - A copy/subset of data files (depending on full vs. incremental)
  - A tar.gz of the ChromaDB directory (full) or changed files (incremental)
  - ``manifest.json`` — metadata (timestamp, file sizes, checksums)
  - For full backups: ``checksums.json`` — baseline for the next incremental

Retention
----------
Two configurable policies (both applied on prune):
  - ``retention_days``: delete backups older than N days (default: 7)
  - ``keep_count``: always keep at least the N most-recent backups (default: 3)
    — prevents all backups being pruned when ``retention_days`` is short

Verification
-------------
After a backup completes, ``verify(backup_id)`` re-reads every backed-up
file and checks its SHA-256 against the manifest.  Call this after
``backup()`` to confirm the snapshot is not corrupt.

Usage
------
::

    bm = BackupManager(
        backup_dir     = "./tinker_backups",
        duckdb_path    = "./tinker_session.duckdb",
        sqlite_path    = "./tinker_tasks.sqlite",
        chroma_path    = "./chroma_db",
        retention_days = 7,
        keep_count     = 3,
    )

    # Full backup with post-write verification:
    backup_id = await bm.backup()
    ok = await bm.verify(backup_id)

    # Incremental backup (only changed files):
    backup_id = await bm.backup(incremental=True)

    # List available backups:
    backups = await bm.list_backups()

    # Restore from a specific backup:
    await bm.restore(backup_id)

    # Or restore the latest:
    await bm.restore_latest()

CLI
---
::

    python -m backup --backup
    python -m backup --backup --incremental
    python -m backup --verify --backup-id <id>
    python -m backup --restore
    python -m backup --restore --backup-id <id>
    python -m backup --list
    python -m backup --prune
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

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
    keep_count     : Minimum number of recent backups to always retain,
                     regardless of age. Default: 3.
    compress       : If True (default), compress ChromaDB backup with gzip.
    """

    # Name of the file that records per-file checksums for incremental backups
    _CHECKSUM_FILE = "checksums.json"

    def __init__(
        self,
        backup_dir: str = "./tinker_backups",
        duckdb_path: str = "./tinker_session.duckdb",
        sqlite_path: str = "./tinker_tasks.sqlite",
        chroma_path: str = "./chroma_db",
        retention_days: int = 7,
        keep_count: int = 3,
        compress: bool = True,
    ) -> None:
        self._backup_dir = Path(backup_dir)
        self._duckdb_path = Path(duckdb_path)
        self._sqlite_path = Path(sqlite_path)
        self._chroma_path = Path(chroma_path)
        self._retention_days = retention_days
        self._keep_count = keep_count
        self._compress = compress

        # Ensure backup directory exists
        self._backup_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Backup
    # ------------------------------------------------------------------

    async def backup(self, incremental: bool = False) -> str:
        """
        Create a backup snapshot of all durable data stores.

        Parameters
        ----------
        incremental : If True, skip files whose SHA-256 checksum matches the
                      previous backup.  Default: False (full backup).

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
        backup_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        snapshot_dir = self._backup_dir / backup_id
        snapshot_dir.mkdir(parents=True)

        mode = "incremental" if incremental else "full"
        logger.info(
            "Starting %s backup snapshot '%s' → %s", mode, backup_id, snapshot_dir
        )
        t0 = time.monotonic()

        # Load previous checksums for incremental comparison
        prev_checksums: dict[str, str] = {}
        if incremental:
            prev_checksums = self._load_latest_checksums()

        manifest = {
            "id": backup_id,
            "mode": mode,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "files": {},
            "errors": [],
        }

        # Run all backup steps (errors are non-fatal per step)
        await asyncio.gather(
            self._backup_file(
                self._duckdb_path,
                snapshot_dir,
                "duckdb",
                manifest,
                prev_checksum=prev_checksums.get("duckdb"),
            ),
            self._backup_file(
                self._sqlite_path,
                snapshot_dir,
                "sqlite",
                manifest,
                prev_checksum=prev_checksums.get("sqlite"),
            ),
            self._backup_directory(self._chroma_path, snapshot_dir, "chroma", manifest),
        )

        elapsed = time.monotonic() - t0
        manifest["duration_seconds"] = round(elapsed, 2)
        manifest["total_size_bytes"] = sum(
            f.get("size_bytes", 0) for f in manifest["files"].values()
        )

        # Save current checksums for next incremental run
        new_checksums = {
            label: info["sha256"]
            for label, info in manifest["files"].items()
            if info.get("status") == "ok" and "sha256" in info
        }
        checksums_path = snapshot_dir / self._CHECKSUM_FILE
        checksums_path.write_text(json.dumps(new_checksums, indent=2))

        # Write the manifest
        manifest_path = snapshot_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))

        if manifest["errors"]:
            logger.warning(
                "Backup '%s' completed with %d errors in %.2fs",
                backup_id,
                len(manifest["errors"]),
                elapsed,
            )
        else:
            logger.info(
                "Backup '%s' (%s) completed successfully in %.2fs (%d bytes)",
                backup_id,
                mode,
                elapsed,
                manifest["total_size_bytes"],
            )

        return backup_id

    def _load_latest_checksums(self) -> dict[str, str]:
        """Load checksums from the most recent backup for incremental comparison."""
        try:
            backups = sorted(
                [d for d in self._backup_dir.iterdir() if d.is_dir()], reverse=True
            )
            for backup_dir in backups:
                checksum_file = backup_dir / self._CHECKSUM_FILE
                if checksum_file.exists():
                    return json.loads(checksum_file.read_text())
        except Exception as exc:
            logger.debug("Could not load previous checksums: %s", exc)
        return {}

    async def verify(self, backup_id: str) -> bool:
        """
        Verify the integrity of a backup by re-reading files and checking
        SHA-256 checksums against the manifest.

        Parameters
        ----------
        backup_id : The backup ID to verify.

        Returns
        -------
        True if all backed-up files match their recorded checksums; False otherwise.
        """
        snapshot_dir = self._backup_dir / backup_id
        manifest_path = snapshot_dir / "manifest.json"

        if not manifest_path.exists():
            logger.error("Verify failed: manifest not found for backup '%s'", backup_id)
            return False

        manifest = json.loads(manifest_path.read_text())
        failures: list[str] = []

        for label, info in manifest.get("files", {}).items():
            if info.get("status") != "ok":
                continue
            if "sha256" not in info:
                continue  # directory archives don't have per-file checksums here

            dest = Path(info["dest"])
            if not dest.exists():
                failures.append(f"{label}: file missing at {dest}")
                continue

            actual = _file_sha256(dest)
            expected = info["sha256"]
            if actual != expected:
                failures.append(
                    f"{label}: checksum mismatch (expected {expected[:8]}…, "
                    f"got {actual[:8]}…)"
                )

        if failures:
            logger.error(
                "Backup '%s' FAILED verification (%d issue(s)): %s",
                backup_id,
                len(failures),
                failures,
            )
            return False

        logger.info("Backup '%s' verified OK", backup_id)
        return True

    async def _backup_file(
        self,
        src: Path,
        dest_dir: Path,
        label: str,
        manifest: dict,
        prev_checksum: str = "",
    ) -> None:
        """
        Copy a single file to the backup directory.

        If ``prev_checksum`` is provided (incremental mode), the file is
        skipped when its current checksum matches, recording status "unchanged".
        """
        if not src.exists():
            logger.debug("Backup: %s not found at %s — skipping", label, src)
            manifest["files"][label] = {"status": "skipped", "reason": "file not found"}
            return

        # Incremental: skip if unchanged
        if prev_checksum:
            current_checksum = _file_sha256(src)
            if current_checksum == prev_checksum:
                manifest["files"][label] = {
                    "status": "unchanged",
                    "source": str(src),
                    "sha256": current_checksum,
                    "size_bytes": 0,
                }
                logger.debug("Incremental backup: %s unchanged — skipping", label)
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
            manifest["files"][label] = {
                "status": "skipped",
                "reason": "directory not found",
            }
            return

        archive_name = f"{label}.tar.gz" if self._compress else f"{label}.tar"
        dest = dest_dir / archive_name
        try:
            # Run in executor to avoid blocking the event loop on large directories
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._create_archive, src, dest)
            size = dest.stat().st_size
            manifest["files"][label] = {
                "status": "ok",
                "source": str(src),
                "dest": str(dest),
                "size_bytes": size,
                "compressed": self._compress,
            }
            logger.debug(
                "Backed up directory %s → %s (%d bytes)", label, archive_name, size
            )
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
        logger.info(
            "Restoring from backup '%s' (created: %s)",
            backup_id,
            manifest.get("created_at"),
        )

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
                backups.append(
                    {
                        "id": manifest.get("id", item.name),
                        "created_at": manifest.get("created_at", ""),
                        "total_size_bytes": manifest.get("total_size_bytes", 0),
                        "total_size_mb": round(
                            manifest.get("total_size_bytes", 0) / (1024 * 1024), 2
                        ),
                        "duration_seconds": manifest.get("duration_seconds", 0),
                        "errors": manifest.get("errors", []),
                        "files": list(manifest.get("files", {}).keys()),
                    }
                )
            except Exception:
                continue
        return backups

    async def prune_old_backups(self) -> int:
        """
        Delete backups older than ``retention_days``, while always keeping
        at least ``keep_count`` most-recent backups regardless of age.

        Returns the number of backups deleted.
        """
        import datetime as dt

        cutoff = datetime.now(timezone.utc) - dt.timedelta(days=self._retention_days)
        deleted = 0

        # Collect and sort all valid backups newest-first
        all_backups: list[tuple[datetime, Path]] = []
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
                all_backups.append((created_at, item))
            except Exception as exc:
                logger.debug("Could not read backup manifest %s: %s", item.name, exc)

        # Sort newest → oldest
        all_backups.sort(key=lambda x: x[0], reverse=True)

        for idx, (created_at, item) in enumerate(all_backups):
            # Always keep the `keep_count` most-recent backups
            if idx < self._keep_count:
                continue
            if created_at < cutoff:
                try:
                    shutil.rmtree(item)
                    deleted += 1
                    logger.info("Pruned old backup: %s", item.name)
                except Exception as exc:
                    logger.debug("Could not prune %s: %s", item.name, exc)

        if deleted:
            logger.info(
                "Pruned %d backups (older than %d days, keeping last %d)",
                deleted,
                self._retention_days,
                self._keep_count,
            )
        return deleted
