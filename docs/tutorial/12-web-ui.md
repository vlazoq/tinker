# Chapter 12 — The Web UI

## The Problem

Tinker runs in a terminal, printing log lines.  That's fine for debugging,
but for day-to-day monitoring you want:

- **A dashboard** — see loop counts, current task, critic scores at a glance
- **A config editor** — change thresholds without editing JSON files by hand
- **A task injector** — add a new task to the queue while Tinker is running
- **A DLQ viewer** — see which operations failed permanently and resolve them
- **An audit explorer** — filter and search the audit log

We build three independent UIs that all read the same data files and expose
the same functionality.  Each one is suited for different users:

| UI | Best for |
|----|----------|
| **FastAPI + React** (`webui/`) | Permanent deployment, shareable URLs |
| **Gradio** (`gradio_ui/`) | Data scientists, quick interactive demo |
| **Streamlit** (`streamlit_ui/`) | Analysts, easy Python-only dashboard |

All three UIs are *read-mostly*.  They read SQLite databases and JSON files
that the orchestrator writes.  They never talk to the orchestrator directly —
they share files.

---

## The Architecture Decision

### Shared data layer (`webui/core.py`)

Rather than duplicate database access code across three UIs, we put all
shared logic into one file: `webui/core.py`.

```
webui/
  core.py          ← shared DB helpers, file paths, config schemas
  app.py           ← FastAPI routes
  templates/
    index.html     ← React SPA (vanilla JS, no build tools)
  static/
    tinker.css     ← styling

gradio_ui/
  app.py           ← imports from webui.core

streamlit_ui/
  app.py           ← imports from webui.core
```

When Gradio or Streamlit need to read the task database, they call the same
helper functions as the FastAPI backend.  One source of truth.

### No build tools for the frontend

The React dashboard is written in **vanilla JavaScript** loaded from a CDN.
No Node, no npm, no webpack.  The entire frontend is a single `index.html`
file that works in any browser.

This is a deliberate trade-off:
- **Easier to maintain** — no build pipeline, no `node_modules`
- **Portable** — works offline once the CDN scripts are cached
- **Readable** — a junior developer can open the file and understand it

---

## Step 1 — The Shared Core (`webui/core.py`)

```python
# webui/core.py

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

# ── File paths (all overridable via env vars) ─────────────────────────────────
BASE_DIR   = Path(os.getenv("TINKER_BASE_DIR", Path(__file__).parent.parent))
TASKS_DB   = Path(os.getenv("TINKER_TASK_DB",        BASE_DIR / "tinker_tasks_engine.sqlite"))
DLQ_DB     = Path(os.getenv("TINKER_DLQ_PATH",       BASE_DIR / "tinker_dlq.sqlite"))
AUDIT_DB   = Path(os.getenv("TINKER_AUDIT_LOG_PATH", BASE_DIR / "tinker_audit.sqlite"))
BACKUP_DIR = Path(os.getenv("TINKER_BACKUP_DIR",     BASE_DIR / "tinker_backups"))
FLAGS_FILE = Path(os.getenv("TINKER_FLAGS_FILE",      BASE_DIR / "tinker_flags.json"))
CONFIG_FILE= Path(os.getenv("TINKER_WEBUI_CONFIG",    BASE_DIR / "tinker_webui_config.json"))
STATE_FILE = Path(os.getenv("TINKER_STATE_PATH",      BASE_DIR / "tinker_state.json"))
HEALTH_URL = os.getenv("TINKER_HEALTH_URL", "http://localhost:8081")
```

All paths are read from environment variables.  This means:
- Tests can set `TINKER_TASK_DB=/tmp/test.sqlite` without touching production
- The same code works whether you run from `~/tinker/` or `/srv/tinker/`

### Synchronous and async DB helpers

We need two versions of each database helper:

- **`db_query_sync` / `db_execute_sync`** — called from Gradio and Streamlit,
  which run in synchronous code
