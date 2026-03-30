"""Backup management endpoints."""

import asyncio
import sys

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ui.core import BACKUP_DIR, BASE_DIR, list_backups

router = APIRouter()


@router.get("/api/backups")
async def api_backups():
    backups = await asyncio.to_thread(list_backups)
    return {"backups": backups, "backup_dir": str(BACKUP_DIR)}


@router.post("/api/backups/trigger")
async def api_backups_trigger():
    """Run the backup CLI as a subprocess."""
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
    except TimeoutError:
        return JSONResponse(
            {"ok": False, "error": "Backup timed out after 120s."}, status_code=504
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
