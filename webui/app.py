"""
webui/app.py
────────────
FastAPI backend for the Tinker Web UI.
All API routes return JSON; the React SPA (index.html) consumes them.

Run:  python -m tinker.webui          (default port 8082)
      TINKER_WEBUI_PORT=9000 python -m tinker.webui
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Form, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .core import (
    ORCH_CONFIG_SCHEMA, STAGNATION_CONFIG_SCHEMA,
    FLAG_DEFAULTS, FLAG_DESCRIPTIONS, FLAG_GROUPS,
    TASK_TYPES, SUBSYSTEMS,
    AUDIT_DB, BACKUP_DIR, DLQ_DB, FLAGS_FILE, TASKS_DB,
    db_execute, db_query,
    fetch_grub_status, fetch_health, list_backups,
    load_config, load_flags, load_state,
    new_id, now_iso, save_config, save_flags,
)

# ── App setup ─────────────────────────────────────────────────────────────────
app = FastAPI(title="Tinker Web UI", docs_url="/api/docs", redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")


# ── SPA shell ─────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ── API version ───────────────────────────────────────────────────────────────
@app.get("/api/version")
async def api_version():
    """Return the Tinker API version and schema version for client compatibility checks."""
    return {
        "api_version": "v1",
        "schema_version": 1,
        "app": "tinker-webui",
    }


# ── Health / status ───────────────────────────────────────────────────────────
@app.get("/api/health")
async def api_health():
    return await fetch_health()

@app.get("/api/state")
async def api_state():
    return load_state()

@app.get("/api/grub/status")
async def api_grub_status():
    """Return Grub pipeline status: task counts, queue stats, recent artifacts."""
    return await fetch_grub_status()


# ── Config ────────────────────────────────────────────────────────────────────
@app.get("/api/config")
async def api_config_get():
    saved = load_config()
    # Build response: merge saved values over defaults
    result: dict[str, Any] = {"_saved_at": saved.get("_saved_at"), "orchestrator": {}, "stagnation": {}}
    for section_key, section in ORCH_CONFIG_SCHEMA.items():
        for field_name, meta in section["fields"].items():
            result["orchestrator"][field_name] = saved.get(field_name, meta["default"])
    for section_key, section in STAGNATION_CONFIG_SCHEMA.items():
        result["stagnation"][section_key] = {}
        for field_name, meta in section["fields"].items():
            stag = saved.get("stagnation", {})
            result["stagnation"][section_key][field_name] = stag.get(section_key, {}).get(field_name, meta["default"])
    result["_schema"] = {
        "orchestrator": ORCH_CONFIG_SCHEMA,
        "stagnation": STAGNATION_CONFIG_SCHEMA,
    }
    return result

@app.post("/api/config")
async def api_config_save(body: dict = None, request: Request = None):
    data = await request.json()
    errors: list[str] = []
    to_save: dict[str, Any] = {}

    # Validate orchestrator fields
    for section_key, section in ORCH_CONFIG_SCHEMA.items():
        for field_name, meta in section["fields"].items():
            raw = data.get("orchestrator", {}).get(field_name)
            if raw is None:
                to_save[field_name] = meta["default"]
                continue
            try:
                val = int(raw) if meta["type"] == "int" else float(raw)
                if val < meta["min"]:
                    errors.append(f"{meta['label']} must be >= {meta['min']}")
                else:
                    to_save[field_name] = val
            except (ValueError, TypeError):
                errors.append(f"{meta['label']}: invalid value '{raw}'")

    # Validate stagnation fields
    stagnation_save: dict[str, Any] = {}
    for section_key, section in STAGNATION_CONFIG_SCHEMA.items():
        stagnation_save[section_key] = {}
        for field_name, meta in section["fields"].items():
            raw = data.get("stagnation", {}).get(section_key, {}).get(field_name)
            if raw is None:
                stagnation_save[section_key][field_name] = meta["default"]
                continue
            try:
                val = int(raw) if meta["type"] == "int" else float(raw)
                if val < meta["min"]:
                    errors.append(f"Stagnation {section_key}.{meta['label']} must be >= {meta['min']}")
                else:
                    stagnation_save[section_key][field_name] = val
            except (ValueError, TypeError):
                errors.append(f"Stagnation {section_key}.{meta['label']}: invalid value '{raw}'")

    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=422)

    to_save["stagnation"] = stagnation_save
    save_config(to_save)
    return {"ok": True, "message": "Config saved. Restart the orchestrator to apply changes."}


# ── Feature Flags ─────────────────────────────────────────────────────────────
@app.get("/api/flags")
async def api_flags_get():
    flags = load_flags()
    return {
        "flags": flags,
        "groups": FLAG_GROUPS,
        "descriptions": FLAG_DESCRIPTIONS,
        "flags_file": str(FLAGS_FILE),
    }

@app.post("/api/flags/{flag_name}")
async def api_flags_toggle(flag_name: str, request: Request):
    body = await request.json()
    enabled = bool(body.get("enabled", False))
    flags = load_flags()
    if flag_name not in FLAG_DEFAULTS:
        return JSONResponse({"ok": False, "error": f"Unknown flag: {flag_name}"}, status_code=404)
    flags[flag_name] = enabled
    save_flags(flags)
    return {"ok": True, "flag": flag_name, "enabled": enabled,
            "message": f"Flag '{flag_name}' set to {'enabled' if enabled else 'disabled'}. Takes effect within 30s."}


# ── Task Queue ────────────────────────────────────────────────────────────────
@app.get("/api/tasks")
async def api_tasks():
    tasks = await db_query(
        TASKS_DB,
        "SELECT id, title, type, subsystem, status, priority_score, "
        "created_at, attempt_count, is_exploration, description "
        "FROM tasks ORDER BY priority_score DESC, created_at ASC LIMIT 200"
    )
    stats_rows = await db_query(TASKS_DB, "SELECT status, COUNT(*) as count FROM tasks GROUP BY status")
    stats = {r["status"]: r["count"] for r in stats_rows}
    return {"tasks": tasks, "stats": stats, "task_types": TASK_TYPES, "subsystems": SUBSYSTEMS}

@app.post("/api/tasks/inject")
async def api_tasks_inject(request: Request):
    body = await request.json()
    task_id = new_id()
    ts = now_iso()
    ok = await db_execute(
        TASKS_DB,
        """INSERT INTO tasks
           (id, title, description, type, subsystem, status,
            confidence_gap, is_exploration, created_at, updated_at,
            priority_score, staleness_hours, dependency_depth,
            last_subsystem_work_hours, attempt_count,
            dependencies, outputs, tags, metadata)
           VALUES (?,?,?,?,?,'pending',?,?,?,?,0.5,0.0,0,0.0,0,'[]','[]','[]','{}')""",
        (task_id, body.get("title","Untitled"), body.get("description",""),
         body.get("type","design"), body.get("subsystem","cross_cutting"),
         float(body.get("confidence_gap", 0.5)),
         1 if body.get("is_exploration") else 0,
         ts, ts)
    )
    return {"ok": ok, "id": task_id}


# ── Dead Letter Queue ─────────────────────────────────────────────────────────
@app.get("/api/dlq")
async def api_dlq():
    items = await db_query(
        DLQ_DB,
        "SELECT id, operation, error, status, created_at, retry_count, notes "
        "FROM dlq_items ORDER BY created_at DESC LIMIT 100"
    )
    stats_rows = await db_query(DLQ_DB, "SELECT status, COUNT(*) as count FROM dlq_items GROUP BY status")
    stats = {r["status"]: r["count"] for r in stats_rows}
    return {"items": items, "stats": stats}

@app.post("/api/dlq/{item_id}/resolve")
async def api_dlq_resolve(item_id: str, request: Request):
    body = await request.json()
    ts = now_iso()
    ok = await db_execute(
        DLQ_DB,
        "UPDATE dlq_items SET status='resolved', resolved_at=?, updated_at=?, notes=? WHERE id=?",
        (ts, ts, body.get("notes", "Resolved via web UI"), item_id)
    )
    return {"ok": ok}

@app.post("/api/dlq/{item_id}/discard")
async def api_dlq_discard(item_id: str, request: Request):
    body = await request.json()
    ts = now_iso()
    ok = await db_execute(
        DLQ_DB,
        "UPDATE dlq_items SET status='discarded', resolved_at=?, updated_at=?, notes=? WHERE id=?",
        (ts, ts, body.get("notes", "Discarded via web UI"), item_id)
    )
    return {"ok": ok}


# ── Backups ───────────────────────────────────────────────────────────────────
@app.get("/api/backups")
async def api_backups():
    backups = await asyncio.to_thread(list_backups)
    return {"backups": backups, "backup_dir": str(BACKUP_DIR)}

@app.post("/api/backups/trigger")
async def api_backups_trigger():
    """
    Run the backup CLI as a subprocess.
    Uses `python -m backup --backup` from the tinker root directory,
    which is the same mechanism as `python -m backup --backup` in the terminal.
    """
    from .core import BASE_DIR
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "backup", "--backup",
            cwd=str(BASE_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode == 0:
            msg = stdout.decode().strip() or "Backup created successfully."
            return {"ok": True, "message": msg}
        else:
            err = stderr.decode().strip() or "Backup failed (no output)."
            return JSONResponse({"ok": False, "error": err}, status_code=500)
    except asyncio.TimeoutError:
        return JSONResponse({"ok": False, "error": "Backup timed out after 120s."}, status_code=504)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Audit Log ─────────────────────────────────────────────────────────────────
@app.get("/api/audit")
async def api_audit(event_type: str = "", actor: str = "", trace_id: str = "",
                    page: int = 1, limit: int = 50):
    offset = (page - 1) * limit
    conditions, params = [], []
    if event_type:
        conditions.append("event_type = ?"); params.append(event_type)
    if actor:
        conditions.append("actor = ?"); params.append(actor)
    if trace_id:
        conditions.append("trace_id = ?"); params.append(trace_id)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    items = await db_query(
        AUDIT_DB,
        f"SELECT id, event_type, actor, resource, outcome, trace_id, created_at, details "
        f"FROM audit_events {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        tuple(params) + (limit, offset)
    )
    types_rows = await db_query(AUDIT_DB, "SELECT DISTINCT event_type FROM audit_events ORDER BY event_type")
    return {
        "items": items,
        "event_types": [r["event_type"] for r in types_rows],
        "page": page,
        "has_next": len(items) == limit,
    }


# ── Log streaming (SSE) ───────────────────────────────────────────────────────
@app.get("/api/logs/stream")
async def api_logs_stream(request: Request, level: str = "INFO"):
    """Server-Sent Events: polls tinker_state.json and emits updates."""
    LEVELS = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3}
    min_level = LEVELS.get(level.upper(), 1)

    async def gen() -> AsyncIterator[str]:
        last_micro = -1
        while True:
            if await request.is_disconnected():
                break
            state = load_state()
            totals = state.get("totals", {})
            micro = totals.get("micro", -1)
            if micro != last_micro:
                last_micro = micro
                # Derive last critic score from micro history (not a top-level key)
                micro_hist = state.get("micro_history", [])
                critic = micro_hist[-1].get("critic_score") if micro_hist else None
                evt = json.dumps({
                    "time": now_iso(),
                    "level": "INFO",
                    "micro_loops":  micro,
                    "meso_loops":   totals.get("meso", 0),
                    "macro_loops":  totals.get("macro", 0),
                    "current_task": state.get("current_task_id"),   # correct key
                    "critic_score": critic,
                    "current_level":   state.get("current_level"),
                    "current_subsystem": state.get("current_subsystem"),
                    "consecutive_failures": totals.get("consecutive_failures", 0),
                })
                yield f"data: {evt}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
