"""
backup/__main__.py — CLI for Tinker backup operations.

Usage:
    python -m backup --backup
    python -m backup --restore
    python -m backup --restore --backup-id 20240101_120000
    python -m backup --list
    python -m backup --prune
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


async def _main() -> None:
    # Load .env if available
    env_file = ROOT / ".env"
    if env_file.exists():
        try:
            from dotenv import load_dotenv

            load_dotenv(env_file)
        except ImportError:
            pass

    parser = argparse.ArgumentParser(description="Tinker backup manager")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--backup", action="store_true", help="Create a new backup")
    group.add_argument(
        "--restore",
        action="store_true",
        help="Restore from backup (latest or --backup-id)",
    )
    group.add_argument("--list", action="store_true", help="List all available backups")
    group.add_argument(
        "--prune", action="store_true", help="Prune backups older than retention_days"
    )
    parser.add_argument(
        "--backup-id", default=None, help="Specific backup ID to restore"
    )
    args = parser.parse_args()

    from backup.backup_manager import BackupManager

    bm = BackupManager(
        backup_dir=os.getenv("TINKER_BACKUP_DIR", "./tinker_backups"),
        duckdb_path=os.getenv("TINKER_DUCKDB_PATH", "./tinker_session.duckdb"),
        sqlite_path=os.getenv("TINKER_SQLITE_PATH", "./tinker_tasks.sqlite"),
        chroma_path=os.getenv("TINKER_CHROMA_PATH", "./chroma_db"),
        retention_days=int(os.getenv("TINKER_BACKUP_RETENTION_DAYS", "7")),
    )

    if args.backup:
        backup_id = await bm.backup()
        print(f"✓ Backup created: {backup_id}")

    elif args.restore:
        if args.backup_id:
            ok = await bm.restore(args.backup_id)
        else:
            ok = await bm.restore_latest()
        if ok:
            print("✓ Restore completed successfully")
        else:
            print("✗ Restore failed — check logs for details")
            sys.exit(1)

    elif args.list:
        backups = await bm.list_backups()
        if not backups:
            print("No backups found.")
        else:
            print(f"{'ID':<20} {'Created':<25} {'Size MB':>8} {'Errors':>7}")
            print("-" * 65)
            for b in backups:
                err = len(b["errors"])
                print(
                    f"{b['id']:<20} {b['created_at'][:19]:<25} {b['total_size_mb']:>8.2f} {err:>7}"
                )

    elif args.prune:
        n = await bm.prune_old_backups()
        print(f"✓ Pruned {n} old backup(s)")


if __name__ == "__main__":
    asyncio.run(_main())
