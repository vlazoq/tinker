# Chapter 11 — Observability

## The Problem

Tinker runs autonomously for hours or days.  Without observability you are
flying blind:

- **How do you know it's still running?**  A health endpoint that returns 200
  in under 1 second answers this.
- **How do you know it's running *well*?**  Loop durations, critic scores, and
  task queue depth tell you.
- **How do you know *why* something failed?**  An audit log with every
  significant event answers this.
- **How do you hold it accountable?**  SLA tracking tells you when loops are
  taking too long.

Observability is not a nice-to-have — without it you cannot debug, you
cannot improve, and you cannot trust the system.

---

## The Architecture Decision

We add four observability tools, each answering a different question:

| Tool | Answers |
|------|---------|
| **Health endpoint** | "Is it alive?  What's it doing right now?" |
| **Audit log** | "What happened, when, and why?" |
| **SLA tracker** | "Are loops within acceptable time limits?" |
| **Feature flags** | "Which subsystems are enabled right now?" |

All four are *passive* — they observe without changing behaviour.

---

## Step 1 — Directory Structure

```
tinker/
  observability/
    __init__.py
    audit_log.py     ← append-only event store
    sla_tracker.py   ← percentile tracking
```

---

## Step 2 — The Audit Log

### Why an audit log?

The standard `logging` module writes text to a file.  That's good for
*debugging* — reading human text to understand what happened.  An *audit log*
is different:

- **Structured** — every event has the same fields (type, actor, resource,
  outcome, details)
- **Queryable** — stored in SQLite, so you can run `SELECT * FROM audit_events
  WHERE event_type = 'task_failed'`
- **Append-only** — events are never modified or deleted (immutability gives
  you a trustworthy history)
- **Durable** — survives process restarts

Think of it as a permanent record of everything Tinker decided and did.

### Event types

```python
class AuditEventType(Enum):
    TASK_SELECTED       = "task_selected"
    TASK_COMPLETED      = "task_completed"
    TASK_FAILED         = "task_failed"
    ARTIFACT_STORED     = "artifact_stored"
    MESO_SYNTHESIS      = "meso_synthesis"
    MACRO_SYNTHESIS     = "macro_synthesis"
    STAGNATION_DETECTED = "stagnation_detected"
    CIRCUIT_OPEN        = "circuit_open"
    CONFIG_CHANGED      = "config_changed"
    BACKUP_CREATED      = "backup_created"
    SYSTEM_START        = "system_start"
    SYSTEM_STOP         = "system_stop"
    SLA_BREACH          = "sla_breach"
    DLQ_ENQUEUED        = "dlq_enqueued"
    CUSTOM              = "custom"
```

### The table schema

```sql
CREATE TABLE IF NOT EXISTS audit_events (
    id           TEXT PRIMARY KEY,
    event_type   TEXT NOT NULL,
    actor        TEXT NOT NULL,      -- who did it ("micro_loop", "admin")
    resource     TEXT,               -- what (task ID, artifact ID, ...)
    outcome      TEXT,               -- result ("success", "failure")
    details      TEXT,               -- JSON blob with extra context
    trace_id     TEXT,               -- correlates events in one loop
    session_id   TEXT,               -- which Tinker session
    created_at   TEXT NOT NULL
);
```

There are no `UPDATE` or `DELETE` queries anywhere in the code.  That's the
append-only guarantee — enforced by convention, not by the database.

### The AuditLog class

