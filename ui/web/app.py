"""
ui/web/app.py
─────────────
FastAPI backend for the Tinker Web UI.
All API routes return JSON; the React SPA (index.html) consumes them.

Run:  python -m tinker.ui.web          (default port 8082)
      TINKER_WEBUI_PORT=9000 python -m tinker.ui.web
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from infra.resilience.rate_limiter import TokenBucketRateLimiter

from ui.core import (
    ORCH_CONFIG_SCHEMA,
    STAGNATION_CONFIG_SCHEMA,
    FLAG_DEFAULTS,
    FLAG_DESCRIPTIONS,
    FLAG_GROUPS,
    TASK_TYPES,
    SUBSYSTEMS,
    AUDIT_DB,
    BACKUP_DIR,
    DLQ_DB,
    FLAGS_FILE,
    FRITZ_CONFIG_FILE,
    TASKS_DB,
    db_execute,
    db_query,
    fetch_fritz_status,
    fetch_grub_status,
    fetch_health,
    list_backups,
    load_config,
    load_flags,
    load_state,
    new_id,
    now_iso,
    save_config,
    save_flags,
)

# ── Per-IP HTTP rate limiting ─────────────────────────────────────────────────
# Read limits from environment so operators can tune without code changes.
# Defaults are generous for local / homelab use: 2 req/s steady, burst of 30.
# Lower these if the UI is exposed to an untrusted network.
_WEBUI_RATE_PER_SEC: float = float(os.getenv("TINKER_WEBUI_RATE_PER_SEC", "2.0"))
_WEBUI_RATE_BURST: float = float(os.getenv("TINKER_WEBUI_RATE_BURST", "30.0"))

# Registry of per-IP token-bucket rate limiters.
# Lazily created on first request from each IP; lives for the process lifetime.
# For a homelab deployment the number of distinct IPs is tiny, so unbounded
# growth is not a concern.
_ip_limiters: dict[str, TokenBucketRateLimiter] = {}
_ip_limiters_lock = asyncio.Lock()


async def _limiter_for_ip(ip: str) -> TokenBucketRateLimiter:
    """Return (lazily creating) the token-bucket rate limiter for *ip*."""
    async with _ip_limiters_lock:
        if ip not in _ip_limiters:
            _ip_limiters[ip] = TokenBucketRateLimiter(
                name=f"webui_ip:{ip}",
                rate=_WEBUI_RATE_PER_SEC,
                burst=_WEBUI_RATE_BURST,
            )
        return _ip_limiters[ip]


class _APIRateLimitMiddleware(BaseHTTPMiddleware):
    """
    Token-bucket per-IP rate limiting for ``/api/*`` endpoints.

    Why only /api/?
    ---------------
    Static assets and the SPA shell are served from disk and are not a
    meaningful attack surface.  Applying rate limiting there would break
    normal browser refreshes.  Only programmatic API consumers (scripts,
    dashboards, other services) are throttled.

    Response headers on 429
    -----------------------
    ``Retry-After``       : seconds until the bucket will have tokens again.
    ``X-RateLimit-Limit`` : burst capacity (maximum tokens).
    ``X-RateLimit-Reset`` : Unix timestamp when the client may retry.

    These headers allow any well-behaved HTTP client or browser to back off
    gracefully without hard-coding retry logic.

    Configuration
    -------------
    TINKER_WEBUI_RATE_PER_SEC  — steady-state requests/second per IP (default 2)
    TINKER_WEBUI_RATE_BURST    — burst capacity in requests (default 30)
    """

    async def dispatch(self, request: Request, call_next):
        # Skip rate limiting for non-API paths (static files, SPA shell, etc.)
        if not request.url.path.startswith("/api/"):
            return await call_next(request)

        ip = request.client.host if request.client else "127.0.0.1"
        limiter = await _limiter_for_ip(ip)
        acquired, retry_after = await limiter.try_acquire()

        if not acquired:
            # Round up so clients never retry too soon.
            retry_secs = max(1, int(retry_after) + 1)
            reset_at = int(time.time()) + retry_secs
            return JSONResponse(
                status_code=429,
                content={
                    "error": "Too Many Requests",
                    "detail": (
                        f"API rate limit exceeded. "
                        f"Try again in {retry_secs}s."
                    ),
                },
                headers={
                    "Retry-After": str(retry_secs),
                    "X-RateLimit-Limit": str(int(_WEBUI_RATE_BURST)),
                    "X-RateLimit-Reset": str(reset_at),
                },
            )

        return await call_next(request)


# ── App setup ─────────────────────────────────────────────────────────────────
app = FastAPI(title="Tinker Web UI", docs_url="/api/docs", redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# Rate limiting runs outside CORS so browsers still receive CORS headers even
# on 429 responses.  Starlette applies middleware in reverse registration order
# (last-added = outermost), so we add rate limiting after CORS.
app.add_middleware(_APIRateLimitMiddleware)

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
    result: dict[str, Any] = {
        "_saved_at": saved.get("_saved_at"),
        "orchestrator": {},
        "stagnation": {},
    }
    for section_key, section in ORCH_CONFIG_SCHEMA.items():
        for field_name, meta in section["fields"].items():
            result["orchestrator"][field_name] = saved.get(field_name, meta["default"])
    for section_key, section in STAGNATION_CONFIG_SCHEMA.items():
        result["stagnation"][section_key] = {}
        for field_name, meta in section["fields"].items():
            stag = saved.get("stagnation", {})
            result["stagnation"][section_key][field_name] = stag.get(
                section_key, {}
            ).get(field_name, meta["default"])
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
                    errors.append(
                        f"Stagnation {section_key}.{meta['label']} must be >= {meta['min']}"
                    )
                else:
                    stagnation_save[section_key][field_name] = val
            except (ValueError, TypeError):
                errors.append(
                    f"Stagnation {section_key}.{meta['label']}: invalid value '{raw}'"
                )

    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=422)

    to_save["stagnation"] = stagnation_save
    save_config(to_save)
    return {
        "ok": True,
        "message": "Config saved. Restart the orchestrator to apply changes.",
    }


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
        return JSONResponse(
            {"ok": False, "error": f"Unknown flag: {flag_name}"}, status_code=404
        )
    flags[flag_name] = enabled
    save_flags(flags)
    return {
        "ok": True,
        "flag": flag_name,
        "enabled": enabled,
        "message": f"Flag '{flag_name}' set to {'enabled' if enabled else 'disabled'}. Takes effect within 30s.",
    }


# ── Orchestrator control (pause / resume / confirm) ───────────────────────────
# These endpoints let the Dashboard control the running orchestrator.
# They read the shared state file written by the orchestrator process and
# respond via a simple "pending_confirmations" list in that file.
# The orchestrator process polls for responses when it is waiting in
# ConfirmationGate._api_wait().

@app.get("/api/confirmations")
async def api_confirmations_list():
    """Return all pending confirmation requests visible in the state file."""
    state = load_state()
    return {
        "pending": state.get("pending_confirmations", []),
        "count": len(state.get("pending_confirmations", [])),
    }


@app.post("/api/confirm/{request_id}")
async def api_confirm(request_id: str, request: Request):
    """
    Approve or deny a pending confirmation request.

    Body: {"approved": true|false}

    The orchestrator's ConfirmationGate is waiting on an asyncio Event keyed
    by request_id.  Writing the decision to the shared state file is not
    enough — the orchestrator needs an in-process signal.

    Since the webui and orchestrator typically run in separate processes, the
    simplest approach is a small "response file" that the orchestrator polls.
    The orchestrator checks for this file in its _api_wait loop.
    """
    import os, json, tempfile
    from pathlib import Path

    body = await request.json()
    approved = bool(body.get("approved", False))

    # Write a response file that the orchestrator's ConfirmationGate polls.
    # The orchestrator deletes this file once it reads it.
    response_dir = Path(os.getenv("TINKER_CONFIRM_DIR", "./tinker_confirmations"))
    response_dir.mkdir(parents=True, exist_ok=True)
    response_path = response_dir / f"{request_id}.json"

    data = {"request_id": request_id, "approved": approved}
    fd, tmp = tempfile.mkstemp(dir=str(response_dir), suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, str(response_path))
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    verdict = "approved" if approved else "denied"
    return {"ok": True, "request_id": request_id, "verdict": verdict}


@app.post("/api/pause")
async def api_pause():
    """
    Request the orchestrator to pause between micro loops.

    The orchestrator reads the state file and acts on the pause flag.
    Since this endpoint runs in a separate webui process, it writes a
    control file that the orchestrator watches.
    """
    import os, json, tempfile
    from pathlib import Path

    control_dir = Path(os.getenv("TINKER_CONTROL_DIR", "./tinker_control"))
    control_dir.mkdir(parents=True, exist_ok=True)
    ctrl_path = control_dir / "pause.json"

    fd, tmp = tempfile.mkstemp(dir=str(control_dir), suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"action": "pause"}, f)
        os.replace(tmp, str(ctrl_path))
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    return {"ok": True, "message": "Pause requested. Orchestrator will pause after current micro loop."}


@app.post("/api/resume")
async def api_resume():
    """Remove the pause control file, signalling the orchestrator to resume."""
    import os
    from pathlib import Path

    control_dir = Path(os.getenv("TINKER_CONTROL_DIR", "./tinker_control"))
    ctrl_path = control_dir / "pause.json"
    try:
        ctrl_path.unlink(missing_ok=True)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    return {"ok": True, "message": "Resume requested."}


# ── MCP status ────────────────────────────────────────────────────────────────
@app.get("/api/mcp/status")
async def api_mcp_status():
    """
    Return the status of the MCP server and any connected external MCP clients.

    If MCP is not enabled, returns {"enabled": false}.
    If enabled, returns server info and a list of imported external tools.
    """
    # The MCP bridge is attached to the app state when main.py starts the webui.
    bridge = getattr(app.state, "mcp_bridge", None)
    if bridge is None:
        return {"enabled": False, "message": "MCP not enabled or bridge not wired"}
    return bridge.status()


# ── Task Queue ────────────────────────────────────────────────────────────────
@app.get("/api/tasks")
async def api_tasks():
    tasks = await db_query(
        TASKS_DB,
        "SELECT id, title, type, subsystem, status, priority_score, "
        "created_at, attempt_count, is_exploration, description "
        "FROM tasks ORDER BY priority_score DESC, created_at ASC LIMIT 200",
    )
    stats_rows = await db_query(
        TASKS_DB, "SELECT status, COUNT(*) as count FROM tasks GROUP BY status"
    )
    stats = {r["status"]: r["count"] for r in stats_rows}
    return {
        "tasks": tasks,
        "stats": stats,
        "task_types": TASK_TYPES,
        "subsystems": SUBSYSTEMS,
    }


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
        (
            task_id,
            body.get("title", "Untitled"),
            body.get("description", ""),
            body.get("type", "design"),
            body.get("subsystem", "cross_cutting"),
            float(body.get("confidence_gap", 0.5)),
            1 if body.get("is_exploration") else 0,
            ts,
            ts,
        ),
    )
    return {"ok": ok, "id": task_id}


# ── Dead Letter Queue ─────────────────────────────────────────────────────────
@app.get("/api/dlq")
async def api_dlq():
    items = await db_query(
        DLQ_DB,
        "SELECT id, operation, error, status, created_at, retry_count, notes "
        "FROM dlq_items ORDER BY created_at DESC LIMIT 100",
    )
    stats_rows = await db_query(
        DLQ_DB, "SELECT status, COUNT(*) as count FROM dlq_items GROUP BY status"
    )
    stats = {r["status"]: r["count"] for r in stats_rows}
    return {"items": items, "stats": stats}


@app.post("/api/dlq/{item_id}/resolve")
async def api_dlq_resolve(item_id: str, request: Request):
    body = await request.json()
    ts = now_iso()
    ok = await db_execute(
        DLQ_DB,
        "UPDATE dlq_items SET status='resolved', resolved_at=?, updated_at=?, notes=? WHERE id=?",
        (ts, ts, body.get("notes", "Resolved via web UI"), item_id),
    )
    return {"ok": ok}


@app.post("/api/dlq/{item_id}/discard")
async def api_dlq_discard(item_id: str, request: Request):
    body = await request.json()
    ts = now_iso()
    ok = await db_execute(
        DLQ_DB,
        "UPDATE dlq_items SET status='discarded', resolved_at=?, updated_at=?, notes=? WHERE id=?",
        (ts, ts, body.get("notes", "Discarded via web UI"), item_id),
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
    from ui.core import BASE_DIR

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "backup",
            "--backup",
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
        return JSONResponse(
            {"ok": False, "error": "Backup timed out after 120s."}, status_code=504
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Audit Log ─────────────────────────────────────────────────────────────────
@app.get("/api/audit")
async def api_audit(
    event_type: str = "",
    actor: str = "",
    trace_id: str = "",
    page: int = 1,
    limit: int = 50,
):
    offset = (page - 1) * limit
    conditions, params = [], []
    if event_type:
        conditions.append("event_type = ?")
        params.append(event_type)
    if actor:
        conditions.append("actor = ?")
        params.append(actor)
    if trace_id:
        conditions.append("trace_id = ?")
        params.append(trace_id)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    items = await db_query(
        AUDIT_DB,
        f"SELECT id, event_type, actor, resource, outcome, trace_id, created_at, details "
        f"FROM audit_events {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        tuple(params) + (limit, offset),
    )
    types_rows = await db_query(
        AUDIT_DB, "SELECT DISTINCT event_type FROM audit_events ORDER BY event_type"
    )
    return {
        "items": items,
        "event_types": [r["event_type"] for r in types_rows],
        "page": page,
        "has_next": len(items) == limit,
    }


# ── Fritz ─────────────────────────────────────────────────────────────────────
@app.get("/api/fritz/status")
async def api_fritz_status():
    """Return Fritz config + live git state (branch, SHA, dirty files, remotes)."""
    return await fetch_fritz_status()


@app.post("/api/fritz/ship")
async def api_fritz_ship(request: Request):
    """
    Run Fritz commit-and-ship pipeline.
    Body: { message, task_id, task_description, auto_merge }
    """
    body = await request.json()
    try:
        from ui.core import BASE_DIR as _BASE_DIR
        from agents.fritz.config import FritzConfig
        from agents.fritz.agent import FritzAgent

        config = (
            FritzConfig.from_file(FRITZ_CONFIG_FILE)
            if FRITZ_CONFIG_FILE.exists()
            else FritzConfig()
        )
        agent = FritzAgent(config)
        await agent.setup()
        result = await agent.commit_and_ship(
            message=body.get("message", "chore: automated commit by Fritz"),
            task_id=body.get("task_id", "webui"),
            task_description=body.get("task_description", ""),
            auto_merge=bool(body.get("auto_merge", False)),
        )
        return {
            "ok": result.ok,
            "branch": result.branch,
            "commit_sha": result.commit_sha,
            "pr_url": result.pr_url,
            "pr_number": result.pr_number,
            "merged": result.merged,
            "direct_push": result.direct_push,
            "errors": result.errors,
        }
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/api/fritz/push")
async def api_fritz_push(request: Request):
    """
    Push the current (or specified) branch.
    Body: { branch }
    """
    body = await request.json()
    try:
        from agents.fritz.config import FritzConfig
        from agents.fritz.agent import FritzAgent

        config = (
            FritzConfig.from_file(FRITZ_CONFIG_FILE)
            if FRITZ_CONFIG_FILE.exists()
            else FritzConfig()
        )
        agent = FritzAgent(config)
        await agent.setup()
        branch = body.get("branch") or await agent.git.current_branch()
        result = await agent.push(branch=branch)
        return {"ok": result.ok, "branch": branch, "error": result.stderr}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/api/fritz/pr")
async def api_fritz_create_pr(request: Request):
    """
    Create a pull request on GitHub or Gitea.
    Body: { title, body, head, base, platform }
    """
    body = await request.json()
    try:
        from agents.fritz.config import FritzConfig
        from agents.fritz.agent import FritzAgent

        config = (
            FritzConfig.from_file(FRITZ_CONFIG_FILE)
            if FRITZ_CONFIG_FILE.exists()
            else FritzConfig()
        )
        agent = FritzAgent(config)
        await agent.setup()
        result = await agent.create_pr(
            title=body.get("title", ""),
            body=body.get("body", ""),
            head=body.get("head", ""),
            base=body.get("base"),
            platform=body.get("platform", "auto"),
        )
        return {"ok": result.ok, "url": result.url, "error": result.error, "data": result.data}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.get("/api/fritz/verify")
async def api_fritz_verify():
    """Test GitHub and Gitea credentials. Returns {github: bool, gitea: bool}."""
    try:
        from agents.fritz.config import FritzConfig
        from agents.fritz.agent import FritzAgent

        config = (
            FritzConfig.from_file(FRITZ_CONFIG_FILE)
            if FRITZ_CONFIG_FILE.exists()
            else FritzConfig()
        )
        agent = FritzAgent(config)
        await agent.setup()
        results = await agent.verify_connections()
        return {"ok": True, "connections": results}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Model Library & Presets ───────────────────────────────────────────────────
# All model management is local/LAN-first: models point to Ollama instances.
# The library and presets live in plain JSON files (tinker_models.json,
# tinker_presets.json, tinker_active_preset.json).


def _get_library():
    from core.models.library import ModelLibrary
    return ModelLibrary()


def _get_preset_manager():
    from core.models.library import ModelLibrary
    from core.models.presets import PresetManager
    lib = ModelLibrary()
    return PresetManager(lib), lib


@app.get("/api/models/library")
async def api_models_library_list():
    """List all models in the local model library."""
    lib = _get_library()
    return {"models": [m.to_dict() for m in lib.all()], "count": len(lib)}


@app.post("/api/models/library")
async def api_models_library_add(request: Request):
    """
    Add or update a model in the library.

    Body fields: id, model_tag, display_name, ollama_url, context_window, notes, capabilities
    """
    from core.models.library import ModelEntry
    body = await request.json()
    if not body.get("id") or not body.get("model_tag"):
        return JSONResponse({"ok": False, "error": "id and model_tag are required"}, status_code=422)
    lib = _get_library()
    entry = ModelEntry(
        id=body["id"],
        model_tag=body["model_tag"],
        display_name=body.get("display_name", body["model_tag"]),
        ollama_url=body.get("ollama_url", "http://localhost:11434"),
        context_window=int(body.get("context_window", 8192)),
        notes=body.get("notes", ""),
        capabilities=list(body.get("capabilities", [])),
    )
    lib.add(entry)
    return {"ok": True, "model": entry.to_dict()}


@app.delete("/api/models/library/{model_id}")
async def api_models_library_remove(model_id: str):
    """Remove a model from the library by its id."""
    lib = _get_library()
    removed = lib.remove(model_id)
    if not removed:
        return JSONResponse({"ok": False, "error": f"Model '{model_id}' not found"}, status_code=404)
    return {"ok": True, "removed_id": model_id}


@app.get("/api/models/presets")
async def api_models_presets_list():
    """List all saved model presets."""
    mgr, lib = _get_preset_manager()
    active_name = mgr.active_name()
    presets = []
    for p in mgr.all():
        d = p.to_dict()
        d["is_active"] = p.name == active_name
        main = lib.get(p.main_model_id)
        judge = lib.get(p.judge_model_id)
        d["main_model"] = main.to_dict() if main else None
        d["judge_model"] = judge.to_dict() if judge else None
        presets.append(d)
    return {"presets": presets, "active": active_name}


@app.post("/api/models/presets")
async def api_models_presets_create(request: Request):
    """
    Create or update a preset.

    Body fields: name, display_name, description, main_model_id, judge_model_id,
                 grub_overrides (dict), notes
    """
    from core.models.presets import ModelPreset
    body = await request.json()
    if not body.get("name"):
        return JSONResponse({"ok": False, "error": "name is required"}, status_code=422)
    mgr, _ = _get_preset_manager()
    preset = ModelPreset(
        name=body["name"],
        display_name=body.get("display_name", body["name"].title()),
        description=body.get("description", ""),
        main_model_id=body.get("main_model_id", ""),
        judge_model_id=body.get("judge_model_id", ""),
        grub_overrides=dict(body.get("grub_overrides", {})),
        notes=body.get("notes", ""),
    )
    mgr.add(preset)
    return {"ok": True, "preset": preset.to_dict()}


@app.put("/api/models/presets/{name}")
async def api_models_presets_update(name: str, request: Request):
    """Update an existing preset (same as POST but requires it to exist)."""
    from core.models.presets import ModelPreset
    body = await request.json()
    mgr, _ = _get_preset_manager()
    if mgr.get(name) is None:
        return JSONResponse({"ok": False, "error": f"Preset '{name}' not found"}, status_code=404)
    preset = ModelPreset(
        name=name,
        display_name=body.get("display_name", name.title()),
        description=body.get("description", ""),
        main_model_id=body.get("main_model_id", ""),
        judge_model_id=body.get("judge_model_id", ""),
        grub_overrides=dict(body.get("grub_overrides", {})),
        notes=body.get("notes", ""),
    )
    mgr.add(preset)
    return {"ok": True, "preset": preset.to_dict()}


@app.delete("/api/models/presets/{name}")
async def api_models_presets_delete(name: str):
    """Delete a preset by name."""
    mgr, _ = _get_preset_manager()
    removed = mgr.remove(name)
    if not removed:
        return JSONResponse({"ok": False, "error": f"Preset '{name}' not found"}, status_code=404)
    return {"ok": True, "removed": name}


@app.post("/api/models/presets/{name}/activate")
async def api_models_presets_activate(name: str):
    """
    Activate a preset.

    Writes ``tinker_active_preset.json``.  The running orchestrator detects
    the file change at its next meso-loop boundary and hot-reloads the models.
    No restart required.
    """
    mgr, lib = _get_preset_manager()
    try:
        preset = mgr.activate(name)
    except KeyError:
        return JSONResponse({"ok": False, "error": f"Preset '{name}' not found"}, status_code=404)
    main = lib.get(preset.main_model_id)
    judge = lib.get(preset.judge_model_id)
    return {
        "ok": True,
        "activated": name,
        "main_model": main.to_dict() if main else None,
        "judge_model": judge.to_dict() if judge else None,
        "message": f"Preset '{name}' activated. The orchestrator will apply it at the next loop boundary.",
    }


@app.get("/api/models/active")
async def api_models_active():
    """Return the currently active preset with resolved model details."""
    mgr, _ = _get_preset_manager()
    return mgr.resolved_active()


@app.get("/api/models/ollama/available")
async def api_models_ollama_available(urls: str = ""):
    """
    Query one or more Ollama servers for available models.

    Query param ``urls``: comma-separated list of Ollama base URLs.
    Defaults to ``http://localhost:11434``.

    Returns discovered models with metadata (size, family, quantization).
    Also marks which models are already in the library (``in_library`` field).
    """
    from core.models.ollama_sync import OllamaSync
    lib = _get_library()
    known_tags = {m.model_tag for m in lib.all()}
    server_urls = [u.strip() for u in urls.split(",") if u.strip()] or ["http://localhost:11434"]
    sync = OllamaSync(server_urls)
    try:
        models = await sync.discover_all()
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc), "models": []}, status_code=500)
    # Mark which are already in the library
    for m in models:
        m["in_library"] = m["model_tag"] in known_tags
    return {"ok": True, "models": models, "servers_queried": server_urls}


@app.post("/api/models/ollama/sync")
async def api_models_ollama_sync(request: Request):
    """
    Import selected models from Ollama into the library.

    Body: {"models": [{"model_tag": "qwen3:7b", "ollama_url": "...", ...}]}

    Each entry in ``models`` is used to create a ModelEntry.  If a model with
    the same ``suggested_id`` already exists in the library, it is skipped.
    """
    from core.models.library import ModelEntry
    from core.models.ollama_sync import OllamaSync
    body = await request.json()
    lib = _get_library()
    to_import = body.get("models", [])
    added, skipped = [], []
    for m in to_import:
        suggested_id = m.get("suggested_id", "")
        if not suggested_id:
            # Build one from tag
            suggested_id = m.get("model_tag", "unknown").replace(":", "-").replace(".", "")
        if lib.get(suggested_id):
            skipped.append(suggested_id)
            continue
        caps = OllamaSync._infer_capabilities(m.get("family", ""), m.get("model_tag", ""))
        from core.models.ollama_sync import _infer_context_window
        ctx = _infer_context_window(m.get("model_tag", ""), m.get("parameter_size", ""))
        entry = ModelEntry(
            id=suggested_id,
            model_tag=m["model_tag"],
            display_name=m.get("display_name", m["model_tag"]),
            ollama_url=m.get("ollama_url", "http://localhost:11434"),
            context_window=ctx,
            notes=f"{m.get('size_gb', 0):.2f} GB — {m.get('quantization', '')}",
            capabilities=caps,
        )
        lib.add(entry)
        added.append(suggested_id)
    return {"ok": True, "added": added, "skipped": skipped}


# ── Log streaming (SSE) ───────────────────────────────────────────────────────
@app.get("/api/logs/stream")
async def api_logs_stream(request: Request, level: str = "INFO"):
    """Server-Sent Events: polls tinker_state.json and emits updates."""

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
                evt = json.dumps(
                    {
                        "time": now_iso(),
                        "level": "INFO",
                        "micro_loops": micro,
                        "meso_loops": totals.get("meso", 0),
                        "macro_loops": totals.get("macro", 0),
                        "current_task": state.get("current_task_id"),  # correct key
                        "critic_score": critic,
                        "current_level": state.get("current_level"),
                        "current_subsystem": state.get("current_subsystem"),
                        "consecutive_failures": totals.get("consecutive_failures", 0),
                    }
                )
                yield f"data: {evt}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
