"""Health, state, and version endpoints."""

import contextlib

from fastapi import APIRouter

from ui.core import fetch_grub_status, fetch_health, load_state

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


@router.get("/api/health/components")
async def api_health_components():
    """Report status of optional components: research enhancer, webhooks, auto-memory."""
    import json
    import os

    components = {}

    # Research Enhancer
    components["research_enhancer"] = {
        "enabled": True,
        "query_rewrite": os.getenv("TINKER_RESEARCH_QUERY_REWRITE", "true").lower() == "true",
        "memory_first": os.getenv("TINKER_RESEARCH_MEMORY_FIRST", "true").lower() == "true",
        "summarize": os.getenv("TINKER_RESEARCH_SUMMARIZE", "true").lower() == "true",
        "iterative_rounds": int(os.getenv("TINKER_RESEARCH_ITERATIVE_ROUNDS", "2")),
        "num_results": int(os.getenv("TINKER_RESEARCH_NUM_RESULTS", "10")),
        "max_scrape": int(os.getenv("TINKER_RESEARCH_MAX_SCRAPE", "5")),
    }

    # Webhook Dispatcher
    raw_endpoints = os.getenv("TINKER_WEBHOOK_ENDPOINTS", "")
    webhook_endpoints = []
    if raw_endpoints.strip():
        with contextlib.suppress(json.JSONDecodeError):
            webhook_endpoints = json.loads(raw_endpoints)
    components["webhook_dispatcher"] = {
        "enabled": len(webhook_endpoints) > 0,
        "endpoint_count": len(webhook_endpoints),
        "endpoints": [
            {"url": ep.get("url", ""), "events": ep.get("events", ["*"])}
            for ep in webhook_endpoints
        ],
    }

    # Auto-Memory
    memory_dir = os.getenv("TINKER_MEMORY_DIR", "./tinker_memory")
    components["auto_memory"] = {
        "enabled": True,
        "memory_dir": memory_dir,
        "high_threshold": float(os.getenv("TINKER_MEMORY_HIGH_THRESHOLD", "0.85")),
        "low_threshold": float(os.getenv("TINKER_MEMORY_LOW_THRESHOLD", "0.4")),
    }

    # Human Judge
    components["human_judge"] = {
        "mode": os.getenv("TINKER_JUDGE_MODE", "llm"),
        "timeout": float(os.getenv("TINKER_HUMAN_JUDGE_TIMEOUT", "600")),
    }

    # Research Team
    components["research_team"] = {
        "enabled": True,
        "concurrency": int(os.getenv("TINKER_RESEARCH_CONCURRENCY", "3")),
    }

    return {"components": components}


@router.get("/api/grub/status")
async def api_grub_status():
    """Return Grub pipeline status: task counts, queue stats, recent artifacts."""
    return await fetch_grub_status()