```python
# tinker/observability/audit_log.py

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class AuditEventType(Enum):
    TASK_SELECTED       = "task_selected"
    TASK_COMPLETED      = "task_completed"
    TASK_FAILED         = "task_failed"
    ARTIFACT_STORED     = "artifact_stored"
    MESO_SYNTHESIS      = "meso_synthesis"
    MACRO_SYNTHESIS     = "macro_synthesis"
    STAGNATION_DETECTED = "stagnation_detected"
    CIRCUIT_OPEN        = "circuit_open"
    CONFIG_CHANGED      = "config_changed"
    BACKUP_CREATED      = "backup_created"
    SYSTEM_START        = "system_start"
    SYSTEM_STOP         = "system_stop"
    SLA_BREACH          = "sla_breach"
    DLQ_ENQUEUED        = "dlq_enqueued"
    CUSTOM              = "custom"


class AuditLog:
    """SQLite-backed immutable audit log."""

    def __init__(self, db_path: str = "tinker_audit.sqlite") -> None:
        self._db_path        = db_path
        self._conn           = None
        self._lock           = asyncio.Lock()
        self._buffer: list[dict] = []
        self._flush_interval = 5.0    # write to disk every 5 s
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
                    details      TEXT,
                    trace_id     TEXT,
                    session_id   TEXT,
                    created_at   TEXT NOT NULL
                )
            """)
            await self._conn.execute(
                "CREATE INDEX IF NOT EXISTS audit_type_idx "
                "ON audit_events (event_type, created_at)"
            )
            await self._conn.commit()
            logger.info("AuditLog connected to %s", self._db_path)
            # Start background flush every 5 seconds
            self._flush_task = asyncio.create_task(self._periodic_flush())
        except ImportError:
            logger.warning("aiosqlite not available — AuditLog disabled")
        except Exception as exc:
            logger.warning("AuditLog failed to connect: %s", exc)

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
        actor:      str,
        resource:   Optional[str]  = None,
        outcome:    Optional[str]  = None,
        details:    Optional[dict] = None,
        trace_id:   Optional[str]  = None,
        session_id: Optional[str]  = None,
    ) -> Optional[str]:
        """
        Record an audit event.  Returns the event ID, or None if disabled.

        Events are buffered (max 50) and flushed every 5 seconds.
        This means one DB write covers many micro-loop steps instead of
        one write per step — much more efficient.
        """
        if not self._conn:
            return None

        event_id = str(uuid.uuid4())
        now      = datetime.now(timezone.utc).isoformat()

        self._buffer.append({
            "id":         event_id,
            "event_type": event_type.value,
            "actor":      actor,
            "resource":   resource,
            "outcome":    outcome,
            "details":    json.dumps(details) if details else None,
            "trace_id":   trace_id,
            "session_id": session_id,
            "created_at": now,
        })

        if len(self._buffer) >= 50:
            await self._flush_buffer()

        return event_id

    async def _flush_buffer(self) -> None:
        """Write buffered events to SQLite in one batch."""
        if not self._buffer or not self._conn:
            return
        async with self._lock:
            events = self._buffer[:]
            self._buffer.clear()
        try:
            await self._conn.executemany("""
                INSERT OR IGNORE INTO audit_events
                    (id, event_type, actor, resource, outcome,
                     details, trace_id, session_id, created_at)
                VALUES
                    (:id, :event_type, :actor, :resource, :outcome,
                     :details, :trace_id, :session_id, :created_at)
            """, events)
            await self._conn.commit()
        except Exception as exc:
            logger.error("AuditLog flush failed: %s", exc)
            self._buffer = events + self._buffer   # put them back

    async def _periodic_flush(self) -> None:
        """Background task that flushes the buffer every few seconds."""
        while True:
            try:
                await asyncio.sleep(self._flush_interval)
                await self._flush_buffer()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("AuditLog periodic flush error: %s", exc)
```

### Key design choices

**Why buffer instead of writing on every event?**

The micro loop can fire 10–20 times per minute.  If each event triggers a
SQLite write, you're doing 20 disk writes per minute for the audit log alone.
Batching them into one `executemany` every 5 seconds reduces this to at most
12 writes per minute *total*, regardless of event volume.

**Why `INSERT OR IGNORE`?**

Each event has a UUID primary key.  `INSERT OR IGNORE` means that if we
somehow try to insert the same event twice (e.g. the buffer was partially
flushed), we don't get an error — we just skip the duplicate silently.

---

## Step 3 — SLA Tracker

### What is an SLA?

A Service Level Agreement (SLA) is a performance target.  For Tinker:

> "95% of micro loops must complete in under 60 seconds."

Why does this matter?  Because without targets, you have no idea if performance
is degrading gradually.  If micro loops start taking 90 seconds instead of 40,
you want to know *before* the system falls hours behind schedule.

### Percentiles (not averages)

We use percentiles rather than averages:

- **p50 (median)** — half of loops are faster than this
- **p95** — 95% of loops are faster than this (the SLA target)
- **p99** — 99% of loops are faster than this (detects outliers)

Averages hide tail behaviour.  If 99% of loops take 30 seconds but 1% take 300
seconds, the average looks fine (~ 33 s) but users experience severe outliers.

### Implementation

