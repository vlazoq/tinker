"""
infra/resilience/dead_letter_queue.py
================================

Dead Letter Queue (DLQ) for Tinker — captures permanently failed operations.

What is a dead letter queue?
-----------------------------
A DLQ is a holding area for operations that have failed permanently and cannot
be retried automatically.  Instead of silently dropping failures (data loss),
Tinker writes them to the DLQ with full context — what failed, why, and when.

Operators can then:
  - Inspect the DLQ to understand what went wrong
  - Replay items once the root cause is fixed
  - Discard items that are no longer relevant

Why Tinker needs this
---------------------
The micro loop silently swallows many non-fatal errors (see micro_loop.py).
For example, if ``task_engine.complete_task()`` fails, the loop logs a warning
and continues.  Without a DLQ, there's no record that the completion was lost.
With a DLQ, the failed operation is preserved for later investigation or replay.

Storage
-------
The DLQ is persisted to a SQLite database (default: ``tinker_dlq.sqlite``).
SQLite is chosen because:
  - It's always available (no external service needed)
  - It's durable (survives process restarts)
  - It's queryable for debugging (standard SQL)
  - It doesn't require Redis, which might be the reason for the failure

Usage
------
::

    dlq = DeadLetterQueue("tinker_dlq.sqlite")
    await dlq.connect()

    # On failure:
    await dlq.enqueue(
        operation="complete_task",
        payload={"task_id": task_id, "artifact_id": artifact_id},
        error=str(exc),
        context={"micro_loop_iteration": 42, "subsystem": "api_gateway"},
    )

    # To replay:
    pending = await dlq.pending_items(limit=10)
    for item in pending:
        try:
            await replay_operation(item)
            await dlq.mark_resolved(item["id"])
        except Exception as e:
            await dlq.mark_failed(item["id"], str(e))

    # Stats:
    stats = await dlq.stats()
    print(stats)  # {'total': 5, 'pending': 3, 'resolved': 1, 'discarded': 1}
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from .migrations import SQLiteMigrationRunner

logger = logging.getLogger(__name__)

# Baseline migration — establishes the schema_migrations table for this DB.
# Future schema changes should be added as version 2, 3, etc.
DLQ_MIGRATIONS: list[tuple[int, str]] = [
    (1, "-- baseline"),
]


class DeadLetterQueue:
    """
    SQLite-backed dead letter queue for failed Tinker operations.

    All writes are serialised through an asyncio lock to prevent concurrent
    SQLite writes from causing database errors.

    Parameters
    ----------
    db_path : Path to the SQLite database file (default: ``tinker_dlq.sqlite``).
    """

    def __init__(self, db_path: str = "tinker_dlq.sqlite") -> None:
        self._db_path = db_path
        self._conn = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """
        Open the SQLite connection and create the DLQ table if it doesn't exist.

        Call this once at startup before any enqueue/dequeue operations.
        """
        try:
            import aiosqlite  # type: ignore

            self._conn = await aiosqlite.connect(self._db_path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA journal_mode=WAL")  # concurrent reads
            await self._conn.execute("""
                CREATE TABLE IF NOT EXISTS dlq_items (
                    id           TEXT PRIMARY KEY,
                    operation    TEXT NOT NULL,
                    payload      TEXT NOT NULL,   -- JSON
                    error        TEXT NOT NULL,
                    context      TEXT,            -- JSON optional
                    status       TEXT NOT NULL DEFAULT 'pending',
                    created_at   TEXT NOT NULL,
                    updated_at   TEXT NOT NULL,
                    retry_count  INTEGER NOT NULL DEFAULT 0,
                    resolved_at  TEXT,
                    notes        TEXT
                )
            """)
            await self._conn.execute("""
                CREATE INDEX IF NOT EXISTS dlq_status_idx ON dlq_items (status, created_at)
            """)
            await self._conn.commit()
            SQLiteMigrationRunner(self._db_path).migrate(DLQ_MIGRATIONS)
            logger.info("DeadLetterQueue connected to %s", self._db_path)
        except ImportError:
            logger.warning(
                "aiosqlite not available — DeadLetterQueue operating in memory-only mode"
            )
        except Exception as exc:
            logger.warning("DeadLetterQueue failed to connect: %s — DLQ is disabled", exc)

    async def close(self) -> None:
        """Close the SQLite connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    async def enqueue(
        self,
        operation: str,
        payload: dict,
        error: str,
        context: dict | None = None,
    ) -> str | None:
        """
        Add a failed operation to the dead letter queue.

        Parameters
        ----------
        operation : Human-readable name of the failed operation
                    (e.g. "complete_task", "store_artifact", "meso_synthesis").
        payload   : All data needed to replay the operation (task ID, artifact ID, etc.).
        error     : The exception message or error description.
        context   : Optional additional context (loop iteration, subsystem, etc.).

        Returns
        -------
        str : The item ID (UUID) that can be used to retrieve or update the item.
        None : If the DLQ is disabled (aiosqlite unavailable).
        """
        if not self._conn:
            logger.debug("DLQ disabled — dropping failed operation: %s (%s)", operation, error)
            return None

        item_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()

        async with self._lock:
            try:
                await self._conn.execute(
                    """
                    INSERT INTO dlq_items
                        (id, operation, payload, error, context, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                    (
                        item_id,
                        operation,
                        json.dumps(payload),
                        error,
                        json.dumps(context) if context else None,
                        now,
                        now,
                    ),
                )
                await self._conn.commit()
                logger.warning(
                    "DLQ: Enqueued failed operation '%s' (id=%s): %s",
                    operation,
                    item_id[:8],
                    error[:100],
                )
                return item_id
            except Exception as exc:
                logger.error("DLQ.enqueue failed: %s", exc)
                return None

    async def mark_resolved(self, item_id: str, notes: str = "") -> bool:
        """
        Mark a DLQ item as successfully resolved (replayed or manually handled).

        Parameters
        ----------
        item_id : The UUID returned by ``enqueue()``.
        notes   : Optional explanation of how it was resolved.

        Returns
        -------
        True if the item was found and updated, False otherwise.
        """
        if not self._conn:
            return False

        now = datetime.now(UTC).isoformat()
        async with self._lock:
            try:
                cursor = await self._conn.execute(
                    """
                    UPDATE dlq_items
                    SET status='resolved', resolved_at=?, updated_at=?, notes=?
                    WHERE id=? AND status='pending'
                """,
                    (now, now, notes, item_id),
                )
                await self._conn.commit()
                return cursor.rowcount > 0
            except Exception as exc:
                logger.error("DLQ.mark_resolved failed: %s", exc)
                return False

    async def mark_discarded(self, item_id: str, reason: str = "") -> bool:
        """
        Mark a DLQ item as permanently discarded (cannot or should not be replayed).

        Parameters
        ----------
        item_id : The UUID returned by ``enqueue()``.
        reason  : Why the item is being discarded.

        Returns
        -------
        True if the item was updated, False otherwise.
        """
        if not self._conn:
            return False

        now = datetime.now(UTC).isoformat()
        async with self._lock:
            try:
                cursor = await self._conn.execute(
                    """
                    UPDATE dlq_items
                    SET status='discarded', updated_at=?, notes=?
                    WHERE id=?
                """,
                    (now, reason, item_id),
                )
                await self._conn.commit()
                return cursor.rowcount > 0
            except Exception as exc:
                logger.error("DLQ.mark_discarded failed: %s", exc)
                return False

    async def increment_retry(self, item_id: str) -> bool:
        """Increment the retry_count for a pending item."""
        if not self._conn:
            return False

        now = datetime.now(UTC).isoformat()
        async with self._lock:
            try:
                cursor = await self._conn.execute(
                    """
                    UPDATE dlq_items
                    SET retry_count = retry_count + 1, updated_at=?
                    WHERE id=?
                """,
                    (now, item_id),
                )
                await self._conn.commit()
                return cursor.rowcount > 0
            except Exception as exc:
                logger.error("DLQ.increment_retry failed: %s", exc)
                return False

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    async def pending_items(self, limit: int = 50) -> list[dict]:
        """
        Return pending DLQ items ordered by creation time (oldest first).

        Parameters
        ----------
        limit : Maximum number of items to return.

        Returns
        -------
        list[dict] : Each dict has keys: id, operation, payload, error, context,
                     created_at, retry_count.
        """
        if not self._conn:
            return []

        try:
            cursor = await self._conn.execute(
                """
                SELECT id, operation, payload, error, context, created_at, retry_count
                FROM dlq_items
                WHERE status = 'pending'
                ORDER BY created_at ASC
                LIMIT ?
            """,
                (limit,),
            )
            rows = await cursor.fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["payload"] = json.loads(item["payload"])
                if item.get("context"):
                    item["context"] = json.loads(item["context"])
                result.append(item)
            return result
        except Exception as exc:
            logger.error("DLQ.pending_items failed: %s", exc)
            return []

    async def get_item(self, item_id: str) -> dict | None:
        """Retrieve a single DLQ item by ID."""
        if not self._conn:
            return None
        try:
            cursor = await self._conn.execute("SELECT * FROM dlq_items WHERE id=?", (item_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            if item.get("context"):
                item["context"] = json.loads(item["context"])
            return item
        except Exception as exc:
            logger.error("DLQ.get_item failed: %s", exc)
            return None

    async def stats(self) -> dict:
        """
        Return aggregate statistics about the DLQ.

        Returns
        -------
        dict with keys: total, pending, resolved, discarded.
        """
        if not self._conn:
            return {
                "total": 0,
                "pending": 0,
                "resolved": 0,
                "discarded": 0,
                "disabled": True,
            }

        try:
            cursor = await self._conn.execute("""
                SELECT status, COUNT(*) as cnt
                FROM dlq_items
                GROUP BY status
            """)
            rows = await cursor.fetchall()
            counts = {row["status"]: row["cnt"] for row in rows}
            total = sum(counts.values())
            return {
                "total": total,
                "pending": counts.get("pending", 0),
                "resolved": counts.get("resolved", 0),
                "discarded": counts.get("discarded", 0),
            }
        except Exception as exc:
            logger.error("DLQ.stats failed: %s", exc)
            return {
                "total": 0,
                "pending": 0,
                "resolved": 0,
                "discarded": 0,
                "error": str(exc),
            }

    async def purge_resolved(self, older_than_days: int = 7) -> int:
        """
        Delete resolved/discarded items older than ``older_than_days`` days.

        Keeps the DLQ from growing unbounded.  Returns the number of rows deleted.
        """
        if not self._conn:
            return 0
        # Simple approach: delete status != pending AND created_at < N days ago
        # We use Python's time instead of SQLite date functions for portability.
        import datetime as dt

        cutoff_dt = (datetime.now(dt.UTC) - dt.timedelta(days=older_than_days)).isoformat()
        async with self._lock:
            try:
                cursor = await self._conn.execute(
                    """
                    DELETE FROM dlq_items
                    WHERE status IN ('resolved', 'discarded')
                    AND created_at < ?
                """,
                    (cutoff_dt,),
                )
                await self._conn.commit()
                count = cursor.rowcount
                if count > 0:
                    logger.info("DLQ purged %d old resolved/discarded items", count)
                return count
            except Exception as exc:
                logger.error("DLQ.purge_resolved failed: %s", exc)
                return 0


# ---------------------------------------------------------------------------
# Auto-replay scheduler
# ---------------------------------------------------------------------------


class DLQAutoReplayer:
    """
    Background asyncio task that periodically retries pending DLQ items.

    Usage::

        async def my_handler(item: dict) -> None:
            await replay_operation(item["operation"], item["payload"])

        replayer = DLQAutoReplayer(dlq, handler=my_handler, interval=60.0)
        await replayer.start()
        # … later …
        await replayer.stop()

    The handler receives the raw DLQ item dict (same shape as ``pending_items()``
    returns).  If the handler raises, the item's ``retry_count`` is incremented
    and it stays pending.  If the handler succeeds, the item is marked resolved.

    Parameters
    ----------
    dlq          : The DeadLetterQueue to drain.
    handler      : Async callable ``(item: dict) -> None`` that replays one item.
    interval     : Seconds between replay passes.  Default 60.
    batch_size   : Max items to attempt per pass.  Default 10.
    max_retries  : Items that have been retried this many times are discarded
                   rather than retried again.  Default 5.
    """

    def __init__(
        self,
        dlq: DeadLetterQueue,
        handler: Callable[[dict], Awaitable[None]],
        interval: float = 60.0,
        batch_size: int = 10,
        max_retries: int = 5,
    ) -> None:
        self._dlq = dlq
        self._handler = handler
        self._interval = interval
        self._batch_size = batch_size
        self._max_retries = max_retries
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the background replay loop."""
        if self._task is not None and not self._task.done():
            logger.warning("DLQAutoReplayer is already running")
            return
        self._task = asyncio.create_task(self._run(), name="dlq-auto-replayer")
        logger.info(
            "DLQAutoReplayer started (interval=%.0fs, batch=%d, max_retries=%d)",
            self._interval,
            self._batch_size,
            self._max_retries,
        )

    async def stop(self) -> None:
        """Cancel the background loop and wait for it to finish."""
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        logger.info("DLQAutoReplayer stopped")

    async def _run(self) -> None:
        """Main replay loop — runs until cancelled."""
        while True:
            try:
                await self._replay_pass()
            except Exception as exc:
                logger.error("DLQAutoReplayer._replay_pass error: %s", exc)
            await asyncio.sleep(self._interval)

    async def _replay_pass(self) -> None:
        """Attempt to replay one batch of pending DLQ items."""
        items = await self._dlq.pending_items(limit=self._batch_size)
        if not items:
            return

        resolved = failed = discarded = 0
        for item in items:
            item_id: str = item["id"]

            # Discard items that have exhausted their retry budget
            if item.get("retry_count", 0) >= self._max_retries:
                await self._dlq.mark_discarded(
                    item_id,
                    reason=f"Exceeded max_retries={self._max_retries}",
                )
                discarded += 1
                logger.warning(
                    "DLQ item %s discarded after %d retries",
                    item_id[:8],
                    item["retry_count"],
                )
                continue

            # Attempt replay
            try:
                await self._handler(item)
                await self._dlq.mark_resolved(item_id, notes="auto-replayed")
                resolved += 1
            except Exception as exc:
                await self._dlq.increment_retry(item_id)
                failed += 1
                logger.warning(
                    "DLQ replay failed for item %s (retry %d): %s",
                    item_id[:8],
                    item.get("retry_count", 0) + 1,
                    exc,
                )

        if resolved or failed or discarded:
            logger.info(
                "DLQAutoReplayer pass: resolved=%d failed=%d discarded=%d",
                resolved,
                failed,
                discarded,
            )
