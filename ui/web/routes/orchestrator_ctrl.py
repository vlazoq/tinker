"""Orchestrator control endpoints: confirmations, pause, resume, mode switching."""

import json
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from runtime.orchestrator.config import SystemMode
from ui.core import load_state

router = APIRouter()


@router.get("/api/confirmations")
async def api_confirmations_list():
    """Return all pending confirmation requests visible in the state file."""
    state = load_state()
    return {
        "pending": state.get("pending_confirmations", []),
        "count": len(state.get("pending_confirmations", [])),
    }


def _sanitize_id(value: str) -> str:
    """Strip path separators and special characters to prevent path traversal."""
    name = Path(value).name  # discard any directory components
    if not name or name in (".", ".."):
        raise ValueError(f"Invalid identifier: {value!r}")
    return name


@router.post("/api/confirm/{request_id}")
async def api_confirm(request_id: str, request: Request):
    """Approve or deny a pending confirmation request."""
    try:
        request_id = _sanitize_id(request_id)
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid request_id"}, status_code=400)

    body = await request.json()
    approved = bool(body.get("approved", False))

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


@router.post("/api/pause")
async def api_pause():
    """Request the orchestrator to pause between micro loops."""
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

    return {
        "ok": True,
        "message": "Pause requested. Orchestrator will pause after current micro loop.",
    }


@router.post("/api/resume")
async def api_resume():
    """Remove the pause control file, signalling the orchestrator to resume."""
    control_dir = Path(os.getenv("TINKER_CONTROL_DIR", "./tinker_control"))
    ctrl_path = control_dir / "pause.json"
    try:
        ctrl_path.unlink(missing_ok=True)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    return {"ok": True, "message": "Resume requested."}


@router.get("/api/mcp/status")
async def api_mcp_status():
    """Return the status of the MCP server and connected external MCP clients."""
    # The MCP bridge is attached to the app state when main.py starts the webui.
    # We import app lazily to avoid circular imports.
    from ui.web.app import app

    bridge = getattr(app.state, "mcp_bridge", None)
    if bridge is None:
        return {"enabled": False, "message": "MCP not enabled or bridge not wired"}
    return bridge.status()


# ── System mode switching ────────────────────────────────────────────────────
# The orchestrator reads mode.json from the control directory on each micro
# loop iteration, allowing runtime mode switching without restart.

def _mode_file() -> Path:
    control_dir = Path(os.getenv("TINKER_CONTROL_DIR", "./tinker_control"))
    control_dir.mkdir(parents=True, exist_ok=True)
    return control_dir / "mode.json"


def _read_mode() -> dict:
    """Read the current mode from the control file."""
    path = _mode_file()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {"system_mode": "architect", "research_topic": ""}


@router.get("/api/mode")
async def api_mode_get():
    """Return the current system mode (architect or research)."""
    mode_data = _read_mode()
    # Also include the live state if available
    state = load_state()
    return {
        "system_mode": mode_data.get("system_mode", "architect"),
        "research_topic": mode_data.get("research_topic", ""),
        "valid_modes": [m.value for m in SystemMode],
        "orchestrator_running": state.get("status") == "running",
    }


@router.post("/api/mode")
async def api_mode_set(request: Request):
    """Switch the system mode at runtime.

    Body: {"system_mode": "architect"|"research", "research_topic": "..."}
    """
    body = await request.json()
    new_mode = body.get("system_mode", "architect")
    research_topic = body.get("research_topic", "")

    valid_modes = tuple(m.value for m in SystemMode)
    if new_mode not in valid_modes:
        return JSONResponse(
            {"ok": False, "error": f"Invalid mode '{new_mode}'. Must be one of {valid_modes}"},
            status_code=422,
        )

    if new_mode == "research" and not research_topic.strip():
        return JSONResponse(
            {"ok": False, "error": "research_topic is required when switching to research mode"},
            status_code=422,
        )

    mode_data = {
        "system_mode": new_mode,
        "research_topic": research_topic.strip(),
    }

    path = _mode_file()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(mode_data, f)
        os.replace(tmp, str(path))
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    label = "research" if new_mode == "research" else "architect"
    msg = f"Switched to {label} mode."
    if new_mode == "research":
        msg += f" Research topic: {research_topic.strip()}"
    msg += " Takes effect on the next micro loop iteration."

    return {"ok": True, "system_mode": new_mode, "research_topic": research_topic.strip(), "message": msg}


@router.get("/api/research/status")
async def api_research_status():
    """Return the current research crawler status and knowledge pool stats."""
    mode_data = _read_mode()
    state = load_state()
    return {
        "system_mode": mode_data.get("system_mode", "architect"),
        "research_topic": mode_data.get("research_topic", ""),
        "has_pool_context": bool(state.get("research_pool_context")),
        "pool_context_length": len(state.get("research_pool_context", "") or ""),
    }