```python
# tinker/observability/sla_tracker.py

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

logger = logging.getLogger(__name__)


@dataclass
class SLADefinition:
    name:        str
    p95_seconds: float = 60.0
    p99_seconds: float = 120.0
    max_seconds: Optional[float] = None
    window_size: int   = 200      # rolling window size


@dataclass
class SLAReport:
    name:        str
    count:       int   = 0
    p50_s:       float = 0.0
    p95_s:       float = 0.0
    p99_s:       float = 0.0
    max_s:       float = 0.0
    avg_s:       float = 0.0
    sla_p95:     float = 0.0
    sla_p99:     float = 0.0
    p95_breach:  bool  = False
    p99_breach:  bool  = False
    max_breach:  bool  = False
    breach_count: int  = 0

    def to_dict(self) -> dict:
        return {
            "name":         self.name,
            "count":        self.count,
            "p50_s":        round(self.p50_s, 2),
            "p95_s":        round(self.p95_s, 2),
            "p99_s":        round(self.p99_s, 2),
            "max_s":        round(self.max_s, 2),
            "avg_s":        round(self.avg_s, 2),
            "sla_p95":      self.sla_p95,
            "sla_p99":      self.sla_p99,
            "p95_breach":   self.p95_breach,
            "p99_breach":   self.p99_breach,
            "breach_count": self.breach_count,
        }


def _percentile(sorted_data: list[float], pct: float) -> float:
    """Return the p-th percentile of a sorted list (0 < pct < 100)."""
    if not sorted_data:
        return 0.0
    k      = (len(sorted_data) - 1) * pct / 100
    floor_k = int(k)
    ceil_k  = min(floor_k + 1, len(sorted_data) - 1)
    frac   = k - floor_k
    return sorted_data[floor_k] * (1 - frac) + sorted_data[ceil_k] * frac


class SLATracker:
    """Tracks SLA compliance across all Tinker loop types."""

    def __init__(self, alert_on_breach=None) -> None:
        self._definitions:  dict[str, SLADefinition] = {}
        self._measurements: dict[str, Deque[float]]  = {}
        self._alert_on_breach = alert_on_breach

    def define(
        self,
        name:        str,
        p95_seconds: float         = 60.0,
        p99_seconds: float         = 120.0,
        max_seconds: Optional[float] = None,
        window_size: int           = 200,
    ) -> None:
        """Register an SLA for a loop type.  Call once at startup."""
        self._definitions[name] = SLADefinition(
            name=name, p95_seconds=p95_seconds,
            p99_seconds=p99_seconds, max_seconds=max_seconds,
            window_size=window_size,
        )
        self._measurements[name] = deque(maxlen=window_size)

    def record(self, name: str, duration_seconds: float) -> Optional[SLAReport]:
        """
        Record a loop duration.

        Returns an SLAReport if the measurement caused a breach, else None.
        Calls the alert callback if one was provided at construction time.
        """
        if name not in self._definitions:
            self.define(name, p95_seconds=300.0, p99_seconds=600.0)

        self._measurements[name].append(duration_seconds)

        measurements = list(self._measurements[name])
        if len(measurements) >= 10:
            report = self.report(name)
            if report.p95_breach or report.p99_breach or report.max_breach:
                if self._alert_on_breach:
                    try:
                        self._alert_on_breach(report)
                    except Exception as exc:
                        logger.warning("SLA breach alert callback raised: %s", exc)
                return report
        return None

    def report(self, name: str) -> SLAReport:
        """Generate a compliance report for one loop type."""
        if name not in self._definitions:
            return SLAReport(name=name)

        sla_def = self._definitions[name]
        data    = sorted(self._measurements.get(name, []))
        n       = len(data)

        if n == 0:
            return SLAReport(
                name=name, sla_p95=sla_def.p95_seconds,
                sla_p99=sla_def.p99_seconds,
            )

        p50 = _percentile(data, 50)
        p95 = _percentile(data, 95)
        p99 = _percentile(data, 99)

        return SLAReport(
            name         = name,
            count        = n,
            p50_s        = p50,
            p95_s        = p95,
            p99_s        = p99,
            max_s        = data[-1],
            avg_s        = sum(data) / n,
            sla_p95      = sla_def.p95_seconds,
            sla_p99      = sla_def.p99_seconds,
            p95_breach   = p95 > sla_def.p95_seconds,
            p99_breach   = p99 > sla_def.p99_seconds,
            max_breach   = (
                sla_def.max_seconds is not None
                and data[-1] > sla_def.max_seconds
            ),
            breach_count = sum(1 for d in data if d > sla_def.p99_seconds),
        )

    def all_reports(self) -> dict[str, dict]:
        """Return compliance reports for all defined SLAs."""
        return {name: self.report(name).to_dict() for name in self._definitions}
```

