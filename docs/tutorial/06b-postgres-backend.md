# Chapter 06b — PostgreSQL Backend (Enterprise Task Registry)

← Back to [Chapter 06 — The Task Engine](./06-task-engine.md)

---

## The Problem

Chapter 06 introduced `SQLiteTaskRegistry` — a single-file store that works
perfectly for one Tinker process on one machine.  Production deployments look
different:

```
Machine 1 (Tinker design loop)
Machine 2 (Grub worker 1)         ← all writing to the same task queue
Machine 3 (Grub worker 2)
```

SQLite cannot safely be shared across network mounts (NFS lock semantics are
implementation-defined; concurrent writes corrupt the journal).  You need a
shared, networked database.

`PostgresTaskRegistry` is that database.  It is a **drop-in replacement** —
same 14 public methods, same return types, no changes to the orchestrator or
any other caller.

---

## Architecture Decision: The Registry Abstraction

Both backends implement `AbstractTaskRegistry`.  The orchestrator programs
only to the abstract interface; it never imports either concrete class
directly.

```
tasks/
├── abstract_registry.py   ← the interface (ABC with 14 abstract methods)
├── registry.py            ← SQLiteTaskRegistry (default)
├── postgres_registry.py   ← PostgresTaskRegistry (production scale)
└── registry_factory.py    ← create_task_registry("sqlite") or ("postgres", dsn=...)
```

Adding a third backend (MySQL, DynamoDB, Redis) requires:
1. Subclass `AbstractTaskRegistry`
2. Implement all 14 abstract methods
3. Register the new name in `registry_factory.py`

No other files change.

---

## Enterprise Features

The PostgreSQL backend adds four capabilities that `SQLiteTaskRegistry`
cannot provide:

| Feature | Why it matters |
|---------|----------------|
| **Connection pool** | Multiple threads share connections without blocking |
| **Connection retry with exponential back-off** | Survives server restarts, network blips |
| **Query timeout** | Prevents a slow query from stalling the whole process |
| **Schema migration versioning** | ALTER TABLE applied exactly once, tracked in DB |
| **Bulk writes (`save_batch`)** | 1 round-trip for N tasks instead of N round-trips |
| **Health check** | `/health` endpoints can probe the DB in one call |

All of these are also available as abstract methods on `AbstractTaskRegistry`
so the SQLite backend implements them too (at a simpler level).

---

## Step 1 — Install psycopg2

```bash
pip install psycopg2-binary
```

Add to `requirements/base.in`:

```
psycopg2-binary>=2.9
```

Then regenerate the lock file:

```bash
make deps
```

---

## Step 2 — The Connection Pool

```python
# tasks/postgres_registry.py

import psycopg2
import psycopg2.pool as _pool

class PostgresTaskRegistry(AbstractTaskRegistry):

    def __init__(
        self,
        dsn:              str   = "",
        *,
        min_conn:         int   = 1,
        max_conn:         int   = 10,
        max_retries:      int   = 3,
        retry_base_delay: float = 0.5,
        query_timeout_ms: int | None = None,
        connection_factory: Callable | None = None,
    ) -> None:
        ...
        self._pool = _pool.ThreadedConnectionPool(
            minconn=min_conn, maxconn=max_conn, dsn=dsn,
            # Optional: enforce a per-query time limit on all connections
            options=f"-c statement_timeout={query_timeout_ms}" if query_timeout_ms else None,
        )
```

`ThreadedConnectionPool` keeps between `min_conn` and `max_conn` open
connections.  Each thread in the process borrows one connection, executes its
queries, then returns it.  Borrowing and returning are thread-safe.

### Why not a single shared connection?

SQLite uses a single `threading.Lock`-protected connection because SQLite
itself is not thread-safe.  PostgreSQL is fully concurrent — multiple threads
can send queries simultaneously over different connections.  A pool lets them
do exactly that.

---

## Step 3 — Connection Retry with Exponential Back-off

Production PostgreSQL deployments occasionally have brief unavailability:
- Rolling restarts during upgrades
- Network hiccups between app server and database
- Connection pool exhaustion during traffic spikes

Without retry, a brief 2-second restart causes a hard error and drops the
task that was being processed.  With retry, the loop pauses and tries again:

```python
# The _conn() context manager retries pool acquisition for transient errors.

@contextmanager
def _conn(self) -> Generator[Any, None, None]:
    for attempt in range(self._max_retries + 1):
        if attempt > 0:
            delay = self._retry_base_delay * (2 ** (attempt - 1))
            log.warning("Transient error, retrying in %.1fs", delay)
            time.sleep(delay)
        try:
            conn = self._pool.getconn()
            break
        except Exception as exc:
            if not _is_transient(exc):
                raise  # never retry logic errors

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        self._pool.putconn(conn)
```

### What counts as "transient"?

`_is_transient(exc)` checks the error message for known transient patterns:

| Fragment | Meaning |
|----------|---------|
| `could not connect to server` | Server is down / restarting |
| `connection refused` | Port not yet listening |
| `too many connections` | Pool temporarily exhausted |
| `server closed the connection` | Server-side timeout/restart |
| `SSL connection has been closed` | Network-level reset |
| `connection timed out` | Network delay |

Everything else (bad SQL, constraint violations, missing tables) is permanent
and is never retried.

### Retry schedule

| Attempt | Delay (base=0.5s) |
|---------|-------------------|
| 1 | immediate |
| 2 | 0.5 s |
| 3 | 1.0 s |
| 4 | 2.0 s |

After `max_retries` (default 3), the last exception is re-raised.

---

## Step 4 — Query Timeout

A rogue query (full-table scan with no index, or a deadlock) can hold a
connection for minutes.  The `query_timeout_ms` parameter caps every query:

```python
registry = PostgresTaskRegistry(
    dsn              = "postgresql://...",
    query_timeout_ms = 5000,   # 5 seconds
)
```

Under the hood this injects `-c statement_timeout=5000` into every connection
when the pool is created.  PostgreSQL enforces this server-side and raises
`QueryCanceledError` when the limit is hit.  The connection's transaction is
rolled back automatically.

**Recommended values:**

| Use case | Timeout |
|----------|---------|
| API / real-time reads | 3 000 ms |
| Background task queue | 10 000 ms |
| Schema migrations | None (no limit) |
| Bulk imports | None (no limit) |

---

## Step 5 — Schema Migration Versioning

Production databases evolve.  When you add a column or index, you need to
`ALTER TABLE` exactly once across all deployments, without blocking or
data loss.

`PostgresTaskRegistry` maintains a `schema_migrations` bookkeeping table:

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    description TEXT    NOT NULL,
    applied_at  TEXT    NOT NULL
)
```

Migrations are a list in `postgres_registry.py`:

```python
_MIGRATIONS = [
    (
        1,
        "Initial schema: tasks table and indexes",
        [_CREATE_TABLE] + _CREATE_INDEXES,
    ),
    # Future migration (add to the end):
    # (
    #     2,
    #     "Add attempt_limit column",
    #     ["ALTER TABLE tasks ADD COLUMN IF NOT EXISTS attempt_limit INTEGER"],
    # ),
]
```

At startup, `_run_migrations()`:
1. Creates `schema_migrations` if it does not exist
2. Queries which versions are already applied
3. For each unapplied migration (ascending version), runs the SQL statements
   in a transaction and records the version in `schema_migrations`

**Rules for migrations:**
- Never edit an existing entry — add a new one instead
- Versions must be consecutive integers starting at 1
- Each migration runs in its own transaction (a failure does not corrupt earlier ones)
- Idempotent: running migrations twice is safe

### Inspecting applied migrations

```python
migrations = registry.list_applied_migrations()
# [{"version": 1, "description": "Initial schema...", "applied_at": "2024-01-01..."}]
```

Or via SQL directly:
```sql
SELECT * FROM schema_migrations ORDER BY version;
```

---

## Step 6 — Bulk Writes: `save_batch()`

Seeding a fresh task queue from a 50-subsystem design artifact would take 50
separate `INSERT … ON CONFLICT` round-trips in the original code.  With
`save_batch()`:

```python
# Before (50 round-trips)
for task in design_tasks:
    registry.save(task)

# After (1 round-trip, single transaction)
registry.save_batch(design_tasks)
```

Implementation uses `executemany` with the same upsert SQL:

```python
def save_batch(self, tasks: list[Task]) -> list[Task]:
    if not tasks:
        return tasks
    sql = (
        f"INSERT INTO tasks ({cols}) VALUES ({vals}) "
        f"ON CONFLICT (id) DO UPDATE SET {_UPSERT_SET}"
    )
    params_list = [[_task_to_row(t)[c] for c in _COLUMNS] for t in tasks]
    with self._conn() as conn:
        cur = self._cursor(conn)
        cur.executemany(sql, params_list)
    return tasks
```

All rows are committed atomically — if any row fails, the entire batch is
rolled back.

**Both backends implement `save_batch`:**
- `PostgresTaskRegistry.save_batch`: `executemany` in one transaction
- `SQLiteTaskRegistry.save_batch`: `executemany` in one transaction

SQLite's `executemany` is equally fast because it avoids the SQLite
commit-per-statement overhead.

---

## Step 7 — Health Checks

`health_check()` returns `True`/`False` without raising:

```python
if not registry.health_check():
    logger.error("Task registry unavailable — skipping this cycle")
    return

tasks = registry.pending_ordered()
```

The implementation:

```python
def health_check(self) -> bool:
    try:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute("SELECT 1 FROM tasks LIMIT 1")
        return True
    except Exception:
        return False
