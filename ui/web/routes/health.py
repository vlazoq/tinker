"""Health, state, and version endpoints."""

from fastapi import APIRouter

from ui.core import fetch_health, fetch_grub_status, load_state

router = APIRouter()


@router.get("/api/version")
async def api_version():
    """Return the Tinker API version and schema version for client compatibility checks."""
    return {
        "api_version": "v1",
        "schema_version": 1,
        "app": "tinker-webui",
    }


@router.get("/api/health")
async def api_health():
    return await fetch_health()


@router.get("/api/state")
async def api_state():
    return load_state()


@router.get("/api/grub/status")
async def api_grub_status():
    """Return Grub pipeline status: task counts, queue stats, recent artifacts."""
    return await fetch_grub_status()