---

## Step 4 — Integration into the Orchestrator

```python
# In Orchestrator.__init__()

from observability.audit_log  import AuditLog, AuditEventType
from observability.sla_tracker import SLATracker

self._audit = AuditLog("tinker_audit.sqlite")
self._sla   = SLATracker()
self._sla.define("micro_loop", p95_seconds=60.0,  p99_seconds=120.0, max_seconds=300.0)
self._sla.define("meso_loop",  p95_seconds=180.0, p99_seconds=300.0, max_seconds=600.0)
self._sla.define("macro_loop", p95_seconds=300.0, p99_seconds=600.0, max_seconds=1800.0)


# In Orchestrator.run() — before entering the main loop:
await self._audit.connect()
await self._audit.log(AuditEventType.SYSTEM_START, actor="orchestrator")


# In Orchestrator._tick() — after a successful micro loop:
import time
t_start = time.monotonic()

result = await run_micro_loop(task, ...)

duration = time.monotonic() - t_start
self._sla.record("micro_loop", duration)

await self._audit.log(
    AuditEventType.TASK_COMPLETED,
    actor      = "micro_loop",
    resource   = task.id,
    outcome    = "success",
    details    = {
        "critic_score":  result.critic_score,
        "artifact_id":   result.artifact_id,
        "subsystem":     task.subsystem,
        "duration_s":    round(duration, 2),
    },
)


# In Orchestrator._on_shutdown():
await self._audit.log(AuditEventType.SYSTEM_STOP, actor="orchestrator")
await self._audit.close()
```

---

## Step 5 — The Health Endpoint

The orchestrator also exposes an HTTP health server on port 8081 (separate
from the web UI on 8082).  It answers one question: "is Tinker alive and
what is it doing right now?"

```python
# In Orchestrator.__init__() — start the health server

import json
from aiohttp import web

async def _health_handler(request):
    return web.Response(
        text        = json.dumps(self.state.to_dict()),
        content_type = "application/json",
    )

async def _start_health_server(port: int = 8081):
    app = web.Application()
    app.router.add_get("/health", _health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Health server running on http://0.0.0.0:%d/health", port)
```

The web UI (Chapter 12) polls this endpoint every few seconds to populate
the dashboard.  When the orchestrator is offline, the web UI falls back to
reading `tinker_state.json` directly from disk.

---

## Step 6 — Try It

```python
# test_observability.py
import asyncio
import time
from observability.audit_log   import AuditLog, AuditEventType
from observability.sla_tracker import SLATracker

async def main():
    # 1. Audit log
    audit = AuditLog("test_audit.sqlite")
    await audit.connect()

    eid = await audit.log(
        AuditEventType.SYSTEM_START,
        actor      = "test",
        outcome    = "success",
        details    = {"version": "1.0"},
    )
    print(f"Logged event: {eid[:8]}...")

    await asyncio.sleep(0.1)   # let the background task flush
    await audit.close()

    # 2. SLA tracker
    sla = SLATracker()
    sla.define("micro_loop", p95_seconds=2.0, p99_seconds=5.0)

    for i in range(20):
        sla.record("micro_loop", 1.0 + i * 0.1)   # 1.0s, 1.1s, ... 2.9s

    report = sla.report("micro_loop")
    print(f"p50={report.p50_s:.1f}s  p95={report.p95_s:.1f}s  "
          f"breach={report.p95_breach}")

asyncio.run(main())
```

Expected output:

```
Logged event: a3f2e8...
p50=1.9s  p95=2.8s  breach=True
```

The p95 breach fires because the last few measurements (2.7s, 2.8s, 2.9s)
exceeded the 2.0s target.  In the real system this would trigger a warning
log and an optional alert callback.

---

## Key Concepts Introduced

| Concept | What it means |
|---------|---------------|
| Append-only audit log | Events are only inserted, never modified |
| Write buffering | Batch DB writes for efficiency (50 events or 5 seconds) |
| Percentile SLAs | p95/p99 targets detect tail latency, not just averages |
| Rolling window | SLA uses last N measurements, not all history |
| Alert callback | SLA tracker calls your function when a breach is detected |
| Health endpoint | Simple HTTP GET that returns current state as JSON |

The most important lesson here is the **separation of concerns**: the
`AuditLog` does not know about loops, and the `SLATracker` does not know
about SQLite.  Each tool does one thing.  The orchestrator wires them together.

---

→ Next: [Chapter 12 — The Web UI](./12-web-ui.md)