- **`db_query` / `db_execute`** — async wrappers used by FastAPI (they call
  the sync versions via `asyncio.to_thread()` so they don't block the event loop)

```python
def db_query_sync(db: Path, sql: str, params: tuple = ()) -> list[dict]:
    """Run a SELECT and return rows as list-of-dicts.  Returns [] on any error."""
    if not db.exists():
        return []
    try:
        con = sqlite3.connect(str(db), timeout=5, check_same_thread=False)
        con.row_factory = sqlite3.Row
        rows = con.execute(sql, params).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def db_execute_sync(db: Path, sql: str, params: tuple = ()) -> bool:
    """Run an INSERT/UPDATE.  Returns True on success, False on failure."""
    if not db.exists():
        return False
    try:
        con = sqlite3.connect(str(db), timeout=5, check_same_thread=False)
        con.execute(sql, params)
        con.commit()
        con.close()
        return True
    except Exception:
        return False


async def db_query(db: Path, sql: str, params: tuple = ()) -> list[dict]:
    return await asyncio.to_thread(db_query_sync, db, sql, params)

async def db_execute(db: Path, sql: str, params: tuple = ()) -> bool:
    return await asyncio.to_thread(db_execute_sync, db, sql, params)
```

### Reading state

The orchestrator writes `tinker_state.json` every loop.  The UIs read it
to show current status.

```python
def load_state() -> dict:
    """Load the orchestrator state file.  Returns {} if not found."""
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}
```

### Fetching health

The health endpoint (`http://localhost:8081/health`) is the *live* view —
it talks directly to the running orchestrator.  When it's offline, we fall
back to reading `tinker_state.json`.

```python
def _state_to_health(state: dict) -> dict:
    """
    Convert the state file format into the health response shape.

    The state file uses totals.{micro,meso,macro}.
    The React dashboard expects loops.{micro,meso,macro}.
    This function bridges that gap.
    """
    totals     = state.get("totals", {})
    micro_hist = state.get("micro_history", [])
    last_critic = micro_hist[-1].get("critic_score") if micro_hist else None

    return {
        "online":           False,
        "from_state_file":  True,
        "status":           state.get("status", "unknown"),
        "uptime_seconds":   state.get("uptime_seconds"),
        "loops": {
            "micro":                totals.get("micro", 0),
            "meso":                 totals.get("meso",  0),
            "macro":                totals.get("macro", 0),
            "consecutive_failures": totals.get("consecutive_failures", 0),
            "current_level":        state.get("current_level", "idle"),
        },
        "current_task_id": state.get("current_task_id"),
        "dlq":             {"pending": 0, "resolved": 0},
        "circuit_breakers":{},
        "memory":          {},
        "rate_limiters":   {},
        "sla":             {},
    }


async def fetch_health() -> dict:
    """Try the live health endpoint; fall back to state file."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{HEALTH_URL}/health")
            if r.status_code == 200:
                return {"online": True, **r.json()}
    except Exception:
        pass

    state = load_state()
    if state:
        return _state_to_health(state)

    return {"online": False, "status": "unknown"}
```

---

## Step 2 — The FastAPI Backend (`webui/app.py`)

FastAPI serves the React SPA and exposes a JSON API.  Every route is simple:
read from a DB or file, return JSON.

```python
# webui/app.py (simplified)

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

app = FastAPI(title="Tinker Web UI")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

templates = Jinja2Templates(directory="webui/templates")
app.mount("/static", StaticFiles(directory="webui/static"), name="static")

# Serve the React SPA for every non-API route
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

# ── API routes ────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def api_health():
    return await fetch_health()

@app.get("/api/tasks")
async def api_tasks():
    tasks = await db_query(
        TASKS_DB,
        "SELECT id, title, type, subsystem, status, priority_score, "
        "created_at, attempt_count FROM tasks "
        "ORDER BY priority_score DESC LIMIT 200"
    )
    stats_rows = await db_query(TASKS_DB,
        "SELECT status, COUNT(*) as count FROM tasks GROUP BY status")
    stats = {r["status"]: r["count"] for r in stats_rows}
    return {"tasks": tasks, "stats": stats}

@app.post("/api/tasks/inject")
async def api_tasks_inject(request: Request):
    body    = await request.json()
    task_id = str(uuid.uuid4())
    ts      = datetime.now(timezone.utc).isoformat()
    ok = await db_execute(
        TASKS_DB,
        """INSERT INTO tasks
           (id, title, description, type, subsystem, status,
            confidence_gap, is_exploration, created_at, updated_at,
            priority_score, staleness_hours, dependency_depth,
            last_subsystem_work_hours, attempt_count,
            dependencies, outputs, tags, metadata)
           VALUES (?,?,?,?,?,'pending',?,?,?,?,0.5,0.0,0,0.0,0,
                   '[]','[]','[]','{}')""",
        (task_id, body.get("title","Untitled"), body.get("description",""),
         body.get("type","design"), body.get("subsystem","cross_cutting"),
         float(body.get("confidence_gap", 0.5)),
         1 if body.get("is_exploration") else 0,
         ts, ts)
    )
    return {"ok": ok, "id": task_id}

@app.get("/api/audit")
async def api_audit(event_type: str = "", actor: str = "",
                    page: int = 1, limit: int = 50):
    offset     = (page - 1) * limit
    conditions, params = [], []
    if event_type:
        conditions.append("event_type = ?"); params.append(event_type)
    if actor:
        conditions.append("actor = ?");      params.append(actor)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    items = await db_query(
        AUDIT_DB,
        f"SELECT id, event_type, actor, resource, outcome, trace_id, "
        f"created_at FROM audit_events {where} "
        f"ORDER BY created_at DESC LIMIT ? OFFSET ?",
        tuple(params) + (limit, offset)
    )
    return {"items": items, "page": page, "has_next": len(items) == limit}
```

### Server-Sent Events (SSE)

SSE lets the browser subscribe to a stream of events without polling.  The
server keeps the HTTP connection open and pushes `data: ...\n\n` lines
whenever there's new information.

```python
import asyncio, json
from fastapi.responses import StreamingResponse
from typing import AsyncIterator

@app.get("/api/logs/stream")
async def api_logs_stream(request: Request):
    """Keep-alive SSE stream: emit a message each time the micro count changes."""

    async def gen() -> AsyncIterator[str]:
        last_micro = -1
        while True:
            if await request.is_disconnected():
                break
            state  = load_state()
            totals = state.get("totals", {})
            micro  = totals.get("micro", -1)

            if micro != last_micro:
                last_micro = micro
                micro_hist = state.get("micro_history", [])
                critic     = micro_hist[-1].get("critic_score") if micro_hist else None
                yield "data: " + json.dumps({
                    "time":                 datetime.now(timezone.utc).isoformat(),
                    "micro_loops":          micro,
                    "meso_loops":           totals.get("meso", 0),
                    "macro_loops":          totals.get("macro", 0),
                    "current_task":         state.get("current_task_id"),
                    "critic_score":         critic,
                    "current_level":        state.get("current_level"),
                    "consecutive_failures": totals.get("consecutive_failures", 0),
                }) + "\n\n"

            await asyncio.sleep(2)

    return StreamingResponse(
        gen(),
        media_type = "text/event-stream",
        headers    = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

**Why SSE instead of WebSockets?**
SSE is one-directional (server → browser) and simpler to implement.  No
handshake, no special server support, and it reconnects automatically on
drop.  WebSockets add complexity without benefit here because the browser
never needs to send data back through the stream.

---

## Step 3 — Running the Web UI

```python
# webui/__main__.py

import os
import uvicorn
from .app import app

port = int(os.getenv("TINKER_WEBUI_PORT", "8082"))
uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
```

Start it:
```bash
python -m webui
```

Open `http://localhost:8082` in your browser.

---

## Step 4 — The Gradio UI (`gradio_ui/app.py`)

Gradio builds UIs from Python functions.  Each tab is a `gr.Tab` block.
The UI re-reads data files on each button click or timer refresh.

```python
# gradio_ui/app.py (simplified)

import gradio as gr
from webui.core import load_state, db_query_sync as dbq, TASKS_DB

def _health_md() -> str:
    """Render current orchestrator state as a Markdown table."""
    state  = load_state()
    if not state:
        return "**Orchestrator offline** — `tinker_state.json` not found."
    totals     = state.get("totals", {})
    micro_hist = state.get("micro_history", [])
    last_critic = micro_hist[-1].get("critic_score") if micro_hist else "—"
    return "\n".join([
        "| Metric | Value |",
        "|--------|-------|",
        f"| Status          | **{state.get('status','—')}** |",
        f"| Micro loops     | **{totals.get('micro','—')}** |",
        f"| Current task    | `{state.get('current_task_id','—')}` |",
        f"| Last critic score | {last_critic} |",
    ])

def _tasks_df():
    import pandas as pd
    rows = dbq(TASKS_DB,
        "SELECT id, title, type, subsystem, status, priority_score "
        "FROM tasks ORDER BY priority_score DESC LIMIT 200")
    return pd.DataFrame(rows) if rows else pd.DataFrame()


with gr.Blocks(title="Tinker") as demo:
    with gr.Tab("Dashboard"):
        health_out = gr.Markdown(value=_health_md)
        gr.Button("Refresh").click(fn=_health_md, outputs=health_out)
    with gr.Tab("Task Queue"):
        gr.Dataframe(value=_tasks_df, every=10)   # auto-refresh every 10s

demo.launch(server_port=8083)
```

The key difference from FastAPI: Gradio is **synchronous**.  Functions like
`_health_md()` are called directly, not via `await`.  That's why we use
`db_query_sync` instead of `db_query` in the Gradio UI.

---

## Step 5 — Grub Status Tab

All three UIs include a **Grub** tab that shows Grub's implementation pipeline
alongside Tinker's design loops.  The data comes from `fetch_grub_status_sync()`
in `webui/core.py`, which reads:

- **Task queue** — `TASKS_DB` filtered to `type IN ('implementation','review')`
- **Grub queue** — `GRUB_QUEUE_DB` (`grub_queue.sqlite`) task counts by status
- **Artifacts** — the 10 most recent `.md` files in `GRUB_ARTIFACTS_DIR`

### What each UI shows in the Grub tab

| UI | Implementation tasks | Grub queue counts | Recent artifacts |
|----|---------------------|------------------|-----------------|
| webui (FastAPI + React) | ✓ table | ✓ metrics | ✓ list with preview |
| gradio_ui | ✓ DataFrame | ✓ Markdown | ✓ Markdown list |
| streamlit_ui | ✓ DataFrame | ✓ st.metric tiles | ✓ expandable list |

To add more Grub detail to any UI, extend `fetch_grub_status_sync()` in
`webui/core.py` — the data shape automatically flows to all three frontends.

---

## Step 6 — Running All Three UIs

```bash
# Terminal 1: Web UI (React)
python -m webui

# Terminal 2: Gradio UI
python -m gradio_ui

# Terminal 3: Streamlit UI
python -m streamlit_ui
```

| UI | Default port | Tech |
|----|--------------|------|
| webui | 8082 | FastAPI + vanilla React |
| gradio_ui | 8083 | Gradio |
| streamlit_ui | 8501 | Streamlit |

---

## Key Concepts Introduced

| Concept | What it means |
|---------|---------------|
| Shared core module | One file (`core.py`) imports from three UIs — no duplication |
| Sync vs async wrappers | `db_query_sync` for Gradio/Streamlit; `db_query` (async) for FastAPI |
| `asyncio.to_thread()` | Run blocking SQLite calls without blocking the FastAPI event loop |
| SSE streaming | Keep HTTP connection open, push updates when state changes |
| State file fallback | When orchestrator is offline, read `tinker_state.json` from disk |
| `_state_to_health()` | Transform state file format → API response format |

The most important lesson here is the **state file as integration point**.
The orchestrator writes a JSON file.  The UIs read it.  No direct coupling,
no shared process — just a file that both sides agree on.

---

→ Next: [Chapter 13 — Integration: Wiring It All Together](./13-integration.md)
