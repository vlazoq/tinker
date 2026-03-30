"""Orchestrator control endpoints: confirmations, pause, resume."""

import json
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

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


@router.post("/api/confirm/{request_id}")
async def api_confirm(request_id: str, request: Request):
    """Approve or deny a pending confirmation request."""
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
