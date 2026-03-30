"""Configuration and feature flags endpoints."""

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ui.core import (
    FLAG_DEFAULTS,
    FLAG_DESCRIPTIONS,
    FLAG_GROUPS,
    FLAGS_FILE,
    ORCH_CONFIG_SCHEMA,
    STAGNATION_CONFIG_SCHEMA,
    load_config,
    load_flags,
    save_config,
    save_flags,
)

router = APIRouter()


@router.get("/api/config")
async def api_config_get():
    saved = load_config()
    result: dict[str, Any] = {
        "_saved_at": saved.get("_saved_at"),
        "orchestrator": {},
        "stagnation": {},
    }
    for _section_key, section in ORCH_CONFIG_SCHEMA.items():
        for field_name, meta in section["fields"].items():
            result["orchestrator"][field_name] = saved.get(field_name, meta["default"])
    for section_key, section in STAGNATION_CONFIG_SCHEMA.items():
        result["stagnation"][section_key] = {}
        for field_name, meta in section["fields"].items():
            stag = saved.get("stagnation", {})
            result["stagnation"][section_key][field_name] = stag.get(section_key, {}).get(
                field_name, meta["default"]
            )
    result["_schema"] = {
        "orchestrator": ORCH_CONFIG_SCHEMA,
        "stagnation": STAGNATION_CONFIG_SCHEMA,
    }
    return result


@router.post("/api/config")
async def api_config_save(body: dict | None = None, request: Request = None):
    data = await request.json()
    errors: list[str] = []
    to_save: dict[str, Any] = {}

    for _section_key, section in ORCH_CONFIG_SCHEMA.items():
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
                errors.append(f"Stagnation {section_key}.{meta['label']}: invalid value '{raw}'")

    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=422)

    to_save["stagnation"] = stagnation_save
    save_config(to_save)
    return {
        "ok": True,
        "message": "Config saved. Restart the orchestrator to apply changes.",
    }


@router.get("/api/flags")
async def api_flags_get():
    flags = load_flags()
    return {
        "flags": flags,
        "groups": FLAG_GROUPS,
        "descriptions": FLAG_DESCRIPTIONS,
        "flags_file": str(FLAGS_FILE),
    }


@router.post("/api/flags/{flag_name}")
async def api_flags_toggle(flag_name: str, request: Request):
    body = await request.json()
    enabled = bool(body.get("enabled", False))
    flags = load_flags()
    if flag_name not in FLAG_DEFAULTS:
        return JSONResponse({"ok": False, "error": f"Unknown flag: {flag_name}"}, status_code=404)
    flags[flag_name] = enabled
    save_flags(flags)
    return {
        "ok": True,
        "flag": flag_name,
        "enabled": enabled,
        "message": f"Flag '{flag_name}' set to {'enabled' if enabled else 'disabled'}. Takes effect within 30s.",
    }
