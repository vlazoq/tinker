"""
infra/observability/audit_log.py
===========================

Immutable append-only audit log for Tinker.

Why an audit log?
------------------
An audit log records every significant event in the system with WHO did WHAT
WHEN.  It's:
  - Immutable: events cannot be modified or deleted (append-only)
  - Structured: every event has the same fields for easy querying
  - Durable: persisted to disk, survives process restarts
  - Queryable: stored in SQLite for SQL queries

Without an audit log:
  - You can't explain why the architecture evolved a certain way
  - You can't replay or reproduce previous results
  - You can't satisfy compliance audits ("who changed this?")
  - Forensic investigation of incidents is nearly impossible

Events logged
--------------
  TASK_SELECTED     : A task was selected from the queue
  TASK_COMPLETED    : A task was completed with artifact
  TASK_FAILED       : A task failed in the micro loop
  ARTIFACT_STORED   : An artifact was stored to memory
  MESO_SYNTHESIS    : A meso-level synthesis completed
  MACRO_SYNTHESIS   : A macro architectural snapshot committed
  STAGNATION_DETECTED: Anti-stagnation intervention triggered
  CIRCUIT_OPEN      : A circuit breaker opened
  CONFIG_CHANGED    : Configuration was changed at runtime
  BACKUP_CREATED    : A backup snapshot was created
  SYSTEM_START      : Tinker started
  SYSTEM_STOP       : Tinker stopped
  CUSTOM            : Application-defined event

Usage
------
::

    audit = AuditLog("tinker_audit.sqlite")
    await audit.connect()

    await audit.log(
        event_type = AuditEventType.TASK_COMPLETED,
        actor      = "micro_loop",
        resource   = task_id,
        outcome    = "success",
        details    = {"artifact_id": artifact_id, "critic_score": 0.85},
        trace_id   = current_trace_id(),
    )
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from infra.resilience.migrations import SQLiteMigrationRunner

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tuneable constants (extracted from magic literals for maintainability)
# ---------------------------------------------------------------------------

#: How often the background flush task drains the event buffer to SQLite.
_FLUSH_INTERVAL_SECONDS: float = 5.0

#: Trigger an immediate flush when the in-memory buffer reaches this many events.
#: Keeps memory bounded and ensures high-throughput bursts are persisted quickly.
_FLUSH_BUFFER_MAX: int = 50

# Baseline migration — establishes the schema_migrations table for this DB.
# Future schema changes should be added as version 2, 3, etc.
AUDIT_MIGRATIONS: list[tuple[int, str]] = [
    (1, (
        "CREATE INDEX IF NOT EXISTS idx_audit_event_type "
        "ON audit_events(event_type)"
    )),
    (2, (
        "CREATE INDEX IF NOT EXISTS idx_audit_trace_id "
        "ON audit_events(trace_id) WHERE trace_id IS NOT NULL"
    )),
    (3, (
        "CREATE INDEX IF NOT EXISTS idx_audit_created_at "
        "ON audit_events(created_at)"
    )),
]


class AuditEventType(Enum):
    """Types of auditable events in Tinker."""

    TASK_SELECTED = "task_selected"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    ARTIFACT_STORED = "artifact_stored"
    MESO_SYNTHESIS = "meso_synthesis"
    MACRO_SYNTHESIS = "macro_synthesis"
    STAGNATION_DETECTED = "stagnation_detected"
    CIRCUIT_OPEN = "circuit_open"
    CIRCUIT_CLOSE = "circuit_close"
    CONFIG_CHANGED = "config_changed"
    BACKUP_CREATED = "backup_created"
    SYSTEM_START = "system_start"
    SYSTEM_STOP = "system_stop"
    SLA_BREACH = "sla_breach"
    DLQ_ENQUEUED = "dlq_enqueued"
    CUSTOM = "custom"


class AuditLog:
    """
    SQLite-backed immutable audit log.

    The table uses INSERT-only operations — records are never UPDATE'd or
    DELETE'd by normal operation.  This provides an append-only guarantee
    at the application level (not enforced at the SQLite level, but by
    not exposing any delete/update methods).

    Parameters
    ----------
    db_path : Path to the SQLite file (default: "tinker_audit.sqlite").
    """

    def __init__(self, db_path: str = "tinker_audit.sqlite") -> None:
        self._db_path = db_path
        self._conn = None
        self._lock = asyncio.Lock()
        self._buffer: list[dict] = []
        self._flush_interval = _FLUSH_INTERVAL_SECONDS
        self._flush_task: Optional[asyncio.Task] = None

    async def connect(self) -> None:
        """Open the SQLite connection and create the audit table."""
        try:
            import aiosqlite

            self._conn = await aiosqlite.connect(self._db_path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_events (
                    id           TEXT PRIMARY KEY,
                    event_type   TEXT NOT NULL,
                    actor        TEXT NOT NULL,
                    resource     TEXT,
                    outcome      TEXT,
                    details      TEXT,      -- JSON
                    trace_id     TEXT,
                    session_id   TEXT,
                    created_at   TEXT NOT NULL
                )
            """)
            await self._conn.execute(
                "CREATE INDEX IF NOT EXISTS audit_type_idx ON audit_events (event_type, created_at)"
            )
            await self._conn.execute(
                "CREATE INDEX IF NOT EXISTS audit_trace_idx ON audit_events (trace_id)"
            )
            await self._conn.commit()
            SQLiteMigrationRunner(self._db_path).migrate(AUDIT_MIGRATIONS)
            logger.info("AuditLog connected to %s", self._db_path)

            # Start background flush task
            self._flush_task = asyncio.create_task(self._periodic_flush())
        except ImportError:
            logger.warning("aiosqlite not available — AuditLog disabled")
        except Exception as exc:
            logger.warning("AuditLog failed to connect: %s — audit disabled", exc)

    async def close(self) -> None:
        """Flush pending events and close the connection."""
        if self._flush_task:
            self._flush_task.cancel()
        await self._flush_buffer()
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def log(
        self,
        event_type: AuditEventType,
        actor: str,
        resource: Optional[str] = None,
        outcome: Optional[str] = None,
        details: Optional[dict] = None,
        trace_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        Record an audit event.

        Events are buffered and flushed to SQLite every few seconds for
        efficiency (avoids one DB write per micro loop step).

        Parameters
        ----------
        event_type : Type of event (from AuditEventType).
        actor      : Who performed the action (e.g. "micro_loop", "admin").
        resource   : What was acted upon (task ID, artifact ID, etc.).
        outcome    : Result of the action ("success", "failure", etc.).
        details    : Optional additional details as a dict (JSON-encoded).
        trace_id   : Trace ID from the current loop for correlation.
        session_id : The Tinker session ID.

        Returns
        -------
        str : The event ID, or None if the audit log is disabled.
        """
        if not self._conn:
            return None

        event_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        event = {
            "id": event_id,
            "event_type": event_type.value,
            "actor": actor,
            "resource": resource,
            "outcome": outcome,
            "details": json.dumps(details) if details else None,
            "trace_id": trace_id,
            "session_id": session_id,
            "created_at": now,
        }

        # Buffer the event (avoid blocking the main loop on every write)
        self._buffer.append(event)

        # Flush immediately if buffer is large
        if len(self._buffer) >= _FLUSH_BUFFER_MAX:
            await self._flush_buffer()

        return event_id

    async def query(
        self,
        event_type: Optional[AuditEventType] = None,
        actor: Optional[str] = None,
        trace_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """
        Query audit events with optional filters.

        Parameters
        ----------
        event_type : Filter by event type.
        actor      : Filter by actor.
        trace_id   : Filter by trace ID (to see all events in a trace).
        limit      : Maximum results.
        offset     : Skip first N results (for pagination).

        Returns
        -------
        list[dict] : Matching audit events, newest first.
        """
        if not self._conn:
            return []

        conditions = []
        params = []
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type.value)
        if actor:
            conditions.append("actor = ?")
            params.append(actor)
        if trace_id:
            conditions.append("trace_id = ?")
            params.append(trace_id)

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        params.extend([limit, offset])

        try:
            cursor = await self._conn.execute(
                f"SELECT * FROM audit_events {where} "
                f"ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params,
            )
            rows = await cursor.fetchall()
            result = []
            for row in rows:
                d = dict(row)
                if d.get("details"):
                    try:
                        d["details"] = json.loads(d["details"])
                    except Exception as _json_exc:
                        logger.debug(
                            "AuditLog.query: failed to parse details JSON for event %s: %s",
                            d.get("id"),
                            _json_exc,
                        )
                result.append(d)
            return result
        except Exception as exc:
            logger.error("AuditLog.query failed: %s", exc)
            return []

    async def stats(self) -> dict:
        """Return aggregate statistics about audit events."""
        if not self._conn:
            return {"total": 0, "disabled": True}
        try:
            cursor = await self._conn.execute(
                "SELECT event_type, COUNT(*) as cnt FROM audit_events GROUP BY event_type"
            )
            rows = await cursor.fetchall()
            counts = {row["event_type"]: row["cnt"] for row in rows}
            return {"total": sum(counts.values()), "by_type": counts}
        except Exception as exc:
            logger.error("AuditLog.stats failed: %s", exc)
            return {"total": 0, "error": str(exc)}

    # ------------------------------------------------------------------
    # Internal buffer management
    # ------------------------------------------------------------------

    async def _flush_buffer(self) -> None:
        """Write buffered events to SQLite."""
        if not self._buffer or not self._conn:
            return

        async with self._lock:
            events = self._buffer[:]
            self._buffer.clear()

        try:
            await self._conn.executemany(
                """
                INSERT OR IGNORE INTO audit_events
                    (id, event_type, actor, resource, outcome, details, trace_id, session_id, created_at)
                VALUES
                    (:id, :event_type, :actor, :resource, :outcome, :details, :trace_id, :session_id, :created_at)
            """,
                events,
            )
            await self._conn.commit()
        except Exception as exc:
            logger.error("AuditLog flush failed: %s", exc)
            # Put events back in buffer to retry — hold the lock so that
            # concurrent flushes or log() calls cannot interleave with the
            # prepend and lose events.
            async with self._lock:
                self._buffer = events + self._buffer

    async def _periodic_flush(self) -> None:
        """Background task: flush the buffer every few seconds."""
        while True:
            try:
                await asyncio.sleep(self._flush_interval)
                await self._flush_buffer()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("AuditLog periodic flush error: %s", exc)