```

This checks:
1. **Connectivity** — can we acquire a connection from the pool?
2. **Schema** — does the `tasks` table exist?

Both must pass for `True`.

### Wiring to a `/health` endpoint

```python
# webui/app.py

@app.get("/health")
def health():
    registry_ok = registry.health_check()
    return {
        "ok":       registry_ok,
        "registry": "postgres" if registry_ok else "unavailable",
    }
```

---

## Step 8 — Testability Without a Server

All the enterprise features are testable without a PostgreSQL server using
the `connection_factory` parameter:

```python
from unittest.mock import MagicMock

mock_conn = MagicMock()
registry  = PostgresTaskRegistry(connection_factory=lambda: mock_conn)
# Now all queries go to mock_conn — no real DB needed
```

In `connection_factory` mode:
- No pool is created
- Retry logic is bypassed (the mock never raises transient errors)
- `query_timeout_ms` is stored but not injected (no pool to configure)

This is why all 87 tests in `tasks/tests/test_postgres_registry.py` run
in under 0.3 seconds with no external dependencies.

### Testing retry logic

Retry tests use `__new__` to build a registry with a mock pool:

```python
def _make_pool_registry(getconn_side_effects):
    mock_pool = MagicMock()
    mock_pool.getconn.side_effect = getconn_side_effects

    registry = PostgresTaskRegistry.__new__(PostgresTaskRegistry)
    registry._single           = None
    registry._pool             = mock_pool
    registry._max_retries      = 3
    registry._retry_base_delay = 0.1
    registry._query_timeout_ms = None
    return registry, mock_pool

# Fail twice, succeed on third attempt
transient  = Exception("could not connect to server: connection refused")
mock_conn  = MagicMock()
registry, _ = _make_pool_registry([transient, transient, mock_conn])

with patch("tasks.postgres_registry.time.sleep"):
    with registry._conn() as conn:
        assert conn is mock_conn  # eventually got a real connection
```

---

## Switching Backends

### In code

```python
from tasks.registry_factory import create_task_registry

# Development / single-machine
registry = create_task_registry("sqlite", db_path="tinker_tasks.sqlite")

# Production / multi-machine
registry = create_task_registry(
    "postgres",
    dsn              = "postgresql://tinker:pw@db.host/tinker_tasks",
    max_conn         = 20,
    query_timeout_ms = 5000,
)
```

### Via environment variables

```bash
# .env

# SQLite (default)
TINKER_REGISTRY_BACKEND=sqlite
TINKER_DB_PATH=./tinker_tasks.sqlite

# PostgreSQL
TINKER_REGISTRY_BACKEND=postgres
TINKER_POSTGRES_DSN=postgresql://tinker:pw@db.host/tinker_tasks
TINKER_PG_MAX_CONN=20
TINKER_PG_QUERY_TIMEOUT_MS=5000
```

---

## Running the Tests

```bash
# PostgreSQL registry tests (87 tests, no server needed)
python -m pytest tasks/tests/test_postgres_registry.py -v

# All registry tests (SQLite + PostgreSQL)
python -m pytest tasks/tests/ -v
```

### Test coverage

| Test class | What it verifies |
|------------|-----------------|
| `TestRowHelpers` | `_pg_row_to_dict`, `_task_to_row` conversions |
| `TestSchemaInit` | CREATE TABLE + indexes executed on construction |
| `TestSave` | Upsert SQL, params count, commit, rollback |
| `TestGet` | None when not found; Task when found; parameterised SQL |
| `TestDelete` | rowcount→bool; parameterised DELETE |
| `TestListAll` | Empty → []; multiple → all tasks |
| `TestByStatus` | status IN (...) SQL; multi-status |
| `TestPendingOrdered` | ORDER BY priority_score DESC |
| `TestCountByStatus` | GROUP BY status → dict |
| `TestOldestPending` | ORDER BY created_at ASC LIMIT 1 |
| `TestClose` | pool.closeall() or conn.close() |
| `TestRegistryFactory` | factory returns right type |
| `TestIsTransient` | Transient vs permanent error classification |
| `TestConnectionRetry` | Retry on transient; no retry on permanent; exponential back-off delays; pool conn returned on error |
| `TestQueryTimeoutConfig` | timeout stored; pool receives `options` kwarg |
| `TestHealthCheck` | True on success; False on exception; never raises; checks tasks table |
| `TestSaveBatch` | Empty no-op; executemany once; upsert SQL; rollback on error |
| `TestSchemaMigrations` | Migrations list valid; create table executed; skip if applied; `list_applied_migrations` |
| `TestSQLiteHealthCheck` | True/False on SQLite; save_batch atomic; upsert dedup |
| `TestAbstractRegistryInterface` | All 14 methods abstract; both backends implement all |

---

← Back to [Chapter 06 — The Task Engine](./06-task-engine.md)
→ Next: [Chapter 07 — The Context Assembler](./07-context-assembler.md)
