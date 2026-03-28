"""Model library, presets, and Ollama sync endpoints."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


def _get_library():
    from core.models.library import ModelLibrary
    return ModelLibrary()


def _get_preset_manager():
    from core.models.library import ModelLibrary
    from core.models.presets import PresetManager
    lib = ModelLibrary()
    return PresetManager(lib), lib


@router.get("/api/models/library")
async def api_models_library_list():
    """List all models in the local model library."""
    lib = _get_library()
    return {"models": [m.to_dict() for m in lib.all()], "count": len(lib)}


@router.post("/api/models/library")
async def api_models_library_add(request: Request):
    """Add or update a model in the library."""
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


@router.delete("/api/models/library/{model_id}")
async def api_models_library_remove(model_id: str):
    """Remove a model from the library by its id."""
    lib = _get_library()
    removed = lib.remove(model_id)
    if not removed:
        return JSONResponse({"ok": False, "error": f"Model '{model_id}' not found"}, status_code=404)
    return {"ok": True, "removed_id": model_id}


@router.get("/api/models/presets")
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


@router.post("/api/models/presets")
async def api_models_presets_create(request: Request):
    """Create or update a preset."""
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


@router.put("/api/models/presets/{name}")
async def api_models_presets_update(name: str, request: Request):
    """Update an existing preset."""
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


@router.delete("/api/models/presets/{name}")
async def api_models_presets_delete(name: str):
    """Delete a preset by name."""
    mgr, _ = _get_preset_manager()
    removed = mgr.remove(name)
    if not removed:
        return JSONResponse({"ok": False, "error": f"Preset '{name}' not found"}, status_code=404)
    return {"ok": True, "removed": name}


@router.post("/api/models/presets/{name}/activate")
async def api_models_presets_activate(name: str):
    """Activate a preset. The orchestrator hot-reloads at next loop boundary."""
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


@router.get("/api/models/active")
async def api_models_active():
    """Return the currently active preset with resolved model details."""
    mgr, _ = _get_preset_manager()
    return mgr.resolved_active()


@router.get("/api/models/ollama/available")
async def api_models_ollama_available(urls: str = ""):
    """Query one or more Ollama servers for available models."""
    from core.models.ollama_sync import OllamaSync
    lib = _get_library()
    known_tags = {m.model_tag for m in lib.all()}
    server_urls = [u.strip() for u in urls.split(",") if u.strip()] or ["http://localhost:11434"]
    sync = OllamaSync(server_urls)
    try:
        models = await sync.discover_all()
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc), "models": []}, status_code=500)
    for m in models:
        m["in_library"] = m["model_tag"] in known_tags
    return {"ok": True, "models": models, "servers_queried": server_urls}


@router.post("/api/models/ollama/sync")
async def api_models_ollama_sync(request: Request):
    """Import selected models from Ollama into the library."""
    from core.models.library import ModelEntry
    from core.models.ollama_sync import OllamaSync
    body = await request.json()
    lib = _get_library()
    to_import = body.get("models", [])
    added, skipped = [], []
    for m in to_import:
        suggested_id = m.get("suggested_id", "")
        if not suggested_id:
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
