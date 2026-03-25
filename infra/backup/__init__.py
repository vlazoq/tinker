"""
backup/ — Automated backup and restore for Tinker data stores.

Provides scheduled and on-demand backup of all durable storage:

  backup_manager   — Orchestrates backups of DuckDB, ChromaDB, and SQLite.
                     Supports local filesystem and optional cloud destinations.

Use:
    python -m backup --backup    # trigger a manual backup
    python -m backup --restore   # restore from latest backup
    python -m backup --list      # list available backups
"""
